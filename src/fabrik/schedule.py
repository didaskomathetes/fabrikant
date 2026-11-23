"""Schedule-View-Backend: nächste Cron-Runs + One-time-Overrides.

- `next_cron_times()` = :func:`fabrik.cron.next_times` (dieselbe Cron-Semantik wie
  der Scheduler, gemeinsamer Code).
- `parse_when()` übersetzt UI-Eingaben ("15:05", "in 10 min", ISO-8601) in ein lokales
  DateTime in der Zukunft.
- One-time-Overrides (einmalige Runs) liegen in `state/onetime-runs.json` (atomic,
  Pattern wie `pause.py`); der Scheduler feuert fällige Einträge in `tick()` wie
  manuelle Runs (ADR 0019: `paused` blockiert manuelle Runs nicht, `disabled` schon).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import config, cron
from .i18n import t
from .specs import load_projects

ONETIME_FILE = "onetime-runs.json"
# Start-Fehler eines fälligen One-time: Retry nach Backoff, höchstens so oft.
ONETIME_RETRY_DELAY = timedelta(minutes=1)
ONETIME_MAX_ATTEMPTS = 5

_lock = threading.Lock()

_WHEN_REL_RE = re.compile(
    r"^in\s+(\d+)\s*(sec|secs|sek|s|min|mins|minute|minuten|m|h|hr|std|stdn|stunde|stunden|d|day|days|tag|tage)$",
    re.IGNORECASE,
)
_WHEN_REL_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "sek": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minuten": 60,
    "h": 3600,
    "hr": 3600,
    "std": 3600,
    "stdn": 3600,
    "stunde": 3600,
    "stunden": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "tag": 86400,
    "tage": 86400,
}
_WHEN_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


# ---------------------------------------------------------------- Cron-Ausblick

next_cron_times = cron.next_times


class NotFoundError(ValueError):
    """Unbekanntes Projekt bzw. unbekannter Loop (API: 404 statt 400)."""


# ---------------------------------------------------------------- When-Parsing


def _local_naive(dt: datetime) -> datetime:
    """Mit Timezone → lokale, naive Zeit (Vergleichbarkeit mit `datetime.now()`)."""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt


def parse_when(raw: object, now: datetime | None = None) -> datetime:
    """UI-Eingabe → lokales (naives) DateTime in der Zukunft.

    Erkannt: "15:05" / "15:05:30" (Uhrzeit heute, in der Vergangenheit → morgen),
    "in 10 min" / "in 2h" / "in 1d" (relativ zu `now`), ISO-8601
    ("2026-11-06T09:00", "2026-11-06 09:00:30"; mit Timezone → lokal).
    ValueError mit deutscher Meldung bei Unverstandem / Vergangenheit."""
    now = now or datetime.now()
    text = str(raw or "").strip()
    if not text:
        raise ValueError("leere Zeitangabe — z. B. 15:05, in 10 min, 2026-11-06T09:00")

    m = _WHEN_TIME_RE.match(text)
    if m:
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            raise ValueError(t("schedule.invalid_time_of_day", text=text))
        dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return dt

    m = _WHEN_REL_RE.match(text)
    if m:
        n = int(m.group(1))
        if n < 1:
            raise ValueError(t("schedule.relative_value_must_be_1", text=text))
        return now + timedelta(seconds=n * _WHEN_REL_UNITS[m.group(2).lower()])

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(t("schedule.time_not_recognized_allowed_15", text=text)) from None
    dt = _local_naive(dt)
    if dt <= now:
        raise ValueError(t("schedule.time_is_in_the_past", text=text))
    return dt


# ---------------------------------------------------------------- One-time-Store


def _onetime_path() -> Path:
    return config.STATE_DIR / ONETIME_FILE


def _load_onetimes() -> dict[str, dict] | None:
    """Einträge laden; `None` bei fehlendem/grobschlächtig defektem File, sonst
    `{"proj/loop": entry}` (korrupte Einzel-Einträge werden verworfen)."""
    path = _onetime_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, dict] = {}
    for key, entry in raw.items():
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("at"), str)
            and isinstance(entry.get("project"), str)
            and isinstance(entry.get("loop"), str)
        ):
            try:
                at = _local_naive(datetime.fromisoformat(entry["at"]))
            except ValueError:
                continue
            out[str(key)] = {**entry, "at": at.isoformat(timespec="seconds")}
    return out


def _save_onetimes(data: dict[str, dict]) -> None:
    path = _onetime_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _onetime_key(project: str, loop: str) -> str:
    return f"{project}/{loop}"


def _valid_project_loop(project: str, loop: str) -> None:
    spec = load_projects().get(project)
    if not spec or spec.get("_error"):
        raise NotFoundError(t("schedule.unknown_project", project=project))
    if loop not in (spec.get("loops") or {}):
        raise NotFoundError(f"unbekannter Loop: {project}/{loop}")


def add_onetime(project: str, loop: str, at: datetime, note: str = "") -> dict:
    """One-time-Run ansetzen (ersetzt einen bereits angesetzt für denselben Loop)."""
    _valid_project_loop(project, loop)
    entry = {
        "project": project,
        "loop": loop,
        "at": at.isoformat(timespec="seconds"),
        "note": str(note or ""),
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        data = _load_onetimes()
        if data is None:
            data = {}
        data[_onetime_key(project, loop)] = entry
        _save_onetimes(data)
    return entry


def list_onetimes() -> list[dict]:
    """Alle angesetzt One-time-Runs, sortiert nach `at`."""
    with _lock:
        data = _load_onetimes() or {}
    return sorted(data.values(), key=lambda e: e["at"])


def remove_onetime(project: str, loop: str) -> dict | None:
    """Angelegten One-time-Run entfernen; `None`, wenn keiner angesetzt war."""
    with _lock:
        data = _load_onetimes()
        if data is None:
            return None
        entry = data.pop(_onetime_key(project, loop), None)
        if entry is not None:
            _save_onetimes(data)
    return entry


def requeue_failed(entry: dict, now: datetime) -> dict | None:
    """Fälligen One-time nach Start-Fehler erneut ansetzen (Backoff, max. Versuche).

    Ein inzwischen neu angesetzter Eintrag für denselben Loop gewinnt (kein
    Überschreiben). `None`, wenn verworfen (Versuche erschöpft bzw. ersetzt)."""
    attempts = int(entry.get("attempts", 0)) + 1
    if attempts >= ONETIME_MAX_ATTEMPTS:
        return None
    retry = {**entry, "attempts": attempts, "at": (now + ONETIME_RETRY_DELAY).isoformat(timespec="seconds")}
    key = _onetime_key(entry["project"], entry["loop"])
    with _lock:
        data = _load_onetimes() or {}
        if key in data:
            return None
        data[key] = retry
        _save_onetimes(data)
    return retry


def pop_due(now: datetime) -> list[dict]:
    """Fällige Einträge (`at <= now`) atomar holen UND entfernen (ein Tick feuert sie)."""
    with _lock:
        data = _load_onetimes()
        if not data:
            return []
        due: list[dict] = []
        for key, entry in list(data.items()):
            if datetime.fromisoformat(entry["at"]) <= now:
                due.append(entry)
                del data[key]
        if due:
            _save_onetimes(data)
    return due
