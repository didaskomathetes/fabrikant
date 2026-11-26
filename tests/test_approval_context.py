"""Tests: Approval-Kontext (was/warum/Issue/Rechte) + reichhaltigere Failure-Logs.

Deckt die Helpers in engine.py (_issue_link, _format_permissions, _step_intent,
_approval_context, _emit_approval), das Engine-Wiring (record['approval'] + Log)
und die Chat-Darstellung (_approval_detail) ab.
"""

from __future__ import annotations

import json
import os
import urllib.request

from fabrikant import llm
from fabrikant.chat import _approval_detail
from fabrikant.engine import (
    _approval_context,
    _format_permissions,
    _issue_link,
    _pr_link,
    _step_intent,
    run_loop,
)


def _write_spec(projects_dir, tmp_path, name, *, tier=None, github=None, steps_yaml):
    """Spec mit optionalem Tier + GitHub-Repo und freiem Steps-YAML."""
    root = tmp_path / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    body = "\n".join("    " + ln for ln in steps_yaml.strip().splitlines())
    lines = [f"name: {name}", f"root: {root}"]
    if github:
        lines.append(f"github: {github}")  # Projekt-Level (nicht im Loop)
    lines += ["loops:", "  s:"]
    if tier is not None:
        lines.append(f"    tier: {tier}")
    lines.append("    steps:")
    lines.append(body)
    (projects_dir / f"{name}.fabrikant.yaml").write_text("\n".join(lines) + "\n")
    return root


def _log_text(runs_dir, project, run_id):
    f = runs_dir / project / f"{run_id}.log"
    return f.read_text() if f.is_file() else ""


# ---------------------------------------------------------------- _issue_link
def test_issue_link_on_issue_with_repo():
    spec = {"github": "owner/repo"}
    assert _issue_link(spec, "on_issue:#42") == "https://github.com/owner/repo/issues/42"


def test_issue_link_without_repo_is_empty():
    assert _issue_link({}, "on_issue:#42") == ""


def test_issue_link_non_issue_trigger_is_empty():
    assert _issue_link({"github": "owner/repo"}, "cron") == ""
    assert _issue_link({"github": "owner/repo"}, "manual") == ""


def test_issue_link_empty_trigger_is_empty():
    assert _issue_link({"github": "owner/repo"}, "") == ""


# ---------------------------------------------------------------- _pr_link
def test_pr_link_prefers_pr_url_var():
    assert _pr_link({"github": "o/r"}, "on_pr:#5", {"pr_url": "https://x/pull/5"}) == "https://x/pull/5"


def test_pr_link_from_on_pr_trigger_with_and_without_sha():
    spec = {"github": "o/r"}
    assert _pr_link(spec, "on_pr:#295", None) == "https://github.com/o/r/pull/295"
    assert _pr_link(spec, "on_pr:#295@abc123", {}) == "https://github.com/o/r/pull/295"


def test_pr_link_empty_without_pr_context():
    assert _pr_link({"github": "o/r"}, "on_issue:#7", {}) == ""
    assert _pr_link({}, "on_pr:#7", {}) == ""


# ---------------------------------------------------------------- _format_permissions
def test_format_permissions_dicts():
    perms: list[dict | str] = [
        {"permission": "edit", "pattern": "*", "action": "allow"},
        {"permission": "bash", "pattern": "*", "action": "allow"},
    ]
    assert _format_permissions(perms) == ["edit * → allow", "bash * → allow"]


def test_format_permissions_empty():
    assert _format_permissions(None) == []
    assert _format_permissions([]) == []


def test_format_permissions_non_dict_items():
    assert _format_permissions(["edit *"]) == ["edit *"]


# ---------------------------------------------------------------- _step_intent
def test_step_intent_shell():
    assert _step_intent({"type": "shell", "cmd": "make\n  deploy"}) == "Shell-Befehl: make deploy"


