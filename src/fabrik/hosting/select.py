"""Provider selection and credential resolution (ADR 0061).

Decides *which* provider a repo/spec talks to and builds the adapter instance.

Selection order (first match wins):

1. an explicit ``hosting:`` spec field (``github`` | ``gitlab``);
2. the repo reference itself — a URL whose host is ``github.com`` → GitHub, any
   other host → GitLab;
3. a project-level ``github:`` field → GitHub;
4. default → GitHub (backwards compatible: existing specs are GitHub).

Credential resolution lives in the adapters: GitHub reads ``GITHUB_TOKEN`` /
``GITHUB_REVIEWER_TOKEN`` (env or token file, ADR 0010); GitLab reads
``FABRIK_GITLAB_TOKEN`` (env) or ``~/.config/gitlab/token`` (see
:func:`fabrik.hosting.gitlab.gitlab_token`).
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from .base import Provider
from .github import GitHubProvider
from .gitlab import GitLabProvider

GITHUB_HOST = "github.com"
GITLAB_HOST_ENV = "FABRIK_GITLAB_HOST"
DEFAULT_GITLAB_HOST = "gitlab.com"
HOSTING_KEY = "hosting"


def _repo_host(repo: str | None) -> str | None:
    """Host of a repo URL, or ``None`` for a bare ``owner/name`` slug."""
    if not repo:
        return None
    repo = repo.strip()
    if repo.startswith(("http://", "https://")):
        return urlsplit(repo).netloc.lower() or None
    if repo.startswith("git@"):
        return repo.partition("@")[2].partition(":")[0].lower() or None
    return None


def _infer_kind(repo: str | None, spec: dict) -> str:
    host = _repo_host(repo)
    if host is not None:
        return "github" if host == GITHUB_HOST else "gitlab"
    if spec.get("github"):
        return "github"
    return "github"


def _gitlab_host(repo: str | None, spec: dict) -> str:
    host = _repo_host(repo)
    if host and host != GITHUB_HOST:
        return host
    return str(spec.get("gitlab_host") or os.environ.get(GITLAB_HOST_ENV, "") or DEFAULT_GITLAB_HOST).strip()


def get_provider(repo: str | None = None, spec: dict | None = None, role: str = "dev") -> Provider:
    """Build the :class:`Provider` for ``repo``/``spec``.

    ``role`` selects the GitHub dual-identity (ADR 0010); it is ignored for GitLab
    (single token). ``spec`` may be ``None`` (e.g. a step that only knows the repo).
    """
    spec = spec or {}
    hosting = str(spec.get(HOSTING_KEY) or "").strip().lower()
    kind = hosting if hosting in ("github", "gitlab") else _infer_kind(repo, spec)
    if kind == "gitlab":
        return GitLabProvider(host=_gitlab_host(repo, spec))
    return GitHubProvider(role=role)
