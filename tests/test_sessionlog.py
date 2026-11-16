"""Tests: SQLite Session-Log (ADR 0002) — Schema, Lifecycle, Redaction,
Importer, Engine-Wiring, Agent-Events."""

import json
import sqlite3

import pytest

from fabrik import secrets, sessionlog
from fabrik.engine import run_loop


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Frisches Session-Log-File pro Test (Global-Connection reset)."""
    monkeypatch.setenv("FABRIK_SESSIONLOG", str(tmp_path / "sl" / "test.db"))
    monkeypatch.setattr(sessionlog, "_conn", None)
    yield sessionlog.connect()
    monkeypatch.setattr(sessionlog, "_conn", None)


def _events(c, sid):
    return [r[0] for r in c.execute("SELECT kind FROM events WHERE session_id=? ORDER BY seq", (sid,))]


# ---------------------------------------------------------------- Schema
def test_schema_created(fresh_db):
    tables = {r[0] for r in fresh_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "events", "artifacts", "checkpoints"} <= tables


# ---------------------------------------------------------------- Lifecycle
def test_lifecycle(fresh_db):
    lg = sessionlog.SessionLog("s1", "proj", "loop", trigger="cron", spec_hash="abc")
    lg.start()
    lg.event("step_start", step_name="a", step_type="shell")
    lg.event("step_done", step_name="a", step_type="shell", status="pass", duration_s=1.0)
    lg.checkpoint(0, "a", "pass", duration_s=1.0)
    lg.artifact("a", "report", "/tmp/x.md")
    lg.finish("pass", tokens_in=10, tokens_out=5, cost_usd=0.001)

    row = fresh_db.execute(
        "SELECT status, tokens_in, tokens_out, cost_usd, source FROM sessions WHERE session_id='s1'"
    ).fetchone()
    assert row == ("pass", 10, 5, 0.001, "native")
    assert _events(fresh_db, "s1") == ["run_start", "step_start", "step_done", "run_end"]
    cp = fresh_db.execute("SELECT step_idx, step_name, status FROM checkpoints WHERE session_id='s1'").fetchall()
    assert cp == [(0, "a", "pass")]
    ar = fresh_db.execute("SELECT kind, path FROM artifacts WHERE session_id='s1'").fetchall()
    assert ar == [("report", "/tmp/x.md")]


def test_set_status_without_run_end(fresh_db):
    lg = sessionlog.SessionLog("s2", "proj", "loop")
    lg.start()
    lg.set_status("needs_approval")
    lg.event("approval", step_name="a")
    status = fresh_db.execute("SELECT status FROM sessions WHERE session_id='s2'").fetchone()[0]
    assert status == "needs_approval"
    assert "run_end" not in _events(fresh_db, "s2")


def test_reap_orphaned_run_finishes_sessionlog(fresh_db, runs_dir):
    from fabrik import runs

    lg = sessionlog.SessionLog("orphan-1", "proj", "loop")
    lg.start()
    d = runs_dir / "proj"
    d.mkdir()
    (d / "orphan-1.json").write_text(json.dumps({"id": "orphan-1", "project": "proj", "status": "running"}))

    assert runs.reap_orphaned_runs() == ["orphan-1"]
    record = json.loads((d / "orphan-1.json").read_text())
    row = fresh_db.execute("SELECT status, finished_at FROM sessions WHERE session_id='orphan-1'").fetchone()
    assert row == ("error", record["finished"])


def test_finish_orphaned_run_sqlite_error_warns(monkeypatch, capsys):
    def boom():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sessionlog, "connect", boom)
    sessionlog.finish_orphaned_run("x", "2026-01-01T00:00:00")  # darf nicht werfen
    assert "disk I/O error" in capsys.readouterr().out


def test_serve_reaps_before_scheduler(monkeypatch):
    from fabrik import server

    order = []

    class FakeServer:
        def __init__(self, *a):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

    class FakeSched:
        def start(self):
            order.append("scheduler")

    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(server, "Scheduler", FakeSched)
    monkeypatch.setattr(server, "reap_orphaned_runs", lambda: order.append("reap") or [])
    monkeypatch.setattr(server, "requeue_approved_runs", lambda: order.append("approved") or [])
    monkeypatch.setattr(server, "requeue_queued_runs", lambda: order.append("queued") or [])
    server.serve(0)
    assert order[0] == "reap"
    assert order.index("scheduler") > order.index("queued")


def test_resume_no_seq_collision(fresh_db):
    # Review M1/L3: Resume setzt den seq-Counter an, Status zurück auf running,
    # keine doppelte sessions-Zeile.
    lg = sessionlog.SessionLog("r1", "proj", "loop")
    lg.start()
    lg.event("step_start", step_name="a", step_type="shell")
    lg.set_status("needs_approval")
    lg2 = sessionlog.SessionLog("r1", "proj", "loop")
    lg2.start()
    lg2.event("step_done", step_name="a", step_type="shell", status="pass")
    lg2.finish("pass")
    seqs = [r[0] for r in fresh_db.execute("SELECT seq FROM events WHERE session_id='r1' ORDER BY seq")]
    assert len(seqs) == len(set(seqs)) and seqs == sorted(seqs)  # eindeutig + monoton
    assert len(seqs) == 5
    n = fresh_db.execute("SELECT COUNT(*) FROM sessions WHERE session_id='r1'").fetchone()[0]
    assert n == 1
    status = fresh_db.execute("SELECT status FROM sessions WHERE session_id='r1'").fetchone()[0]
    assert status == "pass"


def test_checkpoint_duplicate_kept(fresh_db):
    # Review M3: append-only — Dup-Checkpoint (Step-Rerun) bleibt das alte.
    lg = sessionlog.SessionLog("s9", "p", "l")
    lg.start()
    lg.checkpoint(0, "a", "pass", duration_s=1.0)
    lg.checkpoint(0, "a", "pass", duration_s=9.9)
    n = fresh_db.execute("SELECT COUNT(*) FROM checkpoints WHERE session_id='s9'").fetchone()[0]
    assert n == 1
    d = json.loads(fresh_db.execute("SELECT data FROM checkpoints WHERE session_id='s9'").fetchone()[0])
    assert d["duration_s"] == 1.0


# ---------------------------------------------------------------- Redaction
def test_event_data_redacted(fresh_db, monkeypatch):
    monkeypatch.setattr(secrets, "_runtime_secrets", ["geheim-wert-123"])
    lg = sessionlog.SessionLog("s3", "proj", "loop")
    lg.start()
    lg.event("step_done", step_name="a", status="pass", output="PASS=geheim-wert-123 ok")
    blobs = [r[0] for r in fresh_db.execute("SELECT data FROM events WHERE session_id='s3'")]
    assert all("geheim-wert-123" not in b for b in blobs)


# ---------------------------------------------------------------- Importer
def _fake_run(sid="old-1", status="pass"):
    return {
        "id": sid,
        "project": "oldproj",
        "loop": "l",
        "trigger": "cron",
        "status": status,
        "spec_hash": "abc",
        "started": "2026-01-01T00:00:00+00:00",
        "finished": "2026-01-01T00:00:10+00:00",
        "steps": [
            {"name": "a", "type": "shell", "status": "pass", "duration_s": 1.0, "summary": "ok"},
            {"name": "b", "type": "shell", "status": "fail", "duration_s": 2.0, "summary": "nope"},
        ],
    }


def test_import_runs(fresh_db, runs_dir):
    d = runs_dir / "oldproj"
    d.mkdir()
    (d / "old-1.json").write_text(json.dumps(_fake_run()))
    assert sessionlog.import_runs(runs_dir) == 1
    row = fresh_db.execute("SELECT source, status FROM sessions WHERE session_id='old-1'").fetchone()
    assert row == ("import", "pass")
    assert _events(fresh_db, "old-1") == ["step_done", "step_done"]
    cps = fresh_db.execute(
        "SELECT step_idx, status FROM checkpoints WHERE session_id='old-1' ORDER BY step_idx"
    ).fetchall()
    assert cps == [(0, "pass")]  # nur der pass-Step wird gecheckpointet
    # Idempotenz
    assert sessionlog.import_runs(runs_dir) == 0


def test_import_skips_native_sessions(fresh_db, runs_dir):
    lg = sessionlog.SessionLog("native-1", "p", "l")
    lg.start()
    d = runs_dir / "p"
    d.mkdir()
    (d / "native-1.json").write_text(json.dumps(_fake_run(sid="native-1")))
    assert sessionlog.import_runs(runs_dir) == 0
    status = fresh_db.execute("SELECT source, status FROM sessions WHERE session_id='native-1'").fetchone()
    assert status == ("native", "running")


def test_import_best_effort_connect(fresh_db, runs_dir, monkeypatch):
    # Review M2: Connect-Fehler → 0, keine Exception.
    def _boom(*a, **k):
        raise sqlite3.OperationalError("defekt")

    monkeypatch.setattr(sessionlog, "connect", _boom)
    assert sessionlog.import_runs(runs_dir) == 0


def test_import_skips_broken_files(fresh_db, runs_dir):
    # Review M2: defekte Dateien werden übersprungen, gute landen.
    d = runs_dir / "oldproj"
    d.mkdir()
    (d / "ok-1.json").write_text(json.dumps(_fake_run(sid="ok-1")))
    (d / "bad-1.json").write_text("{defekt")
    (d / "no-id.json").write_text(json.dumps({"steps": []}))
    assert sessionlog.import_runs(runs_dir) == 1
    row = fresh_db.execute("SELECT source FROM sessions WHERE session_id='ok-1'").fetchone()
    assert row == ("import",)


def test_import_backfills_usage(fresh_db, runs_dir):
    # Review L6: importierte Sessions tragen die Step-Usage-Summen.
    rec = _fake_run(sid="u-1")
    rec["steps"][0]["usage"] = {"prompt_tokens": 7, "completion_tokens": 3, "cost_usd": 0.002}
    d = runs_dir / "oldproj"
    d.mkdir()
    (d / "u-1.json").write_text(json.dumps(rec))
    assert sessionlog.import_runs(runs_dir) == 1
    row = fresh_db.execute("SELECT tokens_in, tokens_out, cost_usd FROM sessions WHERE session_id='u-1'").fetchone()
    assert row == (7, 3, 0.002)


# ---------------------------------------------------------------- Engine
def _write_spec(projects_dir, tmp_path, name, steps_yaml):
    root = tmp_path / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    body = "\n".join("    " + ln for ln in steps_yaml.strip().splitlines())
    (projects_dir / f"{name}.fabrik.yaml").write_text(f"name: {name}\nroot: {root}\nloops:\n  s:\n    steps:\n{body}\n")
    return root


def test_engine_writes_sessionlog(fresh_db, projects_dir, tmp_path):
    _write_spec(projects_dir, tmp_path, "slproj", "- name: ok\n  type: shell\n  cmd: echo hi\n")
    rec = run_loop("slproj", "s", trigger="test")
    assert rec["status"] == "pass"
    row = fresh_db.execute("SELECT status, tokens_in, source FROM sessions WHERE session_id=?", (rec["id"],)).fetchone()
    assert row == ("pass", 0, "native")
    assert _events(fresh_db, rec["id"]) == ["run_start", "step_start", "step_done", "run_end"]
    cp = fresh_db.execute("SELECT step_name, status FROM checkpoints WHERE session_id=?", (rec["id"],)).fetchall()
    assert cp == [("ok", "pass")]


def test_engine_approval_event(fresh_db, projects_dir, tmp_path):
    _write_spec(
        projects_dir,
        tmp_path,
        "gateproj",
        "- name: ok\n  type: shell\n  cmd: echo hi\n- name: gated\n  type: shell\n  cmd: echo x\n  approval: true\n",
    )
    rec = run_loop("gateproj", "s", trigger="test")
    assert rec["status"] == "needs_approval"
    status = fresh_db.execute("SELECT status FROM sessions WHERE session_id=?", (rec["id"],)).fetchone()[0]
    assert status == "needs_approval"
    assert "approval" in _events(fresh_db, rec["id"])


# ---------------------------------------------------------------- Agent
class _FakeOpenCode:
    """Fakes den opencode-Poll-Stream: 2 Polls (running → done), 2 Tools."""

    @classmethod
    def create(cls, step, endpoint, model, stale_after):
        return cls(endpoint, model, stale_after=stale_after)

    def __init__(self, endpoint, model, stale_after=900):
        self._seq = iter(
            [
                {
                    "status": "running",
                    "text": "arbeite...",
                    "tools": ["read"],
                    "tokens": {"input": 4, "output": 1},
                    "cost": 0.0,
                },
                {
                    "status": "done",
                    "text": "Erledigt",
                    "tools": ["read", "bash"],
                    "tokens": {"input": 10, "output": 5},
                    "cost": 0.001,
                },
            ]
        )

    def health(self):
        return True

    def create_task(self, workdir, label, permissions=None):
        return "sess-fake"

    def send_prompt(self, sid, prompt, workdir):
        pass

    def poll(self, sid, workdir):
        return next(self._seq)

    def transcript(self, sid, workdir):
        return []

    def cancel(self, sid, workdir):
        pass


def test_agent_events_landed(fresh_db, projects_dir, tmp_path, monkeypatch):
    import fabrik.steps.agent as agent_mod
    from fabrik.backends import registry

    monkeypatch.setitem(registry.ADAPTERS, "opencode", _FakeOpenCode)
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    _write_spec(
        projects_dir,
        tmp_path,
        "agentproj",
        "- name: agent-step\n  type: agent\n  backend: opencode\n"
        "  endpoint: http://127.0.0.1:9\n  model: fake-model\n  prompt: Mach was\n",
    )
    rec = run_loop("agentproj", "s", trigger="test")
    assert rec["status"] == "pass"
    kinds = _events(fresh_db, rec["id"])
    assert "tool_call" in kinds and "message" in kinds and "cost" in kinds
    tools = [
        json.loads(r[0]).get("tool")
        for r in fresh_db.execute(
            "SELECT data FROM events WHERE session_id=? AND kind='tool_call' ORDER BY seq", (rec["id"],)
        )
    ]
    assert tools == ["read", "bash"]  # Dedupe: read nur einmal
    row = fresh_db.execute(
        "SELECT tokens_in, tokens_out, cost_usd FROM sessions WHERE session_id=?", (rec["id"],)
    ).fetchone()
    assert row == (10, 5, 0.001)  # keine Doppelzählung


class _FlakyTranscriptOpenCode(_FakeOpenCode):
    """Transcript liefert erst Messages (mit Tool-Part), dann temporär ``[]``."""

    def __init__(self, endpoint, model, stale_after=900):
        super().__init__(endpoint, model, stale_after)
        self._transcripts = iter(
            [
                [{"info": {"finish": "stop"}, "parts": [{"type": "tool", "tool": "read"}]}],
                [],
            ]
        )

    def transcript(self, sid, workdir):
        return next(self._transcripts, [])


def test_agent_empty_transcript_poll_does_not_duplicate_tools(fresh_db, projects_dir, tmp_path, monkeypatch):
    import fabrik.steps.agent as agent_mod
    from fabrik.backends import registry

    monkeypatch.setitem(registry.ADAPTERS, "opencode", _FlakyTranscriptOpenCode)
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    _write_spec(
        projects_dir,
        tmp_path,
        "agentproj3",
        "- name: agent-step\n  type: agent\n  backend: opencode\n"
        "  endpoint: http://127.0.0.1:9\n  model: fake-model\n  prompt: Mach was\n",
    )
    rec = run_loop("agentproj3", "s", trigger="test")
    assert rec["status"] == "pass"
    tools = [
        json.loads(r[0]).get("tool")
        for r in fresh_db.execute(
            "SELECT data FROM events WHERE session_id=? AND kind='tool_call' ORDER BY seq", (rec["id"],)
        )
    ]
    # Poll 1: Tool aus der Message; Poll 2 (leeres Transcript): nur das neue "bash".
    assert [t["tool"] if isinstance(t, dict) else t for t in tools] == ["read", "bash"]


def test_agent_usage_in_run_record(fresh_db, projects_dir, tmp_path, monkeypatch):
    import fabrik.steps.agent as agent_mod
    from fabrik.backends import registry

    monkeypatch.setitem(registry.ADAPTERS, "opencode", _FakeOpenCode)
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    _write_spec(
        projects_dir,
        tmp_path,
        "agentproj2",
        "- name: agent-step\n  type: agent\n  backend: opencode\n"
        "  endpoint: http://127.0.0.1:9\n  model: fake-model\n  prompt: Mach was\n",
    )
    rec = run_loop("agentproj2", "s", trigger="test")
    step = rec["steps"][0]
    assert step["usage"]["provider"] == "opencode"
    assert step["usage"]["prompt_tokens"] == 10
    assert step["usage"]["completion_tokens"] == 5
    assert step["usage"]["cost_usd"] == 0.001