def test_step_intent_agent():
    out = _step_intent({"type": "agent", "backend": "opencode", "model": "anthropic/claude"})
    assert "Agent-Backend: opencode" in out
    assert "model: anthropic/claude" in out


def test_step_intent_git():
    assert (
        _step_intent({"type": "git", "fetch": True, "branch": "main", "push": True}) == "Git: fetch, branch main, push"
    )


def test_step_intent_pr():
    assert _step_intent({"type": "pr", "repo": "owner/repo", "base": "main"}) == "PR anlegen: owner/repo (base main)"


def test_step_intent_unknown_type():
    assert _step_intent({"type": "teleport"}) == "Step-Typ: teleport"


def test_step_intent_shell_substitutes_run_vars():
    step = {"type": "shell", "cmd": 'gh issue edit {issue_number} --repo o/r --add-label "{label}"'}
    out = _step_intent(step, {"issue_number": "42", "label": "needs approval"})
    assert out == 'Shell-Befehl: gh issue edit 42 --repo o/r --add-label "needs approval"'


def test_step_intent_unknown_vars_stay_unsubstituted():
    step = {"type": "shell", "cmd": "echo {issue_number} {unknown_var}"}
    out = _step_intent(step, {"issue_number": "7"})
    assert out == "Shell-Befehl: echo 7 {unknown_var}"


def test_step_intent_git_substitutes_branch():
    step = {"type": "git", "branch": "fabrikant/auto/{issue_number}"}
    assert _step_intent(step, {"issue_number": "9"}) == "Git: branch fabrikant/auto/9"


def test_step_intent_issue_label_shows_number_and_diff():
    step = {
        "type": "issue",
        "action": "label",
        "repo": "o/r",
        "number": "{issue_number}",
        "remove_labels": ["needs approval"],
        "labels": ["ready for development"],
    }
    assert _step_intent(step, {"issue_number": "281"}) == "Issue #281: +ready for development −needs approval"


def test_step_intent_issue_label_scalar_and_empty():
    assert _step_intent({"type": "issue", "action": "label", "number": "5", "labels": "bug"}) == "Issue #5: +bug"
    assert _step_intent({"type": "issue", "action": "label", "number": "5"}) == "Issue #5: labels unchanged"


def test_step_intent_issue_other_action():
    assert _step_intent({"type": "issue", "action": "close", "number": "7"}) == "Issue #7: close"
    assert _step_intent({"type": "issue", "action": "create", "repo": "o/r"}) == "Issue o/r: create"


# ---------------------------------------------------------------- _approval_context
def test_approval_context_step_with_permissions():
    step = {
        "name": "deploy",
        "type": "agent",
        "backend": "opencode",
        "permissions": [{"permission": "edit", "pattern": "*", "action": "allow"}],
    }
    ctx = _approval_context({"github": "o/r"}, "on_issue:#7", reason="step", step=step)
    assert ctx.reason == "step"
    assert ctx.step == "deploy"
    assert ctx.what.startswith("Agent-Backend")
    assert ctx.permissions == ["edit * → allow"]
    assert ctx.issue == "https://github.com/o/r/issues/7"


def test_approval_context_on_pr_shows_pr_link_and_title():
    """on_pr-Run: Approver sieht, WELCHE PR (Titel + Link) freigegeben wird."""
    step = {"name": "approve", "type": "shell", "cmd": "echo ok", "approval": True}
    run_ctx = {"pr_title": "ADR-Amendment: Retry", "pr_url": "https://github.com/o/r/pull/295"}
    ctx = _approval_context({"github": "o/r"}, "on_pr:#295", reason="step", step=step, run_ctx=run_ctx)
    assert (ctx.pr, ctx.pr_title) == ("https://github.com/o/r/pull/295", "ADR-Amendment: Retry")
    assert "pr: ADR-Amendment: Retry — https://github.com/o/r/pull/295" in ctx.lines()


