"""Schedule-view backend: next cron runs + one-time overrides.

- `next_cron_times()` = :func:`fabrikant.cron.next_times` (same cron semantics as
  the scheduler, shared code).
- `parse_when()` translates UI input ("15:05", "in 10 min", ISO 8601) into a local
  DateTime in the future.
- One-time overrides (one-off runs) live in `state/onetime-runs.json` (atomic,
  pattern like `pause.py`); the scheduler fires due entries in `tick()` like
  manual runs (ADR 0019: `paused` does not block manual runs, `disabled` does).
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
# Start error of a due one-time: retry after backoff, at most this many times.
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
    """UI input → local (naive) DateTime in the future.

    Recognized: "15:05" / "15:05:30" (time of day today, in the past → tomorrow),
    "in 10 min" / "in 2h" / "in 1d" (relative to `now`), ISO 8601
    ("2026-11-06T09:00", "2026-11-06 09:00:30"; with time zone → local).
    ValueError with a localized message on misunderstanding / past."""
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
    """Load entries; `None` for a missing/grossly broken file, otherwise
    `{"proj/loop": entry}` (corrupt single entries are discarded)."""
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
    """Schedule a one-time run (replaces one already scheduled for the same loop)."""
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
    """Reschedule a due one-time after a start error (backoff, max. attempts).

    An entry meanwhile newly scheduled for the same loop wins (no
    overwriting). `None` if discarded (attempts exhausted or replaced)."""
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
    """Fetch AND remove due entries (`at <= now`) atomically (one tick fires them)."""
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
