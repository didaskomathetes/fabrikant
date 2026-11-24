"""GitHub access for relay (builder identity): tasks, handoff on the branch, status comment, labels."""

from __future__ import annotations

import base64
import urllib.error
import urllib.parse
from typing import Any

from .. import gitops
from ..failures import ConfigError
from ..i18n import t
from .model import StatusState, Task, normalize_login, parse_time
from .status import STATUS_MARKER, parse_state

API = "https://api.github.com"
COMMENTS_PER_PAGE = 100

_TASK_FIELDS = """
  number title body createdAt author { login }
  labels(first: 50) { nodes { name } }
  userContentEdits(last: 20) { nodes { editedAt editor { login } } }
  timelineItems(itemTypes: [LABELED_EVENT], last: 50) {
    nodes { ... on LabeledEvent { createdAt label { name } actor { login } } }
  }
"""
_TASKS_QUERY = (
    "query($owner: String!, $name: String!, $label: String!, $after: String) { repository(owner: $owner, name: $name) {"
    " issues(labels: [$label], states: OPEN, first: 50, after: $after) { nodes {" + _TASK_FIELDS + "} "
    " pageInfo { hasNextPage endCursor } } } }"
)
_TASK_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) {"
    " issue(number: $number) {" + _TASK_FIELDS + "} } }"
)


class GitHubError(RuntimeError):
    """GitHub-API-Antwort ohne verwertbares Ergebnis (GraphQL-errors, fehlendes Issue)."""


def _login(node: Any) -> str | None:
    return (node or {}).get("login") if isinstance(node, dict) else None


def task_from_node(node: dict, label: str) -> Task:
    """GraphQL-Issue-Node → Task (letztes Labeling von `label`, letzte Body-Bearbeitung)."""
    labeled = [
        n
        for n in (node.get("timelineItems") or {}).get("nodes") or []
        if n and (n.get("label") or {}).get("name") == label
    ]
    last_label = max(labeled, key=lambda n: n.get("createdAt") or "", default=None)
    edits = [e for e in (node.get("userContentEdits") or {}).get("nodes") or [] if e and e.get("editedAt")]
    last_edit = max(edits, key=lambda e: e["editedAt"], default=None)
    return Task(
        number=int(node["number"]),
        title=node.get("title") or "",
        body=(node.get("body") or "").replace("\r\n", "\n"),
        labels=frozenset(n["name"] for n in (node.get("labels") or {}).get("nodes") or [] if n),
        author=_login(node.get("author")) or "ghost",
        created_at=parse_time(node.get("createdAt")),
        label_actor=_login(last_label.get("actor")) if last_label else None,
        label_at=parse_time(last_label.get("createdAt")) if last_label else None,
        body_editor=(_login(last_edit.get("editor")) or "ghost") if last_edit else None,
        body_edited_at=parse_time(last_edit["editedAt"]) if last_edit else None,
    )