def test_approval_context_without_pr_ignores_stray_pr_title():
    ctx = _approval_context({}, "manual", reason="tier", tier=1, run_ctx={"pr_title": "t"})
    assert "pr" not in ctx.to_dict()
    assert "pr_title" not in ctx.to_dict()


def test_approval_context_tier():
    ctx = _approval_context({}, "manual", reason="tier", tier=3)
    assert ctx.reason == "tier"
    assert ctx.tier == 3
    assert "Tier 3" in ctx.what
    assert "issue" not in ctx.to_dict()


def test_approval_context_no_issue_when_not_triggered_by_issue():
    ctx = _approval_context({"github": "o/r"}, "manual", reason="step", step={"name": "s", "type": "shell", "cmd": "x"})
    assert "issue" not in ctx.to_dict()


def test_approval_context_substitutes_run_vars_in_what():
    step = {"name": "apply-label", "type": "shell", "cmd": 'gh issue edit {issue_number} --add-label "{label}"'}
    ctx = _approval_context(
        {"github": "o/r"},
        "on_issue:#42",
        reason="step",
        step=step,
        run_ctx={"issue_number": "42", "label": "needs approval"},
    )
    assert ctx.what == 'Shell-Befehl: gh issue edit 42 --add-label "needs approval"'


def test_approval_context_reason_from_run_vars():
    """approval_reason (Template) → 'warum' mit aufgelöster LLM-Begründung."""
    step = {"name": "promote", "type": "issue", "action": "label", "number": "5", "approval_reason": "{reasoning}"}
    ctx = _approval_context({}, "manual", reason="step", step=step, run_ctx={"reasoning": "Braucht neue ADR"})
    assert ctx.why == "Braucht neue ADR"
    assert ctx.lines()[0] == "warum: Braucht neue ADR"


def test_approval_context_reason_missing_var_is_omitted():
    """Fehlt die referenzierte Variable, erscheint kein rohes '{reasoning}'."""
    step = {"name": "promote", "type": "shell", "cmd": "x", "approval_reason": "Grund: {reasoning}"}
    ctx = _approval_context({}, "manual", reason="step", step=step, run_ctx={})
    assert ctx.why == ""
    assert "why" not in ctx.to_dict()


def test_approval_context_reason_static_text_and_non_string():
    step: dict[str, object] = {"name": "s", "type": "shell", "cmd": "x", "approval_reason": "  irreversibel  "}
    assert _approval_context({}, "manual", reason="step", step=step).why == "irreversibel"
    step["approval_reason"] = ["kein", "string"]
    assert _approval_context({}, "manual", reason="step", step=step).why == ""


# ---------------------------------------------------------------- Engine-Wiring
def test_run_loop_step_approval_records_context(runs_dir, projects_dir, tmp_path):
    _write_spec(
        projects_dir,
        tmp_path,
        "appr_step",
        github="o/r",
        steps_yaml=(
            "- name: ok\n  type: shell\n  cmd: echo hi\n- name: gated\n  type: shell\n  cmd: echo x\n  approval: true\n"
        ),
    )
    rec = run_loop("appr_step", "s", trigger="on_issue:#99")
    assert rec["status"] == "needs_approval"
    appr = rec["approval"]
    assert appr["reason"] == "step"
    assert appr["step"] == "gated"
    assert appr["what"] == "Shell-Befehl: echo x"
    assert appr["issue"] == "https://github.com/o/r/issues/99"
    # Kontext auch im Log (nicht nur im Record)
    log = _log_text(runs_dir, "appr_step", rec["id"])
    assert "was: Shell-Befehl: echo x" in log
    assert "issue: https://github.com/o/r/issues/99" in log


