"""GitHub adapter for the hosting port (ADR 0061).

A thin wrapper over the existing ``gitops`` GitHub helpers (``_gh_api``,
``_gh_token``, ``_gh_diff``, ``_gh_dependencies``), so the GitHub code path is
byte-for-byte the one fabrik has always used — no behavioural regression. The
adapter only *normalizes* the provider-specific JSON into the common dataclasses
from :mod:`fabrik.hosting.base`.

Tokens are resolved per call via :func:`fabrik.gitops._gh_token` (fresh, so a
rotated 1h installation token is picked up without a restart). The ``role``
selects the dual-identity (ADR 0010): ``dev`` (builder) by default, ``reviewer``
for reviews.
"""

from __future__ import annotations

import base64
import re
import urllib.error
import urllib.parse
from typing import Any

from .. import gitops
from .base import (
    Comment,
    CompareResult,
    FileChange,
    HostingConfigError,
    Issue,
    PullRequest,
    RepoRef,
    Provider,
)

_GITHUB_SLUG = re.compile(r"^(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$")
_GITHUB_URL = re.compile(r"^(?:https?://|git@)(?:[^@/]+@)?(?P<host>github\.com)[:/](?P<path>.+?)(?:\.git)?/?$")


def _label_names(raw: dict) -> list[str]:
    return [str(lbl.get("name", "")) for lbl in raw.get("labels") or [] if isinstance(lbl, dict)]


def _issue_from(raw: dict) -> Issue:
    # Lenient: partial responses (e.g. a body-only GET) must not crash the parse.
    return Issue(
        number=int(raw.get("number", 0)),
        title=str(raw.get("title", "")),
        body=str(raw.get("body") or ""),
        state=str(raw.get("state", "")),
        labels=_label_names(raw),
        url=str(raw.get("html_url", "")),
        is_pr="pull_request" in raw,
        raw=raw,
    )


def _pr_from(raw: dict) -> PullRequest:
    head = raw.get("head") or {}
    base = raw.get("base") or {}
    return PullRequest(
        number=int(raw.get("number", 0)),
        title=str(raw.get("title", "")),
        body=str(raw.get("body") or ""),
        state=str(raw.get("state", "")),
        labels=_label_names(raw),
        url=str(raw.get("html_url", "")),
        head_ref=str(head.get("ref", "")),
        head_sha=str(head.get("sha", "")),
        base_ref=str(base.get("ref", "")),
        mergeable=raw.get("mergeable"),
        raw=raw,
    )


def _comment_from(raw: dict) -> Comment:
    user = raw.get("user") or {}
    return Comment(
        id=int(raw.get("id", 0)),
        author=str(user.get("login", "")),
        body=str(raw.get("body") or ""),
        created_at=str(raw.get("created_at", "")),
        url=str(raw.get("html_url", "")),
        raw=raw,
    )


def _file_from(raw: dict) -> FileChange:
    return FileChange(
        path=str(raw.get("filename", "")),
        status=str(raw.get("status", "")),
        additions=int(raw.get("additions", 0) or 0),
        deletions=int(raw.get("deletions", 0) or 0),
        patch=str(raw.get("patch", "") or ""),
        raw=raw,
    )


