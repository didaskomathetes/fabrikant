"""Step: pr_review (ADR 0010) — LLM-Review des PR-Diffs + GitHub-Review via Reviewer-Token."""

from __future__ import annotations

import json

import pytest

from fabrikant import gitops
from fabrikant.steps import pr_review

APPROVED = json.dumps({"verdict": "approved", "findings": [], "summary": "ok"})
CHANGES = json.dumps(
    {"verdict": "changes_requested", "findings": [{"severity": "high", "text": "bug in a.py"}], "summary": "fix it"}
)


def _ctx(tmp_path):
    return {"project": "p", "loop": "l", "run_id": "r1", "workdir": str(tmp_path), "pr_number": "7", "branch": "dev"}


PUSHED_SHA = "abc1234def"


def _patch(monkeypatch, verdicts, git_status="M f.txt", heads=None):
    """GitHub-API + Agent-Backend + git faken. Reviewer- und Fix-Calls getrennt zählen.

    ``heads``: Sequenz der PR-Head-SHAs, die GitHub nacheinander meldet
    (letzter Wert bleibt stehen); Default: sofort der gepushte Commit."""
    calls = {"reviews": [], "llm_reviews": 0, "fixes": 0, "git": [], "head_polls": 0, "diffs": 0}
    heads = list(heads or [PUSHED_SHA])
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": f"tok-{role}")
    monkeypatch.setattr(pr_review, "_HEAD_SYNC_INTERVAL_S", 0)

    def _diff(repo, n, tok):
        calls["diffs"] += 1
        return "diff --git a/f b/f"

    monkeypatch.setattr(gitops, "_gh_diff", _diff)

    def _api(method, url, token, payload=None, accept=None):
        if url.endswith("/reviews"):
            calls["reviews"].append((token, payload))
            return {}
        if method == "GET" and url.endswith("/pulls/7"):
            calls["head_polls"] += 1
            return {"head": {"sha": heads.pop(0) if len(heads) > 1 else heads[0]}}
        return {}

    monkeypatch.setattr(gitops, "_gh_api", _api)

    def _agent(ctx, step, emit, backend, prompt):
        if "You are the developer" in prompt:  # Fix-Runde
            calls["fixes"] += 1
            return {"ok": True, "output": "fixed", "usage": {"total_tokens": 1}}
        calls["llm_reviews"] += 1  # Review-Runde
        v = verdicts[min(calls["llm_reviews"] - 1, len(verdicts) - 1)]
        return {"ok": True, "output": v, "usage": {"total_tokens": 1}}

    monkeypatch.setattr(pr_review, "_step_agent_backend", _agent)

    def _git(ctx, base, *args, check=True):
        calls["git"].append(args)
        if args == ("status", "--porcelain"):
            return git_status
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "dev"
        if args == ("rev-parse", "HEAD"):
            return PUSHED_SHA
        return ""

    monkeypatch.setattr(pr_review, "_git", _git)
    # Push läuft über apicommit.push_branch (git oder API je Projekt) — hier als git-Call protokolliert.
    monkeypatch.setattr(
        pr_review, "push_branch", lambda base, branch, protected: calls["git"].append(("push", "origin", branch)) or ""
    )
    return calls


# ---------------------------------------------------------------- Head-Sync nach Fix-Push
def test_rereview_waits_for_pushed_head(monkeypatch, tmp_path):
    """GitHub meldet erst den alten Head → Re-Review erst, wenn der Fix-Commit Head ist."""
    calls = _patch(monkeypatch, [CHANGES, APPROVED], heads=["old", "old", PUSHED_SHA])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is True
    assert calls["head_polls"] == 3
    assert calls["diffs"] == 2


def test_first_round_does_not_poll_head(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, [APPROVED])
    pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert calls["head_polls"] == 0