def test_run_loop_step_approval_substitutes_run_vars(runs_dir, projects_dir, tmp_path):
    """Der Approver sieht den aufgelösten Befehl, nicht das {Platzhalter}-Template."""
    _write_spec(
        projects_dir,
        tmp_path,
        "appr_vars",
        github="o/r",
        steps_yaml=(
            "- name: ok\n  type: shell\n  cmd: echo hi\n"
            "- name: gated\n  type: shell\n  cmd: 'gh issue edit {issue_number} --add-label \"{label}\"'\n  approval: true\n"
        ),
    )
    rec = run_loop(
        "appr_vars", "s", trigger="on_issue:#42", initial_vars={"issue_number": "42", "label": "needs approval"}
    )
    assert rec["status"] == "needs_approval"
    assert rec["approval"]["what"] == 'Shell-Befehl: gh issue edit 42 --add-label "needs approval"'
    log = _log_text(runs_dir, "appr_vars", rec["id"])
    assert 'was: Shell-Befehl: gh issue edit 42 --add-label "needs approval"' in log


def test_run_loop_resume_restores_vars(runs_dir, projects_dir, tmp_path):
    """Nach Approval werden {Variablen} aus dem Record wiederhergestellt.

    Regression: der triage-apply-label-Step lief `gh issue edit {issue_number}`
    mit dem LITERAL-Platzhalter, weil der In-Memory-ctx (issue_number, label)
    in der Approval-Pause verloren ging und das Resume einen frischen ctx baute."""
    _write_spec(
        projects_dir,
        tmp_path,
        "appr_resume",
        steps_yaml=(
            "- name: ok\n  type: shell\n  cmd: echo hi\n"
            "- name: gated\n  type: shell\n  cmd: 'echo {issue_number} {label}'\n  approval: true\n"
        ),
    )
    rec = run_loop(
        "appr_resume",
        "s",
        trigger="on_issue:#42",
        initial_vars={"issue_number": "42", "label": "needs approval"},
    )
    assert rec["status"] == "needs_approval"
    # Variablen überleben den JSON-Roundtrip (Resume liest den Record neu)
    from fabrikant import get_run

    reloaded = get_run(rec["id"], "appr_resume")
    assert reloaded["vars"]["issue_number"] == "42"
    assert reloaded["vars"]["label"] == "needs approval"
    # Resume: der gepauste Step läuft mit den aufgelösten Variablen
    rec2 = run_loop("appr_resume", "s", resume_id=rec["id"])
    assert rec2["status"] == "pass"
    gated = next(s for s in rec2["steps"] if s["name"] == "gated")
    assert gated["status"] == "pass"
    assert gated["summary"] == "42 needs approval"


