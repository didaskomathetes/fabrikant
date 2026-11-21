"""GitLab adapter for the hosting port (ADR 0062).

Implements the :class:`~fabrik.hosting.base.Provider` contract over the GitLab
REST API v4 (``https://<host>/api/v4``). Auth is a ``PRIVATE-TOKEN`` header; the
token is resolved per call from ``FABRIK_GITLAB_TOKEN`` (env) or the token file
``~/.config/gitlab/token`` (overridable via ``FABRIK_GITLAB_TOKEN_FILE``).

The host's version and capabilities are registered once from ``GET /version``
(see :meth:`GitLabProvider.capabilities` / :class:`GitLabCapabilities`). Endpoints
that vary by version or are disabled on restricted instances (e.g. the modern
per-file diffs endpoint, the issue ``.../labels`` sub-endpoint) are handled with
supported fallbacks.

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
from dataclasses import dataclass
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

# The MR diff is computed asynchronously after the MR is created; poll briefly for it.
_DIFF_POLL_TIMEOUT_S = 10.0
_DIFF_POLL_INTERVAL_S = 1.0

# Oldest GitLab with the modern per-file diffs endpoint (``/merge_request_diffs/:id``).
_MIN_MR_DIFFS_VERSION = (14, 0)


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


def _parse_version(raw: str) -> tuple[int, ...]:
    """Parse a GitLab version string into a comparable tuple (``"19.5.8-ee"`` → ``(19, 5, 8)``)."""
    core = raw.split("-", 1)[0]
    parts = [int(p) for p in ("".join(ch for ch in seg if ch.isdigit()) or "0" for seg in core.split("."))]
    return tuple(parts)


@dataclass(frozen=True)
class GitLabCapabilities:
    """Version- and instance-specific capabilities of a GitLab host (ADR 0062).

    Registered once per provider from ``GET /version``. ``mr_diffs_endpoint`` reflects
    the version gate for the modern per-file diffs endpoint; restricted instances may
    still disable it, so callers fall back to the deprecated ``/changes`` endpoint on
    404/405.
    """

    version: str
    version_tuple: tuple[int, ...]
    enterprise: bool
    mr_diffs_endpoint: bool


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

    def __init__(self, host: str = "gitlab.com", base_url: str | None = None):
        self._host = host
        # ``base_url`` overrides the default ``https://<host>/api/v4`` — useful for
        # local/dev or self-hosted instances served over plain http (e.g. a container).
        self._base = (base_url or f"https://{host}/api/v4").rstrip("/")
        self._version_cache: dict | None = None
        self._capabilities: GitLabCapabilities | None = None

    # -- version / capabilities (ADR 0062) -----------------------------------
    def _version_info(self) -> dict:
        """The ``GET /version`` payload, fetched once and cached."""
        cache = self._version_cache
        if cache is None:
            cache = self._request("GET", "/version") or {}
            self._version_cache = cache
        return cache

    def capabilities(self) -> GitLabCapabilities:
        """The registered :class:`GitLabCapabilities` for this host (fetched once)."""
        if self._capabilities is None:
            info = self._version_info()
            version = str(info.get("version", ""))
            vt = _parse_version(version)
            self._capabilities = GitLabCapabilities(
                version=version,
                version_tuple=vt,
                enterprise=bool(info.get("enterprise", False)),
                mr_diffs_endpoint=vt >= _MIN_MR_DIFFS_VERSION,
            )
        return self._capabilities

    @property
    def version(self) -> str:
        """The GitLab instance version string (e.g. ``"19.5.8-ee"``)."""
        return self.capabilities().version

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
        if not labels:
            return
        # The ``.../labels`` sub-endpoint is disabled on some instances; the issue
        # update's ``add_labels`` param is the supported, idempotent way to add.
        self._request("PUT", f"/projects/{self._pid(ref)}/issues/{number}", {"add_labels": ",".join(labels)})

    def remove_label(self, ref, number, label):
        path = f"/projects/{self._pid(ref)}/issues/{number}"
        try:
            current = _label_names(self._request("GET", path))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False  # issue gone → label gone (idempotent)
            raise
        if label not in current:
            return False  # already gone (idempotent)
        self._request("PUT", path, {"remove_labels": label})
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

    def _mr_changes(self, ref: str, number: int) -> list[dict]:
        """Per-file changes for an MR (newest diff).

        Uses the modern ``/merge_request_diffs/:id`` endpoint when the instance
        supports it (GitLab >= 14), falling back to the deprecated ``/changes``
        endpoint (restricted instances disable the former). The diff is computed
        asynchronously, so poll briefly until it is collected.
        """
        pid = self._pid(ref)
        use_modern = self.capabilities().mr_diffs_endpoint

        def fetch() -> list[dict]:
            nonlocal use_modern
            if use_modern:
                try:
                    versions = self._request("GET", f"/projects/{pid}/merge_requests/{number}/versions")
                    if versions:
                        diff_id = max(int(v["id"]) for v in versions)
                        res = self._request("GET", f"/projects/{pid}/merge_request_diffs/{diff_id}")
                        return res.get("diffs") or []
                except urllib.error.HTTPError as e:
                    if e.code not in (404, 405):
                        raise
                    use_modern = False  # restricted instance → stop re-trying the modern endpoint
            res = self._request("GET", f"/projects/{pid}/merge_requests/{number}/changes")
            return (res or {}).get("changes") or []

        deadline = time.monotonic() + _DIFF_POLL_TIMEOUT_S
        changes: list[dict] = []
        while True:
            changes = fetch()
            if changes or time.monotonic() >= deadline:
                return changes
            time.sleep(_DIFF_POLL_INTERVAL_S)

    def pr_files(self, ref, number):
        return [_file_from(c) for c in self._mr_changes(ref, number)]

    def pr_diff(self, ref, number):
        return "\n".join(str(c.get("diff", "")) for c in self._mr_changes(ref, number))

    def post_review(self, ref, number, *, event, body=""):
        if event == "APPROVE":
            self._request("POST", f"/projects/{self._pid(ref)}/merge_requests/{number}/approve", {})
        else:  # REQUEST_CHANGES → a note (GitLab has no request-changes state)
            self._request("POST", f"/projects/{self._pid(ref)}/merge_requests/{number}/notes", {"body": body})

    def compare(self, ref, base, head):
        pid = self._pid(ref)
        fwd = self._request("GET", f"/projects/{pid}/repository/compare?from={base}&to={head}")
        # ``commit`` is ``null`` for an empty/identical compare — guard the nested access.
        ahead = int(((fwd or {}).get("commit") or {}).get("count", 0) or 0)
        rev = self._request("GET", f"/projects/{pid}/repository/compare?from={head}&to={base}")
        behind = int(((rev or {}).get("commit") or {}).get("count", 0) or 0)
        return CompareResult(ahead_by=ahead, behind_by=behind)

    # -- native issue dependencies (GitHub-only) -----------------------------
    def issue_dependencies(self, ref, number, *, kind="blocked_by"):
        raise HostingNotSupported("gitlab: native issue dependencies (GitHub-only, ADR 0031)")

    # -- API commits (commit_via: api) ---------------------------------------
    def commit_files(self, ref, branch, message, files, *, author=None, start_branch=None):
        actions = []
        for f in files:
            if f.get("deleted"):
                actions.append({"action": "delete", "file_path": f["path"]})
            else:
                actions.append({"action": "create", "file_path": f["path"], "content": f["content"]})
        payload: dict[str, Any] = {"branch": branch, "commit_message": message, "actions": actions}
        if start_branch:
            # branch does not exist yet — base it on ``start_branch`` (GitLab creates it)
            payload["start_branch"] = start_branch
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
