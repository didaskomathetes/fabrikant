"""Hosting-provider abstraction (ADR 0062).

The port steps and triggers call to talk to a code-hosting service (GitHub or
self-hosted GitLab). Public API:

- :class:`Provider` — the port (union of the operations steps/triggers need).
- :class:`GitHubProvider` / :class:`GitLabProvider` — the adapters.
- :func:`get_provider` — selection + credential resolution.
- the normalized dataclasses (:class:`Issue`, :class:`PullRequest`, …).
"""

from __future__ import annotations

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
from .github import GitHubProvider
from .gitlab import GitLabProvider, gitlab_token
from .select import current_spec, get_provider, spec_context

__all__ = [
    "Comment",
    "CompareResult",
    "FileChange",
    "GitHubProvider",
    "GitLabProvider",
    "HostingConfigError",
    "HostingError",
    "HostingNotSupported",
    "Issue",
    "Provider",
    "PullRequest",
    "RepoRef",
    "WebhookEvent",
    "current_spec",
    "get_provider",
    "gitlab_token",
    "spec_context",
]
