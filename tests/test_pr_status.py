"""scripts/pr-status.py (ADR 0024) — Merge-Entscheidung (Review-Marker vs. GitHub-Review,
offene Threads), Review-Posting (Event, Cap, 422-Fallback, Idempotenz), Kommentar-Quellen,
Threads und CLI (gh gemockt)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from helpers import load_script

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pr-status.py"
REVIEWER = "owner"
HEAD = "a" * 40
OLD = "b" * 40
REPO = "o/r"


@pytest.fixture(scope="module")
def mod():
    return load_script(SCRIPT.name, "pr_status")


@pytest.fixture(autouse=True)
def _no_reviewer_login_override(mod, monkeypatch):
    """Lokales fabrikant.env (von config.py geladen) darf den Reviewer-Login der Tests nicht überschreiben."""
    monkeypatch.delenv(mod.REVIEWER_LOGIN_ENV, raising=False)


@pytest.fixture
def warnings_seen(mod, capsys, monkeypatch):
    """Reported `warn` texts (stderr, without marker); fresh dedup memory per test."""
    monkeypatch.setattr(mod, "_warned", set())

    def seen() -> list[str]:
        err = capsys.readouterr().err
        return [
            line.removeprefix(mod.WARNING_MARKER) for line in err.splitlines() if line.startswith(mod.WARNING_MARKER)
        ]

    return seen


def _marker(verdict: str, sha: str = HEAD) -> str:
    return f"Summary …\n<!-- fabrikant-review verdict={verdict} sha={sha} -->"


def _review(body: str = "", state: str = "COMMENTED", author: str = REVIEWER) -> dict:
    """Review im `gh pr view --json reviews`-Format."""
    return {"author": {"login": author}, "body": body, "state": state}


def _pr(status: str = "CLEAN", reviews: list[dict] | None = None, labels: tuple[str, ...] = ()) -> dict:
    return {
        "mergeStateStatus": status,
        "headRefOid": HEAD,
        "reviews": reviews or [],
        "labels": [{"name": name} for name in labels],
    }


def _inline(n: int) -> dict:
    return {"path": "fabrikant/src/x.py", "line": n, "body": f"Nit {n}"}


# ── merge_decision ──


@pytest.mark.parametrize(
    ("status", "reviews", "threads", "action"),
    [
        ("CLEAN", [_review(_marker("approved"))], 0, "merge"),  # eigene PR: COMMENT + Marker
        ("CLEAN", [_review(_marker("approved"), "APPROVED")], 0, "merge"),  # Bot-PR: formal + Marker
        ("CLEAN", [_review(_marker("changes_requested"))], 0, "wait"),
        ("CLEAN", [_review(_marker("approved", OLD))], 0, "wait"),  # neue Commits seit Review
        ("CLEAN", [_review(_marker("approved"), author="mallory")], 0, "wait"),  # fremder Marker
        ("CLEAN", [], 0, "wait"),  # kein Review (PR #124-Fall vor dem Fix)
        ("CLEAN", [_review(_marker("approved"))], 2, "wait"),  # offene Inline-Threads
        ("HAS_HOOKS", [_review(_marker("approved"))], 0, "merge"),
        ("BLOCKED", [_review(_marker("approved"))], 0, "wait"),
        ("UNSTABLE", [_review(_marker("approved"))], 0, "wait"),
        ("UNKNOWN", [_review(_marker("approved"))], 0, "wait"),
        ("DRAFT", [_review(_marker("approved"))], 0, "wait"),
        ("NEW_GITHUB_STATE", [_review(_marker("approved"))], 0, "wait"),
        ("DIRTY", [_review(_marker("approved"))], 0, "resolve_conflicts"),
        ("BEHIND", [_review(_marker("approved"))], 0, "resolve_conflicts"),
        ("BEHIND", [], 0, "resolve_conflicts"),  # Rebase unabhängig von der Freigabe
        ("CLEAN", [_review("LGTM", "APPROVED", "human")], 0, "merge"),  # Mensch/pr_review-Step
        ("CLEAN", [_review("lgtm", "APPROVED", "h"), _review("hm", "COMMENTED", "h")], 0, "merge"),
        ("CLEAN", [_review("nein", "CHANGES_REQUESTED", "human"), _review(_marker("approved"))], 0, "wait"),
        # Altes pr_review-CHANGES_REQUESTED desselben Accounts, danach Marker-Approve
        # für den Head → überholt (PRs #180/#197/#215 hingen daran).
        ("CLEAN", [_review("fix x", "CHANGES_REQUESTED"), _review(_marker("approved"), "APPROVED")], 0, "merge"),
        # Marker als COMMENT (nicht entscheidend) überholt das CHANGES_REQUESTED nicht.
        ("CLEAN", [_review("fix x", "CHANGES_REQUESTED"), _review(_marker("approved"))], 0, "wait"),
        ("CLEAN", [_review(_marker("approved"), "APPROVED"), _review("doch nicht", "CHANGES_REQUESTED")], 0, "wait"),
        ("CLEAN", [_review("nein", "CHANGES_REQUESTED", "h"), _review("ok", "APPROVED", "h")], 0, "merge"),
        ("CLEAN", [_review("ok", "APPROVED", "a"), _review("nein", "CHANGES_REQUESTED", "b")], 0, "wait"),
    ],
)
def test_merge_decision(mod, status, reviews, threads, action):
    assert mod.merge_decision(_pr(status, reviews), REVIEWER, threads)["action"] == action


@pytest.mark.parametrize(
    ("status", "reason"),
    [("BLOCKED", "Branch-Protection"), ("UNSTABLE", "Checks"), ("FOO", "unbekannter Merge-Status")],
)
def test_merge_decision_explains_blocked_approval(mod, status, reason):
    """Freigegeben, aber nicht mergebar → der Grund nennt den Status (kein stilles Hängen)."""
    out = mod.merge_decision(_pr(status, [_review(_marker("approved"))]), REVIEWER, 0)
    assert out["action"] == "wait"
    assert reason in out["reason"] and f"mergeStateStatus={status}" in out["reason"]


@pytest.mark.parametrize(
    ("labels", "action"),
    [
        (("fabrikant", "needs approval"), "wait"),  # ADR-PR: nur der approval-Loop mergt (ADR 0011)
        (("needs approval",), "wait"),
        (("fabrikant",), "merge"),
        (("fabrikant", "needs approval-ish"), "merge"),  # exakter Label-Name, kein Teilstring
    ],
)
def test_merge_decision_needs_approval_label_blocks_merge(mod, labels, action):
    out = mod.merge_decision(_pr(reviews=[_review(_marker("approved"))], labels=labels), REVIEWER, 0)
    assert out["action"] == action
    if action == "wait":
        assert "approval-Loop" in out["reason"]


def test_merge_decision_rebases_needs_approval_pr(mod):
    """Konflikt geht vor: der approval-Loop kann einen DIRTY-PR nicht mergen."""
    out = mod.merge_decision(_pr("DIRTY", labels=("needs approval",)), REVIEWER, 0)
    assert out["action"] == "resolve_conflicts"


def test_merge_state_tables_are_disjoint(mod):
    tables = [set(mod.MERGE_STATES), set(mod.REBASE_STATES), set(mod.WAIT_STATES)]
    assert sum(len(t) for t in tables) == len(set().union(*tables))


def test_merge_decision_latest_marker_wins(mod):
    reviews = [_review(_marker("changes_requested")), _review(_marker("approved"))]
    assert mod.merge_decision(_pr(reviews=reviews), REVIEWER, 0)["action"] == "merge"
    assert mod.merge_decision(_pr(reviews=reviews[::-1]), REVIEWER, 0)["action"] == "wait"


def test_review_verdict_ignores_malformed_marker(mod):
    assert mod.review_verdict([_review("<!-- fabrikant-review verdict=approved sha=abc -->")], REVIEWER) is None


# ── build_review / post_review ──


@pytest.mark.parametrize(
    ("verdict", "comment_only", "event"),
    [
        ("approved", True, "COMMENT"),
        ("changes_requested", True, "COMMENT"),
        ("approved", False, "APPROVE"),
        ("changes_requested", False, "REQUEST_CHANGES"),
    ],
)
def test_build_review_event(mod, verdict, comment_only, event):
    req = mod.build_review({"body": _marker(verdict)}, comment_only=comment_only)
    assert (req["event"], req["commit_id"], req["comments"]) == (event, HEAD, [])


def test_build_review_requires_marker(mod):
    with pytest.raises(ValueError, match="Marker"):
        mod.build_review({"body": "nur Text"}, comment_only=True)


def test_build_review_caps_inline_and_keeps_marker_last(mod):
    comments = [_inline(n) for n in range(1, mod.MAX_INLINE + 3)]
    req = mod.build_review({"body": _marker("approved"), "comments": comments}, comment_only=True)
    assert len(req["comments"]) == mod.MAX_INLINE
    assert all(c["side"] == "RIGHT" for c in req["comments"])
    assert mod.OVERFLOW_HEADING in req["body"]
    assert f"`fabrikant/src/x.py:{mod.MAX_INLINE + 2}`" in req["body"]
    assert req["body"].rstrip().endswith(f"sha={HEAD} -->")


def test_build_review_without_inline_moves_all_to_body(mod):
    req = mod.build_review(
        {"body": _marker("approved"), "comments": [{**_inline(1), "side": "LEFT"}]}, comment_only=True, inline=False
    )
    assert req["comments"] == []
    assert "Nit 1" in req["body"]


DIFF = """diff --git a/fabrikant/src/x.py b/fabrikant/src/x.py
--- a/fabrikant/src/x.py
+++ b/fabrikant/src/x.py
@@ -1,3 +1,4 @@
 a
-b
+B
+C
 d
@@ -20,2 +21,2 @@ def f():
 x
