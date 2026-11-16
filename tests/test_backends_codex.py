"""Backends: codex (OpenAI Codex CLI, One-Shot `codex exec`).

Die One-Shot-Mechanik (Popen/poll/cancel) lebt in `fabrik.backends._oneshot`
— die Fakes patchen daher `subprocess.Popen` / `os.killpg` dort. Buffers sind
Bytes (die Base schreibt `prompt.encode()` und dekodiert stdout/stderr).
"""

import io
import os
import signal
import time

import pytest

from fabrik.backends.codex import CodexBackend

_FAKE_EXIT_TIMEOUT_S = 2.0


class FakeStdin:
    """Bytes-Buffer; close() markiert closed, flush() nach close → ValueError.

    Modelliert `subprocess.Popen.communicate()` (flush des stdin-Streams)."""

    def __init__(self):
        self.buf = io.BytesIO()
        self.closed = False

    def write(self, b):
        self.buf.write(b)

    def close(self):
        self.closed = True

    def flush(self):
        if self.closed:
            raise ValueError("flush of closed file")

    def getvalue(self):
        return self.buf.getvalue()


class FakePopen:
    initial_returncode: int | None = None

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.kw = kw
        self.pid = 555
        self.returncode = FakePopen.initial_returncode
        self.stdout_buf = io.BytesIO(b"done-output")
        self.stderr_buf = io.BytesIO(b"")
        self.stdin = FakeStdin()
        self.stdin_buf = self.stdin  # stabiler Zugriff, auch nach send_prompt (stdin→None)

    def poll(self):
        return self.returncode

    def communicate(self):
        if self.stdin is not None:
            self.stdin.flush()
            self.stdin.close()
        # Wie echtes Popen: blockiert bis Prozessende (EOF) — der Drain-Thread
        # der Base ruft communicate() schon beim Spawn.
        deadline = time.monotonic() + _FAKE_EXIT_TIMEOUT_S
        while self.returncode is None and time.monotonic() < deadline:
            time.sleep(0.005)
        return self.stdout_buf.getvalue(), self.stderr_buf.getvalue()


def _mk(tmp_path, monkeypatch):
    """codex-CLI-Stub auf PATH + FakePopen (via _oneshot); → (backend, popen-calls)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "codex"
    exe.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(exe, 0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    calls: list = []

    def fake_popen(cmd, cwd=None, **kw):
        p = FakePopen(cmd, cwd=cwd, **kw)
        calls.append(p)
        return p

    monkeypatch.setattr("fabrik.backends._oneshot.subprocess.Popen", fake_popen)
    return CodexBackend(), calls


def test_health_detects_cli(tmp_path, monkeypatch):
    be, _ = _mk(tmp_path, monkeypatch)
    assert be.health() is True
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert be.health() is False


def test_send_prompt_flags_and_stdin(tmp_path, monkeypatch):
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "do the thing", "/wd")
    p = calls[0]
    assert p.cmd[:4] == ["codex", "exec", "--sandbox", "workspace-write"]
    assert "--model" not in p.cmd
    assert p.stdin_buf.getvalue() == b"do the thing"
    assert p.stdin is None  # send_prompt schließt stdin und setzt es auf None
    assert p.kw.get("cwd") == "/wd"
    assert p.kw.get("start_new_session") is True


def test_send_prompt_model_flag(tmp_path, monkeypatch):
    be, calls = _mk(tmp_path, monkeypatch)
    be.model = "gpt-5-codex"
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    assert calls[0].cmd[-2:] == ["--model", "gpt-5-codex"]


def test_sandbox_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRIK_CODEX_SANDBOX", "danger-full-access")
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    assert "--sandbox" in calls[0].cmd
    assert calls[0].cmd[calls[0].cmd.index("--sandbox") + 1] == "danger-full-access"


def test_one_shot_second_prompt_raises(tmp_path, monkeypatch):
    be, _ = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    with pytest.raises(RuntimeError, match="One-Shot"):
        be.send_prompt(sid, "again", "/wd")


def test_poll_running_then_done(tmp_path, monkeypatch):
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    r = be.poll(sid, "/wd")
    assert r["status"] == "running"
    calls[0].returncode = 0
    r = be.poll(sid, "/wd")
    assert r["status"] == "done"
    assert r["text"] == "done-output"


def test_poll_failed_captures_stderr(tmp_path, monkeypatch):
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    calls[0].stderr_buf = io.BytesIO(b"boom: auth required")  # Output vor Exit (wie real)
    calls[0].returncode = 1
    r = be.poll(sid, "/wd")
    assert r["status"] == "failed"
    assert "boom" in r["text"]


def test_cancel_kills_process_group(tmp_path, monkeypatch):
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    killed = []
    monkeypatch.setattr("fabrik.procs.os.getpgid", lambda pid: 555)
    monkeypatch.setattr("fabrik.procs.os.killpg", lambda pg, sig: killed.append((pg, sig)))
    be.cancel(sid, "/wd")
    assert killed == [(555, signal.SIGKILL)]


def test_cancel_after_exit_is_noop(tmp_path, monkeypatch):
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    calls[0].returncode = 0
    killed = []
    monkeypatch.setattr("fabrik.procs.os.killpg", lambda pg, sig: killed.append((pg, sig)))
    be.cancel(sid, "/wd")
    assert killed == []


def test_cancel_unknown_session_is_noop(tmp_path, monkeypatch):
    be, _ = _mk(tmp_path, monkeypatch)
    be.cancel("codex-unknown", "/wd")  # kein Raise


def test_dispatch_agent_step_codex(tmp_path, monkeypatch):
    from fabrik.steps.agent import _step_agent

    _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(FakePopen, "initial_returncode", 0)  # Prozess ist sofort fertig
    ctx = {"project": "p", "run_id": "r1", "workdir": str(tmp_path)}
    step = {"name": "a", "prompt": "p", "backend": "codex"}
    res = _step_agent(ctx, step, lambda _m: None)
    assert res["ok"] is True
    assert res["output"] == "done-output"
    assert res["agent_session"].startswith("codex-")
