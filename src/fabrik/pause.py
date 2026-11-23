"""Pause/Disable pro Projekt und Loop (ADR 0019).

Zustand: `active` | `paused` | `disabled`, optional mit `until` (Auto-Resume).
Persistiert in ``state/run-states.json`` (nicht in der Spec → überlebt Restarts
und Spec-Änderungen). Jede Änderung landet im Audit-Log
``state/run-state-changes.jsonl`` (wer, wann, von, zu, until).

- `paused`: Scheduler-Trigger feuern nicht; manuelle Runs bleiben erlaubt.
- `disabled`: wie `paused`, zusätzlich keine manuellen Runs.
- Projekt-Zustand schlägt Loop-Zustand (Projekt != active → gilt für alle Loops).
- Laufende Runs bleiben unberührt; eingereihte, noch nicht gestartete Runs
  prüft die Engine beim Start erneut (Trigger: paused/disabled, manuell
  inklusive Resume: disabled).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from . import config
from .i18n import t
from .specs import load_projects

ACTIVE, PAUSED, DISABLED = "active", "paused", "disabled"
STATES = (ACTIVE, PAUSED, DISABLED)

_lock = threading.Lock()


class UnknownTargetError(ValueError):
    """Project or loop does not exist (HTTP 404 in the API)."""


def _states_path() -> Path:
    return config.STATE_DIR / "run-states.json"


def _audit_path() -> Path:
    return config.STATE_DIR / "run-state-changes.jsonl"


def _key(project: str, loop: str | None) -> str:
    return f"{project}/{loop}" if loop else project


def _load() -> dict[str, dict]:
    """Liest die Zustände (key → {state, until?}). Defekte Datei → leer."""
    p = _states_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("state") in (PAUSED, DISABLED)}


def _save(data: dict[str, dict]) -> None:
    p = _states_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(p)  # atomar: kein halb geschriebener Zustand nach Crash


def _expired(entry: dict, now: datetime) -> bool:
    until = entry.get("until")
    if not until:
        return False
    try:
        dt = datetime.fromisoformat(until)
    except (TypeError, ValueError):
        return False  # defektes until (falscher Typ/Format) → bleibt gesetzt (sicherer als Auto-Resume)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)  # wie parse_until: ohne Zeitzone gilt UTC
    return dt <= now


def _audit(actor: str, project: str, loop: str | None, old: str, new: str, until: str | None) -> None:
    rec = {
        "ts": config.utcnow().isoformat(),
        "actor": actor,
        "project": project,
        "loop": loop,
        "from": old,
        "to": new,
        "until": until,
    }
    _audit_path().parent.mkdir(parents=True, exist_ok=True)
    with _audit_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    scope = _key(project, loop)
    print(f"[fabrik] State {scope}: {old} → {new}" + (f" bis {until}" if until else "") + t("pause.by", actor=actor))


def parse_until(raw: str | None) -> str | None:
    """ISO-8601-Zeitstempel → UTC-ISO-String; ohne Zeitzone gilt UTC. Muss in der Zukunft liegen."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError as e:
        raise ValueError(t("pause.until_invalid_iso_8601_timestamp", raw=raw)) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    if dt <= config.utcnow():
        raise ValueError(t("pause.until_is_not_in_the"))
    return dt.isoformat()


def get_state(project: str, loop: str | None = None) -> dict:
    """Eigener (nicht effektiver) Zustand: {"state", "until"}; abgelaufene `until` zählen als active."""
    entry = _load().get(_key(project, loop))
    if not entry or _expired(entry, config.utcnow()):
        return {"state": ACTIVE, "until": None}
    return {"state": entry["state"], "until": entry.get("until")}


def effective_state(project: str, loop: str) -> str:
    """Wirksamer Zustand eines Loops: Projekt-Zustand schlägt Loop-Zustand."""
    proj = get_state(project)["state"]
    return proj if proj != ACTIVE else get_state(project, loop)["state"]


def trigger_blocked(project: str, loop: str) -> bool:
    """True, wenn Trigger (Scheduler) für den Loop nicht feuern dürfen."""
    return effective_state(project, loop) != ACTIVE


def manual_block_reason(project: str, loop: str) -> str | None:
    """Begründung, falls ein manueller Run verboten ist (nur `disabled`), sonst None."""
    if effective_state(project, loop) == DISABLED:
        return t("pause.is_disabled_no_manual_runs", v1=_key(project, loop))
    return None


def set_state(project: str, loop: str | None, state: str, *, until: str | None = None, actor: str = "unknown") -> dict:
    """Setzt den Zustand von Projekt (loop=None) oder Loop; protokolliert die Änderung.

    Raises ValueError bei unbekanntem Projekt/Loop/State oder ungültigem `until`.
    """
    if state not in STATES:
        raise ValueError(t("pause.state_must_be_one_of", v1=", ".join(STATES)))
    spec = load_projects().get(project)
    if not spec or spec.get("_error"):
        raise UnknownTargetError(t("pause.unknown_project", project=project))
    if loop and loop not in (spec.get("loops") or {}):
        raise UnknownTargetError(t("pause.unknown_loop", loop=loop))
    if state == ACTIVE and until:
        raise ValueError(t("pause.until_is_only_allowed_with"))
    until_iso = parse_until(until)
    with _lock:
        data = _load()
        key = _key(project, loop)
        old_entry = data.get(key)
        old = old_entry["state"] if old_entry and not _expired(old_entry, config.utcnow()) else ACTIVE
        if state == ACTIVE:
            data.pop(key, None)
        else:
            data[key] = {"state": state, **({"until": until_iso} if until_iso else {})}
        _save(data)
        _audit(actor, project, loop, old, state, until_iso)
    return {"project": project, "loop": loop, "state": state, "until": until_iso}


def resume_expired() -> list[str]:
    """Entfernt Zustände mit abgelaufenem `until` (Auto-Resume) und protokolliert sie. Gibt die Keys zurück."""
    now = config.utcnow()
    with _lock:
        data = _load()
        due = [k for k, v in data.items() if _expired(v, now)]
        for k in due:
            entry = data.pop(k)
            project, _, loop = k.partition("/")
            _audit("auto-resume", project, loop or None, entry["state"], ACTIVE, None)
        if due:
            _save(data)
    return due
