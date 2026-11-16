"""Schedule-View-Backend: Cron-Ausblick, When-Parsing, One-time-Store, Scheduler-Fire."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fabrik import pause, schedule
from fabrik import scheduler as scheduler_mod
from fabrik.scheduler import Scheduler, cron_matches

NOW = datetime(2026, 11, 5, 11, 0, 0)


# ---------------------------------------------------------------- next_cron_times


def test_cron_every_10_minutes():
    out = schedule.next_cron_times("*/10 * * * *", NOW, 3)
    assert out == [datetime(2026, 11, 5, 11, 10), datetime(2026, 11, 5, 11, 20), datetime(2026, 11, 5, 11, 30)]


def test_cron_specific_time_next_day():
    out = schedule.next_cron_times("0 9 * * *", NOW, 2)
    assert out[0] == datetime(2026, 11, 6, 9, 0)
    assert out[1] == datetime(2026, 11, 7, 9, 0)


def test_cron_crosses_year():
    out = schedule.next_cron_times("0 3 1 1 *", NOW, 1)
    assert out == [datetime(2027, 1, 1, 3, 0)]


def test_cron_never_matching_returns_empty():
    # Februar hat nie 31 Tage → nie passend (wie cron_matches: False für immer)
    assert schedule.next_cron_times("0 31 2 * *", NOW, 3) == []


def test_cron_invalid_expression_returns_empty():
    assert schedule.next_cron_times("nicht cron", NOW, 3) == []
    assert schedule.next_cron_times("* * * *", NOW, 3) == []  # 4 Felder


def test_cron_matches_cron_matches_semantics():
    """Jede zurückgegebene Zeit erfüllt cron_matches; ein Minuten-Scan findet
    nichts dazwischen (exakt dieselbe Semantik wie der Scheduler)."""
    for expr in ("*/15 8-18 * * 1-5", "0 12 15 * 1", "5,20,50 6 * * *"):
        out = schedule.next_cron_times(expr, NOW, 3)
        assert out, expr
        for t in out:
            assert cron_matches(expr, t), (expr, t)
        # Minuten-Scan über das Fenster bis zum letzten Ergebnis: keine Lücken
        last = out[-1]
        scanned = []
        t = NOW.replace(second=0, microsecond=0) + timedelta(minutes=1)
        while t <= last:
            if cron_matches(expr, t):
                scanned.append(t)
            t += timedelta(minutes=1)
        assert scanned[: len(out)] == out, (expr, scanned[: len(out)], out)


# ---------------------------------------------------------------- parse_when


def test_parse_when_time_today():
    assert schedule.parse_when("15:05", NOW) == datetime(2026, 11, 5, 15, 5)
    assert schedule.parse_when("15:00:30", NOW) == datetime(2026, 11, 5, 15, 0, 30)


def test_parse_when_time_past_rolls_to_tomorrow():
    assert schedule.parse_when("15:05", datetime(2026, 11, 5, 15, 30)) == datetime(2026, 11, 6, 15, 5)


def test_parse_when_relative():
    assert schedule.parse_when("in 10min", NOW) == NOW + timedelta(minutes=10)
    assert schedule.parse_when("in 2h", NOW) == NOW + timedelta(hours=2)
    assert schedule.parse_when("in 5 sec", NOW) == NOW + timedelta(seconds=5)
    assert schedule.parse_when("in 1 tag", NOW) == NOW + timedelta(days=1)
    assert schedule.parse_when("IN 10 Min", NOW) == NOW + timedelta(minutes=10)


def test_parse_when_iso():
    assert schedule.parse_when("2026-11-06T09:00", NOW) == datetime(2026, 11, 6, 9, 0)
    assert schedule.parse_when("2026-11-06 09:00:30", NOW) == datetime(2026, 11, 6, 9, 0, 30)


def test_parse_when_iso_with_timezone_converted_to_local():
    # Machine-unabhängig: 09:00+01:00 == 08:00 UTC → in die lokale Zeit konvertiert
    expected = datetime(2026, 11, 6, 8, 0, tzinfo=UTC).astimezone().replace(tzinfo=None)
    dt = schedule.parse_when("2026-11-06T09:00:00+01:00", NOW)
    assert dt.tzinfo is None
    assert dt == expected


def test_parse_when_rejects_past_iso():
    with pytest.raises(ValueError):
        schedule.parse_when("2020-01-01T00:00", NOW)


def test_parse_when_rejects_garbage():
    for raw in ("", "asdf", "in 0m", "25:00", "15:75", "in x"):
        with pytest.raises(ValueError):
            schedule.parse_when(raw, NOW)


# ---------------------------------------------------------------- One-time-Store


def _write_project(projects_dir: Path, name: str = "p", loop: str = "l") -> None:
    root = projects_dir / "root"
    root.mkdir(parents=True, exist_ok=True)
    (projects_dir / f"{name}.fabrik.yaml").write_text(
        f"name: {name}\nroot: {root}\nloops:\n  {loop}:\n    triggers:\n      - {{type: manual}}\n"
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )


def test_onetime_add_list_remove(projects_dir):
    _write_project(projects_dir)
    at = datetime(2026, 11, 5, 15, 5)
    entry = schedule.add_onetime("p", "l", at, "notiz")
    assert entry["project"] == "p" and entry["loop"] == "l"
    assert entry["at"] == "2026-11-05T15:05:00"
    assert schedule.list_onetimes() == [entry]
    assert schedule.remove_onetime("p", "l") == entry
    assert schedule.list_onetimes() == []
    assert schedule.remove_onetime("p", "l") is None


def test_onetime_add_replaces_existing(projects_dir):
    _write_project(projects_dir)
    schedule.add_onetime("p", "l", datetime(2026, 11, 5, 15, 5))
    entry = schedule.add_onetime("p", "l", datetime(2026, 11, 5, 16, 0), "neu")
    assert schedule.list_onetimes() == [entry]


def test_onetime_add_validates_project_loop(projects_dir):
    _write_project(projects_dir)
    with pytest.raises(ValueError, match="unbekanntes Projekt"):
        schedule.add_onetime("ghost", "l", NOW)
    with pytest.raises(ValueError, match="unbekannter Loop"):
        schedule.add_onetime("p", "ghost", NOW)


def test_onetime_pop_due_removes_atomically(projects_dir):
    _write_project(projects_dir)
    due = schedule.add_onetime("p", "l", NOW - timedelta(seconds=1))
    got = schedule.pop_due(NOW)
    assert got == [due]
    assert schedule.pop_due(NOW) == []  # zweiter Pop: leer (atomar entfernt)
    assert schedule.list_onetimes() == []


# ---------------------------------------------------------------- Scheduler-Fire


def test_fire_onetimes_starts_run_and_removes(projects_dir, monkeypatch):
    _write_project(projects_dir)
    at = NOW - timedelta(seconds=1)
    schedule.add_onetime("p", "l", at, "wichtig")
    started = []

    def fake_start(name, lname, trigger, *, run_id, note="", initial_vars=None, actor=None):
        started.append((name, lname, trigger, run_id, note, actor))
        return True

    monkeypatch.setattr(scheduler_mod, "start_priority_run", fake_start)
    Scheduler()._fire_onetimes(NOW)
    assert len(started) == 1
    name, lname, trigger, run_id, note, actor = started[0]
    assert (name, lname, trigger, actor) == ("p", "l", "once", "scheduler")
    assert run_id.endswith("-l")
    assert "15:05" not in note and "geplant" in note and "wichtig" in note
    assert schedule.list_onetimes() == []


def test_fire_onetimes_disabled_drops_without_start(projects_dir, monkeypatch):
    _write_project(projects_dir)
    schedule.add_onetime("p", "l", NOW - timedelta(seconds=1))
    monkeypatch.setattr(pause, "effective_state", lambda name, loop=None: pause.DISABLED)
    started = []
    monkeypatch.setattr(scheduler_mod, "start_priority_run", lambda *a, **k: started.append(1))
    Scheduler()._fire_onetimes(NOW)
    assert started == []
    assert schedule.list_onetimes() == []  # verworfen, nicht retryt


def _boom(*a, **k):
    raise RuntimeError("pool down")


def test_fire_onetimes_start_failure_requeues_with_backoff(projects_dir, monkeypatch):
    _write_project(projects_dir)
    schedule.add_onetime("p", "l", NOW - timedelta(seconds=1))
    monkeypatch.setattr(scheduler_mod, "start_priority_run", _boom)
    Scheduler()._fire_onetimes(NOW)
    # erneut angesetzt — mit Backoff statt alter (vergangener) Zeit → kein 2-s-Retry-Sturm
    [entry] = schedule.list_onetimes()
    assert entry["at"] == (NOW + schedule.ONETIME_RETRY_DELAY).isoformat(timespec="seconds")
    assert entry["attempts"] == 1
    assert schedule.pop_due(NOW) == []


def test_fire_onetimes_start_failure_gives_up_after_max_attempts(projects_dir, monkeypatch):
    _write_project(projects_dir)
    schedule.add_onetime("p", "l", NOW)
    monkeypatch.setattr(scheduler_mod, "start_priority_run", _boom)
    now = NOW
    for _ in range(schedule.ONETIME_MAX_ATTEMPTS):
        now += schedule.ONETIME_RETRY_DELAY
        Scheduler()._fire_onetimes(now)
    assert schedule.list_onetimes() == []


def test_requeue_failed_keeps_newer_user_entry(projects_dir):
    """Zwischen Pop und Requeue neu angesetzt → der neue Eintrag gewinnt."""
    _write_project(projects_dir)
    stale = schedule.add_onetime("p", "l", NOW - timedelta(seconds=1))
    assert schedule.pop_due(NOW) == [stale]
    newer = schedule.add_onetime("p", "l", NOW + timedelta(hours=1), "neu")
    assert schedule.requeue_failed(stale, NOW) is None
    assert schedule.list_onetimes() == [newer]


def test_onetime_with_offset_in_file_is_normalized(projects_dir):
    """Hand-editierter Eintrag mit Offset: pop_due vergleicht ohne TypeError."""
    _write_project(projects_dir)
    schedule.add_onetime("p", "l", NOW)
    path = schedule._onetime_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["p/l"]["at"] = "2020-01-01T00:00:00+01:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    [due] = schedule.pop_due(NOW)
    assert datetime.fromisoformat(due["at"]).tzinfo is None


def test_onetime_add_unknown_raises_not_found(projects_dir):
    _write_project(projects_dir)
    with pytest.raises(schedule.NotFoundError):
        schedule.add_onetime("ghost", "l", NOW)


# ---------------------------------------------------------------- Queue-Snapshot


def test_queued_trigger_snapshot_ordering(monkeypatch):
    from fabrik import engine

    monkeypatch.setattr(engine, "_queued_triggers", {("p", "b", "cron"): 200.0, ("p", "a", "cron"): 100.0})
    snap = engine.queued_trigger_snapshot()
    assert [e["loop"] for e in snap] == ["a", "b"]  # Einreichungsreihenfolge
    assert snap[0] == {"project": "p", "loop": "a", "trigger": "cron", "enqueued_at": 100.0}


def test_pool_status_without_pool(monkeypatch):
    from fabrik import engine

    monkeypatch.setattr(engine, "_run_pool", None)
    monkeypatch.setenv("FABRIK_WORKERS", "7")
    # Konfigurierte Größe, nicht 0 (Pool entsteht erst mit dem 1. Trigger-Run)
    assert engine.pool_status() == {"workers": 7, "busy": 0, "queue": 0}


def test_slot_snapshot(monkeypatch):
    from fabrik import runs

    monkeypatch.setattr(runs, "_slots_used", {"p": 2})
    monkeypatch.setattr(runs, "_active_keys", {("p", "l", "issue=1")})
    monkeypatch.setattr(runs, "_priority_waiters", {"p": 1, "p::l": 0})
    snap = runs.slot_snapshot()
    assert snap["slots_used"] == {"p": 2}
    assert snap["active"] == [["p", "l", "issue=1"]]
    assert snap["priority_waiters"] == {"p": 1}  # Null-Warter werden herausgefiltert


def test_worker_pool_counts_busy_workers():
    """`busy` = laufende Works (Grundlage der `est`-Einschätzung der Pool-Queue)."""
    import threading

    from fabrik import engine

    pool = engine._WorkerPool(2)
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        release.wait(5)

    pool.submit(work)
    assert started.wait(5)
    assert pool.busy == 1
    release.set()
    pool.shutdown()
    for t in pool._threads:
        t.join(5)
    assert pool.busy == 0
