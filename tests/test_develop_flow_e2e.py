"""End-to-End: develop-Flow durch die Engine (pr → pr_review → human_gate → Resume → pr_merge).

Führt den Kern des develop-Loops als EINES Szenario durch ``engine.run_loop`` —
mit den echten Step-Implementierungen (``pr``, ``pr_review``, ``human_gate``,
``pr_merge``) und nur den externen Grenzen gemockt (GitHub-API, Agent-Backend,
git, LLM). Damit wird verifiziert, dass die Steps im Zusammenspiel über die Engine
funktionieren, nicht nur isoliert:

  - PR wird mit Label ``fabrik-develop`` angelegt (``pr``-Step; ``review-pr`` filtert auf ``fabrik``)
  - ``pr_review``: Review = Reviewer-Modell, Fix = Builder-Modell (Dual-Identity, ADR 0010)
  - ``human_gate`` pausiert bei ADR-Pfad im Diff (fail-closed, ADR 0010); ohne
    Heuristik-Treffer entscheidet die (gemockte) LLM-Meinung
  - Resume nach Approval → ``pr_merge`` läuft

Die ``changes``/``lint``/``test``/``ship``-Steps (Agent + git) sind hier
bewusst weggelassen: sie sind in ``test_steps_agent``/``test_steps_git``
abgedeckt und würden nur die Worktree-/git-Setup-Komplexität erhöhen, ohne
etwas über den Review-/Gate-/Merge-Pfad zu sagen. ``last_agent_*`` (was der
Builder-Agent-Step in den ctx schreibt) wird per ``initial_vars`` gesetzt.
"""

from __future__ import annotations

import json

from fabrik import gitops, scheduler
from fabrik.engine import run_loop
from fabrik.steps import human_gate, pr_review

REPO = "o/r"
PR_NUMBER = 7
BUILDER_BACKEND = "opencode"
BUILDER_MODEL = "hetzner/qwen3.8-27b"
REVIEWER_BACKEND = "claude_code"
REVIEWER_MODEL = "claude-sonnet-5-5"
FIXED_SHA = "sha-fixed"
DEVELOP_LABEL = "fabrik-develop"  # pr_label im develop-Template (Label-Split)

APPROVED = json.dumps({"verdict": "approved", "findings": [], "summary": "ok"})
CHANGES = json.dumps(
    {"verdict": "changes_requested", "findings": [{"severity": "high", "text": "bug in a.py"}], "summary": "fix it"}
)

# PR-Files für den human_gate: ADR-Pfad → kritisch (Heuristik, kein LLM-Call).
ADR_FILES = [{"filename": "docs/adr/0010-dual-identity.md", "additions": 5, "deletions": 2, "patch": "+ADR change"}]


def _write_spec(projects_dir, tmp_path, name, steps_yaml):
    root = tmp_path / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    body = "\n".join("    " + ln for ln in steps_yaml.strip().splitlines())
    lines = [f"name: {name}", f"root: {root}", "loops:", "  develop:", "    steps:", body]
    (projects_dir / f"{name}.fabrik.yaml").write_text("\n".join(lines) + "\n")


def _develop_steps():
    """Kern des develop-Loops: pr → pr_review → human_gate → pr_merge."""
    return (
        "- name: pr\n  type: pr\n  repo: o/r\n  base: main\n  head: fabrik/auto/42\n"
        f'  labels: [{DEVELOP_LABEL}]\n  title: "fabrik: test (auto)"\n'
        "- name: review\n  type: pr_review\n  repo: o/r\n  backend: claude_code\n"
        f"  model: {REVIEWER_MODEL}\n  max_rounds: 2\n"
        "- name: gate\n  type: human_gate\n  repo: o/r\n  adr_paths: [docs/adr]\n"
        "- name: merge\n  type: pr_merge\n  repo: o/r\n  method: squash\n"
    )