def test_head_sync_timeout_fails_without_stale_review(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, [CHANGES, APPROVED], heads=["old"])
    monkeypatch.setattr(pr_review, "_HEAD_SYNC_TIMEOUT_S", 0)
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is False
    assert "veraltetem Diff" in res["summary"] and PUSHED_SHA[:7] in res["summary"]
    assert calls["diffs"] == 1  # kein zweiter Review auf dem alten Diff
    assert calls["llm_reviews"] == 1


# ---------------------------------------------------------------- Verdict-Pfade
def test_approves_first_round(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, [APPROVED])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is True
    assert "approved" in res["summary"]
    assert len(calls["reviews"]) == 1
    token, payload = calls["reviews"][0]
    assert token == "tok-reviewer"  # Reviewer-Identity (App), nicht die Builder-App
    assert payload["event"] == "APPROVE"
    assert res["review"]["pr"] == 7


def test_fix_round_then_approve(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, [CHANGES, APPROVED])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is True
    assert calls["fixes"] == 1
    assert len(calls["reviews"]) == 2
    assert calls["reviews"][0][1]["event"] == "REQUEST_CHANGES"
    assert calls["reviews"][1][1]["event"] == "APPROVE"
    # Fix-Runde hat commited + gepusht
    assert ("add", ":/") in calls["git"]  # repo-weit, nicht nur der Subpfad
    assert ("push", "origin", "dev") in calls["git"]


