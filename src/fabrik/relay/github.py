"""GitHub-Zugriff für Relay (Builder-Identität): Tasks, Handoff auf dem Branch, Status-Kommentar, Labels."""

from __future__ import annotations

import base64
import urllib.error
import urllib.parse
from typing import Any

from .. import gitops
from ..failures import ConfigError
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
    """Dünner Client; das Token wird pro Call frisch gelesen (1h-App-Tokens, ADR 0010)."""

    def __init__(self, repo: str, *, role: str = "dev", bot_login: str):
        """`bot_login` ist Pflicht: Status-Kommentare werden nur von dieser Identität vertraut
        (ohne gepinnte Identität wäre jeder Bot mit Kommentarrecht vertrauenswürdig)."""
        if not bot_login:
            raise ValueError("bot_login ist Pflicht (Status-Kommentare nur von der gepinnten Identität)")
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
        """Offene Issues mit dem Opt-in-Label (vollständige Cursor-Pagination)."""
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
            raise GitHubError(f"Issue #{number} nicht gefunden in {self.repo}")
        return task_from_node(node, label)

    def file(self, path: str, ref: str) -> str | None:
        """Dateiinhalt auf `ref`; None, wenn Branch oder Datei fehlen; leere Datei → `''`.

        Die REST-Contents-API liefert für Dateien > 1 MB keinen Inhalt (leerer String).
        Ein überdimensionaler Handoff ist ein Bug und scheitert deshalb fail-closed am
        `Handoff.parse` (`HandoffError`) — statt stumm mit leerem Inhalt weiterzulaufen."""
        query = urllib.parse.urlencode({"ref": ref})
        try:
            res = self._rest("GET", f"contents/{urllib.parse.quote(path)}?{query}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return base64.b64decode(res.get("content") or "").decode("utf-8")

    def create_ref(self, ref: str, sha: str) -> None:
        """Ref anlegen (auch außerhalb von heads/tags, z. B. `refs/fabrik/…`); 422, wenn er existiert."""
        self._rest("POST", "git/refs", {"ref": ref, "sha": sha})

    def ref_sha(self, ref: str) -> str | None:
        """Commit-SHA, auf den `ref` zeigt; None, wenn es ihn nicht gibt."""
        try:
            res = self._rest("GET", f"git/ref/{urllib.parse.quote(ref.removeprefix('refs/'))}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        # Bei einem Präfix ohne exakten Treffer liefert die API eine Liste — dann gibt es ihn nicht.
        return str(res["object"]["sha"]) if isinstance(res, dict) and res.get("ref") == ref else None

    def open_pr(self, branch: str) -> int | None:
        """Nummer des offenen PRs vom Branch `branch` (im selben Repo), sonst None."""
        query = urllib.parse.urlencode({"head": f"{self.owner}:{branch}", "state": "open", "per_page": 1})
        pulls = self._rest("GET", f"pulls?{query}") or []
        return int(pulls[0]["number"]) if pulls else None

    def pr_body(self, number: int) -> str:
        """Aktueller Body des PRs `number` (leer, wenn keiner gesetzt ist)."""
        return str((self._rest("GET", f"pulls/{number}") or {}).get("body") or "")

    def set_pr_body(self, number: int, body: str) -> None:
        self._rest("PATCH", f"pulls/{number}", {"body": body})

    def commit_identity(self) -> tuple[str, str]:
        """(name, email) des Bots für Commits (`<id>+<login>[bot]@users.noreply…`)."""
        login = f"{self.bot_login}[bot]"
        user = gitops._gh_api("GET", f"{API}/users/{urllib.parse.quote(login)}", self.token())
        return login, f"{user['id']}+{login}@users.noreply.github.com"

    def _trusted(self, comment: dict) -> bool:
        """Nur die gepinnte `bot_login` — `type == "Bot"` allein würde jeden fremden Bot vertrauen."""
        user = comment.get("user") or {}
        return normalize_login(user.get("login")) == self.bot_login

    def status_comment(self, number: int) -> tuple[int, str] | None:
        """(id, body) des Status-Kommentars — nur vom Bot verfasst (Marker allein ist fälschbar)."""
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
        """Status-Kommentar anlegen oder in-place editieren (kein Call, wenn unverändert).

        Beim Anlegen wird der Autor gegen `bot_login` geprüft: passt das Builder-Token nicht, fände
        `status_comment` den Kommentar nie wieder — jeder Tick legte einen neuen an, der Zustand
        (`failures_seen`, `verify_pending`) ginge verloren. Dann: Kommentar löschen, ConfigError."""
        found = self.status_comment(number)
        if found is None:
            created = self._rest("POST", f"issues/{number}/comments", {"body": body}) or {}
            if not self._trusted(created):
                author = _login(created.get("user"))
                if created.get("id"):
                    self._rest("DELETE", f"issues/comments/{created['id']}")
                raise ConfigError(
                    f"relay.bot_login ist {self.bot_login!r}, das Builder-Token kommentiert aber als {author!r}"
                )
        elif found[1] != body:
            self._rest("PATCH", f"issues/comments/{found[0]}", {"body": body})

    def sync_labels(self, number: int, current: frozenset[str], add: set[str], remove: set[str]) -> None:
        """Labels setzen/entfernen — nur, was sich gegenüber `current` ändert."""
        to_add = sorted(add - current)
        if to_add:
            self._rest("POST", f"issues/{number}/labels", {"labels": to_add})
        for name in sorted((remove & current) - add):
            try:
                self._rest("DELETE", f"issues/{number}/labels/{urllib.parse.quote(name, safe='')}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