def _patch_externals(monkeypatch, verdicts, files):
    """GitHub-API + Agent-Backend + git in ALLEN develop-Steps faken.

    ``verdicts``: Review-Verdicts pro Runde (Fix-Runde nutzt den Builder).
    ``files``: PR-Files für den human_gate (ADR-Pfad → kritisch).
    """
    calls: dict[str, list] = {
        "labels": [],  # (number, labels)
        "reviews": [],  # (token, payload)
        "merge": [],  # (token, payload)
        "agent": [],  # (kind, backend, model)
        "git": [],
        "llm": [],  # human_gate-LLM-Meinung (nur ohne Heuristik-Treffer)
    }
    review_n = 0

    def _api(method, url, token, payload=None, accept=None):
        if method == "GET" and "/pulls?head=" in url:
            return []  # kein existierender PR → Create
        if method == "GET" and "/compare/" in url:
            return {"ahead_by": 1}
        if method == "POST" and url.endswith("/pulls"):
            return {"number": PR_NUMBER, "html_url": f"https://github.com/{REPO}/pull/{PR_NUMBER}"}
        if method == "POST" and url.endswith(f"/issues/{PR_NUMBER}/labels"):
            calls["labels"].append((PR_NUMBER, payload.get("labels")))
            return {}
        if method == "POST" and url.endswith(f"/pulls/{PR_NUMBER}/reviews"):
            calls["reviews"].append((token, payload))
            return {}
        if method == "GET" and url.endswith(f"/pulls/{PR_NUMBER}/files"):
            return files
        if method == "GET" and url.endswith(f"/pulls/{PR_NUMBER}"):
            return {"head": {"sha": FIXED_SHA}}
        if method == "PUT" and url.endswith(f"/pulls/{PR_NUMBER}/merge"):
            calls["merge"].append((token, payload))
            return {"merged": True, "html_url": f"https://github.com/{REPO}/pull/{PR_NUMBER}"}
        return {}

    def _token(role="dev"):
        return f"tok-{role}"

    def _diff(repo, n, tok):
        return "diff --git a/docs/adr/0010-dual-identity.md b/docs/adr/0010-dual-identity.md"

    def _agent(ctx, step, emit, backend, prompt):
        nonlocal review_n
        if "You are the developer" in prompt:  # Fix-Runde → Builder
            calls["agent"].append(("fix", backend, step.get("model")))
            return {"ok": True, "output": "fixed", "usage": {"total_tokens": 1}}
        review_n += 1  # Review-Runde → Reviewer
        calls["agent"].append(("review", backend, step.get("model")))
        v = verdicts[min(review_n - 1, len(verdicts) - 1)]
        return {"ok": True, "output": v, "usage": {"total_tokens": 1}}

    def _git(ctx, base, *args, check=True):
        calls["git"].append(args)
        if args == ("status", "--porcelain"):
            return "M docs/adr/0010-dual-identity.md"
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "fabrik/auto/42"
        if args == ("rev-parse", "HEAD"):
            return FIXED_SHA
        return ""

    def _llm(ctx, step, emit):
        calls["llm"].append(step.get("provider"))
        ctx.update(gate_critical=False, gate_reason="nicht kritisch")
        return {"ok": True, "summary": "gate_critical=False", "usage": {"total_tokens": 1}}

    # Die Steps laufen über den Hosting-Provider, der die `gitops`-Helfer aufruft —
    # an der `gitops`-Naht patchen (ein einziger Ort für alle Steps).
    monkeypatch.setattr(gitops, "_gh_api", _api)
    monkeypatch.setattr(gitops, "_gh_token", _token)
    monkeypatch.setattr(gitops, "_gh_diff", _diff)
    monkeypatch.setattr(pr_review, "_step_agent_backend", _agent)
    monkeypatch.setattr(pr_review, "_HEAD_SYNC_INTERVAL_S", 0)
    monkeypatch.setattr(pr_review, "_git", _git)
    # Push läuft über apicommit.push_branch (git oder API je Projekt) — hier als git-Call protokolliert.
    monkeypatch.setattr(
        pr_review, "push_branch", lambda base, branch, protected: calls["git"].append(("push", "origin", branch)) or ""
    )
    monkeypatch.setattr(human_gate, "_step_llm", _llm)
    return calls


def _builder_vars():
    """Was der Builder-Agent-Step in den ctx schreibt (hier per initial_vars)."""
    return {"last_agent_backend": BUILDER_BACKEND, "last_agent_model": BUILDER_MODEL}


def test_develop_flow_pr_review_gate_merge(runs_dir, projects_dir, tmp_path, monkeypatch):
    """Vollständiger develop-Kern: PR (Label) → Review (Dual-Identity) → Gate-Pause → Resume → Merge."""
    calls = _patch_externals(monkeypatch, [APPROVED], ADR_FILES)
    _write_spec(projects_dir, tmp_path, "dev_e2e", _develop_steps())

    rec = run_loop("dev_e2e", "develop", trigger="on_issue:#42", initial_vars=_builder_vars())

    # 1) PR mit Label `fabrik-develop` angelegt
    assert calls["labels"] == [(PR_NUMBER, [DEVELOP_LABEL])]
    pr_step = next(s for s in rec["steps"] if s["name"] == "pr")
    assert pr_step["status"] == "pass"

    # 2) pr_review: Review = Reviewer-Modell (nicht der Builder)
    review_calls = [c for c in calls["agent"] if c[0] == "review"]
    assert review_calls, "Review-Call fehlt"
    for _kind, backend, model in review_calls:
        assert backend == REVIEWER_BACKEND
        assert model == REVIEWER_MODEL
    # Reviewer-Identity (App) postet die GitHub-Review
    assert calls["reviews"][0][0] == "tok-reviewer"
    assert calls["reviews"][0][1]["event"] == "APPROVE"

    # 3) human_gate: ADR-Pfad → kritisch → Pause (merge läuft NICHT)
    assert rec["status"] == "needs_approval"
    assert rec["waiting_step"] == "gate"
    assert rec["approval"]["reason"] == "criticality"
    assert "ADR" in rec["approval"]["what"]
    assert [s["name"] for s in rec["steps"]] == ["pr", "review", "gate"]
    assert calls["merge"] == []  # Merge vor der Freigabe nicht ausgeführt
    assert calls["llm"] == []  # Heuristik (ADR-Pfad) entscheidet, kein LLM-Call

    # 4) Resume nach Approval → Gate überspringt (Flag), Merge läuft
    rec2 = run_loop("dev_e2e", "develop", resume_id=rec["id"])
    assert rec2["status"] == "pass"
    merge = next(s for s in rec2["steps"] if s["name"] == "merge")
    assert merge["status"] == "pass"
    assert calls["merge"] == [("tok-dev", {"merge_method": "squash"})]


