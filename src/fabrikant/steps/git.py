"""Step: git (fetch/branch/commit/push, protected branches).

`commit` on a clean tree is a successful no-op
("nothing to commit") — idempotent re-runs/resumes do not abort.

`push` goes, depending on the project (`commit_via`), via `git push` or as
GitHub-signed API commits (`apicommit`).

`commit.add` are git pathspecs relative to the workdir (for monorepos the
project subpath!). Default is `:/` = whole repo. If changes remain after
staging (e.g. `.github/` outside `.`), the step fails instead of
silently discarding them (issue #83 / PR #201).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import config, netretry
from ..apicommit import push_branch
from ..context import _substitute, missing_vars
from ..failures import GitError, failed
from ..gitops import repo_lock
from ..i18n import t
from ..runmodel import FailureKind
from ..secrets import _redact
from ..validation import validate_branch_name

# Pathspec magic for the repo root — independent of cwd (subpath worktrees).
STAGE_ALL = ":/"


# git subcommands with network access: retry on transient errors (netretry).
_NET_SUBCOMMANDS = frozenset({"fetch", "push", "pull"})


def _git(ctx: dict, base: Path, *args: str, check: bool = True) -> str:
    cmd = ["git", "-C", str(base), *args]
    if args and args[0] in _NET_SUBCOMMANDS:
        p = netretry.run_git(cmd, timeout=300)
    else:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if check and p.returncode != 0:
        output = p.stderr.strip() or p.stdout.strip()
        raise GitError(f"git {' '.join(args)}: {_redact(output)}", output)
    return (p.stdout or p.stderr).strip()


def _nothing_staged(base: Path) -> bool:
    """True if the index has nothing to commit (exit code, locale-independent)."""
    p = subprocess.run(["git", "-C", str(base), "diff", "--cached", "--quiet"], capture_output=True, timeout=300)
    return p.returncode == 0


def _unstaged_paths(ctx: dict, base: Path) -> list[str]:
    """Repo-relative paths with unstaged or untracked changes (without ignored)."""
    changed = _git(ctx, base, "diff", "--name-only", "--", STAGE_ALL)
    untracked = _git(ctx, base, "ls-files", "--others", "--exclude-standard", "--full-name", "--", STAGE_ALL)
    return sorted({p for p in (changed + "\n" + untracked).splitlines() if p})


def _step_git(ctx: dict, step: dict) -> dict:
    base = Path(ctx["workdir"])
    summary_parts = []
    if step.get("fetch"):
        with repo_lock(base):  # shared repo: not parallel to worktree add (issue #460)
            _git(ctx, base, "fetch", "origin")
        summary_parts.append("fetch")
    if step.get("branch") and (missing := missing_vars(step["branch"], ctx)):
        # Typically a manual run without trigger variables — name it clearly instead of "illegal characters".
        hint = " ".join(f"--var {name}=…" for name in missing)
        raise ValueError(f"Branch {step['branch']!r}: variable(s) missing: {', '.join(missing)} (manual: {hint})")
    branch = _substitute(step["branch"], ctx) if step.get("branch") else None
    if branch:
        validate_branch_name(branch)
        # Start point priority: origin/<branch> (track, remote ref exists)
        # → local branch (exists) → HEAD (NEW branch from the base).
        # check=False: a missing ref is exit 1 — normal case, no gate.
        start_point = "HEAD"
        if _git(ctx, base, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
            start_point = branch
        if step.get("track", True) and _git(
            ctx, base, "rev-parse", "--verify", "--quiet", f"origin/{branch}", check=False
        ):
            start_point = f"origin/{branch}"
        _git(ctx, base, "checkout", "-B", branch, start_point)
        summary_parts.append(f"branch {branch}")
    commit = step.get("commit")
    if commit:
        for target in commit.get("add", [STAGE_ALL]):
            _git(ctx, base, "add", target)
        leftover = _unstaged_paths(ctx, base)
        if leftover:
            return failed(
                t("steps.git.blocked_change_s_outside_commit", v1=len(leftover), v2=", ".join(leftover[:5])),
                FailureKind.ENVIRONMENT,  # the spec's commit.add does not cover the changes
                output="\n".join(leftover),
            )
        if _nothing_staged(base):
            # Clean tree (idempotent re-run/resume): no-op, no error.
            summary_parts.append("commit (no-op: nothing to commit)")
        else:
            _git(ctx, base, "commit", "-m", _substitute(commit["message"], ctx))
            summary_parts.append("commit")
    if step.get("push"):
        protected = step.get("protected") or config.PROTECTED_DEFAULT
        cur = _git(ctx, base, "rev-parse", "--abbrev-ref", "HEAD")
        if cur in protected or cur == "HEAD":
            return failed(t("steps.git.blocked_push_to_protected_branch", cur=cur), FailureKind.ENVIRONMENT, output="")
        validate_branch_name(cur)
        summary_parts.append(push_branch(base, cur, protected=protected))
    return {"ok": True, "summary": ", ".join(summary_parts) or t("steps.git.no_op")}