def test_run_loop_resume_restores_llm_injected_vars(runs_dir, projects_dir, tmp_path, monkeypatch):
    """Triage-Regression: LLM-injectierte Variablen (label) überleben die Approval-Pause.

    Exakter Bug: classify (llm) injectet `label`, apply-label (approval) pausiert,
    Resume lief `gh issue edit {issue_number}` mit Literal-Platzhaltern."""
    for k in list(os.environ):
        if k.startswith("FABRIKANT_LLM"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_ACTIVE_FILE", tmp_path / "active_llm")
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "dummy")
    monkeypatch.setenv("FABRIKANT_LLM_DUMMY_URL", "http://127.0.0.1:1/v1/chat/completions")
    monkeypatch.setenv("FABRIKANT_LLM_DUMMY_MODEL", "test-model")
    llm.reset_cache()
    llm.set_active("dummy")

    payload = {
        "choices": [{"message": {"content": '{"label": "ready to develop", "reasoning": "ok"}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _R())

    _write_spec(
        projects_dir,
        tmp_path,
        "triage_resume",
        steps_yaml=(
            "- name: classify\n  type: llm\n  prompt: 'x'\n"
            "  schema:\n    label: string\n    reasoning: string\n"
            "- name: apply-label\n  type: shell\n  cmd: 'echo {issue_number} {label}'\n  approval: true\n"
        ),
    )
    rec = run_loop("triage_resume", "s", trigger="on_issue:#62", initial_vars={"issue_number": "62"})
    assert rec["status"] == "needs_approval"
    # Beide Quellen (Trigger-Vars + LLM-Output) sind im Record persistiert
    assert rec["vars"]["issue_number"] == "62"
    assert rec["vars"]["label"] == "ready to develop"
    rec2 = run_loop("triage_resume", "s", resume_id=rec["id"])
    assert rec2["status"] == "pass"
    gated = next(s for s in rec2["steps"] if s["name"] == "apply-label")
    assert gated["status"] == "pass"
    assert gated["summary"] == "62 ready to develop"
    llm.reset_cache()


def test_run_loop_tier_gate_records_context(runs_dir, projects_dir, tmp_path):
    _write_spec(
        projects_dir,
        tmp_path,
        "appr_tier",
        tier=2,
        github="o/r",
        steps_yaml="- name: ok\n  type: shell\n  cmd: echo hi\n",
    )
    rec = run_loop("appr_tier", "s", trigger="on_issue:#5")
    assert rec["status"] == "needs_approval"
    appr = rec["approval"]
    assert appr["reason"] == "tier"
    assert appr["tier"] == 2
    assert appr["issue"] == "https://github.com/o/r/issues/5"
    log = _log_text(runs_dir, "appr_tier", rec["id"])
    assert "tier: 2" in log


def test_run_loop_tier1_no_approval_context(runs_dir, projects_dir, tmp_path):
    _write_spec(projects_dir, tmp_path, "appr_t1", tier=1, steps_yaml="- name: ok\n  type: shell\n  cmd: echo hi\n")
    rec = run_loop("appr_t1", "s")
    assert rec["status"] == "pass"
    assert "approval" not in rec


# ---------------------------------------------------------------- Failure-Logs
def test_failure_log_includes_summary_and_output_tail(runs_dir, projects_dir, tmp_path):
    _write_spec(
        projects_dir,
        tmp_path,
        "fail_detail",
        steps_yaml="- name: boom\n  type: shell\n  cmd: 'echo out1; echo out2; echo out3 >&2; exit 1'\n",
    )
    rec = run_loop("fail_detail", "s")
    assert rec["status"] == "fail"
    log = _log_text(runs_dir, "fail_detail", rec["id"])
    # Exit-Code + Summary-Zeile
    assert "boom fehlgeschlagen (exit 1)" in log
    # Output-Tail (letzte Zeilen) landet im Log
    assert "out3" in log


def test_timeout_log_includes_timeout_value(runs_dir, projects_dir, tmp_path):
    _write_spec(
        projects_dir,
        tmp_path,
        "to_detail",
        steps_yaml="- name: slow\n  type: shell\n  cmd: 'sleep 5'\n  timeout: 1\n",
    )
    rec = run_loop("to_detail", "s")
    assert rec["status"] == "fail"
    log = _log_text(runs_dir, "to_detail", rec["id"])
    assert "slow timed out after 1s" in log


def test_exception_log_includes_traceback(runs_dir, projects_dir, tmp_path):
    # pr-Step ohne repo → ValueError → Exception-Pfad mit Traceback
    _write_spec(
        projects_dir,
        tmp_path,
        "exc_detail",
        steps_yaml="- name: badpr\n  type: pr\n  base: main\n",
    )
    rec = run_loop("exc_detail", "s")
    assert rec["status"] == "error"
    log = _log_text(runs_dir, "exc_detail", rec["id"])
    assert "badpr Fehler:" in log
    assert "Traceback" in log


# ---------------------------------------------------------------- Chat-Darstellung
def test_approval_detail_renders_lines():
    ctx = {
        "what": "Shell-Befehl: make deploy",
        "permissions": ["edit * → allow"],
        "issue": "https://github.com/o/r/issues/1",
        "tier": 2,
    }
    out = _approval_detail(ctx)
    assert "was: Shell-Befehl: make deploy" in out
    assert "rechte: edit * → allow" in out
    assert "issue: https://github.com/o/r/issues/1" in out
    assert "tier: 2" in out


def test_run_loop_approval_reason_in_record_and_log(runs_dir, projects_dir, tmp_path):
    """approval_reason landet als 'why' im Record und als 'warum' im Log."""
    _write_spec(
        projects_dir,
        tmp_path,
        "appr_why",
        steps_yaml=(
            "- name: gated\n  type: shell\n  cmd: echo x\n"
            "  approval_when: {label: needs approval}\n  approval_reason: '{reasoning}'\n"
        ),
    )
    rec = run_loop("appr_why", "s", initial_vars={"label": "needs approval", "reasoning": "ändert ADR 0017"})
    assert rec["status"] == "needs_approval"
    assert rec["approval"]["why"] == "ändert ADR 0017"
    assert rec["approval"]["condition"] == "label = needs approval"
    assert "warum: ändert ADR 0017" in _log_text(runs_dir, "appr_why", rec["id"])


def test_approval_detail_shows_condition_first():
    out = _approval_detail({"condition": "label = needs approval", "what": "Issue #1: +x"})
    assert out.index("bedingung: label = needs approval") < out.index("was: Issue #1: +x")


def test_approval_detail_shows_why_before_what():
    out = _approval_detail({"condition": "label = needs approval", "why": "neue ADR", "what": "Issue #1: +x"})
    assert out.index("bedingung:") < out.index("warum: neue ADR") < out.index("was:")


def test_approval_detail_empty():
    assert _approval_detail({}) == ""
    assert _approval_detail(None) == ""


def test_approval_detail_persisted_in_record_json(runs_dir, projects_dir, tmp_path):
    """Der Kontext überlebt den JSON-Roundtrip (Record wird neu gelesen)."""
    _write_spec(
        projects_dir,
        tmp_path,
        "appr_json",
        github="o/r",
        steps_yaml="- name: g\n  type: shell\n  cmd: echo x\n  approval: true\n",
    )
    rec = run_loop("appr_json", "s", trigger="on_issue:#3")
    from fabrikant import get_run

    reloaded = get_run(rec["id"], "appr_json")
    assert reloaded["approval"]["issue"] == "https://github.com/o/r/issues/3"
    assert json.loads((runs_dir / "appr_json" / f"{rec['id']}.json").read_text())["approval"]["step"] == "g"


# ---------------------------------------------------------------- Migration PR #302
def test_resume_old_two_step_pause_against_new_gated_merge_step(runs_dir, projects_dir, tmp_path):
    """Issue #354: Runs, die vor PR #302 am alten ``approve``-Step pausierten
    (``steps = [{approve, waiting}]``), laufen nach dem Spec-Wechsel auf den
    gegateten Merge-Step genau einmal durch — ohne zweites Gate."""
    old = "- {name: approve, type: shell, cmd: 'echo ok', approval: true}\n- {name: merge, type: shell, cmd: 'echo m >> merged'}"
    root = _write_spec(projects_dir, tmp_path, "mig", steps_yaml=old)
    rec = run_loop("mig", "s")
    assert rec["status"] == "needs_approval"
    assert rec["waiting_step"] == "approve"
    assert [s["name"] for s in rec["steps"]] == ["approve"]

    new = "- {name: merge-adr-pr, type: shell, cmd: 'echo m >> merged', approval: true}"
    _write_spec(projects_dir, tmp_path, "mig", steps_yaml=new)
    rec2 = run_loop("mig", "s", resume_id=rec["id"])
    assert rec2["status"] == "pass"
    assert (root / "merged").read_text() == "m\n"  # Merge genau einmal, keine erneute Pause
    assert [(s["name"], s["status"]) for s in rec2["steps"]] == [("merge-adr-pr", "pass")]
