"""Dogfooding-Spec (projects/fabrik.fabrik.yaml): Invarianten des refine-Loops.

ADR 0006: Triage → Issue-Refine; ADR 0017: Human-Gate nur bei needs approval
und close proposed (jeder Close braucht Freigabe).
Pins die Struktur:
- Loop `refine` mit list-open → list-closed → classify → refine-body → link-blockers
  → propose-close → apply-label → promote → close-issue → report
- alle Issue-Operationen sind native `issue`-Steps (ADR 0001 §5), kein Shell/gh
- refine-body: Idempotenz-Marker
- apply-label: ohne Gate, Label immer sichtbar; promote: approval_when needs approval;
  close-issue: approval_when close proposed (für jeden close_kind)
- close-issue: Close mit Begründung (state=closed, NICHT delete)
- develop triggert auf das Label 'ready for development'
- refine/approval laufen ohne Worktree (ADR 0020)
- approval: Gate direkt am pr_merge-Step, Begründung = PR-Titel

Die Funktionstests laufen die echten issue-Steps gegen eine Fake-GitHub-API (kein Netzwerk).
"""

import re
import urllib.parse
from pathlib import Path

import pytest
import yaml

from fabrik import context, gitops, spec_expand, specs
from fabrik.engine import _approval_condition
from fabrik.steps import issue

SPEC_PATH = Path(__file__).resolve().parent.parent / "projects" / "fabrik.fabrik.yaml"
REPO = "didaskomathetes/quark-playground"
MARKER = "<!-- Refine (auto) -->"
CLOSE_MARKER = "<!-- Close-Vorschlag (auto) -->"
CLOSE = "close proposed"


def _spec() -> dict:
    """Spec nach vars-/Template-Expansion (ADR 0047) — wie sie die Engine sieht."""
    return spec_expand.expand_spec(yaml.safe_load(SPEC_PATH.read_text()), SPEC_PATH)


def _refine_steps() -> dict[str, dict]:
    return {st["name"]: st for st in _spec()["loops"]["refine"]["steps"]}


_BASE_VARS = {
    "label": "ready for development",
    "reasoning": "klar",
    "background": "Hintergrund-Text",
    "acceptance_criteria": "- Kriterium 1\n- Kriterium 2",
    "close_reason": "",
    "close_kind": "",
    "duplicate_of": "",
    "issue_number": "70",
}


class _FakeGitHub:
    """Zustandsbehaftete Fake-API (Body, Labels, Calls) für gitops._gh_api."""

    def __init__(self):
        self.body = "Original-Body\n"
        self.labels: list[str] = []
        self.state = "open"
        self.calls: list[str] = []

    def __call__(self, method, url, token, payload=None, accept=None):
        path = url.split("/issues/", 1)[1]
        if method == "GET":
            return {"body": self.body}
        if method == "PATCH":
            self.calls.append("close" if (payload or {}).get("state") == "closed" else "edit")
            self.body = (payload or {}).get("body", self.body)
            self.state = (payload or {}).get("state", self.state)
        elif method == "POST" and path.endswith("/comments"):
            self.calls.append("comment")
        elif method == "POST":
            self.calls.append("add-label")
            self.labels += payload["labels"]
        elif method == "DELETE":
            self.calls.append("remove-label")
            self.labels.remove(urllib.parse.unquote(path.rsplit("/", 1)[1]))
        return {}


def _run_step(monkeypatch, name: str, gh: _FakeGitHub | None = None, **vars) -> tuple[dict, _FakeGitHub]:
    """Führt den refine-Step `name` mit gerenderten Variablen gegen die Fake-API aus."""
    gh = gh or _FakeGitHub()
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", gh)
    ctx = {**_BASE_VARS, **vars}
    res = issue._step_issue(ctx, _refine_steps()[name])
    assert res["ok"] is True, res
    return res, gh


