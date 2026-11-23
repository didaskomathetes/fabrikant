"""Gemeinsamer Agent-State über parallele Runs (ADR 0028).

Agent-Loops (review-pr, pr-reply) pflegen "bereits verarbeitet" in JSON-Dateien
unter `<workdir>/state/`. Jeder Run hat einen eigenen Worktree, daher liegt die
Quelle der Wahrheit in `config.STATE_DIR/agent/<projekt>/`:

- `seed`: kopiert den gemeinsamen Stand beim Run-Start (bzw. Resume) in den Worktree.
- `merge_back`: überträgt am Run-Ende (bzw. bei der Pause) NUR die Änderungen
  des Runs unter Lock in den gemeinsamen Stand — Updates paralleler Runs
  gehen nicht verloren (Key-weiser Merge statt Überschreiben).

`seed` trägt `<workdir>/state/` außerdem lokal in `info/exclude` ein: Projekte, die
das Verzeichnis nicht selbst ignorieren, würden es sonst per `git add :/` committen."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from . import config, gitops
from .filelock import locked
from .i18n import t

# Dateien (relativ zu `<workdir>/state/`), die zwischen Runs geteilt werden.
SHARED_FILES = ("replied-comments.json", "reviewed-prs.json")
_SNAPSHOT_FILE = ".agent-state-snapshot.json"


class AgentStateError(ValueError):
    """State-Datei ist nicht lesbares JSON-Objekt."""


def shared_dir(project: str) -> Path:
    return config.STATE_DIR / "agent" / project


def _load(p: Path) -> dict:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise AgentStateError(t("agent_state.invalid_agent_state_in", p=p, e=e)) from e
    if not isinstance(data, dict):
        raise AgentStateError(t("agent_state.invalid_agent_state_in_expected", p=p))
    return data


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def seed(project: str, workdir: Path) -> dict[str, dict]:
    """Seeds a new worktree, or preserves a resumed worktree.

    The original seed is kept in the worktree so a resume can merge changes
    made before approval against that baseline, even if the canonical state
    changed while the run was paused.
    """
    state_dir = workdir / "state"
    gitops.exclude_dir_locally(workdir, "state")
    marker = state_dir / _SNAPSHOT_FILE
    stored = _load(marker) if marker.is_file() else {}
    has_stored_snapshot = marker.is_file() and isinstance(stored, dict)
    snapshot: dict[str, dict] = {}
    for name in SHARED_FILES:
        shared = shared_dir(project) / name
        with locked(shared):
            data = _load(shared)
        baseline = stored.get(name, data) if has_stored_snapshot else data
        snapshot[name] = baseline if isinstance(baseline, dict) else data
        local = state_dir / name
        if not has_stored_snapshot:
            if data and not local.exists():
                _write(local, data)
        elif not local.exists() and data:
            # A missing file on resume may be an intentional deletion.
            # Do not recreate it from the current canonical state.
            continue
    if not has_stored_snapshot:
        _write(marker, snapshot)
    return snapshot


def merge_back(project: str, workdir: Path, snapshot: dict[str, dict] | None = None) -> None:
    """Überträgt die Run-Änderungen (lokal vs. Snapshot) unter Lock in den gemeinsamen Stand.

    The snapshot is persisted in the worktree by :func:`seed`, so resumes after
    a process restart retain the baseline from before approval.
    """
    snapshot = snapshot or {}
    for name in SHARED_FILES:
        local_file = workdir / "state" / name
        if not local_file.is_file():
            continue
        local, base = _load(local_file), snapshot.get(name, {})
        shared = shared_dir(project) / name
        with locked(shared):
            merged = _load(shared)
            for k, v in local.items():
                if k not in base or base[k] != v:
                    merged[k] = v
            for k in base.keys() - local.keys():  # lokal gelöscht
                # Do not erase a value written by another run after this
                # run's snapshot; concurrent update/delete conflicts keep the
                # update regardless of merge order.
                if merged.get(k) == base[k]:
                    merged.pop(k, None)
            _write(shared, merged)