def test_review_prompt_includes_issue_requirements(monkeypatch, tmp_path):
    # Regression (PR #201): ohne Issue/ACs approvte der Reviewer einen Diff,
    # der den Workflow nur im README behauptete.
    prompts = []
    _patch(monkeypatch, [CHANGES, APPROVED])
    agent = pr_review._step_agent_backend

    def _spy(ctx, step, emit, backend, prompt):
        prompts.append(prompt)
        return agent(ctx, step, emit, backend, prompt)

    monkeypatch.setattr(pr_review, "_step_agent_backend", _spy)
    ctx = {**_ctx(tmp_path), "issue_number": "83", "issue_title": "CI fehlt", "issue_body": "- AC: fabrikant-ci.yml"}
    res = pr_review._step_pr_review(ctx, {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is True
    assert len(prompts) == 3  # Review, Fix, Re-Review
    for prompt in prompts:
        assert "#83 CI fehlt" in prompt
        assert "AC: fabrikant-ci.yml" in prompt


def test_requirements_empty_without_issue(tmp_path):
    assert pr_review._requirements(_ctx(tmp_path)) == ""


def test_requirements_title_with_body_placeholder_not_substituted(tmp_path):
    # Ein {body} im Titel darf nicht durch den Issue-Body ersetzt werden
    # (format_map substituiert in einem Durchgang, kein verkettetes .replace).
    ctx = {
        **_ctx(tmp_path),
        "issue_number": "9",
        "issue_title": "fix {body} handling",
        "issue_body": "the real body",
    }
    out = pr_review._requirements(ctx)
    assert "fix {body} handling" in out
    assert "the real body" in out
    # Der Titel-Platzhalter bleibt wörtlich, nur der echte Body-Block ist gefüllt.
    assert out.count("the real body") == 1


def test_rounds_exhausted_fails(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, [CHANGES])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2", "max_rounds": 1}, lambda m: None)
    assert res["ok"] is False
    assert "changes_requested" in res["summary"]
    assert calls["fixes"] == 1
    assert len(calls["reviews"]) == 2  # beide REQUEST_CHANGES


def test_granted_rounds_extend_budget(monkeypatch, tmp_path):
    """ADR 0059: Eskalations-Freigabe gewährt frische Runden — pr_review addiert sie zum Budget."""
    calls = _patch(monkeypatch, [CHANGES])
    ctx = {**_ctx(tmp_path), pr_review.GRANT_ROUNDS_CTX: 2}
    res = pr_review._step_pr_review(ctx, {"repo": "o/r", "model": "m2", "max_rounds": 1}, lambda m: None)
    # max_rounds=1 + 2 gewährt = 3 → 4 Review-Runden bis zur Erschöpfung
    assert res["ok"] is False
    assert len(calls["reviews"]) == 4
    assert calls["fixes"] == 3


def test_fix_round_without_changes_fails(monkeypatch, tmp_path):
    _patch(monkeypatch, [CHANGES], git_status="")
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is False
    assert "ohne Änderungen" in res["summary"]


def test_unparseable_verdict_fails(monkeypatch, tmp_path):
    _patch(monkeypatch, ["kein json hier"])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is False
    assert "kein Verdict" in res["summary"]


def test_unparseable_verdict_retried_then_succeeds(monkeypatch, tmp_path):
    """Lokale Modelle liefern gelegentlich kein gültiges JSON-Verdict (#80): der
    erste Versuch ist unparsierbar, der Retry liefert ein gültiges Verdict →
    die Runde schlägt nicht fehl."""
    calls = _patch(monkeypatch, ["kein json hier", APPROVED])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is True
    assert "approved" in res["summary"]
    assert calls["llm_reviews"] == 2  # 1. Versuch + 1 Retry


def test_unparseable_verdict_exhausts_retries(monkeypatch, tmp_path):
    """Alle Versuche unparsierbar → nach erschöpftem Retry failt die Runde."""
    calls = _patch(monkeypatch, ["kein json hier"])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is False
    assert "kein Verdict" in res["summary"]
    assert calls["llm_reviews"] == 1 + pr_review.VERDICT_RETRIES


def test_backend_error_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_diff", lambda repo, n, tok: "diff")
    monkeypatch.setattr(pr_review, "_step_agent_backend", lambda *a, **k: {"ok": False, "summary": "backend down"})
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is False
    assert "backend down" in res["summary"]


# ---------------------------------------------------------------- AC-Verifikation (ADR 0013 Stufe 2)
APPROVED_WITH_AC = json.dumps(
    {
        "verdict": "approved",
        "findings": [],
        "summary": "ok",
        "acceptance": [
            {"criterion": "Tests grün", "status": "met", "evidence": "pytest"},
            {"criterion": "Runtime-Verhalten", "status": "unverifiable", "evidence": "nur im Betrieb prüfbar"},
        ],
    }
)


def test_acceptance_lands_in_run_record(monkeypatch, tmp_path):
    """AC-Verifikation pro Kriterium im Run-Record (`step.review`); unverifiable blockiert nicht."""
    calls = _patch(monkeypatch, [APPROVED_WITH_AC])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is True
    assert res["review"]["acceptance"] == [
        {"criterion": "Tests grün", "status": "met", "evidence": "pytest"},
        {"criterion": "Runtime-Verhalten", "status": "unverifiable", "evidence": "nur im Betrieb prüfbar"},
    ]
    assert calls["reviews"][0][1]["event"] == "APPROVE"
    # unverifiable ist im Review-Text nachvollziehbar
    body = calls["reviews"][0][1]["body"]
    assert "met: Tests grün" in body and "unverifiable: Runtime-Verhalten" in body


def test_unmet_acceptance_forces_changes_requested(monkeypatch, tmp_path):
    """Reviewer sagt approved, aber ein AC ist unmet → deterministisch changes_requested
    (bounded Fix-Runden greifen); unmet-Kriterium als high-Finding an den Fixer."""
    verdict = json.dumps(
        {
            "verdict": "approved",
            "findings": [],
            "summary": "ok",
            "acceptance": [{"criterion": "CI-Datei fehlt", "status": "unmet", "evidence": "nicht im Diff"}],
        }
    )
    calls = _patch(monkeypatch, [verdict, APPROVED])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is True
    assert calls["fixes"] == 1  # Fix-Runde ausgelöst
    assert calls["reviews"][0][1]["event"] == "REQUEST_CHANGES"
    body = calls["reviews"][0][1]["body"]
    assert "unmet acceptance criterion: CI-Datei fehlt" in body
    assert "unmet: CI-Datei fehlt" in body
    # Fix-Prompt trägt das unmet-Kriterium als Finding
    assert res["review"]["verdict"] == "approved"  # Endzustand nach Fix + Re-Review


def test_unmet_acceptance_survives_finding_cap(monkeypatch, tmp_path):
    """Unmet-ACs sind high und überleben das max_findings-Cap (sonst fixt die Runde nichts)."""
    verdict = json.dumps(
        {
            "verdict": "approved",
            "findings": [{"severity": "low", "text": f"nit {i}"} for i in range(10)],
            "summary": "ok",
            "acceptance": [{"criterion": "AC fehlt", "status": "unmet", "evidence": ""}],
        }
    )
    calls = _patch(monkeypatch, [verdict, APPROVED])
    pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2", "max_findings": 2}, lambda m: None)
    body = calls["reviews"][0][1]["body"]
    assert "unmet acceptance criterion: AC fehlt" in body


def test_invalid_acceptance_verdict_rejected(monkeypatch, tmp_path):
    """Ungültiges acceptance-JSON (unbekannter Status) → kein Verdict → Step failt."""
    bad = json.dumps(
        {
            "verdict": "approved",
            "findings": [],
            "summary": "ok",
            "acceptance": [{"criterion": "c", "status": "maybe"}],
        }
    )
    calls = _patch(monkeypatch, [bad])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is False
    assert "kein Verdict" in res["summary"]
    assert calls["reviews"] == []  # nichts gepostet


# ---------------------------------------------------------------- Cold-Critic + Config
def test_missing_model_fails(monkeypatch, tmp_path):
    _patch(monkeypatch, [APPROVED])
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r"}, lambda m: None)
    assert res["ok"] is False
    assert "model fehlt" in res["summary"]


def test_same_model_as_builder_fails(monkeypatch, tmp_path):
    _patch(monkeypatch, [APPROVED])
    ctx = _ctx(tmp_path)
    ctx["last_agent_model"] = "m1"
    res = pr_review._step_pr_review(ctx, {"repo": "o/r", "model": "m1"}, lambda m: None)
    assert res["ok"] is False
    assert "≠ Builder-Modell" in res["summary"]


def test_missing_repo_raises():
    with pytest.raises(ValueError, match="repo"):
        pr_review._step_pr_review(_ctx("/tmp"), {}, lambda m: None)


# ---------------------------------------------------------------- PR-Auflösung
def test_pr_number_from_step_def(monkeypatch, tmp_path):
    _patch(monkeypatch, [APPROVED])
    ctx = _ctx(tmp_path)
    ctx["pr_number"] = "1"  # Step-Def muss gewinnen
    res = pr_review._step_pr_review(ctx, {"repo": "o/r", "model": "m2", "pr": "9"}, lambda m: None)
    assert res["ok"] is True
    assert res["review"]["pr"] == 9


def test_pr_number_from_api_lookup(monkeypatch, tmp_path):
    """Kein ctx-pr_number, kein Step-pr → Lookup per Head-Branch."""
    _patch(monkeypatch, [APPROVED])

    def _api(method, url, token, payload=None, accept=None):
        if method == "GET":
            assert "head=o%3Adev" in url  # owner:branch, URL-encoded
            return [{"number": 12}]
        return {}

    monkeypatch.setattr(gitops, "_gh_api", _api)
    ctx = _ctx(tmp_path)
    ctx.pop("pr_number")
    res = pr_review._step_pr_review(ctx, {"repo": "o/r", "model": "m2"}, lambda m: None)
    assert res["ok"] is True
    assert res["review"]["pr"] == 12


def test_pr_number_unresolvable(monkeypatch, tmp_path):
    _patch(monkeypatch, [APPROVED])
    ctx = _ctx(tmp_path)
    ctx.pop("pr_number")
    ctx["branch"] = "-"
    with pytest.raises(ValueError, match="nicht auflösbar"):
        pr_review._step_pr_review(ctx, {"repo": "o/r", "model": "m2"}, lambda m: None)


# ---------------------------------------------------------------- Konvergenz + Builder-Fixes (ADR 0039)
def _spy_agent(monkeypatch, verdicts):
    """Wie _patch, zeichnet aber jeden Agent-Call (backend, model, prompt) auf."""
    calls = _patch(monkeypatch, verdicts)
    seen = []
    inner = pr_review._step_agent_backend

    def _spy(ctx, step, emit, backend, prompt):
        seen.append({"backend": backend, "model": step.get("model"), "prompt": prompt})
        return inner(ctx, step, emit, backend, prompt)

    monkeypatch.setattr(pr_review, "_step_agent_backend", _spy)
    return calls, seen


def _finding(severity, text, **extra):
    return {"severity": severity, "text": text, **extra}


def _changes(*findings):
    return json.dumps({"verdict": "changes_requested", "findings": list(findings), "summary": "s"})


def test_fix_round_uses_builder_backend_and_model(monkeypatch, tmp_path):
    _, seen = _spy_agent(monkeypatch, [CHANGES, APPROVED])
    ctx = _ctx(tmp_path) | {"last_agent_backend": "claude_code", "last_agent_model": "builder"}
    pr_review._step_pr_review(ctx, {"repo": "o/r", "backend": "copilot", "model": "critic"}, lambda m: None)
    review, fix, _ = seen
    assert (review["backend"], review["model"]) == ("copilot", "critic")
    assert (fix["backend"], fix["model"]) == ("claude_code", "builder")


def test_fix_backend_and_model_override_builder(monkeypatch, tmp_path):
    _, seen = _spy_agent(monkeypatch, [CHANGES, APPROVED])
    ctx = _ctx(tmp_path) | {"last_agent_backend": "claude_code", "last_agent_model": "builder"}
    step = {"repo": "o/r", "model": "critic", "fix_backend": "codex", "fix_model": "fixer"}
    pr_review._step_pr_review(ctx, step, lambda m: None)
    assert (seen[1]["backend"], seen[1]["model"]) == ("codex", "fixer")


def test_fix_step_without_builder_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("FABRIKANT_AGENT_BACKEND", "opencode")
    fix = pr_review._fix_step({}, {"backend": "copilot", "model": "critic"})
    assert fix["backend"] == "opencode"
    assert fix["model"] == ""  # → FABRIKANT_AGENT_MODEL im Agent-Backend, nicht das Reviewer-Modell


def test_rereview_prompt_carries_previous_findings(monkeypatch, tmp_path):
    prev = _finding("high", "race in a.py", suggestion="lock around x")
    _, seen = _spy_agent(monkeypatch, [_changes(prev), APPROVED])
    pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2"}, lambda m: None)
    first, fix, second = seen
    assert "previous review round" not in first["prompt"]
    assert "race in a.py" in second["prompt"] and "still unresolved" in second["prompt"]
    assert "suggestion: lock around x" in fix["prompt"]


def test_findings_ranked_and_capped(monkeypatch, tmp_path):
    verdict = _changes(_finding("low", "nit"), _finding("high", "h1"), _finding("medium", "m1"), _finding("high", "h2"))
    calls, seen = _spy_agent(monkeypatch, [verdict])
    step = {"repo": "o/r", "model": "m2", "max_rounds": 0, "max_findings": 2}
    res = pr_review._step_pr_review(_ctx(tmp_path), step, lambda m: None)
    assert [f["text"] for f in res["review"]["findings"]] == ["h1", "h2"]
    assert "at most 2 findings" in seen[0]["prompt"]
    body = calls["reviews"][0][1]["body"]
    assert "h1" in body and "nit" not in body


def test_review_body_includes_suggestion(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, [_changes(_finding("medium", "off by one", suggestion="use >="))])
    pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "m2", "max_rounds": 0}, lambda m: None)
    assert "- [medium] off by one\n  suggestion: use >=" in calls["reviews"][0][1]["body"]


