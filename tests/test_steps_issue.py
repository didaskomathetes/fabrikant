"""Step: issue — native GitHub-Issue-Operationen (ADR 0001 §5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from helpers import http_error

from fabrik import secrets
from fabrik.engine import _dispatch_step
from fabrik.secrets import _redact_record
from fabrik.steps import issue

PROJECTS = Path(__file__).resolve().parent.parent / "projects"


class _Api:
    """Fake für gitops._gh_api: protokolliert Calls, liefert Antworten pro (method, Suffix)."""

    def __init__(self, responses=None, errors=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}
        self.errors = errors or {}

    def __call__(self, method, url, token, payload=None, accept=None):
        assert token == "tok-dev"
        self.calls.append((method, url, payload))
        for (m, suffix), code in self.errors.items():
            if m == method and url.endswith(suffix):
                raise http_error(code, url, "err", body=b"boom")
        return self.responses.get(method, {})


@pytest.fixture
def api(monkeypatch):
    fake = _Api()
    monkeypatch.setattr(issue, "_gh_token", lambda role="dev": "tok-dev")
    monkeypatch.setattr(issue, "_gh_api", fake)
    return fake


def test_create_emits_issue_number_and_url(api):
    api.responses["POST"] = {"number": 42, "html_url": "https://gh/o/r/issues/42"}
    ctx = {"t": "Smell"}
    step = {"action": "create", "repo": "o/r", "title": "x {t}", "body": "b", "labels": ["a", "{t}"]}
    res = issue._step_issue(ctx, step)
    assert res["ok"] is True and "#42" in res["summary"]
    assert ctx["issue_number"] == "42" and ctx["issue_url"] == "https://gh/o/r/issues/42"
    assert api.calls == [
        ("POST", "https://api.github.com/repos/o/r/issues", {"title": "x Smell", "body": "b", "labels": ["a", "Smell"]})
    ]


def test_create_labels_as_comma_string_and_no_labels(api):
    api.responses["POST"] = {"number": 1}
    issue._step_issue({}, {"action": "create", "repo": "o/r", "title": "t", "labels": "a, b,"})
    assert api.calls[0][2]["labels"] == ["a", "b"]
    issue._step_issue({}, {"action": "create", "repo": "o/r", "title": "t"})
    assert "labels" not in api.calls[1][2]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("bug, bug", ["bug"]),
        (["x {t}", "  ", "x Smell"], ["x Smell"]),
        (("a", 7), ["a", "7"]),
        (42, ["42"]),
        (True, ["True"]),
    ],
)
def test_labels_normalizes_scalars_and_dedupes(raw, expected):
    """YAML-Skalare (`labels: 42`) crashen nicht; Duplikate werden wie im pr-Step entfernt."""
    assert issue._labels(raw, {"t": "Smell"}) == expected


def test_create_requires_title(api):
    res = issue._step_issue({}, {"action": "create", "repo": "o/r", "title": "  "})
    assert res["ok"] is False and "title" in res["summary"]
    assert api.calls == []


def test_edit_title_body_state(api):
    res = issue._step_issue(
        {"n": "5"}, {"action": "edit", "repo": "o/r", "number": "{n}", "title": "T", "body": "B", "state": "Closed"}
    )
    assert res["ok"] is True
    assert api.calls == [
        ("PATCH", "https://api.github.com/repos/o/r/issues/5", {"title": "T", "body": "B", "state": "closed"})
    ]


def test_edit_number_falls_back_to_ctx(api):
    issue._step_issue({"issue_number": "9"}, {"action": "edit", "repo": "o/r", "title": "T"})
    assert api.calls[0][1].endswith("/issues/9")


def test_edit_append_keeps_existing_body(api):
    api.responses["GET"] = {"body": "alt"}
    issue._step_issue({"issue_number": "3"}, {"action": "edit", "repo": "o/r", "body": "neu", "append": True})
    assert api.calls[-1] == ("PATCH", "https://api.github.com/repos/o/r/issues/3", {"body": "alt\n\nneu"})


def test_edit_append_to_empty_body(api):
    api.responses["GET"] = {"body": None}
    issue._step_issue({"issue_number": "3"}, {"action": "edit", "repo": "o/r", "body": "neu", "append": True})
    assert api.calls[-1][2] == {"body": "neu"}


def test_edit_append_marker_is_idempotent(api):
    api.responses["GET"] = {"body": "x <!-- M --> y"}
    step = {"action": "edit", "repo": "o/r", "body": "neu", "append": True, "unless_contains": "<!-- M -->"}
    res = issue._step_issue({"issue_number": "3"}, step)
    assert res["ok"] is True and "unverändert" in res["summary"]
    assert [c[0] for c in api.calls] == ["GET"]


def test_edit_validation(api):
    assert issue._step_issue({}, {"action": "edit", "repo": "o/r", "number": "1"})["ok"] is False
    res = issue._step_issue({}, {"action": "edit", "repo": "o/r", "number": "1", "state": "weird"})
    assert res["ok"] is False and "state" in res["summary"]
    assert api.calls == []


def test_label_add_and_remove(api):
    res = issue._step_issue(
        {"issue_number": "7"},
        {"action": "label", "repo": "o/r", "labels": ["ready for development"], "remove_labels": ["needs approval"]},
    )
    assert res["ok"] is True
    base = "https://api.github.com/repos/o/r/issues/7/labels"
    assert api.calls == [
        ("POST", base, {"labels": ["ready for development"]}),
        ("DELETE", base + "/needs%20approval", None),
    ]


def test_label_remove_missing_is_idempotent(api):
    api.errors[("DELETE", "/gone")] = 404
    res = issue._step_issue({"issue_number": "7"}, {"action": "label", "repo": "o/r", "remove_labels": ["gone"]})
    assert res["ok"] is True


def test_label_remove_other_error_fails(api):
    api.errors[("DELETE", "/x")] = 500
    res = issue._step_issue({"issue_number": "7"}, {"action": "label", "repo": "o/r", "remove_labels": ["x"]})
    assert res["ok"] is False and "500" in res["summary"]


def test_label_requires_labels(api):
    res = issue._step_issue({"issue_number": "7"}, {"action": "label", "repo": "o/r"})
    assert res["ok"] is False


def test_close_with_comment(api):
    res = issue._step_issue(
        {"issue_number": "4", "why": "Duplikat"}, {"action": "close", "repo": "o/r", "comment": "{why}"}
    )
    assert res["ok"] is True
    assert api.calls == [
        ("POST", "https://api.github.com/repos/o/r/issues/4/comments", {"body": "Duplikat"}),
        ("PATCH", "https://api.github.com/repos/o/r/issues/4", {"state": "closed"}),
    ]


def test_close_without_comment(api):
    issue._step_issue({"issue_number": "4"}, {"action": "close", "repo": "o/r"})
    assert [c[0] for c in api.calls] == ["PATCH"]


def test_http_error_becomes_failed_result(api):
    api.errors[("PATCH", "/issues/4")] = 403
    res = issue._step_issue({"issue_number": "4"}, {"action": "close", "repo": "o/r"})
    assert res["ok"] is False and "403" in res["summary"] and "boom" in res["summary"]


def test_invalid_inputs(api):
    assert issue._step_issue({}, {"action": "delete", "repo": "o/r"})["ok"] is False
    assert issue._step_issue({}, {"action": "close", "repo": "o/r"})["ok"] is False  # number fehlt
    with pytest.raises(ValueError):
        issue._step_issue({}, {"action": "create", "title": "t"})


def test_only_if_and_skip_if_empty(api):
    step = {"action": "close", "repo": "o/r", "only_if": {"label": "close"}}
    res = issue._step_issue({"issue_number": "1", "label": "other"}, step)
    assert res["ok"] is True and "übersprungen" in res["summary"]
    issue._step_issue({"issue_number": "1", "label": "close"}, step)
    assert len(api.calls) == 1

    api.calls.clear()
    skip = {"action": "create", "repo": "o/r", "title": "t", "skip_if_empty": ["a", "b"]}
    assert "übersprungen" in issue._step_issue({"a": " ", "b": ""}, skip)["summary"]
    assert api.calls == []
    api.responses["POST"] = {"number": 2}
    issue._step_issue({"a": "", "b": "x"}, skip)
    assert len(api.calls) == 1
    assert "übersprungen" in issue._step_issue({}, {**skip, "skip_if_empty": "a"})["summary"]


def test_dispatch_and_token_never_in_result(monkeypatch):
    secret = "ghs_supersecret_token_123"
    seen = {}

    def fake_api(method, url, token, payload=None, accept=None):
        seen["token"] = token
        return {"number": 1, "html_url": "https://gh/1"}

    monkeypatch.setenv("GITHUB_TOKEN", secret)
    monkeypatch.setattr(issue, "_gh_api", fake_api)
    res = _dispatch_step({}, {"type": "issue", "action": "create", "repo": "o/r", "title": "t"}, lambda m: None)
    assert seen["token"] == secret
    assert secret not in json.dumps(res)


def test_token_redacted_in_run_record(monkeypatch):
    """Selbst wenn ein Fehlertext das Token enthielte, maskiert der Run-Record es."""
    secret = "ghs_supersecret_token_123"
    monkeypatch.setattr(secrets, "_env_secrets", [secret])
    record = {"steps": [{"summary": f"issue: boom {secret}", "output": secret}], "vars": {"issue_url": secret}}
    dumped = json.dumps(_redact_record(record))
    assert secret not in dumped and "[REDACTED]" in dumped


@pytest.mark.parametrize(
    ("project", "loop"),
    [("fabrik", "refine"), ("fabrik", "refactor-sweep")],
)
def test_migrated_flows_use_issue_steps_without_gh(project, loop):
    spec = yaml.safe_load((PROJECTS / f"{project}.fabrik.yaml").read_text())
    loops = spec["loops"]
    steps = loops[loop]["steps"] if isinstance(loops, dict) else next(lp for lp in loops if lp["name"] == loop)["steps"]
    assert any(s["type"] == "issue" for s in steps)
    assert not [s for s in steps if s["type"] == "shell" and "gh " in s.get("cmd", "")]
    for s in steps:
        if s["type"] == "issue":
            assert s["action"] in issue.ACTIONS


def test_sweep_prompt_has_no_gh_calls():
    text = (PROJECTS.parent / "prompts" / "fabrik-refactor-sweep.prompt.md").read_text()
    assert "gh issue" not in text


# ---------------------------------------------------------------- list / block (ADR 0031)
class _RoutedApi:
    """Fake für gitops._gh_api mit Antworten pro (method, URL-Teilstring); erster Treffer gewinnt."""

    def __init__(self, routes: list[tuple[str, str, object]]):
        self.routes = routes
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, url, token, payload=None, accept=None):
        self.calls.append((method, url, payload))
        for m, part, res in self.routes:
            if m == method and part in url:
                if isinstance(res, int):
                    raise http_error(res, url, "err", body=b"boom")
                return res(url) if callable(res) else res
        return {}


@pytest.fixture
def routed(monkeypatch):
    def _install(routes, blocked_by=(), deps=None):
        """`blocked_by`: Blocker von jedem Issue; `deps`: {(nummer, kind): [nummern]} (Vorrang)."""
        fake = _RoutedApi(routes)
        monkeypatch.setattr(issue, "_gh_token", lambda role="dev": "tok-dev")
        monkeypatch.setattr(issue, "_gh_api", fake)

        def _deps(repo, n, tok, kind="blocked_by"):
            nums = (deps or {}).get((int(n), kind), blocked_by if kind == "blocked_by" else ())
            return [{"number": b} for b in nums]

        monkeypatch.setattr(issue, "_gh_dependencies", _deps)
        return fake

    return _install


def _gh_issue(num, **extra):
    return {"number": num, "id": 1000 + num, "title": f"T{num}", "state": "open", **extra}


def test_list_emits_lines_without_prs_and_self(routed):
    items = [
        _gh_issue(1, labels=[{"name": "p1"}, {"name": "fabrik"}], body="Zeile 1\n\n  Zeile 2"),
        _gh_issue(2, pull_request={}),
        _gh_issue(7),  # eigenes Issue
        _gh_issue(3, body=None),
    ]
    api = routed([("GET", "/issues?", items)])
    ctx = {"issue_number": "7"}
    res = issue._step_issue(ctx, {"action": "list", "repo": "o/r", "labels": ["fabrik"], "emit": "open_issues"})
    assert res["ok"] is True and "2 Issues (open) → open_issues" in res["summary"]
    assert ctx["open_issues"] == "#1 [p1, fabrik] T1 — Zeile 1 Zeile 2\n#3 T3"
    url = api.calls[0][1]
    assert "state=open" in url and "labels=fabrik" in url and "sort=updated" in url and "page=1" in url


def test_list_truncates_body_and_respects_limit(routed):
    items = [_gh_issue(n, body="x" * 500) for n in range(1, 6)]
    routed([("GET", "/issues?", items)])
    ctx: dict = {}
    issue._step_issue(ctx, {"action": "list", "repo": "o/r", "state": "closed", "limit": 2})
    lines = ctx["issues"].splitlines()  # Default-Variable
    assert len(lines) == 2
    assert lines[0] == "#1 T1 — " + "x" * issue.LIST_BODY_CHARS


def test_list_paginates_until_short_page(routed):
    per = issue._PER_PAGE

    def page(url):
        p = int(url.split("&page=")[1].split("&")[0])
        start = (p - 1) * per
        return [_gh_issue(n) for n in range(start + 1, start + 1 + (per if p == 1 else 3))]

    api = routed([("GET", "/issues?", page)])
    ctx: dict = {}
    issue._step_issue(ctx, {"action": "list", "repo": "o/r", "limit": 500})
    assert len(ctx["issues"].splitlines()) == per + 3
    assert len(api.calls) == 2


def test_list_invalid_state(routed):
    api = routed([])
    res = issue._step_issue({}, {"action": "list", "repo": "o/r", "state": "all"})
    assert res["ok"] is False and "state" in res["summary"]
    assert api.calls == []


def test_refs_parsing_ignores_noise_self_and_duplicates():
    assert issue._refs("#12, 14 #12; Phase-2 (#15) #7", own="7") == [12, 14]
    assert issue._refs("", own="7") == []


def test_block_cap_keeps_prerequisite(routed):
    refs = ", ".join(f"#{n}" for n in range(10, 10 + issue.MAX_BLOCKERS + 1))
    api = routed([("GET", "/issues/", lambda url: _gh_issue(int(url.rsplit("/", 1)[1])))])
    step = {"action": "block", "repo": "o/r", "blocked_by": refs, "prerequisite": "#50"}
    res = issue._step_issue({"issue_number": "7"}, step)
    posted = [c[2]["issue_id"] for c in api.calls if c[0] == "POST"]
    assert len(posted) == issue.MAX_BLOCKERS + 1 and 1050 in posted
    assert f"#{10 + issue.MAX_BLOCKERS} (über Limit" in res["summary"]


def test_block_links_open_issues_and_skips_invalid(routed):
    api = routed(
        [
            ("GET", "/issues/12", _gh_issue(12)),
            ("GET", "/issues/13", _gh_issue(13, state="closed")),
            ("GET", "/issues/14", _gh_issue(14, pull_request={})),
            ("GET", "/issues/99", 404),
        ]
    )
    ctx = {"issue_number": "7", "blocked_by": "#12, #13, #14, #99, #7"}
    res = issue._step_issue(ctx, {"action": "block", "repo": "o/r", "blocked_by": "{blocked_by}"})
    assert res["ok"] is True
    posts = [c for c in api.calls if c[0] == "POST"]
    assert posts == [("POST", "https://api.github.com/repos/o/r/issues/7/dependencies/blocked_by", {"issue_id": 1012})]
    assert ctx["blockers"] == "#12"
    assert "#13 (geschlossen)" in res["summary"] and "#14 (PR)" in res["summary"]
    assert "#99 (nicht gefunden)" in res["summary"]


def test_block_is_idempotent_for_existing_blockers(routed):
    api = routed([], blocked_by=[12])
    ctx = {"issue_number": "7"}
    res = issue._step_issue(ctx, {"action": "block", "repo": "o/r", "blocked_by": "#12"})
    assert res["ok"] is True and ctx["blockers"] == "#12"
    assert api.calls == []


def test_block_caps_number_of_blockers(routed):
    refs = ", ".join(f"#{n}" for n in range(10, 10 + issue.MAX_BLOCKERS + 2))
    api = routed([("GET", "/issues/", lambda url: _gh_issue(int(url.rsplit("/", 1)[1])))])
    res = issue._step_issue({"issue_number": "7"}, {"action": "block", "repo": "o/r", "blocked_by": refs})
    assert len([c for c in api.calls if c[0] == "POST"]) == issue.MAX_BLOCKERS
    assert "über Limit" in res["summary"]


def test_block_without_refs_is_noop(routed):
    api = routed([])
    res = issue._step_issue({"issue_number": "7"}, {"action": "block", "repo": "o/r", "blocked_by": "keine"})
    assert res["ok"] is True and "keine Blocker" in res["summary"]
    assert api.calls == []


def test_block_api_error_fails_closed(routed):
    """z. B. 422 (Zyklus) → Step schlägt fehl, der Refine-Run labelt nicht."""
    routed([("GET", "/issues/12", _gh_issue(12)), ("POST", "/dependencies/blocked_by", 422)])
    res = issue._step_issue({"issue_number": "7"}, {"action": "block", "repo": "o/r", "blocked_by": "#12"})
    assert res["ok"] is False and "HTTP 422" in res["summary"]


def test_block_lookup_error_other_than_404_fails(routed):
    routed([("GET", "/issues/12", 500)])
    res = issue._step_issue({"issue_number": "7"}, {"action": "block", "repo": "o/r", "blocked_by": "#12"})
    assert res["ok"] is False and "HTTP 500" in res["summary"]


def test_block_requires_number(routed):
    res = issue._step_issue({}, {"action": "block", "repo": "o/r", "blocked_by": "#12"})
    assert res["ok"] is False and "number" in res["summary"]


def test_close_state_reason(routed):
    api = routed([])
    res = issue._step_issue(
        {"issue_number": "4", "k": "Not_Planned"}, {"action": "close", "repo": "o/r", "state_reason": "{k}"}
    )
    assert res["ok"] is True and "(not_planned)" in res["summary"]
    assert api.calls == [
        ("PATCH", "https://api.github.com/repos/o/r/issues/4", {"state": "closed", "state_reason": "not_planned"})
    ]


def test_close_empty_state_reason_uses_github_default(routed):
    api = routed([])
    issue._step_issue({"issue_number": "4", "k": ""}, {"action": "close", "repo": "o/r", "state_reason": "{k}"})
    assert api.calls[-1][2] == {"state": "closed"}


def test_close_invalid_state_reason(routed):
    api = routed([])
    res = issue._step_issue({"issue_number": "4"}, {"action": "close", "repo": "o/r", "state_reason": "wontfix"})
    assert res["ok"] is False and "state_reason" in res["summary"]
    assert api.calls == []


def test_close_duplicate_moves_dependents_to_original(routed):
    """#4 ist Duplikat von #12: #20 (blockiert von #4) wird von #12 blockiert, #21 hatte #12 schon."""
    api = routed(
        [("GET", "/issues/12", _gh_issue(12)), ("GET", "/issues/4", _gh_issue(4))],
        deps={(4, "blocking"): [20, 21], (21, "blocked_by"): [12, 4]},
    )
    ctx = {"issue_number": "4", "dup": "#12", "k": "duplicate"}
    step = {"action": "close", "repo": "o/r", "comment": "Duplikat", "state_reason": "{k}", "duplicate_of": "{dup}"}
    res = issue._step_issue(ctx, step)
    assert res["ok"] is True and "umgehängt auf #12: #20, #21" in res["summary"]
    writes = [(m, u.removeprefix("https://api.github.com/repos/o/r/issues/"), p) for m, u, p in api.calls if m != "GET"]
    assert writes == [
        ("POST", "20/dependencies/blocked_by", {"issue_id": 1012}),
        ("DELETE", "20/dependencies/blocked_by/1004", None),
        ("DELETE", "21/dependencies/blocked_by/1004", None),  # #12 bestand schon → nur lösen
        ("POST", "4/comments", {"body": "Duplikat"}),
        ("PATCH", "4", {"state": "closed", "state_reason": "duplicate", "duplicate_issue_id": 1012}),
    ]