def test_develop_flow_fix_round_uses_builder(runs_dir, projects_dir, tmp_path, monkeypatch):
    """Dual-Identity (ADR 0010): changes_requested → Fix-Runde nutzt den BUILDER,
    Re-Review den REVIEWER. Der Builder darf nicht selbst reviewen."""
    calls = _patch_externals(monkeypatch, [CHANGES, APPROVED], ADR_FILES)
    _write_spec(projects_dir, tmp_path, "dev_e2e_fix", _develop_steps())

    rec = run_loop("dev_e2e_fix", "develop", trigger="on_issue:#42", initial_vars=_builder_vars())

    # Review-Runde 1: changes_requested → REQUEST_CHANGES
    assert calls["reviews"][0][1]["event"] == "REQUEST_CHANGES"
    # Fix-Runde: Builder-Backend + Builder-Modell (nicht der Reviewer)
    fix_calls = [c for c in calls["agent"] if c[0] == "fix"]
    assert fix_calls == [("fix", BUILDER_BACKEND, BUILDER_MODEL)]
    # Fix hat commited + gepusht
    assert ("add", ":/") in calls["git"]
    assert ("push", "origin", "fabrik/auto/42") in calls["git"]
    # Re-Review: Reviewer-Modell, APPROVE
    assert calls["reviews"][1][1]["event"] == "APPROVE"
    review_calls = [c for c in calls["agent"] if c[0] == "review"]
    assert all(m == REVIEWER_MODEL for _, _, m in review_calls)
    # Danach Gate-Pause (ADR im Diff)
    assert rec["status"] == "needs_approval"
    assert rec["waiting_step"] == "gate"


def test_develop_flow_non_critical_automerger(runs_dir, projects_dir, tmp_path, monkeypatch):
    """Ohne ADR-Pfad greift keine Heuristik → die LLM-Meinung (gemockt: nicht kritisch)
    entscheidet → der Loop läuft autonom durch bis zum Merge (keine Pause)."""
    plain_files = [{"filename": "src/foo.py", "additions": 3, "deletions": 1, "patch": "+x"}]
    calls = _patch_externals(monkeypatch, [APPROVED], plain_files)
    _write_spec(projects_dir, tmp_path, "dev_e2e_auto", _develop_steps())

    rec = run_loop("dev_e2e_auto", "develop", trigger="on_issue:#42", initial_vars=_builder_vars())

    assert rec["status"] == "pass"
    assert "approval" not in rec
    assert [s["name"] for s in rec["steps"]] == ["pr", "review", "gate", "merge"]
    assert calls["merge"] == [("tok-dev", {"merge_method": "squash"})]
    assert len(calls["llm"]) == 1  # genau eine LLM-Meinung im human_gate


# ---------------------------------------------------------------- Doppel-Review (on_pr-Trigger)
class _FakeSched(scheduler.Scheduler):
    """Echter Scheduler, Thread nie gestartet: Tests rufen _check_pr direkt auf."""


def _pr(num, labels, sha="abc123"):
    return {
        "number": num,
        "title": "fabrik: test (auto)",
        "state": "open",
        "updated_at": "2026-01-01T00:00:00Z",
        "labels": [{"name": x} for x in labels],
        "head": {"sha": sha, "ref": "fabrik/auto/42"},
        "html_url": f"https://github.com/o/r/pull/{num}",
    }


def test_on_pr_fires_on_develop_labeled_pr(monkeypatch):
    """Label-Split gegen Doppel-Review: der review-pr-Loop (on_pr, Label `fabrik`)
    feuert auf PRs mit Label `fabrik` (manuelle/Refactor-PRs, früher auch develop-PRs).
    develop-PRs tragen `fabrik-develop` → darauf feuert er NICHT (kein Doppel-Review,
    kein Human-Gate-Bypass über pr-reply)."""
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    trig = {"repo": "o/r", "poll": 0, "labels": ["fabrik"], "reconcile": "head"}

    # PR mit Label `fabrik` → review-pr feuert (vor dem Split: auch develop-PRs → Doppel-Review)
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t, payload=None: [_pr(7, ["fabrik"])])
    _FakeSched()._check_pr("p", "review-pr", {"github": "o/r"}, trig)
    assert fired == ["on_pr:#7@abc123"]

    # Label-Split: develop-PR mit `fabrik-develop` → review-pr (Label `fabrik`) feuert NICHT
    fired2 = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired2.append(t))
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t, payload=None: [_pr(7, ["fabrik-develop"])])
    _FakeSched()._check_pr("p", "review-pr", {"github": "o/r"}, trig)
    assert fired2 == []