# ---------------------------------------------------------------- Variante + Timeout der Fix-Runde
def test_fix_step_inherits_builder_variant_and_default_timeout():
    fix = pr_review._fix_step({"last_agent_variant": "medium"}, {"backend": "copilot", "model": "critic"})
    assert fix["variant"] == "medium"
    assert fix["timeout"] == pr_review.DEFAULT_FIX_TIMEOUT_S


def test_fix_variant_and_timeout_override():
    step = {"model": "critic", "fix_variant": "low", "fix_timeout": 99}
    fix = pr_review._fix_step({"last_agent_variant": "medium"}, step)
    assert (fix["variant"], fix["timeout"]) == ("low", 99)


def test_fix_round_passes_variant_to_agent(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, [CHANGES, APPROVED])
    steps = []
    inner = pr_review._step_agent_backend

    def _spy(ctx, step, emit, backend, prompt):
        steps.append(step)
        return inner(ctx, step, emit, backend, prompt)

    monkeypatch.setattr(pr_review, "_step_agent_backend", _spy)
    ctx = _ctx(tmp_path) | {"last_agent_backend": "opencode", "last_agent_model": "m", "last_agent_variant": "medium"}
    pr_review._step_pr_review(ctx, {"repo": "o/r", "model": "critic", "fix_timeout": 60}, lambda m: None)
    assert calls["fixes"] == 1
    assert (steps[1]["variant"], steps[1]["timeout"]) == ("medium", 60)
    assert "variant" not in steps[0]  # Review-Runde bleibt unberührt