def test_close_duplicate_unlinks_original_blocked_by_duplicate(routed):
    """Hing das Original selbst am Duplikat, wird nur die Relation gelöst (keine Selbst-Blockade)."""
    api = routed(
        [("GET", "/issues/12", _gh_issue(12)), ("GET", "/issues/4", _gh_issue(4))], deps={(4, "blocking"): [12]}
    )
    res = issue._step_issue({"issue_number": "4"}, {"action": "close", "repo": "o/r", "duplicate_of": "12"})
    assert res["ok"] is True and "umgehängt" not in res["summary"]
    assert [(m, u.rsplit("issues/", 1)[1]) for m, u, _ in api.calls if m in ("POST", "DELETE")] == [
        ("DELETE", "12/dependencies/blocked_by/1004")
    ]


def test_close_duplicate_of_pr_fails_before_closing(routed):
    api = routed([("GET", "/issues/12", _gh_issue(12, pull_request={}))])
    res = issue._step_issue({"issue_number": "4"}, {"action": "close", "repo": "o/r", "duplicate_of": "#12"})
    assert res["ok"] is False and "PR" in res["summary"]
    assert [c[0] for c in api.calls] == ["GET"]


def test_close_duplicate_transfer_error_keeps_issue_open(routed):
    """Umhängen schlägt fehl → kein Close (sonst hingen Abhängige am Duplikat fest)."""
    api = routed(
        [("GET", "/issues/12", _gh_issue(12)), ("GET", "/issues/4", _gh_issue(4)), ("POST", "/dependencies/", 422)],
        deps={(4, "blocking"): [20]},
    )
    res = issue._step_issue({"issue_number": "4"}, {"action": "close", "repo": "o/r", "duplicate_of": "#12"})
    assert res["ok"] is False and "HTTP 422" in res["summary"]
    assert "PATCH" not in [c[0] for c in api.calls]


