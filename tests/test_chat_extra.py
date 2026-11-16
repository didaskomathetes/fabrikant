"""Chat: _chat (Befehle), _chat_resume, _chat_alert_test, _llm_chat, _approval_detail."""

from __future__ import annotations

import json
import threading
import time

import pytest
from helpers import not_none

from fabrik import chat, config, engine, llm, runs


@pytest.fixture
def clean_llm(monkeypatch, tmp_path):
    import os

    for k in list(os.environ):
        if k.startswith("FABRIK_LLM"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_ACTIVE_FILE", tmp_path / "active_llm")
    monkeypatch.setenv("FABRIK_LLM_PROVIDERS", "dummy")
    monkeypatch.setenv("FABRIK_LLM_DUMMY_URL", "http://127.0.0.1:1/v1/chat/completions")
    monkeypatch.setenv("FABRIK_LLM_DUMMY_MODEL", "test-model")
    llm.reset_cache()
    llm.set_active("dummy")
    yield
    llm.reset_cache()


@pytest.fixture(autouse=True)
def _clear_pending():
    """_pending_specs ist Modul-State → pro Test leeren (kein Leak zwischen Tests)."""
    chat._pending_specs.clear()
    yield
    chat._pending_specs.clear()


@pytest.fixture(autouse=True)
def _drain_resume_workers():
    """Resume-Worker früherer Tests abwarten, damit `_resume_workers` (Modul-State) nicht in den nächsten Test leakt."""
    yield
    deadline = time.time() + 5
    while chat._resume_workers and time.time() < deadline:
        time.sleep(0.02)


def _write_spec(projects_dir, name="p", root=None):
    root = root or projects_dir.parent / "proj"
    root.mkdir(parents=True, exist_ok=True)
    (projects_dir / f"{name}.fabrik.yaml").write_text(
        f'name: {name}\nroot: {root}\nloops:\n  l:\n    steps:\n      - {{type: shell, cmd: "true"}}\n'
    )


# ---------------------------------------------------------------- _approval_detail
def test_approval_detail_empty():
    assert chat._approval_detail({}) == ""


def test_approval_detail_full():
    d = chat._approval_detail({"what": "was", "permissions": ["p1"], "issue": "http://x", "tier": 2})
    assert "was: was" in d
    assert "rechte: p1" in d
    assert "issue: http://x" in d
    assert "tier: 2" in d


# ---------------------------------------------------------------- _chat commands
def test_chat_help(projects_dir):
    assert "Befehle" in chat._chat("help")


def test_chat_list(projects_dir, tmp_path):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    out = chat._chat("list")
    assert "p" in out


def test_chat_list_empty(projects_dir):
    assert "(keine Projekte)" in chat._chat("list")


def test_chat_show(projects_dir, tmp_path):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    assert "name: p" in chat._chat("show p")


def test_chat_show_missing(projects_dir):
    assert "Kein Projekt" in chat._chat("show keiner")


def test_chat_lauf_format(projects_dir):
    assert "Format" in chat._chat("lauf p")


def test_chat_lauf_unknown_project(projects_dir, tmp_path):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    assert "Projekt unbekannt" in chat._chat("lauf q l")


def test_chat_lauf_unknown_loop(projects_dir, tmp_path):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    assert "Loop unbekannt" in chat._chat("lauf p q")


def test_chat_lauf_starts(projects_dir, tmp_path, monkeypatch):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    from fabrik import engine

    done = threading.Event()
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: done.set())
    out = chat._chat("lauf p l")
    assert "gestartet" in out
    # Worker muss fertig sein und den Slot freigeben — sonst leakt er in Folge-Tests.
    assert done.wait(5)
    claim = _held_lock(monkeypatch)  # pollt, bis der Slot frei ist
    claim.release()


def test_chat_runs(projects_dir, runs_dir):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "pass"}))
    assert "r1" in chat._chat("runs p")


def test_chat_runs_empty(projects_dir, runs_dir):
    assert "(keine Runs)" in chat._chat("runs")


def test_chat_log(projects_dir, runs_dir):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.log").write_text("a\nb\n")
    assert "b" in chat._chat("log r1")


def test_chat_log_missing(projects_dir, runs_dir):
    assert "nicht gefunden" in chat._chat("log keiner")


# ---------------------------------------------------------------- _chat_resume
def test_chat_resume_not_found(runs_dir):
    reply, resumed = chat._chat_resume("p", "nope", True)
    assert "nicht gefunden" in reply
    assert resumed is False


