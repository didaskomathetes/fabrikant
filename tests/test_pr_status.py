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
    """Lokales fabrik.env (von config.py geladen) darf den Reviewer-Login der Tests nicht überschreiben."""
    monkeypatch.delenv(mod.REVIEWER_LOGIN_ENV, raising=False)


def _marker(verdict: str, sha: str = HEAD) -> str:
    return f"Summary …\n<!-- fabrik-review verdict={verdict} sha={sha} -->"


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
    return {"path": "fabrik/src/x.py", "line": n, "body": f"Nit {n}"}


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
        (("fabrik", "needs approval"), "wait"),  # ADR-PR: nur der approval-Loop mergt (ADR 0011)
        (("needs approval",), "wait"),
        (("fabrik",), "merge"),
        (("fabrik", "needs approval-ish"), "merge"),  # exakter Label-Name, kein Teilstring
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
    assert mod.review_verdict([_review("<!-- fabrik-review verdict=approved sha=abc -->")], REVIEWER) is None


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
    assert f"`fabrik/src/x.py:{mod.MAX_INLINE + 2}`" in req["body"]
    assert req["body"].rstrip().endswith(f"sha={HEAD} -->")


def test_build_review_without_inline_moves_all_to_body(mod):
    req = mod.build_review(
        {"body": _marker("approved"), "comments": [{**_inline(1), "side": "LEFT"}]}, comment_only=True, inline=False
    )
    assert req["comments"] == []
    assert "Nit 1" in req["body"]


DIFF = """diff --git a/fabrik/src/x.py b/fabrik/src/x.py
--- a/fabrik/src/x.py
+++ b/fabrik/src/x.py
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
    assert mod.diff_lines(DIFF) == {"fabrik/src/x.py": [{1, 2, 3, 4}, {21, 22}], "": [set()]}


@pytest.mark.parametrize(
    ("comment", "ok"),
    [
        ({"path": "fabrik/src/x.py", "line": 3}, True),
        ({"path": "fabrik/src/x.py", "line": 22, "start_line": 21}, True),
        ({"path": "fabrik/src/x.py", "line": 10}, False),  # zwischen den Hunks
        ({"path": "fabrik/src/x.py", "line": 21, "start_line": 4}, False),  # über Hunk-Grenze
        ({"path": "other.py", "line": 1}, False),
        ({"path": "fabrik/src/x.py"}, False),  # ohne Zeile
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
    assert "`fabrik/src/x.py:10`" in req["body"] and "`fabrik/src/x.py:2`" not in req["body"]


def test_insert_before_marker_without_marker_appends(mod):
    assert mod._insert_before_marker("text", "X") == "text\n\nX"


def test_already_reviewed(mod):
    reviews = [_review(_marker("approved", OLD)), _review(_marker("approved"), author="mallory")]
    assert not mod.already_reviewed(reviews, REVIEWER, HEAD)
    assert mod.already_reviewed([*reviews, _review(_marker("changes_requested"))], REVIEWER, HEAD)


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
    }


def test_new_comments_skips_replied_and_own_replies(mod):
    items = mod.collect_comments(
        124,
        [_rest(1, "Review 1", 1), _rest(2, f"Erledigt\n{mod.REPLY_MARKER}", 2), _rest(3, "Frage", 3)],
        [],
        [_rest(4, f"Übernommen\n{mod.REPLY_MARKER}", 4, path="x.py", line=1, in_reply_to_id=9)],
    )
    assert [i["id"] for i in mod.new_comments(items, {"124-1": True})] == [3]


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


def test_post_review_posts_once(mod, fake_gh):
    responses, calls = fake_gh
    _setup_post(responses, author="agent-host[bot]")
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
    assert sent["event"] == "COMMENT" and "`fabrik/src/x.py:3`" in sent["body"]


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
    responses["pr view"] = _pr(reviews=[_review(_marker("approved"))], labels=("fabrik", "needs approval"))
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


def test_cli_post_review(mod, fake_gh, capsys, tmp_path):
    responses, _ = fake_gh
    _setup_post(responses)
    responses["--method POST"] = {"html_url": "https://x/r3"}
    payload = tmp_path / "review.json"
    payload.write_text(json.dumps({"body": _marker("approved")}))
    assert mod.main(["post-review", "124", "--repo", REPO, "--payload", str(payload)]) == 0
    assert json.loads(capsys.readouterr().out)["posted"] is True


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
        stdout = '{"login": "owner"}'

    def run(*_a, **kw):
        seen.update(kw)
        return Done()

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod._gh_json("api", "user", stdin="{}") == {"login": "owner"}
    assert seen["input"] == "{}"


# ── ADR-Kollisionsprüfung (ADR 0032, aus PR #204) ──

ADR = "fabrik/docs/adr"


def _adr_added(name: str, adr_dir: str = ADR) -> list[dict]:
    return [{"filename": f"{adr_dir}/{name}", "status": "added"}]


def _adr_modified(name: str) -> list[dict]:
    return [{"filename": f"{ADR}/{name}", "status": "modified"}]


def _adr_renamed(old: str, new: str) -> list[dict]:
    return [{"filename": f"{ADR}/{new}", "status": "renamed", "previous_filename": f"{ADR}/{old}"}]


def _collision(mod, fake_gh, files: list[dict]):
    responses, _ = fake_gh
    responses["contents/fabrik/docs/adr"] = [{"name": "0005-existing.md"}, {"name": "README.md"}]
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
    assert _collision(mod, fake_gh, _adr_added("0005-x.md", "echochamber/docs/adr")) is None


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
    responses["contents/fabrik/docs/adr"] = [{"name": "0005-existing.md"}]
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

    responses["contents/fabrik/docs/adr"] = boom
    assert "fehlgeschlagen" in mod.adr_collision(REPO, 7, {"baseRefName": "main"})


def test_adr_collision_reads_all_file_pages(mod, fake_gh, monkeypatch):
    """Große PRs: die Dateiliste wird paginiert gelesen (kein `gh pr diff`)."""
    responses, _ = fake_gh
    monkeypatch.setattr(mod, "PAGE_SIZE", 1)
    responses["contents/fabrik/docs/adr"] = [{"name": "0005-existing.md"}]
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
    assert mod.post_review(REPO, 124, {"body": _marker("approved")})["posted"] is False
    assert not any("POST" in args for args, _ in calls)
