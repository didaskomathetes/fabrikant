"""5-field cron: one semantics for scheduler, retry validation and schedule view.

Fields: minute hour day-of-month month day-of-week (0 = Sun). Parts: ``*``, ``a``,
``a-b``, list with ``,``, step ``/n`` (with ``*`` counted from 0). dom and dow
are combined with AND (not OR as in Vixie cron). Invalid expressions
(also step 0) never match.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Search horizon for `next_times`: expressions that never (or more rarely) fire
# (e.g. "0 0 31 2 *") return shorter lists instead of searching endlessly. Feb
# 29 certainly occurs within 8 years (century exception 2100).
NEXT_TIMES_HORIZON = timedelta(days=8 * 366)
_INVALID = (ValueError, IndexError, ZeroDivisionError)


def field_ok(field: str, value: int, maxv: int) -> bool:
    """Does `value` match a single cron field? Raises on an invalid field."""
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
    """Does the day of `dt` match (dom AND month AND dow)? Raises on an invalid field."""
    return field_ok(f_dom, dt.day, 31) and field_ok(f_mon, dt.month, 12) and field_ok(f_dow, (dt.weekday() + 1) % 7, 6)


def matches(expr: str, dt: datetime) -> bool:
    """Does the cron expression hit the minute `dt`? Invalid → False."""
    parts = str(expr).split()
    if len(parts) != 5:
        return False
    f_min, f_hour, f_dom, f_mon, f_dow = parts
    try:
        return field_ok(f_min, dt.minute, 59) and field_ok(f_hour, dt.hour, 23) and day_matches(f_dom, f_mon, f_dow, dt)
    except _INVALID:
        return False


def day_times(f_min: str, f_hour: str) -> list[timedelta]:
    """All times of a day (offset from 0:00), sorted. Raises on an invalid field."""
    return [
        timedelta(hours=h, minutes=m)
        for h in range(24)
        if field_ok(f_hour, h, 23)
        for m in range(60)
        if field_ok(f_min, m, 59)
    ]


def next_times(expr: str, after: datetime, count: int = 5) -> list[datetime]:
    """The next `count` fire times after `after` (naive/local, `after` itself does not count).

    Day by day instead of minute by minute: the times of a day are determined once,
    then only matching days within :data:`NEXT_TIMES_HORIZON` are checked.
    Invalid or never-matching expressions → shorter list or ``[]``."""
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