def test_chat_resume_wrong_status(runs_dir):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "pass"}))
    reply, resumed = chat._chat_resume("p", "r1", True)
    assert "nicht needs_approval" in reply
    assert resumed is False


def test_chat_resume_reject(runs_dir, monkeypatch, tmp_path):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "needs_approval"}))
    scratch = tmp_path / "scratch" / "r1"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(config, "SCRATCH_DIR", scratch.parent)
    out, resumed = chat._chat_resume("p", "r1", False)
    assert "abgelehnt" in out
    assert resumed is False
    assert json.loads((d / "r1.json").read_text())["status"] == "rejected"
    assert not scratch.exists()


def test_chat_resume_reject_updates_retry_chain(runs_dir, monkeypatch):
    """Abgelehnter Versuch → Kette wird aktualisiert (dismissed, Claim frei)."""
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "needs_approval"}))
    ended = []
    monkeypatch.setattr(
        engine.retry_chains, "record_run_end", lambda project, run: ended.append((project, run["status"]))
    )
    chat._chat_resume("p", "r1", False)
    assert ended == [("p", "rejected")]


def test_chat_resume_reject_logs_scratch_cleanup_failure(runs_dir, monkeypatch):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "needs_approval"}))

    monkeypatch.setattr(engine, "_cleanup_scratch", lambda _run_id, emit: emit("Scratch-Cleanup fehlgeschlagen"))
    out, resumed = chat._chat_resume("p", "r1", False)

    assert "abgelehnt" in out
    assert resumed is False
    assert "Scratch-Cleanup fehlgeschlagen" in (d / "r1.log").read_text()


def test_chat_resume_approve(runs_dir, monkeypatch):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "needs_approval"}))
    monkeypatch.setattr(chat, "run_loop", lambda *a, **k: None)
    out, resumed = chat._chat_resume("p", "r1", True)
    assert "setzt fort" in out
    assert resumed is True


def _needs_approval_run(runs_dir, rid="r1"):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(
        json.dumps({"id": rid, "project": "p", "loop": "l", "status": "needs_approval", "started": "2026"})
    )
    return d / f"{rid}.json"


def _held_lock(monkeypatch):
    """Belegter einziger Run-Slot von Projekt "p" (simuliert einen laufenden Run)."""
    monkeypatch.setattr(runs, "max_parallel_for", lambda _name: 1)
    # Resume-Worker vorheriger Tests geben ihren Slot asynchron frei → kurz pollen.
    deadline = time.monotonic() + 5
    while (claim := runs.claim_run("p", "busy")) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert claim is not None
    return claim


def test_chat_resume_lock_busy_queues_and_resumes_after_release(runs_dir, monkeypatch):
    """Projekt am Limit → Freigabe wird eingereiht und nach Slot-Freigabe fortgesetzt (ADR 0015)."""
    f = _needs_approval_run(runs_dir)
    lock = _held_lock(monkeypatch)
    resumed_ids = []
    done = threading.Event()

    def fake_run_loop(project, loop, **k):
        resumed_ids.append(k["resume_id"])
        done.set()

    monkeypatch.setattr(chat, "run_loop", fake_run_loop)
    out, resumed = chat._chat_resume("p", "r1", True)
    assert "gespeichert" in out
    assert resumed is True
    rec = json.loads(f.read_text())
    assert rec["status"] == "needs_approval"
    assert rec["approval_queued"]
    assert not done.wait(0.2), "darf nicht laufen, solange der andere Run den Slot hält"

    lock.release()
    assert done.wait(5)
    assert resumed_ids == ["r1"]


def test_chat_resume_disabled_while_queued_is_dropped(runs_dir, monkeypatch):
    """Während des Wartens deaktiviert (ADR 0019) → kein Resume, der Run bleibt
    needs_approval und kann nach Resume erneut freigegeben werden."""
    f = _needs_approval_run(runs_dir)
    lock = _held_lock(monkeypatch)
    resumed_ids = []
    monkeypatch.setattr(chat, "run_loop", lambda *a, **k: resumed_ids.append(k["resume_id"]))
    out, resumed = chat._chat_resume("p", "r1", True)
    assert resumed is True
    monkeypatch.setattr(chat.pause, "manual_block_reason", lambda _p, _l: "p/l ist deaktiviert")
    lock.release()
    deadline = time.monotonic() + 5
    while "r1" in chat._resume_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "r1" not in chat._resume_workers, "Resume-Worker hat sich nicht beendet"
    assert resumed_ids == [], "Resume trotz Disable gestartet"
    assert json.loads(f.read_text())["status"] == "needs_approval"


