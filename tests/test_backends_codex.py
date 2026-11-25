"""Backends: codex (OpenAI Codex CLI, One-Shot `codex exec`).

Die One-Shot-Mechanik (Popen/poll/cancel) lebt in `fabrikant.backends._oneshot`
— die Fakes patchen daher `subprocess.Popen` / `os.killpg` dort. Buffers sind
Bytes (die Base schreibt `prompt.encode()` und dekodiert stdout/stderr).
"""

import io
import os
import signal
import time

import pytest

from fabrikant.backends.codex import CodexBackend

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

    monkeypatch.setattr("fabrikant.backends._oneshot.subprocess.Popen", fake_popen)
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
    assert p.cmd[:5] == ["codex", "exec", "--json", "--sandbox", "workspace-write"]
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
    monkeypatch.setenv("FABRIKANT_CODEX_SANDBOX", "danger-full-access")
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


_SPEND_CAP = "You hit your spend cap set by the owner of your workspace."


def test_poll_failed_reports_error_lines_not_banner(tmp_path, monkeypatch):
    """Real output: warning + banner + prompt before the error — the cause must not disappear behind
    the truncation; duplicate ERROR lines once."""
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    banner = "WARNING: could not create PATH aliases\n" + "OpenAI Codex v0.167.0\n" + "x" * 400 + "\n"
    calls[0].stderr_buf = io.BytesIO(f"{banner}ERROR: {_SPEND_CAP}\nERROR: {_SPEND_CAP}\n".encode())
    calls[0].returncode = 1
    r = be.poll(sid, "/wd")
    assert r["status"] == "failed"
    assert r["text"] == _SPEND_CAP


def test_poll_failed_without_output(tmp_path, monkeypatch):
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    calls[0].stdout_buf = io.BytesIO(b"")
    calls[0].returncode = 1
    assert be.poll(sid, "/wd")["text"] == "codex: kein Output"


def test_cancel_kills_process_group(tmp_path, monkeypatch):
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    killed = []
    monkeypatch.setattr("fabrikant.procs.os.getpgid", lambda pid: 555)
    monkeypatch.setattr("fabrikant.procs.os.killpg", lambda pg, sig: killed.append((pg, sig)))
    be.cancel(sid, "/wd")
    assert killed == [(555, signal.SIGKILL)]


def test_cancel_after_exit_is_noop(tmp_path, monkeypatch):
    be, calls = _mk(tmp_path, monkeypatch)
    sid = be.create_task("/wd", "t")
    be.send_prompt(sid, "p", "/wd")
    calls[0].returncode = 0
    killed = []
    monkeypatch.setattr("fabrikant.procs.os.killpg", lambda pg, sig: killed.append((pg, sig)))
    be.cancel(sid, "/wd")
    assert killed == []


def test_cancel_unknown_session_is_noop(tmp_path, monkeypatch):
    be, _ = _mk(tmp_path, monkeypatch)
    be.cancel("codex-unknown", "/wd")  # kein Raise


def test_dispatch_agent_step_codex(tmp_path, monkeypatch):
    from fabrikant.steps.agent import _step_agent

    _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(FakePopen, "initial_returncode", 0)  # Prozess ist sofort fertig
    ctx = {"project": "p", "run_id": "r1", "workdir": str(tmp_path)}
    step = {"name": "a", "prompt": "p", "backend": "codex"}
    res = _step_agent(ctx, step, lambda _m: None)
    assert res["ok"] is True
    assert res["output"] == "done-output"
    assert res["agent_session"].startswith("codex-")


# ---------------------------------------------------------------- Usage (--json, Issue #1)
_JSONL = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i0","type":"reasoning","text":"hm"}}',
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"first"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":7}}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i2","type":"agent_message","text":"final answer"}}',
        '{"type":"turn.completed","usage":{"input_tokens":150,"cached_input_tokens":40,"cache_write_input_tokens":20,"output_tokens":10}}',
    ]
)


def test_parse_takes_cumulative_usage_of_last_turn_and_last_agent_message():
    """turn.completed trägt die Thread-Summe — Addieren würde frühere Turns doppelt zählen."""
    r = CodexBackend()._parse(_JSONL, "", 0)
    assert r["status"] == "done" and r["text"] == "final answer"
    assert r["tokens"] == {"input": 150, "output": 10, "cache_read": 40, "cache_write": 20}
    assert r["cost"] == 0.0


def test_parse_plain_stdout_fallback_has_zero_tokens():
    r = CodexBackend()._parse("just text\n", "", 0)
    assert r["text"] == "just text" and r["tokens"] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def test_parse_ignores_garbage_and_bad_usage_values():
    out = 'not json\n[1]\n{"type":"turn.completed","usage":{"input_tokens":"x","output_tokens":-3}}\n'
    r = CodexBackend()._parse(out, "", 0)
    assert r["tokens"] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def test_parse_failure_reports_turn_failed_message_and_keeps_usage():
    out = '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":1}}\n{"type":"turn.failed","error":{"message":"spend cap reached"}}\n'
    r = CodexBackend()._parse(out, "", 1)
    assert r["status"] == "failed" and r["text"] == "spend cap reached"
    assert r["tokens"] == {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0}


def test_parse_failure_falls_back_to_stderr():
    r = CodexBackend()._parse("", "boom", 2)
    assert r["status"] == "failed" and r["text"] == "boom"