def test_create_emit_keeps_issue_number(routed):
    api = routed([("POST", "/issues", {"number": 50, "html_url": "u"})])
    ctx = {"issue_number": "7"}
    issue._step_issue(ctx, {"action": "create", "repo": "o/r", "title": "t", "emit": "prerequisite_number"})
    assert ctx == {"issue_number": "7", "prerequisite_number": "50"}
    assert [c[0] for c in api.calls] == ["POST"]


def test_create_inherit_labels_is_a_whitelist(routed):
    """Nur gelistete Labels wandern mit — Status-Labels wie 'ready for development' nie."""
    src = _gh_issue(7, labels=[{"name": "p1"}, {"name": "ready for development"}, {"name": "fabrik"}])
    api = routed([("GET", "/issues/7", src), ("POST", "/issues", {"number": 50})])
    step = {"action": "create", "repo": "o/r", "title": "t", "labels": ["fabrik"], "inherit_labels": ["p0", "p1"]}
    issue._step_issue({"issue_number": "7"}, step)
    assert api.calls[-1][2]["labels"] == ["fabrik", "p1"]


def test_create_inherit_labels_without_source_issue(routed):
    api = routed([("POST", "/issues", {"number": 50})])
    issue._step_issue({}, {"action": "create", "repo": "o/r", "title": "t", "inherit_labels": ["p1"]})
    assert [c[0] for c in api.calls] == ["POST"] and "labels" not in api.calls[0][2]


def test_failure_kind_static_vs_substituted(api):
    """Nur die statische Spec-Angabe `action` ist ein Konfigurationsfehler; Werte aus dem Kontext → unknown."""
    assert issue._step_issue({}, {"action": "frobnicate", "repo": "o/r"})["failure_kind"] == "environment"
    assert issue._step_issue({}, {"action": "edit", "repo": "o/r", "number": "{n}"})["failure_kind"] == "unknown"
    assert (
        issue._step_issue({"t": " "}, {"action": "create", "repo": "o/r", "title": "{t}"})["failure_kind"] == "unknown"
    )