# ---------------------------------------------------------------- statische Invarianten
def test_refine_loop_structure():
    steps = _refine_steps()
    assert list(steps) == [
        "list-open",
        "list-closed",
        "classify",
        "dependencies",
        "refine-body",
        "create-prerequisite",
        "link-blockers",
        "propose-close",
        "apply-label",
        "promote",
        "close-issue",
        "report",
    ]
    # classify: untrusted-Markierung + vollständiges Schema
    classify = steps["classify"]
    assert classify["untrusted"] == ["issue_title", "issue_body", "open_issues", "closed_issues"]
    assert classify["schema"]["reasoning"] == "string"
    # Felder, die leer sein dürfen, sind optional mit Default "" (LLMs lassen sie weg, #80)
    for key in ("background", "acceptance_criteria", "close_reason", "duplicate_of"):
        assert classify["schema"][key] == {"type": "string", "default": ""}
    # label: enum-Whitelist — der llm-Step lehnt Abweichungen ab.
    label_spec = classify["schema"]["label"]
    assert label_spec["type"] == "string"
    assert label_spec["enum"] == ["ready for development", "needs approval", CLOSE]


def test_refine_loop_has_no_shell_gh():
    """ADR 0001 §5: Issue-Operationen laufen über native issue-Steps."""
    for name, st in _refine_steps().items():
        assert st["type"] != "shell", name
    assert _refine_steps()["refine-body"]["unless_contains"] == MARKER  # Idempotenz-Guard


def test_human_gate_only_for_needs_approval_and_close():
    """ADR 0017: ready for development läuft ohne Pause durch; needs approval und jeder Close gaten."""
    steps = _refine_steps()
    assert not any(st.get("approval") for st in steps.values())
    assert "approval_when" not in steps["apply-label"]
    assert steps["promote"]["approval_when"] == {"label": "needs approval"}
    assert steps["close-issue"]["approval_when"] == {"label": CLOSE}


def _gated_steps(**vars) -> list[str]:
    """Refine-Steps, vor denen die Engine bei diesen Classify-Werten pausieren würde."""
    ctx = {**_BASE_VARS, **vars}
    return [name for name, st in _refine_steps().items() if _approval_condition(st, ctx) is not None]


def test_gate_per_classification():
    """ADR 0017: genau ein Gate bei needs approval / close proposed, sonst keins."""
    assert _gated_steps(label="ready for development") == []
    assert _gated_steps(label="needs approval") == ["promote"]
    # Jeder Close-Grund gatet: auch Duplikat/gelöst (Reopen triggert refine nicht erneut,
    # completed gibt Abhängige frei — ADR 0031).
    for kind, dup in (("duplicate", "#65"), ("completed", ""), ("not_planned", ""), ("", "")):
        assert _gated_steps(label=CLOSE, close_kind=kind, duplicate_of=dup) == ["close-issue"], kind


def test_classify_prompt_defaults_to_ready():
    """Regressionsschutz (ADR 0017): needs approval nur bei benannten Gründen, nicht 'im Zweifel'."""
    prompt = _refine_steps()["classify"]["prompt"]
    assert 'Im Zweifel: "needs approval"' not in prompt
    for reason in ("Architektur:", "Sehr groß:", "Kritisch:", "Security", "DSGVO"):
        assert reason in prompt, reason
    assert "KEIN Grund" in prompt  # bloßes Berühren von Engine/ADR-Themen gatet nicht


def test_classify_prompt_requires_ac_check_hint_format():
    """ADR 0013 Stufe 1: AC im Format `- [ ] <Kriterium> — Prüfung: <…>` — der
    Prüfhinweis ist ein Reviewer-Hinweis, kein ausführbares Kommando."""
    prompt = _refine_steps()["classify"]["prompt"]
    assert "- [ ] <Kriterium> — Prüfung: <Test | Befehl | Beobachtung>" in prompt
    assert "KEIN ausführbares Kommando" in prompt
    # Das Format steht auch im Antwort-Schema (nicht nur als Prosa-Regel).
    assert "Prüfung: <Test | Befehl | Beobachtung>" in prompt.split("Antwort-Schema:", 1)[1]


def test_close_issue_closes_not_deletes():
    step = _refine_steps()["close-issue"]
    assert step["action"] == "close"
    assert "delete" not in step["action"]