def test_chat_resume_queued_records_approved_by(runs_dir, monkeypatch):
    """Eingereihte Freigabe (kein Slot) hält den Approver trotzdem im Audit fest."""
    f = _needs_approval_run(runs_dir)
    lock = _held_lock(monkeypatch)
    monkeypatch.setattr(chat, "run_loop", lambda *a, **k: None)
    chat._chat_resume("p", "r1", True, actor="anna")
    rec = json.loads(f.read_text())
    assert rec["approval_queued"]
    assert rec["approved_by"] == "anna"
    lock.release()


def test_queued_resume_wins_against_new_run_after_release(runs_dir, monkeypatch):
    """Regression: nach Lock-Freigabe darf kein neuer Trigger-Run die wartende
    Freigabe überholen (ohne Vorrang verhungerte die Freigabe)."""
    _needs_approval_run(runs_dir)
    lock = _held_lock(monkeypatch)  # laufender Run
    release_resume = threading.Event()
    started = threading.Event()

    def fake_run_loop(*a, **k):
        started.set()
        release_resume.wait(5)

    monkeypatch.setattr(chat, "run_loop", fake_run_loop)
    chat._chat_resume("p", "r1", True)
    assert runs.has_priority_waiter("p")
    assert runs.claim_run("p", "trigger") is None

    lock.release()  # laufender Run endet → Slot kurz frei
    assert runs.claim_run("p", "trigger") is None, "neuer Run muss der Freigabe weichen"
    assert started.wait(5), "Freigabe muss fortsetzen"
    assert not runs.has_priority_waiter("p")
    release_resume.set()


def test_chat_resume_queued_twice_starts_one_worker(runs_dir, monkeypatch):
    _needs_approval_run(runs_dir)
    lock = _held_lock(monkeypatch)
    calls = []
    done = threading.Event()
    monkeypatch.setattr(chat, "run_loop", lambda *a, **k: (calls.append(1), done.set()))
    chat._chat_resume("p", "r1", True)
    out, resumed = chat._chat_resume("p", "r1", True)
    assert "bereits freigegeben" in out
    assert resumed is True
    lock.release()
    assert done.wait(5)
    time.sleep(0.1)
    assert calls == [1]


def test_chat_resume_queued_then_rejected_does_not_resume(runs_dir, monkeypatch):
    """Reject während des Wartens gewinnt: der Worker prüft den Status nach dem Slot-Erwerb."""
    f = _needs_approval_run(runs_dir)
    lock = _held_lock(monkeypatch)
    calls = []
    monkeypatch.setattr(chat, "run_loop", lambda *a, **k: calls.append(1))
    chat._chat_resume("p", "r1", True)
    chat._chat_resume("p", "r1", False)
    lock.release()
    deadline = time.time() + 5
    while "r1" in chat._resume_workers and time.time() < deadline:
        time.sleep(0.02)
    assert calls == []
    assert json.loads(f.read_text())["status"] == "rejected"


def test_chat_resume_queued_approval_with_actor_can_be_rejected(runs_dir, monkeypatch):
    """Wartende Freigabe mit Audit (approved_by gesetzt) bleibt ablehnbar."""
    f = _needs_approval_run(runs_dir)
    lock = _held_lock(monkeypatch)
    calls = []
    monkeypatch.setattr(chat, "run_loop", lambda *a, **k: calls.append(1))
    chat._chat_resume("p", "r1", True, actor="ann")
    assert json.loads(f.read_text())["approved_by"] == "ann"
    out, _ = chat._chat_resume("p", "r1", False, actor="bob")
    assert "abgelehnt" in out
    lock.release()
    deadline = time.time() + 5
    while "r1" in chat._resume_workers and time.time() < deadline:
        time.sleep(0.02)
    assert calls == []
    rec = json.loads(f.read_text())
    assert rec["status"] == "rejected" and rec["rejected_by"] == "bob" and "approval_queued" not in rec