# ---------------------------------------------------------------- fix_checks (vor Commit/Push)
def _fake_shell(monkeypatch, results):
    """_step_shell faken: ``results`` = {cmd: result}; protokolliert (cmd, timeout)."""
    ran = []

    def _shell(ctx, step, emit):
        ran.append((step["cmd"], step["timeout"]))
        return results.get(step["cmd"], {"ok": True})

    monkeypatch.setattr(pr_review, "_step_shell", _shell)
    return ran


def test_fix_checks_green_then_push(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, [CHANGES, APPROVED])
    ran = _fake_shell(monkeypatch, {})
    step = {"repo": "o/r", "model": "critic", "fix_checks": ["lint", "test"], "fix_check_timeout": 42}
    res = pr_review._step_pr_review(_ctx(tmp_path), step, lambda m: None)
    assert res["ok"] is True
    assert ran == [("lint", 42), ("test", 42)]
    assert ("push", "origin", "dev") in calls["git"]


def test_fix_check_failure_blocks_commit_and_push(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, [CHANGES, APPROVED])
    red = {
        "ok": False,
        "summary": "exit 1: boom",
        "exit_code": 1,
        "output": "a\nFAILED test_x",
        "failure_kind": "outcome",
    }
    ran = _fake_shell(monkeypatch, {"test": red})
    step = {"repo": "o/r", "model": "critic", "fix_checks": ["lint", "test", "never"]}
    res = pr_review._step_pr_review(_ctx(tmp_path), step, lambda m: None)
    assert res["ok"] is False
    assert "fix check 'test' red" in res["summary"] and "FAILED test_x" in res["summary"]
    assert res["failure_kind"] == "outcome"
    assert [c for c, _ in ran] == ["lint", "test"]  # stoppt beim ersten roten Check
    assert not any(g[0] in ("add", "commit", "push") for g in calls["git"])
    assert ran[0][1] == pr_review.DEFAULT_FIX_CHECK_TIMEOUT_S


def test_no_fix_checks_runs_nothing(monkeypatch, tmp_path):
    _patch(monkeypatch, [CHANGES, APPROVED])
    ran = _fake_shell(monkeypatch, {})
    res = pr_review._step_pr_review(_ctx(tmp_path), {"repo": "o/r", "model": "critic"}, lambda m: None)
    assert res["ok"] is True and ran == []
