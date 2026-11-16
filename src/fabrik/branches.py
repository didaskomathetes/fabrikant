"""Branch-Overrides: pro Projekt die Ziel-Branch zur Laufzeit wechseln (UI).

Die Spec-YAML bleibt der kanonische Default. Ein Override in
``state/branch-overrides.json`` wird beim Spec-Load darübergelegt, ohne die
YAML-Datei (inkl. Comments) zu reiben. Der Worktree synchronisiert beim
nächsten Run auf ``origin/<branch>`` (gitops.ensure_worktree). ADR 0004.
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
    """Liest die Branch-Overrides (projekt → branch). Defekte Datei → leer."""
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
    """Setzt/aktualisiert den Branch-Override für ein Projekt."""
    with _lock:
        data = load_overrides()
        data[project] = branch
        _write(data)


def clear_override(project: str) -> None:
    """Entfernt den Branch-Override (Projekt fällt auf den Spec-Default zurück)."""
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
    """Legt Branch-Overrides auf geladene Specs (``repo.branch``) — mutiert in place.

    Wird von ``specs.load_projects()`` aufgerufen, damit alle Konsumenten
    (Server, Engine, CLI) den Override sehen. Defekte Specs (``_error``) und
    Projekte ohne ``repo`` bleiben unverändert.
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
    """Lokale + Remote-Branches (ohne ``origin/HEAD``), dedupliziert, sortiert.

    Remote-Namen werden auf den Kurznamen reduziert (``origin/main`` → ``main``),
    damit sich lokale und Remote-Branches nicht doppelt listen. Kein Repo /
    Fehler → leere Liste (UI zeigt dann nur den aktuellen Branch).
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