+y
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-weg
"""


def test_diff_lines(mod):
    assert mod.diff_lines(DIFF) == {"fabrikant/src/x.py": [{1, 2, 3, 4}, {21, 22}], "": [set()]}


@pytest.mark.parametrize(
    ("comment", "ok"),
    [
        ({"path": "fabrikant/src/x.py", "line": 3}, True),
        ({"path": "fabrikant/src/x.py", "line": 22, "start_line": 21}, True),
        ({"path": "fabrikant/src/x.py", "line": 10}, False),  # zwischen den Hunks
        ({"path": "fabrikant/src/x.py", "line": 21, "start_line": 4}, False),  # über Hunk-Grenze
        ({"path": "other.py", "line": 1}, False),
        ({"path": "fabrikant/src/x.py"}, False),  # ohne Zeile
    ],
)
def test_in_diff(mod, comment, ok):
    assert mod.in_diff(comment, mod.diff_lines(DIFF)) is ok


def test_build_review_moves_lines_outside_diff_to_body(mod):
    comments = [_inline(2), _inline(10)]
    req = mod.build_review(
        {"body": _marker("approved"), "comments": comments}, comment_only=True, hunks=mod.diff_lines(DIFF)
    )
    assert [c["line"] for c in req["comments"]] == [2]
    assert "`fabrikant/src/x.py:10`" in req["body"] and "`fabrikant/src/x.py:2`" not in req["body"]


def test_insert_before_marker_without_marker_appends(mod):
    assert mod._insert_before_marker("text", "X") == "text\n\nX"


def test_already_reviewed(mod):
    reviews = [_review(_marker("approved", OLD)), _review(_marker("approved"), author="mallory")]
    assert not mod.already_reviewed(reviews, REVIEWER, HEAD)
    assert mod.already_reviewed([*reviews, _review(_marker("changes_requested"))], REVIEWER, HEAD)


def test_review_check_current_head(mod):
    pr = {"headRefOid": HEAD, "reviews": [_review(_marker("changes_requested"))]}
    assert mod.review_check(pr, REVIEWER)["reviewed"] is True
    assert mod.review_check(pr, REVIEWER, HEAD)["reviewed"] is True


def test_review_check_missing_review_fails(mod):
    # Markers of another author or for an old SHA do not count.
    pr = {
        "headRefOid": HEAD,
        "reviews": [_review(_marker("approved", OLD)), _review(_marker("approved"), author="mallory")],
    }
    res = mod.review_check(pr, REVIEWER)
    assert res["reviewed"] is False and "kein Review" in res["reason"]


def test_review_check_head_moved_is_ok(mod):
    pr = {"headRefOid": HEAD, "reviews": []}
    res = mod.review_check(pr, REVIEWER, OLD)
    assert res["reviewed"] is True and "weitergezogen" in res["reason"]


def test_cli_review_check_exit_code(mod, fake_gh, monkeypatch, capsys):
    responses, _ = fake_gh
    monkeypatch.setenv("FABRIKANT_REVIEWER_LOGIN", REVIEWER)
    responses["pr view"] = {"headRefOid": HEAD, "reviews": []}
    assert mod.main(["review-check", "7", "--repo", "o/r"]) == 1
    assert json.loads(capsys.readouterr().out)["reviewed"] is False
    responses["pr view"] = {"headRefOid": HEAD, "reviews": [_review(_marker("approved"))]}
    assert mod.main(["review-check", "7", "--repo", "o/r", "--sha", HEAD]) == 0


# ── Threads ──


def _thread(tid: str, resolved: bool, ids: list[int]) -> dict:
    nodes = [{"databaseId": i, "body": f"c{i}", "author": {"login": REVIEWER}} for i in ids]
    return {"id": tid, "isResolved": resolved, "path": "x.py", "line": 3, "comments": {"nodes": nodes}}


def test_open_threads_and_lookup(mod):
    threads = [_thread("T1", True, [1]), _thread("T2", False, [2, 3])]
    assert mod.open_threads(threads) == [
        {"id": "T2", "path": "x.py", "line": 3, "comment_id": 2, "author": REVIEWER, "body": "c2"}
    ]
    assert mod.thread_for_comment(threads, 3)["id"] == "T2"  # auch Antworten im Thread
    assert mod.thread_for_comment(threads, 9) is None


# ── new comments ──


def _rest(cid: int, body: str, minute: int, author: str = REVIEWER, **extra) -> dict:
    return {"id": cid, "user": {"login": author}, "body": body, "html_url": f"u{cid}", **extra} | {
        "created_at": f"2026-10-19T12:{minute:02d}:00Z",
        "submitted_at": f"2026-10-19T12:{minute:02d}:00Z",
    }


def test_collect_comments_all_sources(mod):
    issue = [_rest(1, "Frage", 3, author="human")]
    reviews = [_rest(10, _marker("approved"), 1), _rest(11, "", 2)]  # leerer Body → übersprungen
    inline = [_rest(20, "Nit", 1, path="x.py", line=None, original_line=7, in_reply_to_id=None)]
    items = mod.collect_comments(124, issue, reviews, inline)
    assert [(i["key"], i["kind"]) for i in items] == [
        ("124-review-10", "review"),
        ("124-inline-20", "inline"),
        ("124-1", "comment"),
    ]
    assert items[1] | {} == {
        "key": "124-inline-20",
        "kind": "inline",
        "created": "2026-10-19T12:01:00Z",
        "path": "x.py",
        "line": 7,  # Fallback original_line (Zeile im neuen Stand weggefallen)
        "in_reply_to": None,
        "id": 20,
        "author": REVIEWER,
        "body": "Nit",
        "url": "u20",
        "bot": False,
    }


def test_new_comments_skips_replied_and_own_replies(mod):
    items = mod.collect_comments(
        124,
        [_rest(1, "Review 1", 1), _rest(2, f"Erledigt\n{mod.REPLY_MARKER}", 2), _rest(3, "Frage", 3)],
        [],
        [_rest(4, f"Übernommen\n{mod.REPLY_MARKER}", 4, path="x.py", line=1, in_reply_to_id=9)],
    )
    assert [i["id"] for i in mod.new_comments(items, {"124-1": True})] == [3]


def test_new_comments_infers_replied_from_old_markers(mod):
    """Replies with the old marker form (without `re=`) still count: inline via `in_reply_to`,
    PR comment/review via the `Re: <url>` line."""
    review = {**_rest(1, "QA-Review", 1), "submitted_at": "2026-11-13T01:00:00Z", "html_url": "https://gh/r1"}
    items = mod.collect_comments(
        124,
        [_rest(2, f"Re: https://gh/r1\nAntwort\n{mod.REPLY_MARKER}", 2), _rest(3, "Frage", 3)],
        [review],
        [
            _rest(4, "Nit", 4, path="x.py", line=1),
            _rest(5, f"Ok\n{mod.REPLY_MARKER}", 5, path="x.py", line=1, in_reply_to_id=4),
        ],
    )
    assert [i["key"] for i in mod.new_comments(items)] == ["124-3"]


def test_new_comments_ignore_bots_keeps_reviewer_app(mod):
    bot = {"login": "github-actions[bot]", "type": "Bot"}
    qa = {"login": "fabrikant-reviewer[bot]", "type": "Bot"}
    items = mod.collect_comments(
        124,
        [{**_rest(1, "plan", 1), "user": bot}, {**_rest(2, "QA", 2), "user": qa}, _rest(3, "Mensch", 3)],
        [],
        [],
    )
    assert [i["key"] for i in mod.new_comments(items)] == ["124-1", "124-2", "124-3"]
    assert [i["key"] for i in mod.new_comments(items, keep_bots={"fabrikant-reviewer"})] == ["124-2", "124-3"]
    assert [i["key"] for i in mod.new_comments(items, keep_bots=set())] == ["124-3"]


def test_new_comments_skips_bot_receipts_but_keeps_inline_findings(mod):
    """Regression: Codex receipts (status comment, review text) were answered every round."""
    codex = {"login": "chatgpt-codex-connector[bot]", "type": "Bot"}
    summary = {**_rest(1, "<!-- codex-pull-request-review-summary -->\nstatus", 1), "user": codex}
    receipt = {**_rest(2, "<!-- codex-review-receipt:v1 -->\n### 💡 Codex Review", 2), "user": codex}
    finding = {**_rest(3, "<!-- codex-review-receipt:v1 --> P2: validate x", 3, path="a.py", line=1), "user": codex}
    human = _rest(4, "<!-- codex-review-receipt:v1 --> copied by a human", 4)
    items = mod.collect_comments(124, [summary, human], [{**receipt, "submitted_at": receipt["created_at"]}], [finding])
    keys = ["124-inline-3", "124-4"]
    assert [i["key"] for i in mod.new_comments(items, keep_bots={"chatgpt-codex-connector"})] == keys
    assert [i["key"] for i in mod.new_comments(items)] == keys


def test_new_comments_uses_reply_marker_keys_without_state(mod):
    """Stateless: `re=` keys in the reply marker mark comments as answered (throwaway worktree,
    sandbox) — one reply can carry several keys."""
    items = mod.collect_comments(
        124,
        [_rest(1, "Frage A", 1), _rest(2, "Frage B", 2), _rest(3, "Frage C", 3)],
        [],
        [_rest(4, "Nit", 4, path="x.py", line=1)],
    )
    items += mod.collect_comments(124, [_rest(5, "Antwort\n<!-- fabrikant-reply re=124-1,124-inline-4 -->", 5)], [], [])
    assert [i["key"] for i in mod.new_comments(items)] == ["124-2", "124-3"]
    assert [i["key"] for i in mod.new_comments(items, {"124-2": True})] == ["124-3"]  # old state counts too


@pytest.mark.parametrize("content", [None, "kaputt{", "[1, 2]"])
def test_load_replied_falls_back_to_empty(mod, tmp_path, content):
    p = tmp_path / "replied.json"
    if content is not None:
        p.write_text(content)
    assert mod.load_replied(p) == {}


def test_load_replied_reads_state(mod, tmp_path):
    p = tmp_path / "replied.json"
    p.write_text('{"124-1": true}')
    assert mod.load_replied(p) == {"124-1": True}


# ── gh-Anbindung (gemockt) ──


@pytest.fixture
def fake_gh(mod, monkeypatch):
    """_gh_text durch eine Tabelle ersetzen (erster passender Teilstring gewinnt).

    Strings gehen roh zurück (z. B. `pr diff`), alles andere als JSON (für _gh_json).
    Werte dürfen Callables sein (bekommen stdin) — für POST-Fehlerpfade."""
    calls: list[tuple[tuple[str, ...], str | None]] = []
    responses: dict[str, object] = {}

    def fake(*args: str, stdin: str | None = None):
        calls.append((args, stdin))
        for key, value in responses.items():
            if key in " ".join(args):
                out = value(stdin) if callable(value) else value
                return out if isinstance(out, str) else json.dumps(out)
        raise AssertionError(f"unerwarteter gh-Aufruf: {args}")

    monkeypatch.setattr(mod, "_gh_text", fake)
    return responses, calls


def _threads_response(threads: list[dict]) -> dict:
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}}


def test_fetch_paged(mod, fake_gh, monkeypatch):
    responses, calls = fake_gh
    monkeypatch.setattr(mod, "PAGE_SIZE", 2)
    responses["page=1"] = [{"id": 1}, {"id": 2}]
    responses["page=2"] = [{"id": 3}]
    assert [c["id"] for c in mod.fetch_paged("repos/o/r/issues/7/comments")] == [1, 2, 3]
    assert len(calls) == 2


def _setup_post(responses, reviews=(), author=REVIEWER, head=HEAD):
    responses["api user"] = {"login": REVIEWER}
    responses["pr view"] = {"author": {"login": author}, "reviews": list(reviews), "headRefOid": head}
    responses["pr diff"] = DIFF
    responses["closingIssuesReferences"] = _closing([])


def test_post_review_posts_once(mod, fake_gh):
    responses, calls = fake_gh
    _setup_post(responses, author="fabrikant-agent[bot]")
    responses["--method POST"] = {"html_url": "https://x/r1"}
    out = mod.post_review(REPO, 124, {"body": _marker("approved"), "comments": [_inline(1)]})
    assert out == {"posted": True, "url": "https://x/r1", "inline": 1}
    sent = json.loads(calls[-1][1])
    assert (sent["event"], sent["commit_id"], len(sent["comments"])) == ("APPROVE", HEAD, 1)


@pytest.mark.parametrize("verdict", ["approved", "changes_requested"])
def test_post_review_comment_only_never_approves(mod, fake_gh, verdict):
    """Fremde PR + comment_only: Verdikt bleibt Marker, GitHub-Event ist COMMENT."""
    responses, calls = fake_gh
    _setup_post(responses, author="colleague")
    responses["--method POST"] = {"html_url": "https://x/r1"}
    assert mod.post_review(REPO, 124, {"body": _marker(verdict)}, comment_only=True)["posted"] is True
    assert json.loads(calls[-1][1])["event"] == "COMMENT"


def test_post_review_is_idempotent_per_sha(mod, fake_gh):
    responses, calls = fake_gh
    _setup_post(responses, reviews=[_review(_marker("approved"))])
    out = mod.post_review(REPO, 124, {"body": _marker("approved")})
    assert out["posted"] is False
    assert not any("POST" in args for args, _ in calls)


def test_post_review_refuses_stale_sha(mod, fake_gh):
    """Payload für alten Commit: nicht posten — auch nicht, wenn es für den alten SHA
    schon ein Review gibt (das darf ein Review des aktuellen Heads nicht ersetzen)."""
    responses, calls = fake_gh
    _setup_post(responses, reviews=[_review(_marker("approved", OLD))])
    out = mod.post_review(REPO, 124, {"body": _marker("approved", OLD)})
    assert out["posted"] is False and out["stale"] is True
    assert not any("POST" in args for args, _ in calls)


def test_post_review_falls_back_without_inline_on_422(mod, fake_gh):
    responses, calls = fake_gh
    _setup_post(responses)

    def post(stdin):
        if json.loads(stdin)["comments"]:
            raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 422: Line could not be resolved")
        return {"html_url": "https://x/r2"}

    responses["--method POST"] = post
    out = mod.post_review(REPO, 124, {"body": _marker("approved"), "comments": [_inline(3)]})
    assert out["inline"] == 0 and "fallback" in out
    sent = json.loads(calls[-1][1])
    assert sent["event"] == "COMMENT" and "`fabrikant/src/x.py:3`" in sent["body"]


@pytest.mark.parametrize(
    ("stderr", "comments"),
    [("HTTP 500: boom", [_inline(1)]), ("HTTP 422: bad body", [])],  # kein Fallback möglich/sinnvoll
)
def test_post_review_reraises_other_errors(mod, fake_gh, stderr, comments):
    responses, _ = fake_gh
    _setup_post(responses)

    def post(_stdin):
        raise subprocess.CalledProcessError(1, "gh", stderr=stderr)

    responses["--method POST"] = post
    with pytest.raises(subprocess.CalledProcessError):
        mod.post_review(REPO, 124, {"body": _marker("approved"), "comments": comments})


def test_cli_merge_check(mod, fake_gh, capsys):
    responses, _ = fake_gh
    responses["graphql"] = _threads_response([_thread("T1", True, [1])])
    responses["pr view"] = _pr(reviews=[_review(_marker("approved"))])
    responses["api user"] = {"login": REVIEWER}
    assert mod.main(["merge-check", "124", "--repo", REPO]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "merge"


def test_cli_merge_check_waits_on_needs_approval_label(mod, fake_gh, capsys):
    """CLI fragt `labels` mit ab — sonst greift die ADR-0011-Sperre nie."""
    responses, calls = fake_gh
    responses["graphql"] = _threads_response([])
    responses["pr view"] = _pr(reviews=[_review(_marker("approved"))], labels=("fabrikant", "needs approval"))
    responses["api user"] = {"login": REVIEWER}
    assert mod.main(["merge-check", "124", "--repo", REPO]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "wait"
    (view,) = [args for args, _ in calls if args[:2] == ("pr", "view")]
    assert "labels" in view[view.index("--json") + 1].split(",")


def test_cli_new_comments(mod, fake_gh, capsys, tmp_path):
    responses, _ = fake_gh
    responses["issues/124/comments"] = [_rest(1, "Review", 1)]
    responses["pulls/124/reviews"] = []
    responses["pulls/124/comments"] = [_rest(2, "Nit", 2, path="x.py", line=1)]
    assert mod.main(["new-comments", "124", "--repo", REPO, "--state", str(tmp_path / "s.json")]) == 0
    assert [i["key"] for i in json.loads(capsys.readouterr().out)] == ["124-1", "124-inline-2"]
    assert mod.main(["new-comments", "124", "--repo", REPO]) == 0  # without state file
    assert len(json.loads(capsys.readouterr().out)) == 2
    responses["issues/124/comments"] = [{**_rest(1, "Review", 1), "user": {"login": "qa[bot]", "type": "Bot"}}]
    args = ["new-comments", "124", "--repo", REPO, "--ignore-bots"]
    assert mod.main(args) == 0  # no `gh api user` call (fake_gh would reject it)
    assert [i["key"] for i in json.loads(capsys.readouterr().out)] == ["124-inline-2"]
    assert mod.main([*args, "--keep-bot", "app/qa"]) == 0
    assert [i["key"] for i in json.loads(capsys.readouterr().out)] == ["124-1", "124-inline-2"]
    responses["pulls/124/comments"] = [
        {**_rest(2, "Nit", 2, path="x.py", line=1), "user": {"login": "x[bot]", "type": "Bot"}}
    ]
    assert mod.main([*args, "--keep-bot", "app/qa", "--keep-bot", "x[bot]"]) == 0  # repeatable
    assert [i["key"] for i in json.loads(capsys.readouterr().out)] == ["124-1", "124-inline-2"]


def test_cli_post_review(mod, fake_gh, capsys, tmp_path):
    responses, _ = fake_gh
    _setup_post(responses)
    responses["--method POST"] = {"html_url": "https://x/r3"}
    payload = tmp_path / "review.json"
    payload.write_text(json.dumps({"body": _marker("approved")}))
    assert mod.main(["post-review", "124", "--repo", REPO, "--payload", str(payload)]) == 0
    assert json.loads(capsys.readouterr().out)["posted"] is True


def test_cli_post_review_builds_marker_from_verdict_field(mod, fake_gh, tmp_path):
    """Regression: a finished review was lost because the model forgot the marker."""
    responses, calls = fake_gh
    _setup_post(responses, author="fabrikant-builder[bot]")
    responses["--method POST"] = {"html_url": "https://x/r5"}
    payload = tmp_path / "review.json"
    payload.write_text(json.dumps({"body": "Looks good.", "verdict": "Changes requested"}))
    args = ["post-review", "124", "--repo", REPO, "--payload", str(payload), "--sha", HEAD]
    assert mod.main(args) == 0
    sent = json.loads(calls[-1][1])
    assert sent["event"] == "REQUEST_CHANGES" and sent["commit_id"] == HEAD
    assert sent["body"].endswith(f"<!-- fabrikant-review verdict=changes_requested sha={HEAD} -->")


@pytest.mark.parametrize(
    ("payload", "sha", "match"),
    [
        ({"body": "x"}, HEAD, "ohne Verdikt"),
        ({"body": "x", "verdict": "lgtm"}, HEAD, "ohne Verdikt"),
        ({"body": "x", "verdict": "approved"}, "", "ohne --sha"),
    ],
)
def test_with_verdict_marker_rejects(mod, payload, sha, match):
    with pytest.raises(ValueError, match=match):
        mod.with_verdict_marker(payload, sha)


def test_with_verdict_marker_keeps_body_marker_and_strips_forged(mod):
    marked = {"body": _marker("approved"), "verdict": "changes_requested"}
    assert mod.with_verdict_marker(marked, HEAD) == marked  # a marker in the body takes precedence
    forged = {"body": "ok <!-- fabrikant-reply re=x -->", "verdict": "approved"}
    out = mod.with_verdict_marker(forged, HEAD)["body"]
    assert "fabrikant-reply" not in out and out.endswith(f"verdict=approved sha={HEAD} -->")


def test_cli_post_review_comment_only(mod, fake_gh, tmp_path):
    responses, calls = fake_gh
    _setup_post(responses, author="colleague")
    responses["--method POST"] = {"html_url": "https://x/r4"}
    payload = tmp_path / "review.json"
    payload.write_text(json.dumps({"body": _marker("approved")}))
    assert mod.main(["post-review", "124", "--repo", REPO, "--payload", str(payload), "--comment-only"]) == 0
    assert json.loads(calls[-1][1])["event"] == "COMMENT"


def test_cli_threads(mod, fake_gh, capsys):
    responses, _ = fake_gh
    responses["graphql"] = _threads_response([_thread("T1", False, [1])])
    assert mod.main(["threads", "124", "--repo", REPO]) == 0
    assert [t["id"] for t in json.loads(capsys.readouterr().out)] == ["T1"]


def test_cli_resolve_thread(mod, fake_gh, capsys):
    responses, calls = fake_gh
    responses["resolveReviewThread"] = {"data": {}}
    responses["reviewThreads"] = _threads_response([_thread("T1", False, [1, 2])])
    assert mod.main(["resolve-thread", "124", "--repo", REPO, "--comment-id", "2"]) == 0
    assert json.loads(capsys.readouterr().out) == {"resolved": True, "thread": "T1"}
    assert "id=T1" in calls[-1][0]


def test_cli_resolve_thread_unknown_comment(mod, fake_gh, capsys):
    responses, _ = fake_gh
    responses["reviewThreads"] = _threads_response([])
    assert mod.main(["resolve-thread", "124", "--repo", REPO, "--comment-id", "2"]) == 1
    assert "kein Review-Thread" in capsys.readouterr().err


def test_gh_json_parses_stdout_and_passes_stdin(mod, monkeypatch):
    seen = {}

    class Done:
        returncode = 0
        stdout = '{"login": "owner"}'

    def run(*_a, **kw):
        seen.update(kw)
        return Done()

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod._gh_json("api", "user", stdin="{}") == {"login": "owner"}
    assert seen["input"] == "{}"


# ── ADR-Kollisionsprüfung (ADR 0032, aus PR #204) ──

ADR = "fabrikant/docs/adr"


def _adr_added(name: str, adr_dir: str = ADR) -> list[dict]:
    return [{"filename": f"{adr_dir}/{name}", "status": "added"}]


def _adr_modified(name: str) -> list[dict]:
    return [{"filename": f"{ADR}/{name}", "status": "modified"}]


def _adr_renamed(old: str, new: str) -> list[dict]:
    return [{"filename": f"{ADR}/{new}", "status": "renamed", "previous_filename": f"{ADR}/{old}"}]


def _collision(mod, fake_gh, files: list[dict]):
    responses, _ = fake_gh
    responses["contents/fabrikant/docs/adr"] = [{"name": "0005-existing.md"}, {"name": "README.md"}]
    responses["pulls/7/files"] = files
    return mod.adr_collision(REPO, 7, {"baseRefName": "main"})


def test_adr_collision_ignores_modified_existing_adr(mod, fake_gh):
    assert _collision(mod, fake_gh, _adr_modified("0005-existing.md")) is None


def test_adr_collision_flags_new_adr_with_taken_number(mod, fake_gh):
    assert "0005" in _collision(mod, fake_gh, _adr_added("0005-other.md"))


def test_adr_collision_accepts_new_free_number(mod, fake_gh):
    assert _collision(mod, fake_gh, _adr_added("0006-new.md")) is None


def test_adr_collision_ignores_other_adr_dirs(mod, fake_gh):
    """Andere Projekte im Monorepo haben eigene Nummernkreise."""
    assert _collision(mod, fake_gh, _adr_added("0005-x.md", "my-app/docs/adr")) is None


def test_adr_collision_flags_duplicate_new_numbers(mod, fake_gh):
    diff = _adr_added("0006-a.md") + _adr_added("0006-b.md")
    assert "0006" in _collision(mod, fake_gh, diff)


def test_adr_collision_rename_same_number_ok(mod, fake_gh):
    assert _collision(mod, fake_gh, _adr_renamed("0005-existing.md", "0005-renamed.md")) is None


def test_adr_collision_rename_to_taken_number(mod, fake_gh):
    assert "0005" in _collision(mod, fake_gh, _adr_renamed("0004-old.md", "0005-x.md"))


def test_adr_collision_without_base_skips_lookup(mod, fake_gh):
    _, calls = fake_gh
    assert mod.adr_collision(REPO, 7, {}) is None
    assert calls == []


def test_cli_merge_check_waits_on_adr_collision(mod, fake_gh, capsys):
    """Kollision schlägt jede Freigabe: kein Merge, bevor die Nummer repariert ist."""
    responses, _ = fake_gh
    responses["contents/fabrikant/docs/adr"] = [{"name": "0005-existing.md"}]
    responses["pulls/124/files"] = _adr_added("0005-other.md")
    responses["pr view"] = {**_pr(reviews=[_review(_marker("approved"))]), "baseRefName": "main"}
    assert mod.main(["merge-check", "124", "--repo", REPO]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "wait" and "0005" in out["reason"]


def test_adr_collision_api_error_waits_instead_of_crashing(mod, fake_gh):
    """z. B. 404/5xx der Contents-API → merge-check bekommt eine wait-Begründung (fail closed)."""
    responses, _ = fake_gh

    def boom(stdin):
        raise subprocess.CalledProcessError(1, ["gh"])

    responses["contents/fabrikant/docs/adr"] = boom
    assert "fehlgeschlagen" in mod.adr_collision(REPO, 7, {"baseRefName": "main"})


def test_adr_collision_reads_all_file_pages(mod, fake_gh, monkeypatch):
    """Große PRs: die Dateiliste wird paginiert gelesen (kein `gh pr diff`)."""
    responses, _ = fake_gh
    monkeypatch.setattr(mod, "PAGE_SIZE", 1)
    responses["contents/fabrikant/docs/adr"] = [{"name": "0005-existing.md"}]
    responses["files?per_page=1&page=1"] = _adr_modified("0001-a.md")
    responses["files?per_page=1&page=2"] = _adr_added("0005-late.md")
    responses["files?per_page=1&page=3"] = []
    assert "0005" in mod.adr_collision(REPO, 7, {"baseRefName": "main"})


@pytest.mark.parametrize("login", ["my-reviewer-app", "app/my-reviewer-app", "my-reviewer-app[bot]"])
def test_normalize_login_unifies_bot_forms(mod, login):
    assert mod.normalize_login(login) == "my-reviewer-app"


def test_reviewer_login_env_override_skips_api_user(mod, fake_gh, monkeypatch):
    """App-Tokens dürfen /user nicht abfragen → Override, kein `gh api user`."""
    _, calls = fake_gh
    monkeypatch.setenv(mod.REVIEWER_LOGIN_ENV, "my-reviewer-app[bot]")
    assert mod.reviewer_login() == "my-reviewer-app"
    assert calls == []


def test_post_review_idempotent_for_bot_reviewer(mod, fake_gh, monkeypatch):
    """gh-JSON liefert Bot-Reviews als `app/<name>` — muss als eigenes Review zählen."""
    responses, calls = fake_gh
    monkeypatch.setenv(mod.REVIEWER_LOGIN_ENV, "qa[bot]")
    responses["pr view"] = {
        "author": {"login": "app/builder"},
        "reviews": [_review(_marker("approved"), author="app/qa")],
        "headRefOid": HEAD,
    }
    responses["pr diff"] = DIFF
    responses["closingIssuesReferences"] = _closing([])
    assert mod.post_review(REPO, 124, {"body": _marker("approved")})["posted"] is False
    assert not any("POST" in args for args, _ in calls)


# ── sync-base ──


def _rest_pr(mergeable: bool | None = True, state: str = "clean", labels: tuple[str, ...] = ()) -> dict:
    return {
        "head": {"sha": HEAD},
        "base": {"ref": "main"},
        "mergeable": mergeable,
        "mergeable_state": state,
        "labels": [{"name": n} for n in labels],
    }


@pytest.mark.parametrize(
    ("mergeable", "state", "behind", "action"),
    [
        (True, "clean", 0, "up_to_date"),
        (True, "behind", 3, "update"),
        (False, "dirty", 3, "conflict"),
        (True, "dirty", 0, "conflict"),
        (None, "unknown", 2, "pending"),
    ],
)
def test_sync_decision(mod, mergeable, state, behind, action):
    assert mod.sync_decision(_rest_pr(mergeable, state), behind)["action"] == action


def _calls_with(calls, needle: str) -> list[tuple[str, ...]]:
    return [args for args, _ in calls if needle in " ".join(args)]


def test_sync_base_updates_behind_branch_and_drops_stale_label(mod, fake_gh):
    responses, calls = fake_gh
    responses["update-branch"] = ""
    responses["-X DELETE"] = ""
    responses["compare/main..."] = {"behind_by": 2}
    responses["repos/o/r/pulls/7"] = _rest_pr(state="behind", labels=(mod.CONFLICT_LABEL,))
    out = mod.sync_base(REPO, 7)
    assert out == {"action": "updated", "reason": "2 Commit(s) hinter der Base", "head": HEAD}
    (put,) = _calls_with(calls, "update-branch")
    assert f"expected_head_sha={HEAD}" in put
    assert _calls_with(calls, f"labels/{mod.CONFLICT_LABEL}")


def test_sync_base_up_to_date_touches_nothing(mod, fake_gh):
    responses, calls = fake_gh
    responses["compare/main..."] = {"behind_by": 0}
    responses["repos/o/r/pulls/7"] = _rest_pr()
    assert mod.sync_base(REPO, 7)["action"] == "up_to_date"
    assert not _calls_with(calls, "-X")


def test_sync_base_conflict_sets_label_once(mod, fake_gh):
    responses, calls = fake_gh
    responses["compare/main..."] = {"behind_by": 1}
    responses["-X POST"] = ""
    responses["repos/o/r/pulls/7"] = _rest_pr(False, "dirty")
    assert mod.sync_base(REPO, 7)["action"] == "conflict"
    (post,) = _calls_with(calls, "-X POST")
    assert f"labels[]={mod.CONFLICT_LABEL}" in post
    assert not _calls_with(calls, "update-branch")
    calls.clear()
    responses["repos/o/r/pulls/7"] = _rest_pr(False, "dirty", labels=(mod.CONFLICT_LABEL,))
    mod.sync_base(REPO, 7)
    assert not _calls_with(calls, "-X POST")  # label already there


def test_sync_base_pending_leaves_labels_alone(mod, fake_gh):
    responses, calls = fake_gh
    responses["compare/main..."] = {"behind_by": 1}
    responses["repos/o/r/pulls/7"] = _rest_pr(None, "unknown", labels=(mod.CONFLICT_LABEL,))
    assert mod.sync_base(REPO, 7)["action"] == "pending"
    assert not _calls_with(calls, "-X")


def _http_422(message: str):
    def raise_(stdin):
        raise subprocess.CalledProcessError(1, ["gh"], stderr=f"gh: {message} (HTTP 422)")

    return raise_


@pytest.mark.parametrize(
    ("message", "action", "labelled"),
    [("merge conflict between base and head", "conflict", True), ("expected head sha didn't match", "pending", False)],
)
def test_sync_base_update_branch_422(mod, fake_gh, message, action, labelled):
    responses, calls = fake_gh
    responses["update-branch"] = _http_422(message)
    responses["-X POST"] = ""
    responses["compare/main..."] = {"behind_by": 1}
    responses["repos/o/r/pulls/7"] = _rest_pr(state="behind")
    assert mod.sync_base(REPO, 7)["action"] == action
    assert bool(_calls_with(calls, "-X POST")) is labelled


def test_sync_base_other_http_errors_propagate(mod, fake_gh):
    responses, _ = fake_gh
    responses["update-branch"] = lambda stdin: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, ["gh"], stderr="gh: Forbidden (HTTP 403)")
    )
    responses["compare/main..."] = {"behind_by": 1}
    responses["repos/o/r/pulls/7"] = _rest_pr(state="behind")
    with pytest.raises(subprocess.CalledProcessError):
        mod.sync_base(REPO, 7)


def test_cli_sync_base(mod, fake_gh, capsys):
    responses, _ = fake_gh
    responses["compare/main..."] = {"behind_by": 0}
    responses["repos/o/r/pulls/7"] = _rest_pr()
    assert mod.main(["sync-base", "7", "--repo", REPO]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "up_to_date"


# ── PR reply: reply-context / post-replies ──

_PR = {"number": 7, "headRefName": "fabrikant/auto/1", "headRefOid": "a" * 40, "baseRefName": "main"}


def _comment(key: str, kind: str = "comment", cid: int = 1, **extra) -> dict:
    return {"key": key, "kind": kind, "id": cid, "author": "qa", "body": "b", "url": f"https://gh/{cid}", **extra}


def test_reply_context_skips_foreign_branch_and_empty(mod):
    opts = {"head_prefix": "fabrikant/auto/", "max_comments": 5, "max_rounds": 3}
    assert (
        mod.reply_context({**_PR, "headRefName": "fabrikant/relay/x"}, [_comment("7-1")], [], **opts)[0]["action"]
        == "skip"
    )
    assert mod.reply_context(_PR, [], [], **opts)[0] == {"action": "skip", "reason": "keine neuen Kommentare"}


def test_reply_context_caps_comments_and_rounds(mod):
    fixes = [{"sha": f"{i}abcdef", "message": "fabrikant: address review comments (PR #7)"} for i in range(3)]
    ctx, picked = mod.reply_context(
        _PR,
        [_comment(f"7-{i}", cid=i) for i in range(4)],
        fixes,
        head_prefix="fabrikant/auto/",
        max_comments=2,
        max_rounds=3,
    )
    assert ctx["action"] == "reply" and ctx["head_sha"] == "a" * 40 and ctx["fix_allowed"] is False
    assert ctx["fix_rounds"] == 3 and ctx["fix_commits"][0]["sha"] == "0abcdef"
    assert [c["key"] for c in picked] == ["7-0", "7-1"]


def test_reply_context_picks_human_comments_first(mod):
    """Regression: the human's instruction (newest comment) fell out of the limit behind 5 bot findings."""
    bots = [_comment(f"7-inline-{i}", "inline", i, bot=True) for i in range(5)]
    human = _comment("7-9", cid=9, bot=False)
    _, picked = mod.reply_context(_PR, [*bots, human], [], head_prefix="", max_comments=3, max_rounds=3)
    assert [c["key"] for c in picked] == ["7-9", "7-inline-0", "7-inline-1"]