def test_chat_resume_blocked_worker_revokes_approval(runs_dir, monkeypatch):
    """Loop während des Wartens deaktiviert: Freigabe zurückgenommen, Run erneut entscheidbar."""
    f = _needs_approval_run(runs_dir)
    lock = _held_lock(monkeypatch)
    monkeypatch.setattr(chat, "run_loop", lambda *a, **k: pytest.fail("darf nicht fortsetzen"))
    monkeypatch.setattr(chat.pause, "manual_block_reason", lambda *a: "deaktiviert")
    chat._chat_resume("p", "r1", True, actor="ann")
    lock.release()
    deadline = time.time() + 5
    while "r1" in chat._resume_workers and time.time() < deadline:
        time.sleep(0.02)
    rec = json.loads(f.read_text())
    assert rec["status"] == "needs_approval" and rec["approved_by"] is None and "approval_queued" not in rec
    assert chat.requeue_approved_runs() == []
    out, _ = chat._chat_resume("p", "r1", False, actor="bob")
    assert "abgelehnt" in out


def test_start_resume_worker_existing_worker_claims_nothing(monkeypatch):
    """Race: Worker existiert schon → kein zweiter Worker, kein Slot belegt."""
    # Im echten Set statt per monkeypatch: dessen Undo liefe erst nach
    # _drain_resume_workers, das sonst 5 s auf den Fake-Worker wartet.
    chat._resume_workers.add("r1")
    monkeypatch.setattr(chat, "spawn_priority_worker", lambda *a, **k: pytest.fail("kein zweiter Worker"))
    try:
        assert chat._start_resume_worker("p", {"id": "r1", "loop": "l"}) is False
        free = runs.claim_run("p", "other")
        assert free is not None, "Slot muss frei sein"
        free.release()
    finally:
        chat._resume_workers.discard("r1")


def test_requeue_approved_runs(runs_dir, monkeypatch):
    """Nach Restart: eingereihte Freigaben bekommen wieder einen Worker, andere nicht."""
    f = _needs_approval_run(runs_dir, "r1")
    rec = json.loads(f.read_text())
    rec["approval_queued"] = "2026-10-19T00:00:00"
    f.write_text(json.dumps(rec))
    _needs_approval_run(runs_dir, "r2")  # nicht freigegeben
    lock = _held_lock(monkeypatch)
    done = threading.Event()
    monkeypatch.setattr(chat, "run_loop", lambda *a, **k: done.set())
    assert chat.requeue_approved_runs() == ["r1"]
    lock.release()
    assert done.wait(5)


def test_chat_resume_thread_start_fails(runs_dir, monkeypatch):
    """Thread-Start-Fehler → resumed=False, Slot wird freigegeben (robuster Start).

    t.start() kann (sehr selten) RuntimeError werfen. Der Worker existiert dann
    nicht — der Slot müsste sonst für immer gehalten werden. Der Run bleibt
    needs_approval; der Client kann erneut approven.
    """
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "needs_approval"}))

    monkeypatch.setattr(runs, "max_parallel_for", lambda _name: 1)

    class _FailingThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("threads can only be started once")

    monkeypatch.setattr(chat.threading, "Thread", _FailingThread)
    out, resumed = chat._chat_resume("p", "r1", True)
    assert resumed is False
    assert "Thread-Start" in out
    free = runs.claim_run("p", "other")
    assert free is not None, "Slot muss bei Thread-Start-Fehler freigegeben werden"
    free.release()
    # Run bleibt needs_approval (wird nicht fortgesetzt)
    assert json.loads((d / "r1.json").read_text())["status"] == "needs_approval"


@pytest.mark.parametrize("max_parallel", [1, 0])  # Slot frei bzw. ausgelastet (queued)
def test_chat_resume_thread_start_fails_reverts_audit(runs_dir, monkeypatch, max_parallel):
    """Thread-Start-Fehler → kein approved_by/approval_queued im Record zurücklassen."""
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "needs_approval"}))
    monkeypatch.setattr(runs, "max_parallel_for", lambda _name: max_parallel)
    if max_parallel == 0:
        monkeypatch.setattr(engine, "claim_run", lambda *a, **k: None)

    class _FailingThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(chat.threading, "Thread", _FailingThread)
    _, resumed = chat._chat_resume("p", "r1", True, actor="anna")
    assert resumed is False
    rec = json.loads((d / "r1.json").read_text())
    assert rec["status"] == "needs_approval"
    assert rec["approved_by"] is None
    assert "approval_queued" not in rec


