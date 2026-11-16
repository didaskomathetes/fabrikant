"""Eskalation an einen Menschen (ADR 0057): escalation-Step (Pause, Fortsetzen, schon erledigt),
`exclude_labels` am on_pr-/on_issue-Trigger, Spec-Validierung, ApprovalRequest.kind und der
Gleichstand der Konstanten zwischen Paket und Host-Skript `scripts/pr-status.py`."""

from __future__ import annotations

import io
import urllib.error

import pytest
from helpers import load_script

import fabrik
from fabrik import config
from fabrik.approval import ApprovalRequest
from fabrik.engine import run_loop
from fabrik.failures import ConfigError
from fabrik.specs import _validate_trigger
from fabrik.steps import escalation

PR_URL = "https://github.com/o/r/pull/7"
LABEL = {"name": "needs human"}


def _ctx(**extra) -> dict:
    return {"project": "p", "loop": "escalation", "run_id": "r1", "pr_number": "7", **extra}


def _comment(body: str, n: int = 1) -> dict:
    return {"body": body, "html_url": f"{PR_URL}#issuecomment-{n}"}


def _fake_github(monkeypatch, item: dict, comments: list[dict], *, delete_status: int | None = None) -> list:
    """GitHub-REST des Steps faken; liefert die Aufrufliste (method, url)."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(escalation, "_gh_token", lambda role="dev": "tok")

    def api(method, url, token, payload=None, accept=None):
        calls.append((method, url))
        if method == "DELETE":
            if delete_status:
                raise urllib.error.HTTPError(url, delete_status, "x", {}, io.BytesIO())  # type: ignore[arg-type]
            return {}
        return comments if "/comments" in url else item

    monkeypatch.setattr(escalation, "_gh_api", api)
    return calls


# ── Escalation.from_comments ──


def test_latest_marker_wins_and_text_is_cleaned():
    comments = [
        _comment("alt\n<!-- fabrik-escalate kind=stalled -->", 1),
        _comment("Fremder Kommentar", 2),
        _comment("Braucht MAS-PR.\n\n<!-- fabrik-reply re=7-1 -->\n<!-- fabrik-escalate kind=blocked re=7-1 -->", 3),
    ]
    esc = escalation.Escalation.from_comments(comments)
    assert esc == escalation.Escalation("blocked", "Braucht MAS-PR.", f"{PR_URL}#issuecomment-3")


def test_unknown_kind_and_missing_marker_fall_back_to_blocked(monkeypatch):
    assert escalation.Escalation.from_comments([_comment("<!-- fabrik-escalate kind=panic -->")]).kind == "blocked"
    manual = escalation.Escalation.from_comments([_comment("nur Text")])
    assert manual.kind == "blocked" and "von Hand" in manual.why
    monkeypatch.setattr(escalation, "MAX_WHY_CHARS", 5)
    assert escalation.Escalation.from_comments([_comment("abcdefgh <!-- fabrik-escalate kind=scope -->")]).why == (
        "abcde …"
    )


# ── Step ──


def test_step_pauses_with_escalation_request(monkeypatch):
    item = {"labels": [LABEL], "html_url": PR_URL, "title": "Fix X", "pull_request": {}}
    _fake_github(monkeypatch, item, [_comment("Stillstand.\n<!-- fabrik-escalate kind=stalled -->")])
    ctx = _ctx()
    res = escalation._step_escalation(ctx, {"repo": "o/r"}, lambda _: None)
    assert res["needs_approval"] and ctx[escalation.RESUME_FLAG] == "true"
    approval = ApprovalRequest.from_dict(res["approval"])
    assert (approval.reason, approval.kind, approval.why) == ("escalation", "stalled", "Stillstand.")
    assert (approval.pr, approval.pr_title, approval.issue) == (PR_URL, "Fix X", "")


def test_step_on_issue_links_issue(monkeypatch):
    item = {"labels": [LABEL], "html_url": "https://github.com/o/r/issues/9"}
    _fake_github(monkeypatch, item, [])
    res = escalation._step_escalation({"issue_number": "9"}, {"repo": "o/r"}, lambda _: None)
    assert res["approval"]["issue"].endswith("/issues/9") and "pr" not in res["approval"]


def test_step_without_label_is_done(monkeypatch):
    calls = _fake_github(monkeypatch, {"labels": []}, [])
    res = escalation._step_escalation(_ctx(), {"repo": "o/r"}, lambda _: None)
    assert res["done"] and not res.get("needs_approval") and len(calls) == 1


@pytest.mark.parametrize(("status", "word"), [(None, "entfernt"), (404, "schon entfernt")])
def test_step_resume_removes_label(monkeypatch, status, word):
    calls = _fake_github(monkeypatch, {}, [], delete_status=status)
    res = escalation._step_escalation(
        _ctx(**{escalation.RESUME_FLAG: "true"}), {"repo": "o/r", "label": "needs human"}, lambda _: None
    )
    assert res["ok"] and word in res["summary"]
    assert calls == [("DELETE", "https://api.github.com/repos/o/r/issues/7/labels/needs%20human")]


def test_step_resume_propagates_other_http_errors(monkeypatch):
    _fake_github(monkeypatch, {}, [], delete_status=500)
    with pytest.raises(urllib.error.HTTPError):
        escalation._step_escalation(_ctx(**{escalation.RESUME_FLAG: "true"}), {"repo": "o/r"}, lambda _: None)


@pytest.mark.parametrize(("ctx", "step"), [({}, {"repo": "o/r"}), (_ctx(), {})])
def test_step_config_errors(monkeypatch, ctx, step):
    monkeypatch.setattr(escalation, "_gh_token", lambda role="dev": "tok")
    with pytest.raises(ConfigError):
        escalation._step_escalation(ctx, step, lambda _: None)


def test_comments_are_paginated(monkeypatch):
    monkeypatch.setattr(escalation, "COMMENTS_PER_PAGE", 2)
    pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}
    monkeypatch.setattr(escalation, "_gh_api", lambda m, url, t: pages[int(url.rsplit("=", 1)[1])])
    assert [c["id"] for c in escalation._comments("o/r", 7, "tok")] == [1, 2, 3]


def test_engine_pauses_and_resume_continues(runs_dir, projects_dir, tmp_path, monkeypatch):
    """Durch die Engine: Pause mit kind im Record, Resume entfernt das Label."""
    from fabrik import engine

    item = {"labels": [LABEL], "html_url": PR_URL, "pull_request": {}}
    calls = _fake_github(monkeypatch, item, [_comment("x <!-- fabrik-escalate kind=exhausted -->")])
    monkeypatch.setattr(engine, "_step_escalation", escalation._step_escalation)
    root = tmp_path / "root"
    root.mkdir()
    (projects_dir / "esc.fabrik.yaml").write_text(
        f"name: esc\nroot: {root}\nloops:\n  e:\n    worktree: false\n    steps:\n"
        "      - name: gate\n        type: escalation\n        repo: o/r\n        item: '7'\n"
    )
    rec = run_loop("esc", "e")
    assert rec["status"] == "needs_approval" and rec["approval"]["kind"] == "exhausted"
    assert rec["approval"]["reason"] == "escalation"
    rec = run_loop("esc", "e", resume_id=rec["id"])
    assert rec["status"] == "pass" and calls[-1][0] == "DELETE"


# ── ApprovalRequest.kind ──


def test_approval_kind_round_trip_and_lines():
    req = ApprovalRequest.from_dict({"reason": "escalation", "kind": "scope", "why": "zu groß"})
    assert req.to_dict() == {"reason": "escalation", "kind": "scope", "why": "zu groß"}
    assert req.lines()[:2] == ["art: scope", "warum: zu groß"]
    assert "kind" not in ApprovalRequest(reason="step").to_dict()


# ── exclude_labels ──


def test_on_pr_exclude_labels(monkeypatch):
    prs = [
        {"number": 1, "updated_at": "t", "labels": [{"name": "fabrik"}]},
        {"number": 2, "updated_at": "t", "labels": [{"name": "fabrik"}, LABEL]},
    ]
    fired = []
    monkeypatch.setattr(fabrik.scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    monkeypatch.setattr(fabrik.scheduler, "_gh_token_safe", lambda: "tok")
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: list(prs))
    monkeypatch.setattr(fabrik.scheduler, "loop_run_triggers", lambda n, ln: set())
    trig = {"repo": "o/r", "poll": 0, "labels": ["fabrik"], "exclude_labels": ["needs human"], "reconcile": True}
    fabrik.Scheduler()._check_pr("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_pr:#1"]


def test_unprocessed_issues_skip_excluded_without_using_quota(monkeypatch):
    issues = [{"number": 1, "labels": [LABEL]}, {"number": 2, "labels": []}]
    monkeypatch.setattr(fabrik.scheduler, "_iter_open_issues_oldest_first", lambda repo, tok, labels: iter(issues))
    todo = fabrik.scheduler._unprocessed_issues("o/r", "tok", [], set(), 1, exclude=["needs human"])
    assert [it["number"] for it in todo] == [2]


@pytest.mark.parametrize(
    ("trig", "ok"),
    [
        ({"type": "on_pr", "repo": "o/r", "exclude_labels": ["needs human"]}, True),
        ({"type": "on_issue", "repo": "o/r", "exclude_labels": ["needs human"]}, True),
        ({"type": "on_pr", "repo": "o/r", "exclude_labels": "needs human"}, False),
        ({"type": "on_pr", "repo": "o/r", "exclude_labels": [""]}, False),
        ({"type": "cron", "cron": "* * * * *", "exclude_labels": ["x"]}, False),
    ],
)
def test_validate_exclude_labels(trig, ok):
    errs = _validate_trigger("l", 0, trig, {})
    assert (not errs) is ok, errs


# ── Gleichstand Paket ↔ scripts/pr-status.py ──


def test_constants_match_pr_status_script():
    script = load_script("pr-status.py", "pr_status_escalation")
    assert script.HUMAN_LABEL == config.HUMAN_LABEL
    assert script.ESCALATION_KINDS == config.ESCALATION_KINDS
    assert script.ESCALATE_MARKER_RE.pattern == escalation.ESCALATE_MARKER_RE.pattern
    assert set(script.BUILDER_ESCALATION_KINDS) <= set(config.ESCALATION_KINDS)
    assert "escalation" in config.STEP_TYPES