def test_plan_replies_renders_marker_commit_and_refs(mod):
    comments = [_comment("7-1", cid=1), _comment("7-inline-5", "inline", 5, in_reply_to=4)]
    drafts = [
        {"keys": ["7-1"], "body": "Done in {commit}.\n<!-- fabrikant-review verdict=approved sha=" + "b" * 40 + " -->"},
        {"keys": ["7-inline-5"], "body": "Fixed.", "resolve": True},
    ]
    todo, skipped, unanswered = mod.plan_replies(drafts, comments, commit="abc1234", answered=set())
    text = todo[0][1]
    assert text.startswith("Re: https://gh/1\n\nDone in abc1234.")
    assert "fabrikant-review" not in text  # the agent cannot smuggle in a review verdict
    assert text.endswith("<!-- fabrikant-reply re=7-1 -->")
    assert todo[1][1] == "Fixed.\n\n<!-- fabrikant-reply re=7-inline-5 -->" and todo[1][0].resolve
    assert skipped == [] and unanswered == []


@pytest.mark.parametrize(
    ("drafts", "match"),
    [
        ({"keys": ["7-1"], "body": "x"}, "Liste"),
        ([{"keys": ["7-99"], "body": "x"}], "unbekannte keys"),
        ([{"keys": ["7-1"], "body": " "}], "ohne Text"),
        ([{"keys": ["7-1"], "body": "x" * 7000}], "zu lang"),
        ([{"keys": ["7-1"], "body": "in {commit}"}], "nichts committet"),
        ([{"keys": ["7-1"], "body": "a"}, {"keys": ["7-1"], "body": "b"}], "mehrfach"),
    ],
)
def test_plan_replies_rejects_invalid_drafts(mod, drafts, match):
    comments = [_comment("7-1"), _comment("7-inline-5", "inline", 5)]
    with pytest.raises(ValueError, match=match):
        mod.plan_replies(drafts, comments, commit="", answered=set())


