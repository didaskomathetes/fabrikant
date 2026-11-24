"""Backend claude_code: One-Shot-`claude -p`-Subprozess (Fake-Popen, kein Netzwerk)."""

import json
import shutil
import subprocess
import time

import pytest

from fabrikant.backends import ClaudeCodeBackend
from fabrikant.steps.agent import _step_agent_backend

_FAKE_EXIT_TIMEOUT_S = 2.0


class _FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, b):
        self.data += b

    def close(self):
        self.closed = True

    def flush(self):
        # Modelliert subprocess.Popen.communicate(): flush nach close → ValueError
        if self.closed:
            raise ValueError("flush of closed file")


class FakePopen:
    """Popen-Doppel: poll/communicate/kill steuerbar, Stdin wird aufgezeichnet."""

    def __init__(self, args, cwd=None, **kw):
        self.args = args
        self.cwd = cwd
        self.stdin = _FakeStdin()
        self.stdin_buf = self.stdin  # stabiler Zugriff, auch nach send_prompt (stdin→None)
        self._rc: int | None = None
        self._out = b""
        self._err = b""
        self.killed = False
        self.pid = 4242  # für os.getpgid/killpg im cancel-Test

    def poll(self):
        return self._rc

    def communicate(self):
        if self.stdin is not None:
            self.stdin.flush()
            self.stdin.close()
        # Wie echtes Popen: blockiert bis Prozessende (EOF) — der Drain-Thread
        # der Base ruft communicate() schon beim Spawn.
        deadline = time.monotonic() + _FAKE_EXIT_TIMEOUT_S
        while self._rc is None and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._out, self._err

    def kill(self):
        self.killed = True


def _spawn(monkeypatch, be, sid, prompt="hello prompt", directory="/tmp/wd"):
    captured = {}

    def fake_popen(cmd, cwd=None, **kw):
        p = FakePopen(cmd, cwd=cwd, **kw)
        captured["proc"] = p
        return p

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    be.send_prompt(sid, prompt, directory)
    return captured


SUCCESS_JSON = {
    "type": "result",
    "subtype": "success",
    "result": "pong",
    "session_id": "abc",
    "total_cost_usd": 0.13,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 16223,
        "cache_read_input_tokens": 11168,
        "output_tokens": 4,
    },
    "num_turns": 1,
    "is_error": False,
}


# ---------------------------------------------------------------- health
def test_health_true_when_claude_found(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude")
    assert ClaudeCodeBackend("", "sonnet").health() is True


def test_health_false_when_claude_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert ClaudeCodeBackend("", "sonnet").health() is False


# ---------------------------------------------------------------- create_task
def test_create_task_returns_id_and_stores_record():
    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "title")
    assert sid
    rec = be._sessions[sid]
    assert rec["proc"] is None
    assert rec["workdir"] == "/tmp/wd"


# ---------------------------------------------------------------- send_prompt
def test_send_prompt_spawns_process_with_flags_and_stdin(monkeypatch):
    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "t")
    cap = _spawn(monkeypatch, be, sid)
    cmd = cap["proc"].args
    assert "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cap["proc"].cwd == "/tmp/wd"
    assert cap["proc"].stdin_buf.data == b"hello prompt"
    assert cap["proc"].stdin_buf.closed
    assert cap["proc"].stdin is None  # send_prompt schließt stdin und setzt es auf None
    assert be._sessions[sid]["proc"] is cap["proc"]


def test_send_prompt_strips_anthropic_prefix(monkeypatch):
    be = ClaudeCodeBackend("", "anthropic/sonnet")
    sid = be.create_task("/tmp/wd", "t")
    cap = _spawn(monkeypatch, be, sid)
    cmd = cap["proc"].args
    assert cmd[cmd.index("--model") + 1] == "sonnet"


# ---------------------------------------------------------------- poll
def test_poll_running_when_proc_not_started():
    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "t")
    r = be.poll(sid, "/tmp/wd")
    assert r == {
        "status": "running",
        "text": "",
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "cost": 0.0,
        "tools": [],
    }