@pytest.mark.parametrize("loop", ["refine", "approval"])
def test_api_only_loops_run_without_worktree(loop):
    """refine/approval nutzen nur llm/API — ohne Worktree laufen sie parallel zu develop (ADR 0020)."""
    ldef = _spec()["loops"][loop]
    assert ldef.get("worktree") is False
    assert not specs._validate_worktree(loop, ldef, ldef["steps"])


def test_approval_gate_sits_on_merge_with_pr_title():
    """approval-Loop (ADR 0011): Gate direkt am pr_merge — der Approver sieht
    "PR mergen" + PR-Titel als Begründung statt eines Platzhalter-Steps."""
    steps = _spec()["loops"]["approval"]["steps"]
    gated = [st for st in steps if _approval_condition(st, {}) is not None]
    assert [st["type"] for st in gated] == ["pr_merge"]
    assert gated[0]["approval_reason"] == "{pr_title}"
    assert not any(st["type"] == "shell" for st in steps)


def test_develop_trigger_label():
    """develop startet nur auf 'ready for development' — nie auf needs approval / close proposed."""
    develop = _spec()["loops"]["develop"]
    assert develop["triggers"][0]["labels"] == ["ready for development"]


def test_close_proposal_visible_before_gate():
    """Begründung + Label stehen auf GitHub, bevor close-issue pausiert."""
    names = list(_refine_steps())
    assert names.index("propose-close") < names.index("apply-label") < names.index("close-issue")
    step = _refine_steps()["propose-close"]
    assert step["only_if"] == {"label": CLOSE} and step["unless_contains"] == CLOSE_MARKER


def test_develop_checks_adr_numbers_before_tests_and_ship():
    """ADR 0032: fix → check nach dem Agent, vor lint/test/ship (Reparatur wird mitgetestet + committet)."""
    names = [st["name"] for st in _spec()["loops"]["develop"]["steps"]]
    assert names.index("changes") < names.index("check-adr") < names.index("lint") < names.index("ship")
    step = next(st for st in _spec()["loops"]["develop"]["steps"] if st["name"] == "check-adr")
    assert step["type"] == "shell"
    assert step["cmd"].index("check-adr-numbers.py fix") < step["cmd"].index("check-adr-numbers.py check")


def _loop_steps(loop: str) -> dict[str, dict]:
    return {st["name"]: st for st in _spec()["loops"][loop]["steps"]}


def test_refine_lists_other_issues_for_duplicate_and_blocker_check():
    """ADR 0031: classify bekommt offene + zuletzt geschlossene fabrik-Issues als Kontext."""
    steps = _refine_steps()
    emitted = {steps[n]["emit"]: steps[n] for n in ("list-open", "list-closed")}
    assert emitted["open_issues"]["state"] == "open" and emitted["closed_issues"]["state"] == "closed"
    for st in emitted.values():
        assert st["action"] == "list" and st["labels"] == ["fabrik"]
    assert "Duplikat" in steps["classify"]["prompt"]
    assert steps["classify"]["untrusted"][-2:] == ["open_issues", "closed_issues"]


def test_classify_close_kind_maps_to_state_reason():
    """Nur `completed` gibt Abhängige frei → Close-Grund muss explizit an GitHub."""
    steps = _refine_steps()
    assert steps["classify"]["schema"]["close_kind"] == {
        "type": "string",
        "enum": ["", "duplicate", "completed", "not_planned"],
        "default": "",
    }
    close = steps["close-issue"]
    assert close["state_reason"] == "{close_kind}" and close["duplicate_of"] == "{duplicate_of}"


def test_dependencies_step_schema_and_untrusted():
    deps = _refine_steps()["dependencies"]
    assert deps["type"] == "llm"
    assert set(deps["schema"]) == {"blocked_by", "prerequisite_title", "prerequisite_body"}
    assert all(v == {"type": "string", "default": ""} for v in deps["schema"].values())
    assert deps["untrusted"] == ["issue_title", "issue_body", "open_issues", "closed_issues"]