class RelayGitHub:
    """Thin client; the token is read fresh per call (1h app tokens, ADR 0010)."""

    def __init__(self, repo: str, *, role: str = "dev", bot_login: str):
        """`bot_login` is required: status comments are only trusted from this identity
        (without a pinned identity every bot with comment rights would be trusted)."""
        if not bot_login:
            raise ValueError(t("relay.github.bot_login_is_required_status"))
        self.repo = repo
        self.owner, self.name = repo.split("/", 1)
        self.role = role
        self.bot_login = normalize_login(bot_login)

    def token(self) -> str:
        return gitops._gh_token(self.role)

    def _rest(self, method: str, path: str, payload: dict | None = None) -> Any:
        return gitops._gh_api(method, f"{API}/repos/{self.repo}/{path}", self.token(), payload)

    def _graphql(self, query: str, **variables: Any) -> dict:
        res = gitops._gh_api("POST", f"{API}/graphql", self.token(), {"query": query, "variables": variables})
        if res.get("errors"):
            raise GitHubError(f"GraphQL: {res['errors']}")
        return res.get("data") or {}

    def tasks(self, label: str) -> list[Task]:
        """Open issues with the opt-in label (complete cursor pagination)."""
        out: list[Task] = []
        after: str | None = None
        while True:
            data = self._graphql(_TASKS_QUERY, owner=self.owner, name=self.name, label=label, after=after)
            conn = ((data.get("repository") or {}).get("issues")) or {}
            out.extend(task_from_node(n, label) for n in conn.get("nodes") or [] if n)
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return out
            after = page.get("endCursor")

    def task(self, number: int, label: str) -> Task:
        data = self._graphql(_TASK_QUERY, owner=self.owner, name=self.name, number=number)
        node = (data.get("repository") or {}).get("issue")
        if not node:
            raise GitHubError(t("relay.github.issue_not_found_in", number=number, v2=self.repo))
        return task_from_node(node, label)

    def file(self, path: str, ref: str) -> str | None:
        """File content at `ref`; None if branch or file is missing; empty file → `''`.

        The REST contents API returns no content for files > 1 MB (empty string).
        An oversized handoff is a bug and therefore fails closed at
        `Handoff.parse` (`HandoffError`) — instead of silently continuing with empty content."""
        query = urllib.parse.urlencode({"ref": ref})
        try:
            res = self._rest("GET", f"contents/{urllib.parse.quote(path)}?{query}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return base64.b64decode(res.get("content") or "").decode("utf-8")

    def create_ref(self, ref: str, sha: str) -> None:
        """Create a ref (also outside heads/tags, e.g. `refs/fabrikant/…`); 422 if it exists."""
        self._rest("POST", "git/refs", {"ref": ref, "sha": sha})

    def ref_sha(self, ref: str) -> str | None:
        """Commit SHA that `ref` points to; None if it does not exist."""
        try:
            res = self._rest("GET", f"git/ref/{urllib.parse.quote(ref.removeprefix('refs/'))}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        # With a prefix without an exact match the API returns a list — then it does not exist.
        return str(res["object"]["sha"]) if isinstance(res, dict) and res.get("ref") == ref else None

    def open_pr(self, branch: str) -> int | None:
        """Number of the open PR from branch `branch` (in the same repo), otherwise None."""
        query = urllib.parse.urlencode({"head": f"{self.owner}:{branch}", "state": "open", "per_page": 1})
        pulls = self._rest("GET", f"pulls?{query}") or []
        return int(pulls[0]["number"]) if pulls else None

    def pr_body(self, number: int) -> str:
        """Current body of PR `number` (empty if none is set)."""
        return str((self._rest("GET", f"pulls/{number}") or {}).get("body") or "")

    def set_pr_body(self, number: int, body: str) -> None:
        self._rest("PATCH", f"pulls/{number}", {"body": body})

    def commit_identity(self) -> tuple[str, str]:
        """(name, email) of the bot for commits (`<id>+<login>[bot]@users.noreply…`)."""
        login = f"{self.bot_login}[bot]"
        user = gitops._gh_api("GET", f"{API}/users/{urllib.parse.quote(login)}", self.token())
        return login, f"{user['id']}+{login}@users.noreply.github.com"

    def _trusted(self, comment: dict) -> bool:
        """Only the pinned `bot_login` — `type == "Bot"` alone would trust any foreign bot."""
        user = comment.get("user") or {}
        return normalize_login(user.get("login")) == self.bot_login

    def status_comment(self, number: int) -> tuple[int, str] | None:
        """(id, body) of the status comment — authored by the bot only (the marker alone is forgeable)."""
        page = 1
        while True:
            batch = self._rest("GET", f"issues/{number}/comments?per_page={COMMENTS_PER_PAGE}&page={page}") or []
            for c in batch:
                if (c.get("body") or "").startswith(STATUS_MARKER) and self._trusted(c):
                    return int(c["id"]), c["body"]
            if len(batch) < COMMENTS_PER_PAGE:
                return None
            page += 1

    def status_state(self, number: int) -> StatusState:
        found = self.status_comment(number)
        return parse_state(found[1]) if found else StatusState()

    def upsert_status(self, number: int, body: str) -> None:
        """Create the status comment or edit it in place (no call if unchanged).

        On creation the author is checked against `bot_login`: if the builder token does not match,
        `status_comment` would never find the comment again — every tick would create a new one, the state
        (`failures_seen`, `verify_pending`) would be lost. Then: delete the comment, ConfigError."""
        found = self.status_comment(number)
        if found is None:
            created = self._rest("POST", f"issues/{number}/comments", {"body": body}) or {}
            if not self._trusted(created):
                author = _login(created.get("user"))
                if created.get("id"):
                    self._rest("DELETE", f"issues/comments/{created['id']}")
                raise ConfigError(t("relay.github.relay_bot_login_is_but", v1=self.bot_login, author=author))
        elif found[1] != body:
            self._rest("PATCH", f"issues/comments/{found[0]}", {"body": body})

    def sync_labels(self, number: int, current: frozenset[str], add: set[str], remove: set[str]) -> None:
        """Set/remove labels — only what changes compared to `current`."""
        to_add = sorted(add - current)
        if to_add:
            self._rest("POST", f"issues/{number}/labels", {"labels": to_add})
        for name in sorted((remove & current) - add):
            try:
                self._rest("DELETE", f"issues/{number}/labels/{urllib.parse.quote(name, safe='')}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
