"""engine.start_priority_run / requeue_queued_runs: manuelle Runs einreihen statt verwerfen (ADR 0015)."""

from __future__ import annotations

import json
import threading
import time

import pytest
from helpers import not_none

from fabrik import chat, engine, runs


class _FailingThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("boom")


@pytest.fixture
def held(runs_dir, monkeypatch):
    """Belegter einziger Run-Slot (simuliert einen laufenden Trigger-Run)."""
    monkeypatch.setattr(runs, "max_parallel_for", lambda _p: 1)
    claim = runs.claim_run("prio", "hold")
    assert claim is not None
    yield claim
    claim.release()


def _wait_free(name: str) -> None:
    """Warten, bis der Worker seinen Claim zurückgegeben hat (finally nach run_loop)."""
    deadline = time.monotonic() + 5
    while (claim := runs.claim_run(name, "probe")) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert claim is not None
    claim.release()


def _wait_status(rid: str, status: str) -> dict:
    deadline = time.monotonic() + 5
    while (rec := not_none(runs.get_run(rid, "prio")))["status"] != status and time.monotonic() < deadline:
        time.sleep(0.01)
    return rec


def test_start_priority_run_free_lock_runs_immediately(runs_dir, monkeypatch):
    done = threading.Event()
    calls = []
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: (calls.append((a, k)), done.set()))
    queued = engine.start_priority_run("prio", "l", "manual", run_id="r1", note="n", initial_vars={"a": "1"})
    assert queued is False
    assert done.wait(5)
    assert calls[0][1] == {
        "trigger": "manual",
        "note": "n",
        "initial_vars": {"a": "1"},
        "run_id": "r1",
        "held_scope": "prio",
        "actor": None,
    }
    assert runs.get_run("r1", "prio") is None, "kein queued-Platzhalter bei Sofortstart"
    _wait_free("prio")


def test_start_priority_run_busy_queues_with_priority(held, runs_dir, monkeypatch):
    """Belegt → queued-Record + Vorrang: nach Lock-Freigabe weicht ein Trigger-Run aus."""
    started = threading.Event()
    finish = threading.Event()

    def fake_run_loop(*a, **k):
        started.set()
        finish.wait(5)

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    assert engine.start_priority_run("prio", "l", "manual", run_id="r1", initial_vars={"x": "y"}) is True
    rec = runs.get_run("r1", "prio")
    assert rec["status"] == "queued"
    assert rec["queued_vars"] == {"x": "y"}
    assert runs.has_priority_waiter("prio")

    held.release()  # laufender Run endet
    assert runs.claim_run("prio", "trigger") is None, "Trigger-Run muss dem eingereihten Run weichen"
    assert started.wait(5)
    assert not runs.has_priority_waiter("prio")
    finish.set()
    _wait_free("prio")


def test_start_priority_run_failure_marks_queued_record_error(held, runs_dir, monkeypatch):
    """run_loop scheitert vor dem Schreiben (z. B. Loop unbekannt) → queued-Record wird error."""

    def boom(*a, **k):
        raise RuntimeError("Loop unbekannt: l")

    monkeypatch.setattr(engine, "run_loop", boom)
    engine.start_priority_run("prio", "l", "manual", run_id="r1")
    held.release()
    rec = _wait_status("r1", "error")
    assert rec["status"] == "error"
    assert "Loop unbekannt" in rec["error"]


def test_start_priority_run_thread_start_fails_cleans_up(held, runs_dir, monkeypatch):
    monkeypatch.setattr(engine.threading, "Thread", _FailingThread)
    with pytest.raises(RuntimeError):
        engine.start_priority_run("prio", "l", "manual", run_id="r1")
    assert not runs.has_priority_waiter("prio")
    assert runs.get_run("r1", "prio")["status"] == "error"


def test_requeue_queued_runs(held, runs_dir, monkeypatch):
    """Server-Restart: queued-Records werden mit ihren Variablen wieder eingereiht."""
    d = runs_dir / "prio"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": "r1", "project": "prio", "loop": "l", "trigger": "chat", "note": "n", "status": "queued"}
    (d / "r1.json").write_text(json.dumps({**rec, "queued_vars": {"k": "v"}, "started": "2026"}))
    (d / "r2.json").write_text(json.dumps({**rec, "id": "r2", "status": "pass", "started": "2026"}))
    done = threading.Event()
    calls = []
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: (calls.append(k), done.set()))
    assert engine.requeue_queued_runs() == ["r1"]
    held.release()
    assert done.wait(5)
    assert calls == [
        {
            "trigger": "chat",
            "note": "n",
            "initial_vars": {"k": "v"},
            "run_id": "r1",
            "held_scope": "prio",
            "actor": None,
        }
    ]
    _wait_free("prio")


def test_chat_start_run_reports_queued(monkeypatch):
    monkeypatch.setattr(chat, "start_priority_run", lambda *a, **k: True)
    assert "eingereiht" in chat._chat_start_run("prio", "l", "")
    monkeypatch.setattr(chat, "start_priority_run", lambda *a, **k: False)
    assert "gestartet" in chat._chat_start_run("prio", "l", "")


