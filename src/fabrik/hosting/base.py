"""Hosting-provider abstraction (ADR 0061): the port that steps and triggers call.

fabrik talks to a code-hosting service (GitHub today, self-hosted GitLab as well).
Historically every step and trigger spoke the GitHub REST API directly (raw
``api.github.com`` URLs, ``_gh_api``). This module introduces a provider port so the
same steps/triggers work against any supported host.

The :class:`Provider` ABC is the union of the operations the steps and triggers need
(create/get/list PRs and issues, labels, comments, merge, diff, compare, reviews,
native issue dependencies, API commits, webhook parsing). Concrete adapters live in
:mod:`fabrik.hosting.github` and :mod:`fabrik.hosting.gitlab`; selection and
credential resolution in :mod:`fabrik.hosting.select`.

The normalized dataclasses below are the *contract* between steps/triggers and a
provider: a step never sees provider-specific JSON, only these types. Each type keeps
the raw provider payload in :attr:`raw` for the rare field that has no common name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- errors


class HostingError(Exception):
    """Base class for hosting-provider errors (a step can catch this to fail cleanly)."""


class HostingConfigError(HostingError, ValueError):
    """The repo/provider configuration is unknown or unsupported (e.g. an unparseable repo ref)."""


class HostingNotSupported(HostingError, NotImplementedError):
    """The operation has no equivalent on this provider.

    Example: native issue dependencies (``blocked_by``) are a GitHub feature; GitLab has
    no equivalent, so :meth:`Provider.issue_dependencies` raises this there.
    """


# --------------------------------------------------------------------------- data types


@dataclass(frozen=True)
class RepoRef:
    """A parsed repository reference: host + ``owner/name``.

    ``slug`` is the provider-agnostic ``owner/name`` form used by the steps.
    """

    host: str
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class Issue:
    """A normalized issue (pull requests are issues on both providers)."""

    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    url: str
    is_pr: bool = False
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class PullRequest:
    """A normalized pull request / merge request."""

    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    url: str
    head_ref: str
    head_sha: str
    base_ref: str
    mergeable: bool | None = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Comment:
    """A normalized issue/PR comment."""

    id: int
    author: str
    body: str
    created_at: str
    url: str
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class FileChange:
    """One changed file of a PR/MR diff (enough for the human_gate heuristics)."""

    path: str
    status: str
    additions: int
    deletions: int
    patch: str
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class CompareResult:
    """Result of comparing two refs (how far ``head`` is ahead/behind ``base``).

    ``ahead_by``/``behind_by`` are ``None`` when the host did not report a count
    (e.g. an empty/unknown compare response) — callers must treat that as "unknown",
    not "zero".
    """

    ahead_by: int | None
    behind_by: int | None
    diff: str = ""
    files: list[FileChange] = field(default_factory=list)


@dataclass
class WebhookEvent:
    """A parsed inbound webhook payload (``on_webhook`` trigger, ADR 0027).

    ``action`` is the provider event action (e.g. ``opened``/``closed``); ``kind`` is
    ``issue`` or ``pr``; ``number`` the item number; ``repo`` the ``owner/name`` slug.
    """

    kind: str
    action: str
    number: int
    repo: str
    title: str = ""
    url: str = ""
    raw: dict = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- the port


class Provider(ABC):
    """The code-hosting port: the union of operations steps and triggers need.

    Concrete adapters must not inherit from this class (duck-typing is fine, like
    ``AgentBackend``/``RunLauncher``); the ABC documents the contract and gives a
    single place to add operations.

    All methods take ``ref`` — an ``owner/name`` slug or a full clone/API URL — and
    resolve it via :meth:`parse_repo`. Tokens are resolved per call by the adapter
    (fresh, so a rotated token is picked up without a restart).
    """

    #: provider name (``github`` / ``gitlab``)
    name: str = "abstract"

    # -- repo / urls ---------------------------------------------------------
    @abstractmethod
    def parse_repo(self, ref: str) -> RepoRef:
        """Parse ``ref`` (``owner/name`` or a URL) into a :class:`RepoRef`."""

    def repo_url(self, ref: str) -> str:
        """Web URL of the repository."""
        r = self.parse_repo(ref)
        return f"https://{r.host}/{r.slug}"

    def issue_url(self, ref: str, number: int) -> str:
        """Web URL of an issue."""
        return f"{self.repo_url(ref)}/issues/{number}"

    def pr_url(self, ref: str, number: int) -> str:
        """Web URL of a pull/merge request."""
        return f"{self.repo_url(ref)}/pull/{number}"

    # -- issues --------------------------------------------------------------
    @abstractmethod
    def create_issue(self, ref: str, title: str, body: str = "", labels: list[str] | None = None) -> Issue: ...

    @abstractmethod
    def get_issue(self, ref: str, number: int) -> Issue: ...

    @abstractmethod
    def edit_issue(
        self,
        ref: str,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
        duplicate_issue_id: int | None = None,
    ) -> Issue:
        """Edit an issue. ``state_reason`` (``completed``/``not_planned``/``duplicate``) and
        ``duplicate_issue_id`` are GitHub close semantics; providers without the concept
        ignore them (documented gap, ADR 0061)."""

    @abstractmethod
    def add_labels(self, ref: str, number: int, labels: list[str]) -> None: ...

    @abstractmethod
    def remove_label(self, ref: str, number: int, label: str) -> bool:
        """Remove ``label``; return ``False`` if it was already gone (idempotent)."""

    @abstractmethod
    def list_issues(
        self,
        ref: str,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        sort: str = "updated",
        direction: str = "desc",
        page: int = 1,
        per_page: int = 100,
    ) -> list[Issue]: ...

    @abstractmethod
    def create_comment(self, ref: str, number: int, body: str) -> Comment: ...

    @abstractmethod
    def list_comments(self, ref: str, number: int, *, page: int = 1, per_page: int = 100) -> list[Comment]: ...

    # -- pull / merge requests ----------------------------------------------
    @abstractmethod
    def create_pr(self, ref: str, *, title: str, head: str, base: str, body: str = "") -> PullRequest: ...

    @abstractmethod
    def get_pr(self, ref: str, number: int) -> PullRequest: ...

    @abstractmethod
    def list_prs(
        self,
        ref: str,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        head: str | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[PullRequest]: ...

    @abstractmethod
    def merge_pr(self, ref: str, number: int, *, method: str = "squash", sha: str | None = None) -> PullRequest: ...

    @abstractmethod
    def update_branch(self, ref: str, number: int, *, expected_head_sha: str | None = None) -> None: ...

    @abstractmethod
    def pr_files(self, ref: str, number: int) -> list[FileChange]: ...

    @abstractmethod
    def pr_diff(self, ref: str, number: int) -> str:
        """The PR/MR diff as unified-diff text."""

    @abstractmethod
    def post_review(self, ref: str, number: int, *, event: str, body: str = "") -> None:
        """Post a review. ``event`` is ``APPROVE`` or ``REQUEST_CHANGES``."""

    @abstractmethod
    def compare(self, ref: str, base: str, head: str) -> CompareResult: ...

    # -- native issue dependencies (GitHub-only) ----------------------------
    @abstractmethod
    def issue_dependencies(self, ref: str, number: int, *, kind: str = "blocked_by") -> list[Issue]:
        """Native issue dependencies (ADR 0031). ``kind``: ``blocked_by`` | ``blocking``."""

    def add_issue_dependency(self, ref: str, number: int, *, blocker_id: int) -> None:
        """Make ``number`` blocked by the issue with ``blocker_id`` (GitHub-only)."""
        raise HostingNotSupported(f"{self.name}: add_issue_dependency")

    def remove_issue_dependency(self, ref: str, number: int, *, blocker_id: int) -> None:
        """Remove a ``blocked_by`` relation (GitHub-only)."""
        raise HostingNotSupported(f"{self.name}: remove_issue_dependency")

    # -- API commits (commit_via: api) --------------------------------------
    @abstractmethod
    def commit_files(
        self,
        ref: str,
        branch: str,
        message: str,
        files: list[dict[str, Any]],
        *,
        author: dict[str, str] | None = None,
    ) -> str:
        """Commit ``files`` to ``branch`` via the host API; return the new commit SHA.

        ``files`` is a list of ``{"path": str, "content": str, "deleted": bool}``.
        """

    # -- webhook parsing -----------------------------------------------------
    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None:
        """Parse an inbound webhook payload into a :class:`WebhookEvent`, or ``None``.

        The default returns ``None`` (provider does not emit fabrik webhooks).
        """
        return None
