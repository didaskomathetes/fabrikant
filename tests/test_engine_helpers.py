"""Engine-Helfer: Record-Persist (Store-Write: Indentation + Redaction) und Abbruch-Status-Mapping."""

import json

import pytest

from fabrikant import engine


def test_persist_writes_indented_json(runs_dir):
    # `_persist` schreibt über den Store (Vorbedingung: On-Disk-Status running/needs_approval) —
    # der Record muss daher bereits existieren.
    engine.STORE.create("p", "r1", {"id": "r1", "status": "running", "note": "alt"})
    engine._persist("p", "r1", {"id": "r1", "status": "running", "note": "ü"})
    text = (runs_dir / "p" / "r1.json").read_text()
    assert json.loads(text) == {"id": "r1", "status": "running", "note": "ü"}
    assert "ü" in text and "\n " in text  # ensure_ascii=False, indent=1


def test_persist_redacts(runs_dir, monkeypatch):
    engine.STORE.create("p", "r2", {"id": "r2", "status": "running", "note": "secret"})

    # Redaction passiert im Store (runstore._redact_record) — dort patchen, nicht in engine.
    # In-place, wie der echte `_redact_record` (der Return-Wert wird ignoriert).
    def _fake_redact(r):
        r["note"] = "***"

    monkeypatch.setattr("fabrikant.runstore._redact_record", _fake_redact)
    engine._persist("p", "r2", {"id": "r2", "status": "running", "note": "secret"})
    assert json.loads((runs_dir / "p" / "r2.json").read_text())["note"] == "***"


def test_abort_status_mapping():
    assert engine._ABORT_STATUS == {"fail": "fail", "timeout": "fail", "error": "error"}


@pytest.mark.parametrize("cmd", ["true", "false"])
def test_run_reaped_before_final_write_keeps_reaper_outcome(runs_dir, projects_dir, monkeypatch, cmd):
    """Reaper/Stale-Check beendet den Run vor dem finalen Write (running → error, Kette und
    Alerts dort gebucht): die Engine überschreibt den Record nicht und bucht nichts doppelt."""
    (projects_dir / "reaped.fabrikant.yaml").write_text(
        f"name: reaped\nroot: /tmp\nloops:\n  t:\n    steps:\n      - type: shell\n        cmd: '{cmd}'\n"
    )
    real_finish = engine._finish_workdir

    def finish_and_reap(rn, *, paused):
        real_finish(rn, paused=paused)
        engine.STORE.reap(rn.project, rn.run_id, error="stale")

    booked: list[str] = []
    monkeypatch.setattr(engine, "_finish_workdir", finish_and_reap)
    monkeypatch.setattr(engine.retry_chains, "on_run_finished_safe", lambda *a: booked.append("chain") or {})
    monkeypatch.setattr(engine, "_notify", lambda *a, **k: booked.append("notify") or [])

    rec = engine.run_loop("reaped", "t")

    on_disk = json.loads((runs_dir / "reaped" / f"{rec['id']}.json").read_text())
    assert rec["status"] == on_disk["status"] == "error" and on_disk["error"] == "stale"
    assert booked == []


def test_fail_queued_record_leaves_started_run(runs_dir, monkeypatch):
    """Race: queued gelesen, run_loop übernimmt den Platzhalter (running) vor dem Write — der
    laufende Run wird nicht mit error überschrieben."""
    engine.STORE.create("p", "r1", {"id": "r1", "status": "running", "note": "live"})
    monkeypatch.setattr(engine, "get_run", lambda run_id, project: {"id": run_id, "status": "queued"})
    engine._fail_queued_record("p", "r1", "Worker-Start fehlgeschlagen")
    assert json.loads((runs_dir / "p" / "r1.json").read_text())["status"] == "running"


def test_dispatch_unknown_connection_fails_as_environment():
    """Step mit unbekannter `connection:` → Environment-Fehler statt Crash (ADR 0052)."""
    from fabrikant.engine import _dispatch_step
    from fabrikant.failures import FailureKind

    res = _dispatch_step({}, {"type": "agent", "connection": "gibt-es-nicht", "prompt": "x"}, lambda *_: None)
    assert res["ok"] is False and "unbekannt" in res["summary"]
    assert res["failure_kind"] == FailureKind.ENVIRONMENT.value