def test_poll_running_while_process_alive():
    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "t")
    be._sessions[sid]["proc"] = FakePopen([])  # rc=None → läuft
    assert be.poll(sid, "/tmp/wd")["status"] == "running"


def test_poll_done_with_success_json():
    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p._rc = 0
    p._out = json.dumps(SUCCESS_JSON).encode()
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "done"
    assert r["text"] == "pong"
    assert r["tokens"] == {"input": 2 + 16223 + 11168, "output": 4, "cache_read": 11168, "cache_write": 16223}
    assert r["cost"] == 0.13
    assert r["tools"] == []


def test_poll_failed_on_nonzero_rc():
    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p._rc = 1
    p._err = b"boom"
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert "boom" in r["text"]
    assert r["tokens"] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert r["cost"] == 0.0


def test_poll_failed_on_invalid_json():
    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p._rc = 0
    p._out = b"not json"
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert r["text"] == "not json"


def test_poll_failed_on_error_subtype():
    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p._rc = 0
    p._out = json.dumps({**SUCCESS_JSON, "subtype": "error_max_turns", "is_error": True}).encode()
    be._sessions[sid]["proc"] = p
    assert be.poll(sid, "/tmp/wd")["status"] == "failed"


# ---------------------------------------------------------------- cancel
def test_cancel_kills_process_group(monkeypatch):
    """cancel() killt die ganze Prozessgruppe (SIGKILL via killpg), nicht nur
    den direkten Child — sonst bleiben Enkelprozesse orphaned."""
    import os
    import signal

    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    be._sessions[sid]["proc"] = p
    calls = []
    monkeypatch.setattr(os, "getpgid", lambda pid: (calls.append(("getpgid", pid)), pid)[1])
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (calls.append(("killpg", pgid, sig)), p.kill())[1])
    be.cancel(sid, "/tmp/wd")
    assert p.killed
    assert ("killpg", 4242, signal.SIGKILL) in calls


def test_cancel_unknown_session_is_noop():
    be = ClaudeCodeBackend("", "sonnet")
    be.cancel("unbekannt", "/tmp/wd")  # keine Exception


# ---------------------------------------------------------------- agent.py-Dispatch
def test_agent_backend_unknown_raises():
    with pytest.raises(ValueError, match="claude_code"):
        _step_agent_backend({}, {}, lambda m: None, "bogus", "p")


def test_agent_backend_claude_code_requires_model(monkeypatch):
    monkeypatch.delenv("FABRIKANT_AGENT_MODEL", raising=False)
    r = _step_agent_backend({}, {}, lambda m: None, "claude_code", "p")
    assert r["ok"] is False
    assert "model" in r["summary"]


def test_agent_backend_opencode_requires_endpoint(monkeypatch):
    monkeypatch.delenv("FABRIKANT_AGENT_ENDPOINT", raising=False)
    monkeypatch.delenv("FABRIKANT_AGENT_MODEL", raising=False)
    r = _step_agent_backend({}, {}, lambda m: None, "opencode", "p")
    assert r["ok"] is False
    assert "endpoint" in r["summary"]


def test_poll_failed_json_with_nonzero_rc_reports_result():
    """Exit ≠ 0 mit JSON-Ergebnis (z. B. Nutzungslimit): `result` statt roher JSON-Anfang (ADR 0035)."""
    be = ClaudeCodeBackend("", "sonnet")
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p._rc = 1
    p._out = json.dumps(
        {**SUCCESS_JSON, "is_error": True, "result": "Claude AI usage limit reached|1761490800"}
    ).encode()
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert r["text"] == "Claude AI usage limit reached|1761490800"


def test_unhealthy_kind_is_environment():
    assert ClaudeCodeBackend("", "sonnet").unhealthy_kind().value == "environment"  # CLI fehlt