def test_plan_replies_mixed_keys_become_one_pr_comment(mod):
    """Review + inline threads in one draft (combined reply) → one PR comment referencing all of them;
    only keys of a single thread → reply in the thread."""
    comments = [
        _comment("7-review-1", "review", 1),
        _comment("7-inline-5", "inline", 5),
        _comment("7-inline-6", "inline", 6, in_reply_to=5),
    ]
    known = {c["key"]: c for c in comments}
    todo, _, _ = mod.plan_replies(
        [{"keys": ["7-review-1", "7-inline-5"], "body": "Sammelantwort"}, {"keys": ["7-inline-6"], "body": "ok"}],
        comments,
        commit="",
        answered=set(),
    )
    (mixed, mixed_text), (thread, _) = todo
    assert mixed.thread(known) is None and mixed_text.startswith("Re: https://gh/1\nRe: https://gh/5\n\n")
    assert thread.thread(known) == 5


def test_plan_replies_skips_answered_and_reports_unanswered(mod):
    comments = [_comment("7-1", cid=1), _comment("7-2", cid=2), _comment("7-3", cid=3)]
    todo, skipped, unanswered = mod.plan_replies(
        [{"keys": ["7-1"], "body": "a"}, {"keys": ["7-2"], "body": "b"}], comments, commit="", answered={"7-1"}
    )
    assert [d.keys for d, _ in todo] == [("7-2",)] and skipped == ["7-1"] and unanswered == ["7-3"]


