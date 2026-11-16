"""Step: git (fetch/branch/commit/push, geschützte Branches).

`commit` auf einem sauberen Tree ist ein erfolgreicher No-Op
("nothing to commit") — idempotente Re-Runs/Resumes brechen nicht ab.

`push` geht je nach Projekt (`commit_via`) per `git push` oder als von GitHub
signierte API-Commits (`apicommit`).

`commit.add` sind Git-Pathspecs relativ zum workdir (bei Monorepos der
Projekt-Subpfad!). Default ist `:/` = ganzes Repo. Bleiben nach dem Staging
Änderungen übrig (z. B. `.github/` außerhalb von `.`), schlägt der Step fehl,
statt sie still zu verwerfen (Issue #83 / PR #201).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import config
from ..apicommit import push_branch
from ..context import _substitute, missing_vars
from ..failures import GitError, failed
from ..gitops import repo_lock
from ..runmodel import FailureKind
from ..secrets import _redact
from ..validation import validate_branch_name

# Pathspec-Magic für die Repo-Wurzel — unabhängig vom cwd (Subpfad-Worktrees).
STAGE_ALL = ":/"


def _git(ctx: dict, base: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(base), *args], capture_output=True, text=True, timeout=300)
    if check and p.returncode != 0:
        output = p.stderr.strip() or p.stdout.strip()
        raise GitError(f"git {' '.join(args)}: {_redact(output)}", output)
    return (p.stdout or p.stderr).strip()


def _nothing_staged(base: Path) -> bool:
    """True, wenn der Index nichts zu committen hat (Exit-Code, locale-unabhängig)."""
    p = subprocess.run(["git", "-C", str(base), "diff", "--cached", "--quiet"], capture_output=True, timeout=300)
    return p.returncode == 0


def _unstaged_paths(ctx: dict, base: Path) -> list[str]:
    """Repo-relative Pfade mit ungestagten oder untracked Änderungen (ohne Ignored)."""
    changed = _git(ctx, base, "diff", "--name-only", "--", STAGE_ALL)
    untracked = _git(ctx, base, "ls-files", "--others", "--exclude-standard", "--full-name", "--", STAGE_ALL)
    return sorted({p for p in (changed + "\n" + untracked).splitlines() if p})


def _step_git(ctx: dict, step: dict) -> dict:
    base = Path(ctx["workdir"])
    summary_parts = []
    if step.get("fetch"):
        with repo_lock(base):  # gemeinsames Repo: nicht parallel zu worktree add (Issue #460)
            _git(ctx, base, "fetch", "origin")
        summary_parts.append("fetch")
    if step.get("branch") and (missing := missing_vars(step["branch"], ctx)):
        # Typisch ein manueller Run ohne Trigger-Variablen — klar benennen statt "unzulässige Zeichen".
        hint = " ".join(f"--var {name}=…" for name in missing)
        raise ValueError(f"Branch {step['branch']!r}: Variable(n) fehlen: {', '.join(missing)} (manuell: {hint})")
    branch = _substitute(step["branch"], ctx) if step.get("branch") else None
    if branch:
        validate_branch_name(branch)
        # Startpunkt-Priorität: origin/<branch> (track, Remote-Ref existiert)
        # → lokaler Branch (existiert) → HEAD (NEUER Branch aus der Base).
        # check=False: fehlender Ref ist exit 1 — Normalfall, kein Gate.
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
                f"BLOCKED: {len(leftover)} Änderung(en) außerhalb von commit.add "
                f"nicht gestaged: {', '.join(leftover[:5])}",
                FailureKind.ENVIRONMENT,  # commit.add der Spec deckt die Änderungen nicht ab
                output="\n".join(leftover),
            )
        if _nothing_staged(base):
            # Clean tree (idempotenter Re-Run/Resume): No-Op, kein Fehler.
            summary_parts.append("commit (no-op: nothing to commit)")
        else:
            _git(ctx, base, "commit", "-m", _substitute(commit["message"], ctx))
            summary_parts.append("commit")
    if step.get("push"):
        protected = step.get("protected") or config.PROTECTED_DEFAULT
        cur = _git(ctx, base, "rev-parse", "--abbrev-ref", "HEAD")
        if cur in protected or cur == "HEAD":
            return failed(
                f"BLOCKED: push auf geschützten Branch '{cur}' verweigert", FailureKind.ENVIRONMENT, output=""
            )
        validate_branch_name(cur)
        summary_parts.append(push_branch(base, cur, protected=protected))
    return {"ok": True, "summary": ", ".join(summary_parts) or "kein-op"}
