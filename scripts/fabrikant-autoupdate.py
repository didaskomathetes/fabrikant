#!/usr/bin/env python3
"""fabrikant-autoupdate — pull `main` and restart `fabrikant serve` after merges.

Runs periodically (systemd timer `fabrikant-autoupdate.timer`) in the main checkout:

  1. `git fetch origin main` + `git merge --ff-only origin/main` — only on branch
     `main`; a state that cannot be fast-forwarded (local commits, conflicts with
     tracked changes) aborts, nothing is overwritten.
  2. If `pyproject.toml`/`uv.lock` changed → `uv sync`.
  3. Restart needed if anything under `fabrikant/` changed since the commit with
     which the service was started (`state/serve-commit`, written by the
     `ExecStartPre` of `fabrikant.service`). If the file is missing → restart.
  4. Restart only when idle: if a run is running/waiting (`running`/`queued`), it is
     skipped — the next timer tick tries again. A restart
     in the middle of a run would orphan it (`reap_orphaned_runs` → `error`,
     no auto-retry, ADR 0014). `needs_approval` does not block (survives restarts).

Exit 0 = up to date or restarted or deferred until idle; != 0 = error
(e.g. ff-only not possible) → visible in `systemctl --user status`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FABRIKANT_HOME = Path(__file__).resolve().parent.parent
RUNS_DIR = FABRIKANT_HOME / "runs"
SERVE_COMMIT_FILE = FABRIKANT_HOME / "state" / "serve-commit"
REMOTE = "origin"
BRANCH = "main"
SERVICE = "fabrikant.service"
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")
# Runs that a restart would abort or discard.
BUSY_STATUSES = frozenset({"running", "queued"})


def run(*cmd: str) -> str:
    """Run a command in the fabrikant directory; stdout (stripped) or CalledProcessError."""
    return subprocess.run(cmd, cwd=FABRIKANT_HOME, check=True, capture_output=True, text=True).stdout.strip()


def git(*args: str) -> str:
    return run("git", *args)


def pull() -> tuple[str, str]:
    """Fast-forward `main` to `origin/main`; returns (HEAD before, HEAD after)."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        raise RuntimeError(f"Checkout is on {branch!r}, not {BRANCH!r} — no update")
    before = git("rev-parse", "HEAD")
    git("fetch", "--quiet", REMOTE, BRANCH)
    git("merge", "--ff-only", "--quiet", f"{REMOTE}/{BRANCH}")
    return before, git("rev-parse", "HEAD")


def changed(old: str, new: str, *paths: str) -> bool:
    """Are there changes between `old` and `new` under `paths` (relative to fabrikant/)?"""
    if old == new:
        return False
    return subprocess.run(["git", "diff", "--quiet", old, new, "--", *paths], cwd=FABRIKANT_HOME).returncode != 0


def serve_commit() -> str | None:
    """Commit with which the running service was started (None = unknown)."""
    try:
        return SERVE_COMMIT_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def busy_runs(runs_dir: Path | None = None) -> list[str]:
    """Run IDs with a status from BUSY_STATUSES (default: RUNS_DIR); broken records are ignored."""
    busy = []
    for f in (runs_dir or RUNS_DIR).glob("*/*.json"):
        try:
            rec = json.loads(f.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict) and rec.get("status") in BUSY_STATUSES:
            busy.append(f.stem)
    return busy


def main() -> int:
    try:
        before, head = pull()
    except (RuntimeError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", "") or ""
        print(f"[autoupdate] Update failed: {e} {detail}".strip(), file=sys.stderr)
        return 1
    if before != head:
        print(f"[autoupdate] main: {before[:10]} → {head[:10]}")
        if changed(before, head, *DEPENDENCY_FILES):
            print("[autoupdate] Dependencies changed → uv sync")
            run("uv", "sync", "--quiet")
    started = serve_commit()
    if started is not None and not changed(started, head, "."):
        return 0
    busy = busy_runs()
    if busy:
        print(f"[autoupdate] Restart deferred — active runs: {', '.join(sorted(busy))}")
        return 0
    print(f"[autoupdate] Restarting {SERVICE} ({(started or '?')[:10]} → {head[:10]})")
    run("systemctl", "--user", "restart", SERVICE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
