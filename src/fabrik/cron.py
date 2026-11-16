"""5-Feld-Cron: eine Semantik für Scheduler, Retry-Validierung und Schedule-View.

Felder: minute hour day-of-month month day-of-week (0 = So). Teile: ``*``, ``a``,
``a-b``, Liste mit ``,``, Schritt ``/n`` (bei ``*`` ab 0 gezählt). dom und dow
werden UND-verknüpft (nicht ODER wie bei Vixie-Cron). Ungültige Ausdrücke
(auch Schritt 0) matchen nie.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Suchhorizont für `next_times`: Ausdrücke, die nie (oder seltener) feuern
# (z. B. "0 0 31 2 *"), liefern kürzere Listen statt endlos zu suchen. Der
# 29.2. kommt innerhalb von 8 Jahren sicher vor (Jahrhundert-Ausnahme 2100).
NEXT_TIMES_HORIZON = timedelta(days=8 * 366)
_INVALID = (ValueError, IndexError, ZeroDivisionError)


def field_ok(field: str, value: int, maxv: int) -> bool:
    """Passt `value` auf ein einzelnes Cron-Feld? Wirft bei ungültigem Feld."""
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            lo, hi = 0, maxv
        elif "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
        else:
            lo = hi = int(part)
        if lo <= value <= hi and (value - lo) % step == 0:
            return True
    return False


def day_matches(f_dom: str, f_mon: str, f_dow: str, dt: datetime) -> bool:
    """Passt der Tag von `dt` (dom UND Monat UND dow)? Wirft bei ungültigem Feld."""
    return field_ok(f_dom, dt.day, 31) and field_ok(f_mon, dt.month, 12) and field_ok(f_dow, (dt.weekday() + 1) % 7, 6)


def matches(expr: str, dt: datetime) -> bool:
    """Trifft der Cron-Ausdruck die Minute `dt`? Ungültig → False."""
    parts = str(expr).split()
    if len(parts) != 5:
        return False
    f_min, f_hour, f_dom, f_mon, f_dow = parts
    try:
        return field_ok(f_min, dt.minute, 59) and field_ok(f_hour, dt.hour, 23) and day_matches(f_dom, f_mon, f_dow, dt)
    except _INVALID:
        return False


def day_times(f_min: str, f_hour: str) -> list[timedelta]:
    """Alle Uhrzeiten eines Tages (Offset ab 0:00), sortiert. Wirft bei ungültigem Feld."""
    return [
        timedelta(hours=h, minutes=m)
        for h in range(24)
        if field_ok(f_hour, h, 23)
        for m in range(60)
        if field_ok(f_min, m, 59)
    ]


def next_times(expr: str, after: datetime, count: int = 5) -> list[datetime]:
    """Die nächsten `count` Fire-Zeiten nach `after` (naiv/lokal, `after` selbst zählt nicht).

    Tageweise statt minutenweise: Uhrzeiten eines Tages werden einmal bestimmt,
    dann nur passende Tage innerhalb von :data:`NEXT_TIMES_HORIZON` geprüft.
    Ungültige oder nie passende Ausdrücke → kürzere Liste bzw. ``[]``."""
    parts = str(expr).split()
    if len(parts) != 5 or count < 1:
        return []
    f_min, f_hour, f_dom, f_mon, f_dow = parts
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    out: list[datetime] = []
    try:
        times = day_times(f_min, f_hour)
        if not times:
            return []
        day = start.replace(hour=0, minute=0)
        end = start + NEXT_TIMES_HORIZON
        while day <= end and len(out) < count:
            if day_matches(f_dom, f_mon, f_dow, day):
                out.extend(day + t for t in times if day + t >= start)
            day += timedelta(days=1)
    except _INVALID:
        return []
    return out[:count]