def test_link_blockers_runs_before_label():
    """ADR 0031: Voraussetzung + Blocker stehen fest, bevor develop das Label sieht."""
    names = list(_refine_steps())
    assert names.index("create-prerequisite") < names.index("link-blockers") < names.index("apply-label")
    refined = {"label": ["ready for development", "needs approval"]}
    for name in ("create-prerequisite", "link-blockers"):
        assert _refine_steps()[name]["only_if"] == refined, name


def test_prerequisite_is_refined_and_keeps_dependent_issue_number():
    """Voraussetzung → Label fabrik (durchläuft refine), Priorität geerbt, issue_number unverändert."""
    step = _refine_steps()["create-prerequisite"]
    assert step["action"] == "create" and step["labels"] == ["fabrik"]
    assert step["emit"] == "prerequisite_number"
    assert step["inherit_labels"] == _spec()["loops"]["refine"]["triggers"][0]["priority_labels"]
    assert "#{issue_number}" in step["body"]
    link = _refine_steps()["link-blockers"]
    assert link["blocked_by"] == "{blocked_by}"
    assert link["prerequisite"] == "#{prerequisite_number}"
    assert link["skip_if_empty"] == ["blocked_by", "prerequisite_number"]


def test_link_blockers_skipped_on_close_or_without_blockers(monkeypatch):
    for extra in ({"label": CLOSE, "blocked_by": "#12"}, {"blocked_by": ""}):
        res, gh = _run_step(monkeypatch, "link-blockers", gh=None, **extra)
        assert "übersprungen" in res["summary"] and gh.calls == []


def test_deps_backfill_reuses_refine_steps_without_relabel_or_close():
    """Backfill = dieselben Bausteine wie refine, ohne only_if (kein label), ohne Label/Close."""
    backfill, refine = _loop_steps("deps-backfill"), _refine_steps()
    assert list(backfill) == [
        "list-open",
        "list-closed",
        "dependencies",
        "create-prerequisite",
        "link-blockers",
        "report",
    ]
    for name in ("list-open", "list-closed"):
        assert backfill[name] == refine[name], name
    for name in ("dependencies", "create-prerequisite", "link-blockers"):
        assert backfill[name] == {k: v for k, v in refine[name].items() if k != "only_if"}, name
    assert all(st.get("action") not in ("label", "close", "edit") for st in backfill.values())


def test_deps_backfill_trigger_runs_once_per_issue_slowly():
    (trig,) = _spec()["loops"]["deps-backfill"]["triggers"]
    assert trig["type"] == "on_issue" and trig["reconcile"] is True and trig["labels"] == ["fabrik"]
    assert trig["max_per_poll"] == 1 and trig["poll"] >= 120


def test_develop_never_starts_blocked_issues():
    """ADR 0031: develop überspringt Issues mit offenen Blockern (braucht reconcile)."""
    trig = _spec()["loops"]["develop"]["triggers"][0]
    assert trig["skip_blocked"] is True and trig["reconcile"] is True


def test_develop_pr_closes_issue_on_merge():
    """ADR 0013 Stufe 0: develop-PR trägt `Closes #{issue_number}` im Body →
    GitHub schließt das Issue beim Merge (vorher: nur "Issue #N", nie geschlossen).

    Robust via `in` (nicht `startswith` + Newline): unabhängig von
    Leading-Whitespace und Zeilenenden im YAML-Block-Skalar.
    """
    steps = {s["name"]: s for s in _spec()["loops"]["develop"]["steps"]}
    assert "Closes #{issue_number}" in steps["pr"]["body"]


def test_develop_pr_body_carries_issue_number_placeholder():
    """Der Body-Template enthält den `#{issue_number}`-Platzhalter. Fehlt
    issue_number zur Laufzeit, substituiert die Engine leer → kein gültiges
    Closing-Keyword (Issue bleibt offen, statt ein falsches zu schließen)."""
    steps = {s["name"]: s for s in _spec()["loops"]["develop"]["steps"]}
    assert "#{issue_number}" in steps["pr"]["body"]


