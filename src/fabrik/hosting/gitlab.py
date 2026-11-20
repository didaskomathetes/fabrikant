"""GitLab adapter for the hosting port (ADR 0062).

Implements the :class:`~fabrik.hosting.base.Provider` contract over the GitLab
REST API v4 (``https://<host>/api/v4``). Auth is a ``PRIVATE-TOKEN`` header; the
token is resolved per call from ``FABRIK_GITLAB_TOKEN`` (env) or the token file
``~/.config/gitlab/token`` (overridable via ``FABRIK_GITLAB_TOKEN_FILE``).

Differences from GitHub that are documented gaps (see ADR 0062):

- ``update_branch`` has no GitLab equivalent → :class:`HostingNotSupported`.
- ``post_review`` maps ``APPROVE`` to the approve endpoint and ``REQUEST_CHANGES``
  to a note (GitLab has no "request changes" review state).
- ``merge_pr`` ignores ``method`` (squash/merge/rebase is a branch setting, not a
  per-merge parameter on GitLab).
- native issue dependencies are GitHub-only → :class:`HostingNotSupported`.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import config, httpsafe
from .base import (
    Comment,
    CompareResult,
    FileChange,
    HostingConfigError,
    HostingError,
    HostingNotSupported,
    Issue,
    Provider,
    PullRequest,
    RepoRef,
    WebhookEvent,
)

DEFAULT_TOKEN_FILE = "~/.config/gitlab/token"
TOKEN_ENV = "FABRIK_GITLAB_TOKEN"
TOKEN_FILE_ENV = "FABRIK_GITLAB_TOKEN_FILE"

# GitLab MR state values (the steps speak GitHub's ``open``/``closed``).
_MR_STATE_MAP = {"open": "opened", "closed": "closed", "merged": "merged", "all": "all"}


def gitlab_token() -> str:
    """Resolve the GitLab token: ``FABRIK_GITLAB_TOKEN`` env, else the token file.

    Raises:
        HostingError: no token available (neither env nor file).
    """
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if tok:
        return tok
    path = Path(os.environ.get(TOKEN_FILE_ENV, DEFAULT_TOKEN_FILE)).expanduser()
    if path.is_file():
        tok = path.read_text().strip()
        if tok:
            return tok
    raise HostingError(f"no GitLab token (set {TOKEN_ENV} or write {path})")


def _label_names(raw: dict) -> list[str]:
    labels = raw.get("labels") or []
    if isinstance(labels, list):
        return [str(x) for x in labels]
    return [s.strip() for s in str(labels).split(",") if s.strip()]


def _issue_from(raw: dict) -> Issue:
    return Issue(
        number=int(raw["iid"]),
        title=str(raw.get("title", "")),
        body=str(raw.get("description") or ""),
        state=str(raw.get("state", "")),
        labels=_label_names(raw),
        url=str(raw.get("web_url", "")),
        is_pr=False,
        raw=raw,
    )


def _pr_from(raw: dict) -> PullRequest:
    return PullRequest(
        number=int(raw["iid"]),
        title=str(raw.get("title", "")),
        body=str(raw.get("description") or ""),
        state=str(raw.get("state", "")),
        labels=_label_names(raw),
        url=str(raw.get("web_url", "")),
        head_ref=str(raw.get("source_branch", "")),
        head_sha=str(raw.get("sha", "") or (raw.get("head_sha", ""))),
        base_ref=str(raw.get("target_branch", "")),
        mergeable=None,
        raw=raw,
    )


def _comment_from(raw: dict) -> Comment:
    author = raw.get("author") or {}
    return Comment(
        id=int(raw.get("id", 0)),
        author=str(author.get("username", "")),
        body=str(raw.get("body") or ""),
        created_at=str(raw.get("created_at", "")),
        url=str(raw.get("web_url", "")),
        raw=raw,
    )


def _file_from(raw: dict) -> FileChange:
    diff = str(raw.get("diff", "") or "")
    additions = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    if raw.get("new_file"):
        status = "added"
    elif raw.get("deleted_file"):
        status = "removed"
    elif raw.get("renamed_file"):
        status = "renamed"
    else:
        status = "modified"
    return FileChange(
        path=str(raw.get("new_path", "")),
        status=status,
        additions=additions,
        deletions=deletions,
        patch=diff,
        raw=raw,
    )


class GitLabProvider(Provider):
    """GitLab adapter (REST API v4)."""

    name = "gitlab"

    def __init__(self, host: str = "gitlab.com"):
        self._host = host
        self._base = f"https://{host}/api/v4"

    # -- plumbing ------------------------------------------------------------
    def _pid(self, ref: str) -> str:
        """URL-encoded project path (``owner/name`` → ``owner%2Fname``)."""
        r = self.parse_repo(ref)
        return urllib.parse.quote(r.slug, safe="")

    def _request(self, method: str, path: str, payload: dict | None = None, *, raw: bool = False) -> Any:
        """A GitLab REST call with retry on transient network errors (like ``gitops._gh_request``).

        ``raw=True`` returns the decoded body text (for diff endpoints); otherwise the
        parsed JSON. HTTP errors (4xx/5xx) propagate as :class:`urllib.error.HTTPError`.
        """
        url = f"{self._base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "PRIVATE-TOKEN": gitlab_token(),
                "Content-Type": "application/json",
                "User-Agent": "fabrik",
            },
        )
        for attempt in range(config.GH_RETRIES):
            try:
                with httpsafe.urlopen(req, timeout=60) as r:
                    body = r.read()
                    break
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if attempt >= config.GH_RETRIES - 1:
                    raise
                time.sleep(config.GH_RETRY_DELAY_S)
        else:  # pragma: no cover
            raise AssertionError("unreachable")
        if raw:
            return body.decode()
        return json.loads(body) if body else {}

    # -- repo / urls ---------------------------------------------------------
    def parse_repo(self, ref: str) -> RepoRef:
        ref = (ref or "").strip()
        if ref.startswith(("http://", "https://")):
            parts = urllib.parse.urlsplit(ref)
            path = parts.path.strip("/")
            owner, _, name = path.partition("/")
            if not owner or not name:
                raise HostingConfigError(f"unparseable GitLab repo URL: {ref!r}")
            return RepoRef(parts.netloc, owner, name.removesuffix(".git"))
        if ref.startswith("git@"):
            host = ref[len("git@") :].partition(":")[0]  # strip the ``git@`` prefix
            _, _, path = ref.partition(":")
            owner, _, name = path.partition("/")
            if not owner or not name:
                raise HostingConfigError(f"unparseable GitLab repo ref: {ref!r}")
            return RepoRef(host, owner, name.removesuffix(".git"))
        owner, _, name = ref.partition("/")
        if not owner or not name:
            raise HostingConfigError(f"unparseable GitLab repo ref: {ref!r}")
        return RepoRef(self._host, owner, name.removesuffix(".git"))

    # -- issues --------------------------------------------------------------
    def create_issue(self, ref, title, body="", labels=None):
        payload: dict[str, Any] = {"title": title, "description": body}
        if labels:
            payload["labels"] = ",".join(labels)
        return _issue_from(self._request("POST", f"/projects/{self._pid(ref)}/issues", payload))

    def get_issue(self, ref, number):
        return _issue_from(self._request("GET", f"/projects/{self._pid(ref)}/issues/{number}"))

    def edit_issue(self, ref, number, *, title=None, body=None, state=None, state_reason=None, duplicate_issue_id=None):
        # ``state_reason``/``duplicate_issue_id`` have no GitLab equivalent → ignored (ADR 0062).
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["description"] = body
        if state is not None:
            payload["state_event"] = "close" if state == "closed" else "reopen"
        return _issue_from(self._request("PUT", f"/projects/{self._pid(ref)}/issues/{number}", payload))

    def add_labels(self, ref, number, labels):
        if labels:
            self._request("POST", f"/projects/{self._pid(ref)}/issues/{number}/labels", {"labels": ",".join(labels)})

    def remove_label(self, ref, number, label):
        try:
            self._request(
                "DELETE", f"/projects/{self._pid(ref)}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}"
            )
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            return False
        return True

    def list_issues(self, ref, *, state="open", labels=None, sort="updated", direction="desc", page=1, per_page=100):
        query: dict[str, Any] = {
            "state": state,
            "order_by": "updated_at" if sort == "updated" else "created_at",
            "sort": direction,
            "page": page,
            "per_page": per_page,
        }
        if labels:
            query["labels"] = ",".join(labels)
        items = self._request("GET", f"/projects/{self._pid(ref)}/issues?{urllib.parse.urlencode(query)}")
        return [_issue_from(it) for it in (items or [])]

    def create_comment(self, ref, number, body):
        return _comment_from(self._request("POST", f"/projects/{self._pid(ref)}/issues/{number}/notes", {"body": body}))

    def list_comments(self, ref, number, *, page=1, per_page=100):
        items = self._request(
            "GET", f"/projects/{self._pid(ref)}/issues/{number}/notes?page={page}&per_page={per_page}"
        )
        return [_comment_from(it) for it in (items or [])]

    # -- pull / merge requests -----------------------------------------------
    def create_pr(self, ref, *, title, head, base, body=""):
        payload = {"title": title, "source_branch": head, "target_branch": base, "description": body}
        return _pr_from(self._request("POST", f"/projects/{self._pid(ref)}/merge_requests", payload))

    def get_pr(self, ref, number):
        return _pr_from(self._request("GET", f"/projects/{self._pid(ref)}/merge_requests/{number}"))

    def list_prs(self, ref, *, state="open", labels=None, head=None, page=1, per_page=100):
        query: dict[str, Any] = {
            "state": _MR_STATE_MAP.get(state, state),
            "page": page,
            "per_page": per_page,
        }
        if labels:
            query["labels"] = ",".join(labels)
        if head:
            query["source_branch"] = head
        items = self._request("GET", f"/projects/{self._pid(ref)}/merge_requests?{urllib.parse.urlencode(query)}")
        return [_pr_from(it) for it in (items or [])]

    def merge_pr(self, ref, number, *, method="squash", sha=None):
        # GitLab: merge method is a branch setting, not a per-merge parameter (gap).
        self._request("PUT", f"/projects/{self._pid(ref)}/merge_requests/{number}/merge", {})
        return self.get_pr(ref, number)

    def update_branch(self, ref, number, *, expected_head_sha=None):
        raise HostingNotSupported("gitlab: update_branch (no equivalent API; merge the target into the source via git)")

    def _changes(self, ref: str, number: int) -> list[dict]:
        res = self._request("GET", f"/projects/{self._pid(ref)}/merge_requests/{number}/changes")
        return (res or {}).get("changes") or []

    def pr_files(self, ref, number):
        return [_file_from(c) for c in self._changes(ref, number)]

    def pr_diff(self, ref, number):
        return "\n".join(str(c.get("diff", "")) for c in self._changes(ref, number))

    def post_review(self, ref, number, *, event, body=""):
        if event == "APPROVE":
            self._request("POST", f"/projects/{self._pid(ref)}/merge_requests/{number}/approve", {})
        else:  # REQUEST_CHANGES → a note (GitLab has no request-changes state)
            self._request("POST", f"/projects/{self._pid(ref)}/merge_requests/{number}/notes", {"body": body})

    def compare(self, ref, base, head):
        pid = self._pid(ref)
        fwd = self._request("GET", f"/projects/{pid}/repository/compare?from={base}&to={head}")
        ahead = int((fwd or {}).get("commit", {}).get("count", 0) or 0)
        rev = self._request("GET", f"/projects/{pid}/repository/compare?from={head}&to={base}")
        behind = int((rev or {}).get("commit", {}).get("count", 0) or 0)
        return CompareResult(ahead_by=ahead, behind_by=behind)

    # -- native issue dependencies (GitHub-only) -----------------------------
    def issue_dependencies(self, ref, number, *, kind="blocked_by"):
        raise HostingNotSupported("gitlab: native issue dependencies (GitHub-only, ADR 0031)")

    # -- API commits (commit_via: api) ---------------------------------------
    def commit_files(self, ref, branch, message, files, *, author=None):
        actions = []
        for f in files:
            if f.get("deleted"):
                actions.append({"action": "delete", "file_path": f["path"]})
            else:
                actions.append({"action": "create", "file_path": f["path"], "content": f["content"]})
        payload: dict[str, Any] = {"branch": branch, "commit_message": message, "actions": actions}
        if author:
            payload["author_name"] = author.get("name", "")
            payload["author_email"] = author.get("email", "")
        res = self._request("POST", f"/projects/{self._pid(ref)}/repository/commits", payload)
        return str(res["id"])

    # -- webhook parsing -----------------------------------------------------
    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None:
        event = (headers.get("X-Gitlab-Event") or "").strip()
        if event not in ("Merge Request Hook", "Issue Hook"):
            return None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return None
        attrs = data.get("object_attributes") or {}
        project = data.get("project") or {}
        repo = str(project.get("path_with_namespace", ""))
        if not repo:
            return None
        kind = "pr" if event == "Merge Request Hook" else "issue"
        return WebhookEvent(
            kind=kind,
            action=str(attrs.get("action", "")),
            number=int(attrs.get("iid", 0)),
            repo=repo,
            title=str(attrs.get("title", "")),
            url=str(attrs.get("url", "")),
            raw=data,
        )
