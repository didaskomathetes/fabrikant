"""Step: pr (create a GitHub PR).

Idempotent (duplicate protection): if an open PR already exists for the head
branch, it is reused — GitHub allows only one open PR per head branch
anyway, so the create call would fail. Important for re-runs/resumes of the same issue.

Empty head: if `head` has no commits ahead of `base` (e.g. the agent found nothing to
do, `ship` was a no-op), GitHub rejects the create with 422. The step checks
this beforehand via the compare API and then ends the run successfully (`done`)
instead of with an error — subsequent steps (review, merge) would have no PR.

Labels: `POST /pulls` has no labels parameter (GitHub silently
ignores it) — they are set afterwards via the issues endpoint, also
for reused PRs (additive, idempotent; heals PRs without labels).

Step def:
    type: pr
    repo: <required, "owner/name">
    base: <optional, default: main>
    head: <optional, default: current branch>
    title: <optional, {vars}-substituted>
    body: <optional, {vars}-substituted>
    labels: <optional, list; e.g. [fabrik] — review loops often filter by label>
"""

from __future__ import annotations

import urllib.error
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..context import _substitute
from ..failures import ConfigError
from ..gitops import _gh_api, _gh_token
from ..i18n import t
from ..validation import validate_branch_name
from .git import _git


def _step_pr(ctx: dict, step: dict) -> dict:
    repo_cfg = step.get("repo")  # "owner/name"
    if not repo_cfg:
        raise ConfigError(t("steps.pr.pr_step_repo_owner_name"))
    token = _gh_token()
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
    existing = _find_open_pr(repo_cfg, head_branch, token)
    if existing is not None:
        _export_pr(ctx, existing)
        _add_labels(repo_cfg, existing["number"], labels, token)
        return {
            "ok": True,
            "summary": t("steps.pr.pr_already_exists_head_reused", v1=existing["number"], head_branch=head_branch),
        }
    if _commits_ahead(repo_cfg, base_branch, head_branch, token) == 0:
        return {
            "ok": True,
            "done": True,
            "summary": t("steps.pr.no_commits_on_compared_to", head_branch=head_branch, base_branch=base_branch),
        }
    title = _substitute(step.get("title", "fabrik: {loop} {run_id}"), ctx)
    body = _substitute(step.get("body", t("steps.pr.created_by_fabrik_loop_trigger")), ctx)
    payload: dict[str, Any] = {"title": title, "head": head_branch, "base": base_branch, "body": body}
    res = _gh_api("POST", f"https://api.github.com/repos/{repo_cfg}/pulls", token, payload)
    if res.get("number") is not None:
        _export_pr(ctx, res)
        _add_labels(repo_cfg, res["number"], labels, token)
    return {"ok": True, "summary": f"PR #{res.get('number')} {res.get('html_url')}"}


def _export_pr(ctx: dict, pr: dict) -> None:
    """PR data for subsequent steps (pr_review, pr_merge, human_gate) into the ctx:
    ``pr_number``, ``pr_url`` and ``pr_title`` (approval context, ADR 0011)."""
    ctx["pr_number"] = str(pr["number"])
    if pr.get("html_url"):
        ctx["pr_url"] = pr["html_url"]
    if pr.get("title"):
        ctx["pr_title"] = pr["title"]


def _add_labels(repo_cfg: str, number: Any, labels: list[str], token: str) -> None:
    """Attach labels to PR #number (issues API; PRs are issues). Empty → no call."""
    if labels:
        _gh_api("POST", f"https://api.github.com/repos/{repo_cfg}/issues/{number}/labels", token, {"labels": labels})


def _find_open_pr(repo_cfg: str, head_branch: str, token: str) -> dict | None:
    """Find the first open PR with head branch `<owner>:<head_branch>`.

    Deterministic duplicate check before create (GitHub allows exactly
    one open PR per head branch). None if none exists.
    """
    owner = repo_cfg.split("/", 1)[0]
    res = _gh_api(
        "GET",
        f"https://api.github.com/repos/{repo_cfg}/pulls?head={quote(f'{owner}:{head_branch}')}&state=open",
        token,
    )
    if isinstance(res, list) and res:
        return res[0]
    return None


def _commits_ahead(repo_cfg: str, base_branch: str, head_branch: str, token: str) -> int | None:
    """Number of commits of `head_branch` ahead of `base_branch` (compare API), None if unknown.

    Branch names are fully percent-encoded (`safe=""`): a `/` in the
    name (e.g. `release/1.0`) would otherwise be a second path segment in the
    compare path → 404. HTTP errors (e.g. 404 because the head branch does not exist
    on the remote) → None: the create path then delivers the clearer
    GitHub error message."""
    try:
        res = _gh_api(
            "GET",
            f"https://api.github.com/repos/{repo_cfg}/compare/{quote(base_branch, safe='')}...{quote(head_branch, safe='')}",
            token,
        )
    except urllib.error.HTTPError:
        return None
    ahead = res.get("ahead_by") if isinstance(res, dict) else None
    return ahead if type(ahead) is int else None


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