def _develop_pr_body(**vars) -> str:
    """Integration: YAML-Parsing + Template-Substitution des develop-`pr`-Bodys.

    Läuft die echte Rendering-Engine (`context._substitute`) gegen den aus der
    Spec geladenen Body — so wird sichergestellt, dass leere/fehlende
    `issue_number` kein gültiges Closing-Keyword erzeugen (sonst würde GitHub
    beim Merge ein falsches Issue schließen).

    `context` ist das Modul `fabrik.context` (Import oben, kein Fixture).
    """
    base = {"issue_number": "70", "issue_title": "Test-Issue"}
    base.update(vars)
    steps = {s["name"]: s for s in _spec()["loops"]["develop"]["steps"]}
    return context._substitute(steps["pr"]["body"], base)


def test_develop_pr_body_render_valid_issue_number():
    """Gültige `issue_number` → `Closes #70` (Issue schließt sich beim Merge)."""
    assert "Closes #70" in _develop_pr_body()


def test_develop_pr_body_render_empty_issue_number():
    """Leere `issue_number` → kein gültiges Closing-Keyword (`Closes #` ohne
    Ziffer schließt bei GitHub nichts)."""
    body = _develop_pr_body(issue_number="")
    assert not re.search(r"Closes #\d", body)


def test_develop_pr_body_render_missing_issue_number():
    """Fehlende `issue_number` → Platzhalter bleibt unverändert, kein
    Closing-Keyword (Engine substituiert fehlende Keys leer/als Platzhalter)."""
    steps = {s["name"]: s for s in _spec()["loops"]["develop"]["steps"]}
    body = context._substitute(steps["pr"]["body"], {})
    assert "Closes #{issue_number}" in body
    assert not re.search(r"Closes #\d", body)


# ---------------------------------------------------------------- Funktionstests (Fake-API)
def test_refine_body_appends_sections(monkeypatch):
    _, gh = _run_step(monkeypatch, "refine-body")
    assert "Original-Body" in gh.body
    assert MARKER in gh.body
    assert "## Hintergrund (auto)" in gh.body and "Hintergrund-Text" in gh.body
    assert "## Acceptance Criteria (auto)" in gh.body
    assert "- Kriterium 1" in gh.body and "- Kriterium 2" in gh.body


def test_refine_body_is_idempotent(monkeypatch):
    _, gh = _run_step(monkeypatch, "refine-body")
    _run_step(monkeypatch, "refine-body", gh)
    assert gh.body.count(MARKER) == 1
    assert gh.calls == ["edit"]


def test_refine_body_noop_when_empty(monkeypatch):
    _, gh = _run_step(monkeypatch, "refine-body", background="", acceptance_criteria="")
    assert gh.body == "Original-Body\n"
    assert gh.calls == []


def test_apply_label_adds_label(monkeypatch):
    _, gh = _run_step(monkeypatch, "apply-label")
    assert gh.labels == ["ready for development"]


def test_apply_label_makes_close_proposal_visible(monkeypatch):
    _, gh = _run_step(monkeypatch, "apply-label", label=CLOSE)
    assert gh.labels == [CLOSE]


def test_propose_close_appends_reason_once(monkeypatch):
    _, gh = _run_step(monkeypatch, "propose-close", label=CLOSE, close_reason="Duplikat von #65")
    _run_step(monkeypatch, "propose-close", gh, label=CLOSE, close_reason="Duplikat von #65")
    assert "Original-Body" in gh.body and "Duplikat von #65" in gh.body
    assert gh.body.count(CLOSE_MARKER) == 1 and gh.calls == ["edit"]


def test_propose_close_noop_for_labels(monkeypatch):
    gh = _FakeGitHub()
    for label in ("ready for development", "needs approval"):
        _run_step(monkeypatch, "propose-close", gh, label=label)
    assert gh.calls == []


def test_apply_label_makes_needs_approval_visible(monkeypatch):
    _, gh = _run_step(monkeypatch, "apply-label", label="needs approval")
    assert gh.labels == ["needs approval"]


def test_promote_relabels_to_ready_for_development(monkeypatch):
    gh = _FakeGitHub()
    gh.labels = ["needs approval"]
    _run_step(monkeypatch, "promote", gh, label="needs approval")
    assert gh.labels == ["ready for development"]