class GitHubProvider(Provider):
    """GitHub adapter (default provider). Wraps the existing ``gitops`` helpers."""

    name = "github"
    HOST = "github.com"
    API = "https://api.github.com"

    def __init__(self, role: str = "dev"):
        self._role = role

    # -- plumbing ------------------------------------------------------------
    def _token(self) -> str:
        return gitops._gh_token(self._role)

    def _api(self, method: str, path: str, payload: dict | None = None) -> Any:
        # 4-arg call (no ``accept``): the diff endpoint uses ``accept`` and goes through
        # ``gitops._gh_diff`` directly, so the common path keeps the historical signature.
        return gitops._gh_api(method, f"{self.API}{path}", self._token(), payload)

    # -- repo / urls ---------------------------------------------------------
    def parse_repo(self, ref: str) -> RepoRef:
        ref = (ref or "").strip()
        m = _GITHUB_SLUG.match(ref)
        if m:
            return RepoRef(self.HOST, m.group("owner"), m.group("name"))
        m = _GITHUB_URL.match(ref)
        if m:
            owner, _, name = m.group("path").partition("/")
            if not name:
                raise HostingConfigError(f"unparseable GitHub repo ref: {ref!r}")
            return RepoRef(self.HOST, owner, name)
        raise HostingConfigError(f"unparseable GitHub repo ref: {ref!r}")

    # -- issues --------------------------------------------------------------
    def create_issue(self, ref, title, body="", labels=None):
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        return _issue_from(self._api("POST", f"/repos/{ref}/issues", payload))

    def get_issue(self, ref, number):
        return _issue_from(self._api("GET", f"/repos/{ref}/issues/{number}"))

    def edit_issue(self, ref, number, *, title=None, body=None, state=None, state_reason=None, duplicate_issue_id=None):
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if state_reason is not None:
            payload["state_reason"] = state_reason
        if duplicate_issue_id is not None:
            payload["duplicate_issue_id"] = duplicate_issue_id
        return _issue_from(self._api("PATCH", f"/repos/{ref}/issues/{number}", payload))

    def add_labels(self, ref, number, labels):
        if labels:
            self._api("POST", f"/repos/{ref}/issues/{number}/labels", {"labels": list(labels)})

    def remove_label(self, ref, number, label):
        try:
            self._api("DELETE", f"/repos/{ref}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}")
        except urllib.error.HTTPError as e:
            if e.code != 404:  # label not (anymore) on the issue → idempotent
                raise
            return False
        return True

    def list_issues(self, ref, *, state="open", labels=None, sort="updated", direction="desc", page=1, per_page=100):
        query = {"state": state, "sort": sort, "direction": direction, "per_page": per_page, "page": page}
        if labels:
            query["labels"] = ",".join(labels)
        items = self._api("GET", f"/repos/{ref}/issues?{urllib.parse.urlencode(query)}")
        return [_issue_from(it) for it in items] if isinstance(items, list) else []

    def create_comment(self, ref, number, body):
        return _comment_from(self._api("POST", f"/repos/{ref}/issues/{number}/comments", {"body": body}))

    def list_comments(self, ref, number, *, page=1, per_page=100):
        items = self._api("GET", f"/repos/{ref}/issues/{number}/comments?per_page={per_page}&page={page}")
        return [_comment_from(it) for it in items] if isinstance(items, list) else []

    # -- pull / merge requests -----------------------------------------------
    def create_pr(self, ref, *, title, head, base, body=""):
        payload = {"title": title, "head": head, "base": base, "body": body}
        return _pr_from(self._api("POST", f"/repos/{ref}/pulls", payload))

    def get_pr(self, ref, number):
        return _pr_from(self._api("GET", f"/repos/{ref}/pulls/{number}"))

    def list_prs(self, ref, *, state="open", labels=None, head=None, page=1, per_page=100):
        query: dict[str, Any] = {"state": state, "per_page": per_page, "page": page}
        if labels:
            query["labels"] = ",".join(labels)
        if head:
            query["head"] = head
        items = self._api("GET", f"/repos/{ref}/pulls?{urllib.parse.urlencode(query)}")
        return [_pr_from(it) for it in items] if isinstance(items, list) else []

    def merge_pr(self, ref, number, *, method="squash", sha=None):
        payload: dict[str, Any] = {"merge_method": method}
        if sha:
            payload["sha"] = sha
        return _pr_from(self._api("PUT", f"/repos/{ref}/pulls/{number}/merge", payload))

    def update_branch(self, ref, number, *, expected_head_sha=None):
        payload = {"expected_head_sha": expected_head_sha} if expected_head_sha else {}
        self._api("PUT", f"/repos/{ref}/pulls/{number}/update-branch", payload)

    def pr_files(self, ref, number):
        items = self._api("GET", f"/repos/{ref}/pulls/{number}/files")
        return [_file_from(it) for it in items] if isinstance(items, list) else []

    def pr_diff(self, ref, number):
        return gitops._gh_diff(ref, number, self._token())

    def post_review(self, ref, number, *, event, body=""):
        self._api("POST", f"/repos/{ref}/pulls/{number}/reviews", {"event": event, "body": body})

    def compare(self, ref, base, head):
        res = self._api(
            "GET",
            f"/repos/{ref}/compare/{urllib.parse.quote(base, safe='')}...{urllib.parse.quote(head, safe='')}",
        )
        res = res if isinstance(res, dict) else {}
        ahead = res.get("ahead_by")
        behind = res.get("behind_by")
        # Missing/non-int count → None ("unknown"), not 0 — the pr-step relies on this
        # to fall through to the Create path (which gives the clearer host error).
        return CompareResult(
            ahead_by=ahead if type(ahead) is int else None,
            behind_by=behind if type(behind) is int else None,
        )

    # -- native issue dependencies (GitHub-only) -----------------------------
    def issue_dependencies(self, ref, number, *, kind="blocked_by"):
        items = gitops._gh_dependencies(ref, number, self._token(), kind)
        return [_issue_from(it) for it in (items or [])]

    def add_issue_dependency(self, ref, number, *, blocker_id):
        self._api("POST", f"/repos/{ref}/issues/{number}/dependencies/blocked_by", {"issue_id": blocker_id})

    def remove_issue_dependency(self, ref, number, *, blocker_id):
        self._api("DELETE", f"/repos/{ref}/issues/{number}/dependencies/blocked_by/{blocker_id}")

    # -- API commits (commit_via: api) ---------------------------------------
    def commit_files(self, ref, branch, message, files, *, author=None):
        """Commit ``files`` to ``branch`` via the Git Data API (blobs → tree → commit → ref).

        Returns the new commit SHA. GitHub signs commits created by an app this way
        (``commit_via: api``, ADR 0010).
        """
        head = self._branch_head(ref, branch)
        base_tree = self._commit_tree(ref, head) if head else ""
        entries: list[dict[str, Any]] = []
        for f in files:
            path = f["path"]
            if f.get("deleted"):
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            blob = self._api(
                "POST",
                "/git/blobs",
                {"content": base64.b64encode(f["content"].encode()).decode(), "encoding": "base64"},
            )
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._api("POST", "/git/trees", {"base_tree": base_tree, "tree": entries})
        payload: dict[str, Any] = {"message": message, "tree": tree["sha"]}
        if head:
            payload["parents"] = [head]
        if author:
            payload["author"] = author
        commit = self._api("POST", "/git/commits", payload)
        self._set_ref(ref, branch, commit["sha"], exists=head is not None)
        return str(commit["sha"])

    def _branch_head(self, ref: str, branch: str) -> str | None:
        try:
            ref_obj = self._api("GET", f"/repos/{ref}/git/ref/heads/{urllib.parse.quote(branch, safe='/')}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return str(ref_obj["object"]["sha"])

    def _commit_tree(self, ref: str, sha: str) -> str:
        return str(self._api("GET", f"/repos/{ref}/git/commits/{sha}")["tree"]["sha"])

    def _set_ref(self, ref: str, branch: str, sha: str, *, exists: bool) -> None:
        path = f"/repos/{ref}/git/refs/heads/{urllib.parse.quote(branch, safe='/')}"
        if exists:
            self._api("PATCH", path, {"sha": sha, "force": False})
        else:
            self._api("POST", f"/repos/{ref}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