def _closing(numbers: list[int]) -> dict:
    nodes = [{"number": n} for n in numbers]
    return {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": nodes}}}}}


def test_cli_reply_context_and_post_replies(mod, fake_gh, capsys, tmp_path):
    responses, calls = fake_gh
    responses["pr view"] = _PR
    responses["issues/7/comments?"] = [_rest(1, "Please change X", 1, author="human")]
    responses["pulls/7/reviews?"] = []
    responses["pulls/7/comments?"] = [_rest(5, "Nit", 2, path="x.py", line=3)]
    responses["pulls/7/commits?"] = [
        {"sha": "f" * 40, "commit": {"message": "fabrikant: address review comments (PR #7)"}}
    ]
    responses["closingIssuesReferences"] = _closing([3])
    responses["labels=fabrikant-followup"] = []
    out = tmp_path / "reply"
    assert mod.main(["reply-context", "7", "--repo", REPO, "--out", str(out), "--head-prefix", "fabrikant/auto/"]) == 0
    assert json.loads(capsys.readouterr().out)["fix_rounds"] == 1
    assert [c["key"] for c in json.loads((out / "comments.json").read_text())] == ["7-1", "7-inline-5"]

    (out / "replies.json").write_text(
        json.dumps([{"keys": ["7-1"], "body": "Done ({commit})."}, {"keys": ["7-inline-5"], "body": "Ok."}])
    )
    responses["--method POST"] = {"html_url": "https://gh/new"}
    assert mod.main(["post-replies", "7", "--repo", REPO, "--dir", str(out), "--commit", "abc1234"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [p["keys"] for p in result["posted"]] == [["7-1"], ["7-inline-5"]]
    posts = [(a, json.loads(stdin)) for a, stdin in calls if "--method" in a]
    assert posts[0][0][3] == f"repos/{REPO}/issues/7/comments" and "Done (abc1234)." in posts[0][1]["body"]
    assert posts[1][0][3] == f"repos/{REPO}/pulls/7/comments/5/replies"

    (out / "replies.json").write_text(json.dumps([{"keys": ["7-1"], "body": "Done ({commit})."}]))
    assert mod.main(["post-replies", "7", "--repo", REPO, "--dir", str(out)]) == 1  # no commit → post nothing


def test_post_replies_retries_resolution_without_second_reply(mod, fake_gh, capsys, tmp_path):
    """Regression: reply posted, resolution failed → the next run skipped the draft and left the thread open."""
    responses, calls = fake_gh
    (tmp_path / "comments.json").write_text(json.dumps([_comment("7-inline-5", "inline", 5)]))
    (tmp_path / "replies.json").write_text(json.dumps([{"keys": ["7-inline-5"], "body": "Fixed.", "resolve": True}]))
    responses["issues/7/comments?"] = []
    responses["pulls/7/reviews?"] = []
    responses["pulls/7/comments?"] = [
        _rest(5, "Nit", 2, path="x.py", line=3),
        _rest(6, "Fixed.\n\n<!-- fabrikant-reply re=7-inline-5 -->", 3, path="x.py", line=3, in_reply_to_id=5),
    ]
    responses["resolveReviewThread"] = {"data": {}}
    responses["reviewThreads"] = _threads_response([_thread("T1", False, [5, 6])])
    args = ["post-replies", "7", "--repo", REPO, "--dir", str(tmp_path), "--commit", "abc1234"]
    assert mod.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["posted"] == [] and result["skipped"] == ["7-inline-5"]
    assert not any("--method" in a for a, _ in calls)  # no second reply
    assert sum("resolveReviewThread" in " ".join(a) for a, _ in calls) == 1

    calls.clear()  # already resolved → nothing to do
    responses["reviewThreads"] = _threads_response([_thread("T1", True, [5, 6])])
    assert mod.main(args) == 0
    assert not any("resolveReviewThread" in " ".join(a) for a, _ in calls)


# ── Eskalation an einen Menschen (ADR 0059) ──

C1, C2, C3 = "c" * 40, "d" * 40, "e" * 40


def _commit(sha: str, *, merge: bool = False, message: str = "feat: x", date: str = "") -> dict:
    """`GET pulls/{n}/commits`-Eintrag; Merge-Commits (Base-Sync) haben zwei Parents."""
    return {
        "sha": sha,
        "parents": [{"sha": "p1"}, {"sha": "p2"}] if merge else [{"sha": "p1"}],
        "commit": {"message": message, "committer": {"date": date}},
    }


def _cr(sha: str) -> dict:
    return _review(_marker("changes_requested", sha))


def test_merge_decision_human_label_blocks_merge(mod):
    pr = _pr(reviews=[_review(_marker("approved"))], labels=("needs human",))
    result = mod.merge_decision(pr, REVIEWER, 0)
    assert result["action"] == "wait" and "ADR 0059" in result["reason"]
    assert mod.merge_decision(pr, REVIEWER, 0, human_label="")["action"] == "merge"  # abschaltbar


def test_reply_context_skips_escalated_pr(mod):
    pr = {**_PR, "labels": [{"name": "needs human"}]}
    ctx, picked = mod.reply_context(pr, [_comment("7-1")], [], head_prefix="", max_comments=5, max_rounds=3)
    assert ctx["action"] == "skip" and "needs human" in ctx["reason"] and picked == []


def test_cli_reply_context_escalated_loads_no_comments(mod, fake_gh, capsys, tmp_path):
    responses, calls = fake_gh
    responses["pr view"] = {**_PR, "labels": [{"name": "needs human"}]}
    assert mod.main(["reply-context", "7", "--repo", REPO, "--out", str(tmp_path / "r")]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "skip"
    assert len(calls) == 1 and not (tmp_path / "r").exists()


def test_plan_replies_escalation_marker_and_validation(mod):
    comments = [_comment("7-1")]
    todo, _, _ = mod.plan_replies(
        [{"keys": ["7-1"], "body": "Needs an upstream change.", "escalate": "blocked"}],
        comments,
        commit="",
        answered=set(),
    )
    assert todo[0][1].endswith("<!-- fabrikant-reply re=7-1 -->\n<!-- fabrikant-escalate kind=blocked re=7-1 -->")
    with pytest.raises(ValueError, match="escalate 'stalled' nicht erlaubt"):
        mod.plan_replies([{"keys": ["7-1"], "body": "x", "escalate": "stalled"}], comments, commit="", answered=set())
    # agent text cannot smuggle in an escalation marker (only the host writes it)
    todo, _, _ = mod.plan_replies(
        [{"keys": ["7-1"], "body": "x <!-- fabrikant-escalate kind=scope -->"}], comments, commit="", answered=set()
    )
    assert "fabrikant-escalate" not in todo[0][1]


def test_post_replies_escalation_sets_label(mod, fake_gh, capsys, tmp_path):
    responses, calls = fake_gh
    (tmp_path / "comments.json").write_text(json.dumps([_comment("7-1")]))
    (tmp_path / "replies.json").write_text(
        json.dumps([{"keys": ["7-1"], "body": "Cannot be solved from here.", "escalate": "blocked"}])
    )
    responses["issues/7/comments?"] = []
    responses["pulls/7/reviews?"] = []
    responses["pulls/7/comments?"] = []
    responses["--method POST"] = {"html_url": "https://gh/new"}
    responses["issues/7/labels"] = ""
    assert mod.main(["post-replies", "7", "--repo", REPO, "--dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["escalated"] == "blocked"
    assert any("labels[]=needs human" in a for a, _ in calls)


@pytest.mark.parametrize(
    ("reviews", "commits", "kind"),
    [
        # #1282: zwei changes_requested, dazwischen nur ein Base-Sync (Merge-Commit) → stalled
        ([_cr(C1), _cr(C2)], [_commit(C1), _commit(C2, merge=True)], "stalled"),
        # derselbe SHA zweimal reviewt → stalled
        ([_cr(C1), _cr(C1)], [_commit(C1)], "stalled"),
        # Builder hat dazwischen committet → kein Stillstand
        ([_cr(C1), _cr(C2)], [_commit(C1), _commit(C2)], None),
        # Builder hat NACH dem letzten changes_requested gepusht (Review ausstehend) → kein Stillstand
        ([_cr(C1), _cr(C2)], [_commit(C1), _commit(C2, merge=True), _commit(C3)], None),
        # Force-Push (alter SHA unbekannt) → nie stalled
        ([_cr(C1), _cr(C2)], [_commit(C2, merge=True)], None),
        # nur eine Runde → zu früh
        ([_cr(C1)], [_commit(C1)], None),
        # letzte Runde approved → nichts zu tun
        ([_cr(C1), _review(_marker("approved", C2))], [_commit(C1), _commit(C2, merge=True)], None),
        # Marker eines anderen Autors zählen nicht
        (
            [_cr(C1), _review(_marker("changes_requested", C2), author="mallory")],
            [_commit(C1), _commit(C2, merge=True)],
            None,
        ),
    ],
)
def test_escalation_decision_stalled(mod, reviews, commits, kind):
    result = mod.escalation_decision(reviews, commits, reviewer=REVIEWER, head=commits[-1]["sha"])
    assert result.get("kind") == kind
    assert result["action"] == ("escalate" if kind else "continue")


def test_escalation_decision_stall_rounds_and_exhausted(mod):
    fix = "fabrikant: address review comments (PR #7)"
    commits = [_commit(C1), _commit(C2, message=fix), _commit(C3, message=fix)]
    reviews = [_cr(C1), _cr(C2), _cr(C3)]
    assert mod.escalation_decision(reviews, commits, reviewer=REVIEWER, head=C3)["action"] == "continue"
    exhausted = mod.escalation_decision(reviews, commits, reviewer=REVIEWER, head=C3, max_rounds=2)
    assert exhausted["kind"] == "exhausted" and "2 Fix-Runden" in exhausted["reason"]
    # Verdikt für alten SHA: neuer Head ist noch nicht reviewt → nicht exhausted
    assert (
        mod.escalation_decision(reviews[:2], commits, reviewer=REVIEWER, head=C3, max_rounds=2)["action"] == "continue"
    )
    # stall_rounds=3: drei Runden ohne Builder-Commit nötig
    merges = [_commit(C1), _commit(C2, merge=True), _commit(C3, merge=True)]
    assert (
        mod.escalation_decision(reviews[:2], merges, reviewer=REVIEWER, head=C2, stall_rounds=3)["action"] == "continue"
    )
    assert mod.escalation_decision(reviews, merges, reviewer=REVIEWER, head=C3, stall_rounds=3)["kind"] == "stalled"


FIX_MSG = "fabrikant: address review comments (PR #7)"
ESCALATED_AT = "2026-11-17T14:00:00Z"


def _escalation(created: str = ESCALATED_AT) -> dict:
    return {"id": 90, "created_at": created, "body": "x\n<!-- fabrikant-escalate kind=exhausted -->"}


def test_new_comments_skips_escalation_notice_of_kept_reviewer_app(mod):
    qa = {"login": "fabrikant-reviewer[bot]", "type": "Bot"}
    items = mod.collect_comments(124, [{**_escalation(), "user": qa, "html_url": "u90"}], [], [])
    assert mod.new_comments(items, keep_bots={"fabrikant-reviewer"}) == []


def test_budget_start_is_last_escalation_comment(mod):
    later = {**_escalation("2026-11-17T15:00:00Z"), "id": 91}
    plain = {"id": 92, "created_at": "2026-11-17T16:00:00Z", "body": "no marker"}
    assert mod.budget_start([_escalation(), later, plain]) == "2026-11-17T15:00:00Z"
    assert mod.budget_start([plain]) == ""


def test_escalation_decision_counts_only_since_continue(mod):
    """Regression: 3 fix rounds before the escalation must not re-escalate right after resuming."""
    old = [_commit(s, message=FIX_MSG, date="2026-11-17T12:00:00Z") for s in (C1, C2)]
    new = _commit(C3, message=FIX_MSG, date="2026-11-17T14:30:00Z")
    old_reviews = [
        {**_cr(C1), "submittedAt": "2026-11-17T12:10:00Z"},
        {**_cr(C2), "submittedAt": "2026-11-17T12:20:00Z"},
    ]
    kw = {"reviewer": REVIEWER, "max_rounds": 2}
    assert mod.escalation_decision(old_reviews, old, head=C2, **kw)["kind"] == "exhausted"  # without budget start
    assert mod.escalation_decision(old_reviews, old, head=C2, since=ESCALATED_AT, **kw)["action"] == "continue"
    reviews = [*old_reviews, {**_cr(C3), "submitted_at": "2026-11-17T14:40:00Z"}]
    decision = mod.escalation_decision(reviews, [*old, new], head=C3, since=ESCALATED_AT, **kw)
    assert decision["action"] == "continue" and "1 Fix-Runde" in decision["reason"]


def test_write_reply_context_resets_fix_budget_after_escalation(mod, fake_gh, tmp_path):
    responses, _ = fake_gh
    responses["pr view"] = {**_PR, "labels": []}
    responses["issues/7/comments?"] = [
        _escalation(),
        _rest(5, "please do X", 1) | {"created_at": "2026-11-17T14:05:00Z"},
    ]
    responses["pulls/7/reviews?"] = []
    responses["pulls/7/comments?"] = []
    responses["pulls/7/commits?"] = [
        _commit(C1, message=FIX_MSG, date="2026-11-17T12:00:00Z"),
        _commit(C2, message=FIX_MSG, date="2026-11-17T14:30:00Z"),
    ]
    responses["closingIssuesReferences"] = _closing([])
    responses["labels=fabrikant-followup"] = []
    opts = {"keep_bots": set(), "head_prefix": "", "max_comments": 5, "max_rounds": 3}
    ctx = mod.write_reply_context(REPO, 7, tmp_path, **opts)
    assert ctx["action"] == "reply" and ctx["fix_rounds"] == 1 and ctx["fix_allowed"] is True
    assert [c["sha"] for c in ctx["fix_commits"]] == [C2[:7]]


def test_cli_escalation_check_escalates_once(mod, fake_gh, capsys, monkeypatch):
    responses, calls = fake_gh
    monkeypatch.setenv(mod.REVIEWER_LOGIN_ENV, REVIEWER)
    responses["pr view"] = {"headRefOid": C2, "reviews": [_cr(C1), _cr(C2)], "labels": []}
    responses["pulls/7/commits?"] = [_commit(C1), _commit(C2, merge=True)]
    responses["issues/7/comments?"] = []
    responses["repos/o/r/issues/7/comments"] = {"html_url": "https://gh/esc"}
    responses["repos/o/r/issues/7/labels"] = ""
    responses["repos/o/r/issues/7"] = {"labels": []}
    assert mod.main(["escalation-check", "7", "--repo", REPO, "--escalate"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "stalled" and result["escalated"] is True and result["url"] == "https://gh/esc"
    body = next(json.loads(stdin)["body"] for a, stdin in calls if stdin)
    assert (
        body.endswith("<!-- fabrikant-escalate kind=stalled -->")
        and "Runden changes_requested" in body
        and "an einen Menschen eskaliert" in body
    )

    calls.clear()
    responses["pr view"] = {"headRefOid": C2, "reviews": [_cr(C1), _cr(C2)], "labels": [{"name": "needs human"}]}
    assert mod.main(["escalation-check", "7", "--repo", REPO, "--escalate"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "continue" and len(calls) == 1


def test_escalate_is_idempotent_and_validates_kind(mod, fake_gh):
    responses, calls = fake_gh
    responses["repos/o/r/issues/7"] = {"labels": [{"name": "needs human"}]}
    assert mod.escalate(REPO, 7, "blocked", "x")["escalated"] is False and len(calls) == 1
    with pytest.raises(ValueError, match="unbekannte Eskalationsart"):
        mod.escalate(REPO, 7, "panic", "x")


def test_escalate_builder_rejects_non_builder_kind(mod, fake_gh):
    """ADR 0059 §3: Builder darf nur blocked/scope eskalieren, nicht stalled/exhausted/access."""
    responses, _ = fake_gh
    responses["repos/o/r/issues/7"] = {"labels": []}
    for kind in ("stalled", "exhausted", "access"):
        with pytest.raises(ValueError, match="unbekannte Eskalationsart"):
            mod.escalate(REPO, 7, kind, "x", builder=True)


@pytest.mark.parametrize("kind", ["blocked", "scope"])
def test_escalate_builder_allows_builder_kinds(mod, fake_gh, kind):
    responses, _ = fake_gh
    responses["repos/o/r/issues/7"] = {"labels": []}
    responses["repos/o/r/issues/7/comments"] = {"html_url": "https://gh/esc"}
    responses["repos/o/r/issues/7/labels"] = ""
    assert mod.escalate(REPO, 7, kind, "x", builder=True)["escalated"] is True


@pytest.mark.parametrize("kind", ["blocked", "stalled", "exhausted", "scope", "access"])
def test_escalate_host_allows_all_kinds(mod, fake_gh, kind):
    """Ohne --builder (Host) sind alle Eskalationsarten erlaubt."""
    responses, _ = fake_gh
    responses["repos/o/r/issues/7"] = {"labels": []}
    responses["repos/o/r/issues/7/comments"] = {"html_url": "https://gh/esc"}
    responses["repos/o/r/issues/7/labels"] = ""
    assert mod.escalate(REPO, 7, kind, "x")["escalated"] is True


# ── Issue references, candidate list, follow-ups ──


def _issue(n: int, title: str = "T", state: str = "open", reason: str | None = None, **extra) -> dict:
    return {
        "number": n,
        "title": title,
        "state": state,
        "state_reason": reason,
        "html_url": f"https://gh/i/{n}",
        **extra,
    }


def _ctx(mod, *issues: dict, closing=(), followups=None):
    return mod.RefContext(
        pr=7, issues={i["number"]: i for i in issues}, closing=frozenset(closing), followups=followups or {}
    )


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("x", "keine Liste"),
        ([42], "Issue-Nummer"),  # seen in a model payload: bare number instead of an object
        ([{"issue": "1", "relation": "covered_by"}], "Issue-Nummer"),
        ([{"issue": True, "relation": "covered_by"}], "Issue-Nummer"),
        ([{"issue": 1, "relation": "related"}], "nicht erlaubt"),
        ([{"issue": 1, "relation": "blocked_by", "why": "x" * 201}], "why"),
    ],
)
def test_issue_ref_parse_drops_invalid_with_warning(mod, warnings_seen, raw, match):
    assert mod.IssueRef.parse_all(raw, "E") == ()
    (warning,) = warnings_seen()
    assert match in warning and warning.startswith("E: ")


def test_issue_ref_parse_keeps_valid_entries_and_caps(mod, warnings_seen):
    good = {"issue": 1, "relation": "covered_by"}
    refs = mod.IssueRef.parse_all([7, good, {**good, "issue": 2}, {**good, "issue": 3}, {**good, "issue": 4}], "E")
    assert [r.issue for r in refs] == [1, 2, 3]
    assert [w.split(":")[0] for w in warnings_seen()] == ["E", "E"]  # invalid + beyond MAX_REFS


def test_issue_ref_parse_strips_markers(mod):
    (ref,) = mod.IssueRef.parse_all(
        [{"issue": 5, "relation": "covered_by", "why": "x <!-- fabrikant-reply re=a -->"}], "E"
    )
    assert ref == mod.IssueRef(5, "covered_by", "x")
    assert mod.IssueRef.parse_all(None, "E") == ()


@pytest.mark.parametrize(
    ("ref", "issues", "closing", "match"),
    [
        ((9, "covered_by"), [], (), "existiert nicht"),
        ((9, "covered_by"), [_issue(9, pull_request={})], (), "ist ein PR"),
        ((9, "blocked_by"), [_issue(9)], (9,), "eigene Issue"),
        ((9, "covered_by"), [_issue(9, state="closed", reason="not_planned")], (), "taugt nicht"),
        ((9, "blocked_by"), [_issue(9, state="closed", reason="completed")], (), "kein Blocker"),
    ],
)
def test_ref_context_valid_drops_with_warning(mod, warnings_seen, ref, issues, closing, match):
    ctx = _ctx(mod, *issues, closing=closing)
    assert match in ctx.problem(mod.IssueRef(*ref))
    assert ctx.valid((mod.IssueRef(*ref),), "E") == ()
    (warning,) = warnings_seen()
    assert match in warning and "verworfen" in warning


def test_ref_context_valid_accepts_and_renders(mod, warnings_seen):
    ctx = _ctx(mod, _issue(9, "Stale @team #12 draft"), _issue(10, state="closed", reason="completed"))
    refs = (mod.IssueRef(9, "covered_by", "same cause"), mod.IssueRef(10, "duplicate_of"))
    assert ctx.valid(refs, "E") == refs and not warnings_seen()
    text = ctx.render(refs, proposed=True)
    assert "→ covered by #9 “Stale @​team `#12` draft”: same cause — proposed, reviewer to confirm" in text
    assert "→ duplicate of #10 “T” — proposed" in text
    assert "proposed" not in ctx.render(refs, proposed=False)


def test_warn_once_per_message(mod, warnings_seen):
    mod.warn("a  b\n c")
    mod.warn("a b c")
    assert warnings_seen() == ["a b c"]


def test_defuse_issue_refs(mod):
    text = "See #12 and #7, step #3, a&#39;b, x/#5\n```\nloop #4\n```\n`#8` stays"
    out = mod.defuse_issue_refs(text, {7})
    assert out.startswith("See `#12` and #7, step `#3`, a&#39;b, x/#5")
    assert "loop #4" in out and "`#8` stays" in out


def test_follow_up_parse(mod):
    fu = mod.FollowUp.parse({"title": "  Add   tests ", "body": "Do X <!-- fabrikant-followup pr=1 re=a -->"}, "E")
    assert fu == mod.FollowUp("Add tests", "Do X")
    assert mod.FollowUp.parse(None, "E") is None
    with pytest.raises(ValueError, match="title"):
        mod.FollowUp.parse({"title": "x" * 121, "body": "b"}, "E")
    with pytest.raises(ValueError, match="body"):
        mod.FollowUp.parse({"title": "t", "body": " "}, "E")


def test_plan_replies_refs_and_follow_up(mod):
    comments = [_comment("7-1", cid=1), _comment("7-inline-5", "inline", 5)]
    drafts = [
        {
            "keys": ["7-1"],
            "body": "Covered elsewhere, not #99.",
            "refs": [{"issue": 9, "relation": "covered_by"}],
            "resolve": True,
        },
        {"keys": ["7-inline-5"], "body": "Deferred.", "follow_up": {"title": "Later", "body": "B"}, "resolve": True},
    ]
    ctx = _ctx(mod, _issue(9, "Other"), followups={"7-inline-5": 41})
    todo, _, _ = mod.plan_replies(drafts, comments, commit="", answered=set(), refs=ctx, followups_left=0)
    (d1, t1), (d2, t2) = todo
    assert not d1.resolve and not d2.resolve  # deferred ≠ done
    assert "not `#99`." in t1 and "→ covered by #9 “Other”" in t1
    assert "→ follow-up #41" in t2  # already exists → does not count against the limit
    with pytest.raises(ValueError, match="erlaubt noch 0"):
        mod.plan_replies(drafts, comments, commit="", answered=set(), refs=_ctx(mod, _issue(9)), followups_left=0)


def test_plan_replies_drops_invalid_ref_and_keeps_thread_open(mod, warnings_seen):
    comments = [_comment("7-1", cid=1), _comment("7-2", cid=2)]
    drafts = [
        {"keys": ["7-1"], "body": "Done.", "refs": [{"issue": 9, "relation": "covered_by"}], "resolve": True},
        {"keys": ["7-2"], "body": "Fixed.", "refs": [9], "resolve": True},
    ]
    (d1, t1), (d2, t2) = mod.plan_replies(drafts, comments, commit="", answered=set(), refs=_ctx(mod))[0]
    assert d1.refs == () and "→" not in t1 and "Done." in t1  # #9 does not exist → dropped, the reply stays
    assert not d1.resolve  # covered_by stays a deferral
    assert d2.refs == () and not d2.resolve  # a dropped reference might have been a deferral
    assert sorted(w.split(":")[0] for w in warnings_seen()) == ["Entwurf für ['7-1']", "Entwurf für ['7-2']"]


def test_reply_draft_resolves_without_refs(mod):
    known = {"7-1": _comment("7-1")}
    assert mod.ReplyDraft.parse({"keys": ["7-1"], "body": "Fixed.", "resolve": True}, known).resolve
    assert mod.ReplyDraft.parse({"keys": ["7-1"], "body": "Fixed.", "resolve": True, "refs": []}, known).resolve


def test_post_replies_creates_follow_up_once(mod, fake_gh, capsys, tmp_path):
    responses, calls = fake_gh
    (tmp_path / "comments.json").write_text(json.dumps([_comment("7-1", cid=1)]))
    (tmp_path / "context.json").write_text(json.dumps({"closing_issues": [3]}))
    (tmp_path / "replies.json").write_text(
        json.dumps([{"keys": ["7-1"], "body": "Later.", "follow_up": {"title": "Do Y", "body": "Because"}}])
    )
    responses["labels=fabrikant-followup"] = []
    responses["issues/7/comments?"] = []
    responses["pulls/7/reviews?"] = []
    responses["pulls/7/comments?"] = []
    responses["repos/o/r/issues --input"] = {"number": 55}
    responses["issues/7/comments --input"] = {"html_url": "https://gh/c"}
    assert mod.main(["post-replies", "7", "--repo", REPO, "--dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["followups"] == {"7-1": 55}
    created = next(json.loads(stdin) for a, stdin in calls if "repos/o/r/issues" in a and stdin and "title" in stdin)
    assert created["labels"] == ["fabrikant-followup"] and created["body"].endswith(
        "<!-- fabrikant-followup pr=7 re=7-1 -->"
    )
    reply = next(json.loads(stdin)["body"] for a, stdin in calls if "repos/o/r/issues/7/comments" in a and stdin)
    assert "→ follow-up #55" in reply

    # second run: follow-up exists (marker) → no new issue
    calls.clear()
    responses["labels=fabrikant-followup"] = [{"number": 55, "body": "x <!-- fabrikant-followup pr=7 re=7-1 -->"}]
    assert mod.main(["post-replies", "7", "--repo", REPO, "--dir", str(tmp_path)]) == 0
    assert "followups" not in json.loads(capsys.readouterr().out)
    assert not any("title" in (stdin or "") for _, stdin in calls)


def test_with_issue_refs_for_review(mod):
    payload = {
        "body": "Dup of #12, see #9.\n" + _marker("approved"),
        "comments": [{"path": "a", "line": 1, "body": "#13?"}],
        "refs": [{"issue": 9, "relation": "duplicate_of", "why": "same"}],
    }
    out = mod.with_issue_refs(payload, _ctx(mod, _issue(9, "Nine")), mod.IssueRef.parse_all(payload["refs"], "R"))
    assert "Dup of `#12`, see #9." in out["body"] and out["comments"][0]["body"] == "`#13`?"
    assert out["body"].index("### Referenced issues") < out["body"].index("<!-- fabrikant-review")
    assert "→ duplicate of #9 “Nine”: same" in out["body"] and "proposed" not in out["body"]


def test_post_review_drops_invalid_refs_and_still_posts(mod, fake_gh, warnings_seen):
    """Regression: one invalid optional reference sank the whole review."""
    responses, calls = fake_gh
    _setup_post(responses, author="fabrikant-builder[bot]")
    responses["closingIssuesReferences"] = _closing([42])
    responses["repos/o/r/issues/42"] = _issue(42)
    responses["--method POST"] = {"html_url": "https://x/r2"}
    payload = {"body": "See #42.\n" + _marker("approved"), "refs": [42, {"issue": 42, "relation": "covered_by"}]}
    assert mod.post_review(REPO, 124, payload)["posted"] is True
    sent = json.loads(calls[-1][1])
    assert "Referenced issues" not in sent["body"] and "See #42." in sent["body"]  # own issue stays linked
    warnings = warnings_seen()
    assert len(warnings) == 2 and "Issue-Nummer" in warnings[0] and "eigene Issue" in warnings[1]


def test_issue_candidates_filters_and_shapes(mod):
    items = [
        _issue(3, "Open", body="a\n b", labels=[{"name": "bug"}]),
        _issue(4, state="closed", reason="completed", closed_at="2026-10-01T00:00:00Z"),
        _issue(5, state="closed", closed_at="2026-01-01T00:00:00Z"),
        _issue(6, pull_request={}),
    ]
    out = mod.issue_candidates(items, closed_since="2026-08-01T00:00:00Z", excerpt=2)
    assert [c["number"] for c in out] == [3, 4]
    assert out[0] | {} == {
        "id": "3",
        "number": 3,
        "url": "https://gh/i/3",
        "title": "Open",
        "state": "open",
        "state_reason": "",
        "labels": ["bug"],
        "excerpt": "a ",
    }


def test_cli_issue_candidates(mod, fake_gh, capsys, tmp_path):
    responses, _ = fake_gh
    responses["state=open"] = [_issue(1)]
    responses["state=closed"] = [_issue(1), _issue(2, state="closed", closed_at="2999-01-01T00:00:00Z")]
    out = tmp_path / "c" / "cand.json"
    assert mod.main(["issue-candidates", "7", "--repo", REPO, "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["issues"] == 2
    assert [c["number"] for c in json.loads(out.read_text())] == [1, 2]


# ── gh calls: network blips ──

GH_DNS = "error connecting to api.github.com\ndial tcp: lookup api.github.com: no such host"
GH_READ_TIMEOUT = 'Post "https://api.github.com/x": read tcp 1.2.3.4: i/o timeout'


@pytest.fixture
def gh_runs(mod, monkeypatch):
    """Replace subprocess.run for gh with a sequence of (returncode, stderr); no real waiting."""
    monkeypatch.setattr(mod, "GH_RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(mod, "_warned", set())
    results: list[tuple[int, str]] = []
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        code, err = results.pop(0)
        return subprocess.CompletedProcess(cmd, code, "{}" if code == 0 else "", err)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return results, calls


@pytest.mark.parametrize(
    ("args", "write"),
    [
        (("api", "repos/o/r/pulls/1"), False),
        (("api", "graphql", "-F", "num=1", "-f", "query=query { x }"), False),
        (("api", "graphql", "-f", "query=mutation { y }"), True),
        (("api", "--method", "POST", "repos/o/r/issues/1/comments", "--input", "-"), True),
        (("api", "-X", "GET", "repos/o/r/issues", "-f", "state=all"), False),
        (("api", "repos/o/r/issues/1/labels", "-f", "labels[]=x"), True),  # fields without a method = POST
        (("pr", "view", "1"), False),
        (("pr", "diff", "1"), False),
        (("pr", "comment", "1"), True),
    ],
)
def test_gh_is_write(mod, args, write):
    assert mod._gh_is_write(args) is write


def test_gh_read_retries_network_errors(mod, gh_runs, capsys):
    results, calls = gh_runs
    results += [(1, GH_DNS), (1, GH_READ_TIMEOUT), (0, "")]
    assert mod._gh_json("api", "repos/o/r/pulls/1") == {}
    assert len(calls) == 3 and capsys.readouterr().err.count(mod.WARNING_MARKER) == 2


def test_gh_write_retries_only_without_connection(mod, gh_runs):
    results, calls = gh_runs
    post = ("api", "--method", "POST", "repos/o/r/issues/1/comments", "--input", "-")
    results += [(1, GH_DNS), (0, "")]
    assert mod._gh_json(*post, stdin="{}") == {}
    results += [(1, GH_READ_TIMEOUT)]  # request may have arrived → do not post twice
    with pytest.raises(mod.GhError) as e:
        mod._gh_json(*post, stdin="{}")
    assert len(calls) == 3 and "i/o timeout" in str(e.value) and e.value.transient


def test_gh_error_is_called_process_error_with_stderr(mod, gh_runs):
    results, _ = gh_runs
    results += [(1, "HTTP 422: Validation Failed")]
    with pytest.raises(mod.GhError) as e:
        mod._gh_text("api", "repos/o/r/x")
    assert "HTTP 422" in str(e.value) and not e.value.transient
    assert isinstance(e.value, subprocess.CalledProcessError)  # existing except clauses still apply


def test_entry_maps_lasting_network_failure_to_tempfail(mod, monkeypatch):
    def boom():
        raise mod.GhError(1, ["gh", "api", "x"], "", GH_DNS)

    monkeypatch.setattr(mod, "main", boom)
    assert mod._entry() == mod.EX_TEMPFAIL

    def bad():
        raise mod.GhError(1, ["gh", "api", "x"], "", "HTTP 404: Not Found")

    monkeypatch.setattr(mod, "main", bad)
    with pytest.raises(mod.GhError):
        mod._entry()
