"""Branch overrides: switch a project's target branch at runtime (UI).

The spec YAML stays the canonical default. An override in
``state/branch-overrides.json`` is laid over it at spec load, without touching the
YAML file (incl. comments). The worktree syncs to ``origin/<branch>`` on the
next run (gitops.ensure_worktree). ADR 0004.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()


def _overrides_path() -> Path:
    return config.STATE_DIR / "branch-overrides.json"


def load_overrides() -> dict[str, str]:
    """Read the branch overrides (project → branch). Broken file → empty."""
    p = _overrides_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}


def save_override(project: str, branch: str) -> None:
    """Sets/updates the branch override for a project."""
    with _lock:
        data = load_overrides()
        data[project] = branch
        _write(data)


def clear_override(project: str) -> None:
    """Removes the branch override (the project falls back to the spec default)."""
    with _lock:
        data = load_overrides()
        if project in data:
            del data[project]
            _write(data)


def _write(data: dict[str, str]) -> None:
    p = _overrides_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def apply_overrides(specs: dict[str, dict]) -> dict[str, dict]:
    """Lays branch overrides onto loaded specs (``repo.branch``) — mutates in place.

    Called by ``specs.load_projects()`` so all consumers
    (server, engine, CLI) see the override. Broken specs (``_error``) and
    projects without ``repo`` stay unchanged.
    """
    ov = load_overrides()
    if not ov:
        return specs
    for name, spec in specs.items():
        if spec.get("_error") or name not in ov:
            continue
        repo = spec.get("repo")
        if isinstance(repo, dict):
            repo["branch"] = ov[name]
    return specs


def list_branches(repo_root: str | Path) -> list[str]:
    """Local + remote branches (without ``origin/HEAD``), deduplicated, sorted.

    Remote names are reduced to the short name (``origin/main`` → ``main``),
    so local and remote branches are not listed twice. No repo /
    error → empty list (the UI then shows only the current branch).
    """
    p = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "-a", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if p.returncode != 0:
        return []
    seen: dict[str, None] = {}
    for line in p.stdout.splitlines():
        b = line.strip()
        if not b or b.endswith("/HEAD"):
            continue
        seen.setdefault(b.removeprefix("origin/"), None)
    return sorted(seen)