# ---------------------------------------------------------------- _chat_alert_test
def test_queued_resume_thread_start_fails_clears_pending(runs_dir, monkeypatch):
    """Thread-Start-Fehler beim EINGEREIHTEN Resume → Vorrang-Zähler zurücksetzen,
    sonst blieben neue Runs des Projekts für immer gesperrt."""
    held = _held_lock(monkeypatch)

    class _FailingThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(chat.threading, "Thread", _FailingThread)
    try:
        assert chat._start_resume_worker("p", {"id": "r1", "loop": "l"}) is False
        assert not runs.has_priority_waiter("p")
    finally:
        held.release()


def test_chat_alert_test_no_notify(projects_dir, tmp_path):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    assert "Kein Projekt" in chat._chat_alert_test(None)


def test_chat_alert_test_sends(projects_dir, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    (projects_dir / "p.fabrik.yaml").write_text(
        f'name: p\nroot: {root}\nnotify:\n  slack: https://x\nloops:\n  l:\n    steps:\n      - {{type: shell, cmd: "true"}}\n'
    )
    monkeypatch.setattr(chat, "_notify", lambda spec, rec: ["slack:200"])
    out = chat._chat_alert_test(None)
    assert "Test-Alerts gesendet" in out


def test_chat_alert_test_filter_project(projects_dir, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    (projects_dir / "p.fabrik.yaml").write_text(
        f'name: p\nroot: {root}\nnotify:\n  slack: https://x\nloops:\n  l:\n    steps:\n      - {{type: shell, cmd: "true"}}\n'
    )
    monkeypatch.setattr(chat, "_notify", lambda spec, rec: ["slack:200"])
    # anderes Projekt gefiltert → kein Send
    assert "Kein Projekt" in chat._chat_alert_test("anderes")


# ---------------------------------------------------------------- _llm_chat
class _FakeResp:
    def __init__(self, body):
        self._body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_llm_chat_no_provider(monkeypatch, projects_dir):
    import os

    for k in list(os.environ):
        if k.startswith("FABRIK_LLM"):
            monkeypatch.delenv(k, raising=False)
    llm.reset_cache()
    assert chat._llm_chat("hi") is None
    llm.reset_cache()


def test_llm_chat_response(clean_llm, monkeypatch, projects_dir):
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp(json.dumps({"choices": [{"message": {"content": "hallo"}}]})),
    )
    assert chat._llm_chat("hi") == "hallo"


def test_llm_chat_empty_content(clean_llm, monkeypatch, projects_dir):
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp(
            json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})
        ),
    )
    assert "leere Antwort" in not_none(chat._llm_chat("hi"))


def test_llm_chat_error(clean_llm, monkeypatch, projects_dir):
    def _boom(req, timeout=None):
        raise RuntimeError("conn")

    monkeypatch.setattr(llm.urllib.request, "urlopen", _boom)
    assert "fehlgeschlagen" in not_none(chat._llm_chat("hi"))


def test_llm_chat_cancelled(clean_llm, monkeypatch, projects_dir, tmp_path):
    ev = threading.Event()
    ev.set()
    assert chat._llm_chat("hi", ev) == "⊘ abgebrochen"


def test_llm_chat_yaml_suggestion(clean_llm, monkeypatch, projects_dir, tmp_path):
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    suggestion = (
        f'```yaml\nname: p\nroot: {root}\nloops:\n  l:\n    steps:\n      - {{type: shell, cmd: "true"}}\n```\n'
    )
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp(json.dumps({"choices": [{"message": {"content": suggestion}}]})),
    )
    out = not_none(chat._llm_chat("hi"))
    assert "Vorschlag validiert" in out


def test_llm_chat_truncated_yaml(clean_llm, monkeypatch, projects_dir):
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp(json.dumps({"choices": [{"message": {"content": "```yaml\nname: x"}}]})),
    )
    out = not_none(chat._llm_chat("hi"))
    assert "abgeschnitten" in out


# ---------------------------------------------------------------- _chat → LLM fallback
def test_chat_llm_fallback_no_config(monkeypatch, projects_dir):
    import os

    for k in list(os.environ):
        if k.startswith("FABRIK_LLM"):
            monkeypatch.delenv(k, raising=False)
    llm.reset_cache()
    out = chat._chat("was ist fabrik?")
    assert "Kein LLM konfiguriert" in out
    llm.reset_cache()


def test_chat_apply_no_pending(projects_dir):
    assert "Kein validierter Vorschlag" in chat._chat("apply p")