def test_promote_noop_for_other_labels(monkeypatch):
    gh = _FakeGitHub()
    for label in ("ready for development", CLOSE):
        _run_step(monkeypatch, "promote", gh, label=label)
    assert gh.calls == []


def test_close_issue_closes_with_comment(monkeypatch):
    _, gh = _run_step(monkeypatch, "close-issue", label=CLOSE, close_reason="Duplikat von #65")
    assert gh.calls == ["comment", "close"]  # Begründung vor Close
    assert gh.state == "closed"


def test_close_issue_noop_for_labels(monkeypatch):
    gh = _FakeGitHub()
    for label in ("ready for development", "needs approval"):
        _run_step(monkeypatch, "close-issue", gh, label=label)
    assert gh.calls == []


# ---------------------------------------------------------------- Review-Findings: Slots, Fix-Runden, Sonar
def test_deps_backfill_takes_no_worktree_slot():
    assert _spec()["loops"]["deps-backfill"]["worktree"] is False


def test_spec_with_api_only_loops_is_valid():
    specs._validate_spec(_spec(), check_root=False)


def test_dependencies_skipped_for_close_proposed():
    assert _refine_steps()["dependencies"]["only_if"] == {"label": ["ready for development", "needs approval"]}
    assert "only_if" not in _loop_steps("deps-backfill")["dependencies"]  # Backfill: immer


def test_builder_variant_shared_by_all_builder_steps():
    variant = _spec()["vars"]["builder_variant"]
    assert variant
    assert _loop_steps("develop")["changes"]["variant"] == variant
    assert _loop_steps("refactor")["refactor"]["variant"] == variant
    assert _loop_steps("refactor-sweep")["sweep"]["variant"] == variant


def test_lint_test_commands_shared_between_develop_and_refactor():
    spec = _spec()
    for loop in ("develop", "refactor"):
        assert _loop_steps(loop)["lint"]["cmd"] == spec["vars"]["lint_cmd"]
    # refactor nutzt das globale test_cmd; develop überschreibt es mit Coverage
    # (coverage.xml für den Quality-Report im review-Step, sonar-scan.sh --pr).
    assert _loop_steps("refactor")["test"]["cmd"] == spec["vars"]["test_cmd"]
    assert (
        _loop_steps("develop")["test"]["cmd"] == spec["vars"]["test_cmd"] + " --cov=src --cov-report=xml:coverage.xml"
    )


def test_develop_review_fix_rounds_are_bounded_and_checked():
    spec = _spec()
    review = _loop_steps("develop")["review"]
    assert review["fix_timeout"] == 1800
    develop_test_cmd = spec["vars"]["test_cmd"] + " --cov=src --cov-report=xml:coverage.xml"
    assert review["fix_checks"] == [spec["vars"]["lint_cmd"], develop_test_cmd]
    assert _loop_steps("develop")["merge"]["update_branch"] is True


def test_sonar_main_scan_is_cron_only():
    """main-Baseline täglich; PRs laufen in `sonar-pr` (sonst überschreiben PR-Scans die main-Analyse)."""
    triggers = _spec()["loops"]["sonar-scan"]["triggers"]
    assert [t["type"] for t in triggers] == ["cron"]


def test_sonar_pr_trigger_reconciles_per_head_and_passes_pr_ref():
    loop = _spec()["loops"]["sonar-pr"]
    on_pr = next(t for t in loop["triggers"] if t["type"] == "on_pr")
    assert on_pr["reconcile"] == "head"
    steps = _loop_steps("sonar-pr")
    assert steps["scan"]["env"] == {
        "SONAR_PR_HEAD_REF": "{pr_head_ref}",
        "SONAR_PR": "{pr_number}",
        "SONAR_REPORT_FILE": ".fabrik/sonar/report.json",
        "SONAR_COMMENT_FILE": ".fabrik/sonar/comment.md",
        "SONAR_LINK_BASE": "https://github.com/didaskomathetes/quark-playground/blob/{pr_head_sha}",
    }
    # Kein LLM-Step; erst kommentieren, dann das Gate bewerten — ein rotes Gate soll im PR landen.
    assert list(steps) == ["scan", "post", "gate"]
    assert steps["gate"]["failure_kind"] == "outcome"
