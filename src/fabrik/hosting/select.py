"""Provider selection and credential resolution (ADR 0062, ADR 0063).

Decides *which* provider a repo/spec talks to and builds the adapter instance.

Selection order (first match wins):

1. an explicit ``hosting:`` spec field — the name of any *registered* provider
   (``github``, ``gitlab``, or a third-party one, ADR 0063);
2. the repo reference itself — a URL whose host is ``github.com`` → GitHub, any
   other host → GitLab;
3. a project-level ``github:`` field → GitHub;
4. default → GitHub (backwards compatible: existing specs are GitHub).

The name is resolved to a factory via the :mod:`fabrik.hosting.registry`; the factory
builds the adapter. Credential resolution lives in the adapters: GitHub reads
``GITHUB_TOKEN`` / ``GITHUB_REVIEWER_TOKEN`` (env or token file, ADR 0010); GitLab
reads ``FABRIK_GITLAB_TOKEN`` (env) or ``~/.config/gitlab/token`` (see
:func:`fabrik.hosting.gitlab.gitlab_token`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .base import GITHUB_HOST, Provider, repo_host
from .registry import get_factory, registered_providers

HOSTING_KEY = "hosting"

# Active run spec, set by the engine for the duration of a run (like ``gitops.project_github``).
# Steps resolve the provider from it without the spec being threaded through every call.
_spec: ContextVar[dict | None] = ContextVar("fabrik_hosting_spec", default=None)


@contextmanager
def spec_context(spec: dict | None) -> Iterator[None]:
    """Bind the active run spec for provider selection (entered by the engine per run)."""
    token = _spec.set(spec)
    try:
        yield
    finally:
        _spec.reset(token)


def current_spec() -> dict:
    """The active run spec (``{}`` outside a run)."""
    return _spec.get() or {}


def _infer_kind(repo: str | None, spec: dict) -> str:
    """Infer the provider name from the repo host (or default to GitHub)."""
    host = repo_host(repo)
    if host is not None:
        return "github" if host == GITHUB_HOST else "gitlab"
    if spec.get("github"):
        return "github"
    return "github"


def get_provider(repo: str | None = None, spec: dict | None = None, role: str = "dev") -> Provider:
    """Build the :class:`Provider` for ``repo``/``spec``.

    The provider name comes from the ``hosting:`` spec field (any *registered*
    provider, ADR 0063) or is inferred from the repo host. The name is resolved to a
    factory via the registry, which builds the adapter.

    ``role`` selects the GitHub dual-identity (ADR 0010); it is ignored by adapters
    that use a single token (e.g. GitLab). ``spec`` may be ``None`` — then the active
    run spec (bound by the engine via :func:`spec_context`) is used.
    """
    spec = spec if spec is not None else current_spec()
    hosting = str(spec.get(HOSTING_KEY) or "").strip().lower()
    kind = hosting if hosting in registered_providers() else _infer_kind(repo, spec)
    return get_factory(kind)(repo, spec, role)
