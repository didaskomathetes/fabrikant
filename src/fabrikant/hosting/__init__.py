"""Hosting-provider abstraction (ADR 0062, ADR 0063).

The port steps and triggers call to talk to a code-hosting service (GitHub or
self-hosted GitLab, or a third-party host registered via the provider registry).
Public API:

- :class:`Provider` — the port (union of the operations steps/triggers need).
- :class:`BaseProvider` — the recommended base class for new adapters (defaults the
  optional operations to :class:`HostingNotSupported`).
- :class:`GitHubProvider` / :class:`GitLabProvider` — the built-in adapters.
- :func:`get_provider` — selection + credential resolution.
- :func:`register_provider` / :func:`get_factory` / :func:`registered_providers` —
  the provider registry (ADR 0063) for adding custom hosts.
- the normalized dataclasses (:class:`Issue`, :class:`PullRequest`, …).
"""

from __future__ import annotations

from .base import (
    GITHUB_HOST,
    BaseProvider,
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
    repo_host,
)
from .github import GitHubProvider
from .gitlab import GitLabProvider, gitlab_token
from .registry import ENTRY_POINT_GROUP, get_factory, load_entry_points, register_provider, registered_providers
from .select import current_spec, get_provider, spec_context

# Discover third-party providers (``fabrikant.hosting_provider`` entry point). Built-ins
# already self-registered at import above; this adds any installed plugins.
load_entry_points()

__all__ = [
    "ENTRY_POINT_GROUP",
    "GITHUB_HOST",
    "BaseProvider",
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
    "get_factory",
    "get_provider",
    "gitlab_token",
    "load_entry_points",
    "register_provider",
    "registered_providers",
    "repo_host",
    "spec_context",
]