def test_chat_start_run_thread_start_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kein Thread")

    monkeypatch.setattr(chat, "start_priority_run", boom)
    assert "Worker-Start fehlgeschlagen" in chat._chat_start_run("prio", "l", "")


# --- spawn_priority_worker -------------------------------------------------


def _wait_event(ev: threading.Event) -> None:
    assert ev.wait(5)


def test_spawn_priority_worker_free_slot_passes_claim_and_releases(runs_dir, monkeypatch):
    monkeypatch.setattr(runs, "max_parallel_for", lambda _p: 1)
    claim = runs.claim_run("prio", "l")
    assert claim is not None
    got, done = [], threading.Event()

    def work(held):
        got.append(held)
        done.set()

    engine.spawn_priority_worker("prio", "l", None, claim=claim, work=work)
    _wait_event(done)
    assert got == [claim]
    _wait_free("prio")


def test_spawn_priority_worker_waits_with_priority(held, runs_dir):
    scope = runs.lock_key("prio", "l")
    done = threading.Event()
    engine.spawn_priority_worker("prio", "l", None, claim=None, work=lambda _h: done.set())
    time.sleep(0.1)
    assert not done.is_set()  # Slot belegt: Worker wartet
    held.release()
    _wait_event(done)
    _wait_free("prio")
    assert scope  # Wartezähler wird vom blockierenden Claim (registered) abgebaut


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_spawn_priority_worker_releases_claim_when_work_raises(runs_dir, monkeypatch):
    monkeypatch.setattr(runs, "max_parallel_for", lambda _p: 1)
    claim = runs.claim_run("prio", "l")
    done = threading.Event()
    threads = []
    real_thread = engine.threading.Thread

    def capture(*a, **k):
        t = real_thread(*a, **k)
        threads.append(t)
        return t

    monkeypatch.setattr(engine.threading, "Thread", capture)

    def work(_held):
        done.set()
        raise ValueError("boom")

    engine.spawn_priority_worker("prio", "l", None, claim=claim, work=work)
    _wait_event(done)
    for t in threads:
        t.join(5)  # Thread muss vor Testende tot sein: der excepthook (Warning) feuert in diesem Test
    _wait_free("prio")


def test_spawn_priority_worker_start_error_rolls_back(runs_dir, monkeypatch):
    monkeypatch.setattr(runs, "max_parallel_for", lambda _p: 1)
    monkeypatch.setattr(engine.threading, "Thread", _FailingThread)
    calls = []
    claim = runs.claim_run("prio", "l")
    with pytest.raises(RuntimeError):
        engine.spawn_priority_worker(
            "prio", "l", None, claim=claim, work=lambda _h: None, on_start_error=lambda: calls.append(1)
        )
    assert calls == [1]
    _wait_free("prio")  # Claim wurde freigegeben


def test_spawn_priority_worker_start_error_without_claim_resets_waiter(held, monkeypatch):
    monkeypatch.setattr(engine.threading, "Thread", _FailingThread)
    seen = []
    monkeypatch.setattr(engine, "mark_priority_waiter", lambda scope, d: seen.append(d))
    with pytest.raises(RuntimeError):
        engine.spawn_priority_worker("prio", "l", None, claim=None, work=lambda _h: None)
    assert seen == [1, -1]


def test_spawn_priority_worker_queued_callback_error_rolls_back(held, monkeypatch):
    seen = []
    monkeypatch.setattr(engine, "mark_priority_waiter", lambda scope, d: seen.append(d))
    on_start_error = []

    def on_queued():
        raise OSError("queued record failed")

    with pytest.raises(OSError, match="queued record failed"):
        engine.spawn_priority_worker(
            "prio",
            "l",
            None,
            work=lambda _h: None,
            on_queued=on_queued,
            on_start_error=lambda: on_start_error.append(True),
        )

    assert seen == [1, -1]
    assert on_start_error == [True]


def test_start_priority_run_disabled_while_queued_is_dropped(held, runs_dir, monkeypatch):
    """Während des Wartens deaktiviert (ADR 0019) → kein Start, queued-Record wird error."""
    calls = []
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: calls.append(a))
    engine.start_priority_run("prio", "l", "manual", run_id="r1")
    monkeypatch.setattr(engine.pause, "manual_block_reason", lambda _p, _l: "prio/l ist deaktiviert")
    held.release()
    rec = _wait_status("r1", "error")
    assert rec["error"] == "prio/l ist deaktiviert"
    assert calls == []
    _wait_free("prio")


def test_start_priority_run_paused_while_queued_still_starts(held, runs_dir, monkeypatch):
    """Nur `paused` (nicht `disabled`) → manuelle Runs bleiben erlaubt (ADR 0019)."""
    calls = []
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: calls.append(a))
    engine.start_priority_run("prio", "l", "manual", run_id="r1")
    monkeypatch.setattr(engine.pause, "effective_state", lambda _p, _l: "paused")
    held.release()
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls, "Manueller Run trotz nur `paused` verworfen"
    _wait_free("prio")
