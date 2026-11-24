"""RelayGitHub: GraphQL → Task, Handoff-Datei, Status-Kommentar (nur vom Bot), Labels."""

import base64
import urllib.error

import pytest
from helpers import http_error

from fabrikant.failures import ConfigError, classify_exception
from fabrikant.relay import github as relay_github
from fabrikant.relay.github import GitHubError, RelayGitHub, task_from_node
from fabrikant.relay.status import STATUS_MARKER
from fabrikant.runmodel import FailureKind

NODE = {
    "number": 7,
    "title": "Import",
    "body": "## Goal\r\nx",
    "createdAt": "2026-10-01T00:00:00Z",
    "author": {"login": "colleague"},
    "labels": {"nodes": [{"name": "fabrikant-relay"}, {"name": "relay: blocked"}]},
    "userContentEdits": {
        "nodes": [
            {"editedAt": "2026-10-03T00:00:00Z", "editor": {"login": "me"}},
            {"editedAt": "2026-10-02T00:00:00Z", "editor": {"login": "colleague"}},
        ]
    },
    "timelineItems": {
        "nodes": [
            {"createdAt": "2026-10-04T00:00:00Z", "label": {"name": "fabrikant-relay"}, "actor": {"login": "me"}},
            {"createdAt": "2026-10-05T00:00:00Z", "label": {"name": "other"}, "actor": {"login": "mallory"}},
            {},
        ]
    },
}


def test_task_from_node_picks_latest_label_event_and_edit():
    t = task_from_node(NODE, "fabrikant-relay")
    assert (t.number, t.body, t.author) == (7, "## Goal\nx", "colleague")
    assert t.labels == frozenset({"fabrikant-relay", "relay: blocked"})
    assert (t.label_actor, t.label_at.day) == ("me", 4)
    assert (t.body_editor, t.body_edited_at.day) == ("me", 3)


def test_task_from_node_without_events_or_edits():
    t = task_from_node({"number": 1, "author": None}, "fabrikant-relay")
    assert (t.author, t.label_actor, t.body_editor, t.labels) == ("ghost", None, None, frozenset())


def test_task_query_fetches_newest_body_edits():
    # `userContentEdits` ist chronologisch aufsteigend sortiert: `first:` würde die
    # ältesten Edits liefern. Bei >20 Edits wäre `body_editor` dann veraltet und
    # `Task.authorization_error` ließe eine fremde Body-Fassung zur Ausführung zu.
    assert "userContentEdits(last: 20)" in relay_github._TASK_FIELDS


def test_task_from_node_picks_newest_edit_beyond_window():
    # >20 Edits: der neueste Editor gewinnt, auch wenn ältere Edits im Fenster liegen.
    edits = [{"editedAt": f"2026-10-{d:02d}T00:00:00Z", "editor": {"login": "mallory"}} for d in range(1, 21)]
    edits.append({"editedAt": "2026-10-21T00:00:00Z", "editor": {"login": "me"}})
    t = task_from_node(
        {"number": 9, "author": {"login": "me"}, "userContentEdits": {"nodes": edits}}, "fabrikant-relay"
    )
    assert (t.body_editor, t.body_edited_at.day) == ("me", 21)


