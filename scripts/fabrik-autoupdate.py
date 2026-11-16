#!/usr/bin/env python3
"""fabrik-autoupdate — `main` nachziehen und `fabrik serve` nach Merges neu starten.

Läuft periodisch (systemd-Timer `fabrik-autoupdate.timer`) im Haupt-Checkout:

  1. `git fetch origin main` + `git merge --ff-only origin/main` — nur auf Branch
     `main`; ein nicht fast-forwardbarer Stand (lokale Commits, Konflikte mit
     getrackten Änderungen) bricht ab, nichts wird überschrieben.
  2. Haben sich `pyproject.toml`/`uv.lock` geändert → `uv sync`.
  3. Neustart nötig, wenn sich unter `fabrik/` seit dem Commit geändert hat, mit
     dem der Dienst gestartet wurde (`state/serve-commit`, schreibt die
     `ExecStartPre` von `fabrik.service`). Fehlt die Datei → Neustart.
  4. Neustart nur im Leerlauf: läuft/wartet ein Run (`running`/`queued`), wird
     übersprungen — der nächste Timer-Tick versucht es erneut. Ein Restart
     mitten im Run würde ihn verwaisen lassen (`reap_orphaned_runs` → `error`,
     kein Auto-Retry, ADR 0014). `needs_approval` blockiert nicht (überlebt Restarts).

Exit 0 = aktuell oder neu gestartet oder auf Leerlauf vertagt; != 0 = Fehler
(z. B. ff-only nicht möglich) → sichtbar in `systemctl --user status`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FABRIK_HOME = Path(__file__).resolve().parent.parent
RUNS_DIR = FABRIK_HOME / "runs"
SERVE_COMMIT_FILE = FABRIK_HOME / "state" / "serve-commit"
REMOTE = "origin"
BRANCH = "main"
SERVICE = "fabrik.service"
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")
# Runs, die ein Restart abbrechen bzw. verwerfen würde.
BUSY_STATUSES = frozenset({"running", "queued"})


def run(*cmd: str) -> str:
    """Befehl im fabrik-Verzeichnis ausführen; stdout (gestrippt) oder CalledProcessError."""
    return subprocess.run(cmd, cwd=FABRIK_HOME, check=True, capture_output=True, text=True).stdout.strip()


def git(*args: str) -> str:
    return run("git", *args)


def pull() -> tuple[str, str]:
    """`main` per fast-forward auf `origin/main` ziehen; gibt (HEAD vorher, HEAD nachher) zurück."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        raise RuntimeError(f"Checkout steht auf {branch!r}, nicht {BRANCH!r} — kein Update")
    before = git("rev-parse", "HEAD")
    git("fetch", "--quiet", REMOTE, BRANCH)
    git("merge", "--ff-only", "--quiet", f"{REMOTE}/{BRANCH}")
    return before, git("rev-parse", "HEAD")


def changed(old: str, new: str, *paths: str) -> bool:
    """Gibt es zwischen `old` und `new` Änderungen unter `paths` (relativ zu fabrik/)?"""
    if old == new:
        return False
    return subprocess.run(["git", "diff", "--quiet", old, new, "--", *paths], cwd=FABRIK_HOME).returncode != 0


def serve_commit() -> str | None:
    """Commit, mit dem der laufende Dienst gestartet wurde (None = unbekannt)."""
    try:
        return SERVE_COMMIT_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def busy_runs(runs_dir: Path | None = None) -> list[str]:
    """Run-Ids mit Status aus BUSY_STATUSES (Default: RUNS_DIR); kaputte Records werden ignoriert."""
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
        print(f"[autoupdate] Update fehlgeschlagen: {e} {detail}".strip(), file=sys.stderr)
        return 1
    if before != head:
        print(f"[autoupdate] main: {before[:10]} → {head[:10]}")
        if changed(before, head, *DEPENDENCY_FILES):
            print("[autoupdate] Abhängigkeiten geändert → uv sync")
            run("uv", "sync", "--quiet")
    started = serve_commit()
    if started is not None and not changed(started, head, "."):
        return 0
    busy = busy_runs()
    if busy:
        print(f"[autoupdate] Neustart vertagt — aktive Runs: {', '.join(sorted(busy))}")
        return 0
    print(f"[autoupdate] Neustart {SERVICE} ({(started or '?')[:10]} → {head[:10]})")
    run("systemctl", "--user", "restart", SERVICE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
