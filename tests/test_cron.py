"""Gemeinsame Cron-Semantik (`fabrikant.cron`): Ausblick == das, was der Scheduler feuert."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from fabrikant import cron

NOW = datetime(2026, 11, 5, 11, 0, 0)


def _scan(expr: str, after: datetime, until: datetime) -> list[datetime]:
    """Referenz: Minute für Minute mit `cron.matches` (= Scheduler-Tick)."""
    out, t = [], after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while t <= until:
        if cron.matches(expr, t):
            out.append(t)
        t += timedelta(minutes=1)
    return out


@pytest.mark.parametrize(
    "expr",
    [
        "0 0 */2 * *",  # Schritt ab 0 → gerade Tage (nicht 1,3,5 …)
        "30 6 * */5 *",  # Schritt ab 0 → Monate 5, 10
        "*/7 */5 * * *",
        "0 0-99 * * *",  # außerhalb des Bereichs: toleriert wie der Scheduler
        "1,,2 3 * * *",  # leerer Listen-Teil wird übersprungen
        "15 8-18/3 * * 1-5",
        "0 12 15 * 1",  # dom UND dow
    ],
)
def test_next_times_equal_scheduler_scan(expr):
    out = cron.next_times(expr, NOW, 4)
    assert out, expr
    assert out == _scan(expr, NOW, out[-1])


def test_next_times_step_offset_from_zero():
    assert [d.day for d in cron.next_times("0 0 */2 * *", NOW, 3)] == [6, 8, 10]
    assert [d.month for d in cron.next_times("0 0 1 */3 *", NOW, 2)] == [12, 3]


def test_next_times_never_matching_is_bounded_and_fast():
    """Nie passend (31.2.) → [] ohne bis Jahr 9999 zu laufen (früher Overflow/500)."""
    t0 = time.monotonic()
    assert cron.next_times("0 0 31 2 *", NOW, 5) == []
    assert cron.next_times("0 0 31 4 *", NOW, 5) == []
    assert time.monotonic() - t0 < 1.0


def test_next_times_leap_day_within_horizon():
    assert cron.next_times("0 0 29 2 *", NOW, 1) == [datetime(2028, 2, 29)]


@pytest.mark.parametrize("expr", ["nicht cron", "* * * *", "*/0 * * * *", "a b c d e"])
def test_invalid_expressions(expr):
    assert cron.next_times(expr, NOW, 3) == []
    assert cron.matches(expr, NOW) is False


def test_next_times_excludes_after_itself():
    assert cron.next_times("0 11 * * *", NOW, 1) == [datetime(2026, 11, 6, 11, 0)]
