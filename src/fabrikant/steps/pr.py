"""Step: pr (create a PR) — via the hosting provider (ADR 0062).

Idempotent (duplicate protection): if an open PR already exists for the head
branch, it is reused — GitHub allows only one open PR per head branch
anyway, so the create call would fail. Important for re-runs/resumes of the same issue.

Empty head: if `head` has no commits ahead of `base` (e.g. the agent found nothing to
do, `ship` was a no-op), the host rejects the create with 422. The step checks
this beforehand via the compare API and then ends the run successfully (`done`)
instead of with an error — subsequent steps (review, merge) would have no PR.

Labels: `POST /pulls` has no labels parameter (GitHub silently
ignores it) — they are set afterwards via the issues endpoint, also
for reused PRs (additive, idempotent; heals PRs without labels).

The step talks to the hosting provider (ADR 0062), not the GitHub API directly:
GitHub and GitLab use the same path.

Step def:
    type: pr
    repo: <required, "owner/name">
    base: <optional, default: main>
    head: <optional, default: current branch>
    title: <optional, {vars}-substituted>
    body: <optional, {vars}-substituted>
    labels: <optional, list; e.g. [fabrikant] — review loops often filter by label>
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..context import _substitute
from ..failures import ConfigError
from ..hosting import Provider, PullRequest, get_provider
from ..i18n import t
from ..validation import validate_branch_name
from .git import _git


def _step_pr(ctx: dict, step: dict) -> dict:
    repo_cfg = step.get("repo")  # "owner/name"
    if not repo_cfg:
        raise ConfigError(t("steps.pr.pr_step_repo_owner_name"))
    provider = get_provider(repo_cfg)
    base_branch = _substitute(step.get("base", "main"), ctx)
    head_branch = (
        _substitute(step["head"], ctx)
        if step.get("head")
        else _git(ctx, Path(ctx["workdir"]), "rev-parse", "--abbrev-ref", "HEAD")
    )
    validate_branch_name(base_branch)
    validate_branch_name(head_branch)
    # Duplicate check before create: open PR with the same head branch →
    # reuse (re-runs/resumes do not create a second PR).
    labels = _normalize_labels(step.get("labels") or [])
    existing = _find_open_pr(provider, repo_cfg, head_branch)
    if existing is not None:
        _export_pr(ctx, existing)
        provider.add_labels(repo_cfg, existing.number, labels)
        return {
            "ok": True,
            "summary": t("steps.pr.pr_already_exists_head_reused", v1=existing.number, head_branch=head_branch),
        }
    if _commits_ahead(provider, repo_cfg, base_branch, head_branch) == 0:
        return {
            "ok": True,
            "done": True,
            "summary": t("steps.pr.no_commits_on_compared_to", head_branch=head_branch, base_branch=base_branch),
        }
    title = _substitute(step.get("title", "fabrikant: {loop} {run_id}"), ctx)
    body = _substitute(step.get("body", t("steps.pr.created_by_fabrikant_loop_trigger")), ctx)
    pr = provider.create_pr(repo_cfg, title=title, head=head_branch, base=base_branch, body=body)
    _export_pr(ctx, pr)
    provider.add_labels(repo_cfg, pr.number, labels)
    return {"ok": True, "summary": f"PR #{pr.number} {pr.url}"}


def _export_pr(ctx: dict, pr: PullRequest) -> None:
    """PR data for subsequent steps (pr_review, pr_merge, human_gate) into the ctx:
    ``pr_number``, ``pr_url`` and ``pr_title`` (approval context, ADR 0011)."""
    ctx["pr_number"] = str(pr.number)
    if pr.url:
        ctx["pr_url"] = pr.url
    if pr.title:
        ctx["pr_title"] = pr.title


def _find_open_pr(provider: Provider, repo_cfg: str, head_branch: str) -> PullRequest | None:
    """Find the first open PR with head branch `<owner>:<head_branch>`.

    Deterministic duplicate check before create (GitHub allows exactly
    one open PR per head branch). None if none exists.
    """
    owner = repo_cfg.split("/", 1)[0]
    prs = provider.list_prs(repo_cfg, head=f"{owner}:{head_branch}", state="open")
    return prs[0] if prs else None


def _commits_ahead(provider: Provider, repo_cfg: str, base_branch: str, head_branch: str) -> int | None:
    """Number of commits of `head_branch` ahead of `base_branch` (compare API), None if unknown.

    HTTP errors (e.g. 404 because the head branch does not exist on the remote)
    → None: the create path then delivers the clearer host error message."""

    try:
        return provider.compare(repo_cfg, base_branch, head_branch).ahead_by
    except Exception:  # noqa: BLE001 — Provider-Fehler → Create-Pfad entscheidet
        return None


def _normalize_labels(labels: Iterable[Any]) -> list[str]:
    """labels config → API list: as strings, empties filtered, deduplicated.

    Order is preserved; non-string values (e.g. numbers, None,
    dicts from YAML) are coerced via `str()`. Empty list → no
    labels call.
    """
    seen: set[str] = set()
    out: list[str] = []
    for x in labels:
        s = str(x).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out