class Api:
    """Fake für gitops._gh_api: (method, url-Suffix) → Antwort oder Exception."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, token, payload=None, accept=None):
        self.calls.append((method, url, payload))
        for (m, suffix), resp in self.responses.items():
            if m == method and url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp(payload) if callable(resp) else resp
        raise AssertionError(f"unerwarteter Call {method} {url}")


@pytest.fixture
def api(monkeypatch):
    def install(responses, bot_login="my-builder-app"):
        fake = Api(responses)
        monkeypatch.setattr(relay_github.gitops, "_gh_api", fake)
        monkeypatch.setattr(relay_github.gitops, "_gh_token", lambda role="dev": "tok")
        return fake, RelayGitHub("o/r", bot_login=bot_login)

    return install


def test_constructor_requires_bot_login():
    with pytest.raises(ValueError, match="bot_login"):
        RelayGitHub("o/r", bot_login="")


def test_tasks_and_task_via_graphql(api):
    fake, gh = api(
        {
            ("POST", "/graphql"): lambda p: {
                "data": {
                    "repository": {
                        "issues": {"nodes": [NODE, None], "pageInfo": {"hasNextPage": False, "endCursor": None}},
                        "issue": NODE if p["variables"].get("number") == 7 else None,
                    }
                }
            }
        }
    )
    assert [t.number for t in gh.tasks("fabrikant-relay")] == [7]
    assert gh.task(7, "fabrikant-relay").number == 7
    with pytest.raises(GitHubError, match="#8"):
        gh.task(8, "fabrikant-relay")
    assert fake.calls[0][2]["variables"] == {"owner": "o", "name": "r", "label": "fabrikant-relay", "after": None}


def test_tasks_paginates_beyond_first_page(api):
    def page(numbers, has_next, cursor):
        return {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [dict(NODE, number=n) for n in numbers],
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    }
                }
            }
        }

    seen: list[str | None] = []

    def graphql(p):
        seen.append(p["variables"]["after"])
        if p["variables"]["after"] is None:
            return page(range(1, 51), True, "cursor-1")  # 50 Issues = volle erste Seite
        return page(range(51, 56), False, "cursor-2")

    _, gh = api({("POST", "/graphql"): graphql})
    numbers = [t.number for t in gh.tasks("fabrikant-relay")]
    assert len(numbers) == 55 and numbers[:3] == [1, 2, 3] and numbers[-1] == 55
    assert seen == [None, "cursor-1"]


def test_graphql_errors_raise(api):
    _, gh = api({("POST", "/graphql"): {"errors": [{"message": "bad"}]}})
    with pytest.raises(GitHubError, match="bad"):
        gh.tasks("x")


def test_file_decodes_content_and_maps_404_to_none(api):
    content = base64.b64encode(b"---\nissue: 7\n---\n").decode()
    fake, gh = api({("GET", "?ref=fabrikant%2Frelay%2F7"): {"content": content}})
    assert gh.file(".fabrikant/relay/7/HANDOFF.md", "fabrikant/relay/7").startswith("---")
    _, gh404 = api({("GET", "?ref=b"): http_error(404)})
    assert gh404.file("p", "b") is None
    _, gh500 = api({("GET", "?ref=b"): http_error(500)})
    with pytest.raises(urllib.error.HTTPError):
        gh500.file("p", "b")


def comment(cid, login, body, typ="Bot"):
    return {"id": cid, "user": {"login": login, "type": typ}, "body": body}


def test_status_comment_only_trusts_the_pinned_bot(api):
    forged = comment(1, "mallory", f"{STATUS_MARKER}\nforged", typ="User")
    real = comment(2, "my-builder-app[bot]", f'{STATUS_MARKER}\n<!-- fabrikant-relay-state {{"audit": "pass"}} -->')
    _, gh = api({("GET", "&page=1"): [forged, real]})
    assert gh.status_comment(7)[0] == 2
    assert gh.status_state(7).audit == "pass"
    other_bot = comment(3, "dependabot[bot]", f"{STATUS_MARKER}\nx")
    _, pinned = api({("GET", "&page=1"): [other_bot, real]}, bot_login="my-builder-app")
    assert pinned.status_comment(7)[0] == 2


def test_status_comment_ignores_foreign_bot(api):
    # Fremder Bot mit Kommentarrecht: sein Status-Kommentar wird nicht vertraut,
    # der echte (spätere) Kommentar des gepinnten Bots gewinnt.
    foreign = comment(1, "some-other-app[bot]", f'{STATUS_MARKER}\n<!-- fabrikant-relay-state {{"audit": "pass"}} -->')
    real = comment(2, "my-builder-app[bot]", f"{STATUS_MARKER}\nplain")
    _, gh = api({("GET", "&page=1"): [foreign, real]})
    assert gh.status_comment(7)[0] == 2
    assert gh.status_state(7).audit == ""
    # Nur fremde Bots → kein Status (kein Default-Vertrauen auf `type == "Bot"`).
    _, only_foreign = api({("GET", "&page=1"): [foreign]})
    assert only_foreign.status_comment(7) is None


def test_status_comment_paginates_and_upsert_creates_or_edits(api):
    page1 = [comment(i, "u", "x", typ="User") for i in range(relay_github.COMMENTS_PER_PAGE)]
    created = comment(9, "my-builder-app[bot]", f"{STATUS_MARKER}\nnew")
    fake, gh = api({("GET", "&page=1"): page1, ("GET", "&page=2"): [], ("POST", "/issues/7/comments"): created})
    assert gh.status_comment(7) is None
    assert gh.status_state(7).audit == ""
    gh.upsert_status(7, f"{STATUS_MARKER}\nnew")
    assert fake.calls[-1][0] == "POST"

    existing = comment(5, "my-builder-app[bot]", f"{STATUS_MARKER}\nold")
    fake, gh = api({("GET", "&page=1"): [existing], ("PATCH", "/issues/comments/5"): {}})
    gh.upsert_status(7, f"{STATUS_MARKER}\nold")  # unverändert → kein PATCH
    assert [c[0] for c in fake.calls] == ["GET"]
    gh.upsert_status(7, f"{STATUS_MARKER}\nnew")
    assert fake.calls[-1][:2] == ("PATCH", "https://api.github.com/repos/o/r/issues/comments/5")


def test_upsert_rejects_token_that_does_not_post_as_bot_login(api):
    # Falsches bot_login: der neue Kommentar würde nie wiedergefunden → jeder Tick ein neuer.
    created = comment(9, "someone-else[bot]", f"{STATUS_MARKER}\nnew")
    fake, gh = api(
        {("GET", "&page=1"): [], ("POST", "/issues/7/comments"): created, ("DELETE", "/issues/comments/9"): {}}
    )
    with pytest.raises(ConfigError, match="someone-else"):
        gh.upsert_status(7, f"{STATUS_MARKER}\nnew")
    assert fake.calls[-1][:2] == ("DELETE", "https://api.github.com/repos/o/r/issues/comments/9")
    assert classify_exception(ConfigError("x")).kind is FailureKind.ENVIRONMENT


def test_sync_labels_adds_and_removes_only_differences(api):
    fake, gh = api(
        {
            ("POST", "/issues/7/labels"): {},
            ("DELETE", "/labels/relay%3A%20done"): http_error(404),
            ("DELETE", "/labels/relay%3A%20blocked"): {},
        }
    )
    gh.sync_labels(
        7,
        frozenset({"fabrikant-relay", "relay: done", "relay: blocked"}),
        {"relay: audited"},
        {"relay: done", "relay: blocked", "relay: missing"},
    )
    assert fake.calls[0] == ("POST", "https://api.github.com/repos/o/r/issues/7/labels", {"labels": ["relay: audited"]})
    assert len(fake.calls) == 3
    fake2, gh2 = api({("DELETE", "/labels/x"): http_error(500)})
    with pytest.raises(urllib.error.HTTPError):
        gh2.sync_labels(7, frozenset({"x"}), set(), {"x"})


def test_commit_identity_uses_bot_user_id(api):
    fake, gh = api({("GET", "/users/my-builder-app%5Bbot%5D"): {"id": 123}}, bot_login="app/my-builder-app")
    assert gh.commit_identity() == (
        "my-builder-app[bot]",
        "123+my-builder-app[bot]@users.noreply.github.com",
    )


def test_create_ref_posts_hidden_ref(api):
    fake, gh = api({("POST", "/git/refs"): {}})
    gh.create_ref("refs/fabrikant/relay/7/audit-1", "abc")
    assert fake.calls == [
        ("POST", "https://api.github.com/repos/o/r/git/refs", {"ref": "refs/fabrikant/relay/7/audit-1", "sha": "abc"})
    ]


def test_ref_sha_exact_match_prefix_list_and_missing(api):
    ref = "refs/fabrikant/relay/7/audit-1"
    _, gh = api({("GET", "/git/ref/fabrikant/relay/7/audit-1"): {"ref": ref, "object": {"sha": "abc"}}})
    assert gh.ref_sha(ref) == "abc"
    _, gh = api({("GET", "/git/ref/fabrikant/relay/7/audit-1"): [{"ref": ref + "0", "object": {"sha": "x"}}]})
    assert gh.ref_sha(ref) is None  # Präfix-Treffer zählt nicht
    _, gh = api({("GET", "/git/ref/fabrikant/relay/7/audit-1"): http_error(404)})
    assert gh.ref_sha(ref) is None
    _, gh = api({("GET", "/git/ref/fabrikant/relay/7/audit-1"): http_error(500)})
    with pytest.raises(urllib.error.HTTPError):
        gh.ref_sha(ref)


def test_open_pr_filters_by_head_branch(api):
    fake, gh = api({("GET", "per_page=1"): [{"number": 42}]})
    assert gh.open_pr("fabrikant/relay/7") == 42
    assert "head=o%3Afabrikant%2Frelay%2F7" in fake.calls[0][1] and "state=open" in fake.calls[0][1]
    _, gh = api({("GET", "per_page=1"): []})
    assert gh.open_pr("fabrikant/relay/7") is None


def test_pr_body_reads_and_patches_the_pull(api):
    fake, gh = api({("GET", "/pulls/42"): {"body": "Text"}, ("PATCH", "/pulls/42"): {}})
    assert gh.pr_body(42) == "Text"
    gh.set_pr_body(42, "Neu")
    assert fake.calls[-1] == ("PATCH", fake.calls[-1][1], {"body": "Neu"})
    _, gh = api({("GET", "/pulls/42"): {"body": None}})
    assert gh.pr_body(42) == ""  # PR ohne Body
