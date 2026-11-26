"""Backends: copilot (GitHub Copilot CLI, One-Shot `copilot -p`)."""

import io
import json
import os
import shutil
import signal
import subprocess
import time

import pytest

from fabrikant.backends import CopilotBackend
from fabrikant.steps.agent import _step_agent_backend

_FAKE_EXIT_TIMEOUT_S = 2.0


class FakeStdin:
    """Bytes-Buffer; close() markiert closed, flush() nach close → ValueError.

    Modelliert `subprocess.Popen`: `communicate()` flusht den (ggf. schon
    geschlossenen) stdin-Stream — genau das, was in Produktion
    `ValueError: flush of closed file` ausgelöst hat."""

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

    def __init__(self, cmd, cwd=None, **kw):
        self.cmd = cmd
        self.cwd = cwd
        self.kw = kw
        self.pid = 777
        self.returncode = FakePopen.initial_returncode
        self._out = b""
        self._err = b""
        self.killed = False
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
        return self._out, self._err

    def kill(self):
        self.killed = True


def _mk(tmp_path, monkeypatch):
    """copilot-CLI-Stub auf PATH + FakePopen; → (popen-calls)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "copilot"
    exe.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(exe, 0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    calls: list = []

    def fake_popen(cmd, cwd=None, **kw):
        p = FakePopen(cmd, cwd=cwd, **kw)
        calls.append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return calls


def _spawn(monkeypatch, be, sid, prompt="hello prompt", directory="/tmp/wd"):
    captured = {}

    def fake_popen(cmd, cwd=None, **kw):
        p = FakePopen(cmd, cwd=cwd, **kw)
        captured["proc"] = p
        return p

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    be.send_prompt(sid, prompt, directory)
    return captured


# ---------------------------------------------------------------- health
def test_health_true_when_copilot_found(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/copilot")
    assert CopilotBackend().health() is True


def test_health_false_when_copilot_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert CopilotBackend().health() is False


# ---------------------------------------------------------------- create_task
def test_create_task_returns_id_and_stores_record():
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "title")
    assert sid.startswith("copilot-")
    rec = be._sessions[sid]
    assert rec["proc"] is None
    assert rec["workdir"] == "/tmp/wd"


# ---------------------------------------------------------------- send_prompt
def test_send_prompt_spawns_with_default_flags_and_stdin(monkeypatch):
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    cap = _spawn(monkeypatch, be, sid)
    cmd = cap["proc"].cmd
    assert cmd[0] == "copilot"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--no-ask-user" in cmd
    assert "--allow-all-tools" in cmd
    assert "--model" not in cmd
    assert cap["proc"].cwd == "/tmp/wd"
    assert cap["proc"].stdin_buf.getvalue() == b"hello prompt"
    assert cap["proc"].stdin is None  # send_prompt schließt stdin und setzt es auf None
    assert be._sessions[sid]["proc"] is cap["proc"]


def test_send_prompt_sets_model_and_strips_anthropic_prefix(monkeypatch):
    be = CopilotBackend(model="anthropic/claude-sonnet-4-5")
    sid = be.create_task("/tmp/wd", "t")
    cap = _spawn(monkeypatch, be, sid)
    cmd = cap["proc"].cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-5"


def test_send_prompt_tools_override_uses_allow_tool(monkeypatch):
    be = CopilotBackend(tools="shell,write")
    sid = be.create_task("/tmp/wd", "t")
    cap = _spawn(monkeypatch, be, sid)
    cmd = cap["proc"].cmd
    assert "--allow-tool=shell,write" in cmd
    assert "--allow-all-tools" not in cmd


def test_create_accepts_tools_list(monkeypatch):
    monkeypatch.delenv("FABRIKANT_COPILOT_TOOLS", raising=False)
    be = CopilotBackend.create({"tools": ["shell", "write"]}, "", "", 900)
    assert be.tools == "shell,write"


def test_send_prompt_sets_auto_update_env(monkeypatch):
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    cap = _spawn(monkeypatch, be, sid)
    assert cap["proc"].kw.get("env", {}).get("COPILOT_AUTO_UPDATE") == "false"


def test_send_prompt_one_shot_only(monkeypatch):
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    _spawn(monkeypatch, be, sid)
    with pytest.raises(RuntimeError, match="One-Shot"):
        be.send_prompt(sid, "again", "/tmp/wd")


# ---------------------------------------------------------------- _extract_result
def test_extract_result_from_result_field():
    out = '{"type":"message","role":"assistant","content":"thinking"}\n{"type":"result","result":"final answer"}\n'
    assert CopilotBackend._extract_result(out) == ("final answer", True)


def test_extract_result_from_assistant_text():
    out = '{"type":"message","role":"assistant","content":"just text"}\n'
    assert CopilotBackend._extract_result(out) == ("just text", True)


def test_extract_result_from_content_blocks():
    out = '{"role":"assistant","content":[{"type":"text","text":"block "},{"type":"text","text":"text"}]}\n'
    assert CopilotBackend._extract_result(out) == ("block text", True)


def test_extract_result_falls_back_to_raw_stdout():
    assert CopilotBackend._extract_result("plain non-json output") == ("plain non-json output", True)
    assert CopilotBackend._extract_result("") == ("", False)


def test_extract_result_prefers_result_over_text():
    out = '{"role":"assistant","content":"early"}\n{"type":"result","result":"late"}\n'
    assert CopilotBackend._extract_result(out) == ("late", True)


def test_extract_result_from_copilot_assistant_message_data():
    """Echtes Copilot-CLI-Schema (1.0.98): Antwort in `assistant.message` →
    `data.content`; die `result`-Zeile hat nur exitCode/usage (KEIN Text)."""
    out = (
        '{"type":"assistant.message_delta","data":{"deltaContent":"all "}}\n'
        '{"type":"assistant.message","data":{"content":"all good","model":"claude-sonnet-5.5"}}\n'
        '{"type":"result","exitCode":0,"usage":{"premiumRequests":1}}\n'
    )
    assert CopilotBackend._extract_result(out) == ("all good", True)


def test_extract_result_from_deltas_when_no_final_message():
    """Nur Streaming-Deltas (keine finale assistant.message) → akkumulierte Deltas."""
    out = (
        '{"type":"assistant.message_delta","data":{"deltaContent":"hel"}}\n'
        '{"type":"assistant.message_delta","data":{"deltaContent":"lo"}}\n'
    )
    assert CopilotBackend._extract_result(out) == ("hello", True)


def test_extract_result_skips_non_dict_json_lines():
    """Gültiges JSON, das KEIN Objekt ist (Array/Number/String) wird übersprungen,
    nicht gecrasht — die eigentliche Antwort wird trotzdem extrahiert."""
    out = '[1, 2, 3]\n42\n"just a string"\n{"type":"assistant.message","data":{"content":"real answer"}}\n'
    assert CopilotBackend._extract_result(out) == ("real answer", True)


def test_extract_result_mixed_stream_uses_json_not_raw_fallback():
    """Gemischter Stream (teils parsierbare, teils kaputte Zeilen):
    `parsed_json` wird gesetzt, sobald EINE Zeile als JSON geparst wird →
    der rohe-stdout-Fallback wird NICHT gewählt. Ohne finale Antwort ist
    das Ergebnis `("", False)` — nicht der rohe Stream-Text."""
    out = 'kein json, nur Text\n{"type":"assistant.turn_start","data":{"turnId":"0"}}\n{"kaputt": \n'
    assert CopilotBackend._extract_result(out) == ("", False)


def test_extract_result_no_final_answer_in_jsonl_is_not_found():
    """Regression (review-pr 2026-10-18): JSONL-Stream OHNE finale Antwort
    (nur turn_start/model.call_start/leere Deltas) → found=False, damit
    `_parse` den Run als failed markiert statt als 'pass' mit leerem Output."""
    out = (
        '{"type":"user.message","data":{"content":"prompt"}}\n'
        '{"type":"assistant.turn_start","data":{"turnId":"0"}}\n'
        '{"type":"model.call_start","data":{"turnId":"0","model":"gpt-5-mini"}}\n'
        '{"type":"assistant.reasoning_delta","data":{"deltaContent":""}}\n'
    )
    assert CopilotBackend._extract_result(out) == ("", False)


def test_extract_result_empty_deltas_only_is_not_found():
    """Nur LEERE Deltas (Model-Call lief an, Antwort kam nie) → found=False."""
    out = '{"type":"assistant.message_delta","data":{"deltaContent":""}}\n'
    assert CopilotBackend._extract_result(out) == ("", False)


def test_extract_result_truncated_stream_without_result_event_is_not_found():
    """Regression (review-pr 2026-10-18, Fall 2): JSONL-Stream wird beim
    Shutdown ABGESCHNITTEN (CLI stdout-Dispose-Timeout) — Session-Events und
    ein Zwischen-Turn (Plan-Text) sind da, aber das terminierende `result`-
    Event fehlt → found=False, damit `_parse` den Run als failed (retryable)
    markiert statt als 'pass' mit dem Plan-Text als 'Antwort'."""
    out = (
        '{"type":"session.start","data":{"sessionId":"x"}}\n'
        '{"type":"assistant.turn_start","data":{"turnId":"0"}}\n'
        '{"type":"assistant.message","data":{"content":"Plan: State-Datei lesen und PRs auflisten"}}\n'
        '{"type":"tool.execution_start","data":{"toolName":"bash"}}\n'
    )
    assert CopilotBackend._extract_result(out) == ("", False)


def test_extract_result_complete_stream_with_result_event():
    """Vollständiger Stream (result-Event vorhanden) → finale assistant.message
    wird extrahiert, auch wenn davor Zwischen-Turns mit Text waren."""
    out = (
        '{"type":"session.start","data":{"sessionId":"x"}}\n'
        '{"type":"assistant.turn_start","data":{"turnId":"0"}}\n'
        '{"type":"assistant.message","data":{"content":"Zwischenstand"}}\n'
        '{"type":"assistant.message","data":{"content":"nichts zu tun — keine offene fabrikant-PR"}}\n'
        '{"type":"result","exitCode":0,"usage":{}}\n'
    )
    assert CopilotBackend._extract_result(out) == ("nichts zu tun — keine offene fabrikant-PR", True)


def test_poll_failed_when_stream_truncated_without_result_event():
    """exit 0 + abgeschnittener JSONL-Stream (Zwischen-Turn-Text, kein
    result-Event) → 'failed' + retryable (statt 'done' mit Plan-Text)."""
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p.returncode = 0
    p._out = (
        b'{"type":"session.start","data":{"sessionId":"x"}}\n'
        b'{"type":"assistant.turn_start","data":{"turnId":"0"}}\n'
        b'{"type":"assistant.message","data":{"content":"Plan: PRs auflisten"}}\n'
    )
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert "ohne finale Antwort" in r["text"]
    assert r["retryable"] is True


# ---------------------------------------------------------------- poll
def test_poll_running_when_proc_not_started():
    be = CopilotBackend()
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
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    be._sessions[sid]["proc"] = FakePopen([])  # rc=None → läuft
    assert be.poll(sid, "/tmp/wd")["status"] == "running"


def test_poll_stale_when_running_too_long(monkeypatch):
    be = CopilotBackend(stale_after=1)
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    be._sessions[sid]["proc"] = p
    be._sessions[sid]["started"] = 0  # weit in der Vergangenheit
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert "inaktiv" in r["text"]


def test_poll_done_with_jsonl_result():
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p.returncode = 0
    p._out = json.dumps({"type": "result", "result": "pong"}).encode()
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "done"
    assert r["text"] == "pong"
    assert r["tokens"] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert r["cost"] == 0.0
    assert r["tools"] == []


def test_poll_done_with_plain_text_fallback():
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p.returncode = 0
    p._out = b"plain answer"
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "done"
    assert r["text"] == "plain answer"


def test_poll_failed_when_rc_zero_but_no_final_answer():
    """Regression (review-pr 2026-10-18): exit 0 + JSONL ohne finale Antwort
    → 'failed' (nicht 'done'), damit der Run fehlschlägt und alertet."""
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p.returncode = 0
    p._out = (
        b'{"type":"assistant.turn_start","data":{"turnId":"0"}}\n'
        b'{"type":"model.call_start","data":{"turnId":"0","model":"gpt-5-mini"}}\n'
        b'{"type":"assistant.reasoning_delta","data":{"deltaContent":""}}\n'
    )
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert "ohne finale Antwort" in r["text"]
    # Transient (API-Ausfall) → der Agent-Step darf die Session neu starten.
    assert r["retryable"] is True


def test_poll_failed_on_nonzero_rc():
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p.returncode = 1
    p._err = b"boom"
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert "boom" in r["text"]


def test_poll_failed_on_nonzero_rc_uses_stdout_when_no_stderr():
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p.returncode = 1
    p._out = b"error detail"
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert "error detail" in r["text"]


# ---------------------------------------------------------------- _session_error_reason
def test_session_error_reason_from_quota_event():
    """Quota-Fehler (HTTP 402): `type` + `message` aus dem `session.error`-Event."""
    out = (
        '{"type":"session.start","data":{"sessionId":"x"}}\n'
        '{"type":"session.error","data":{"type":"quota","message":"monthly quota exceeded (HTTP 402)"}}\n'
    )
    assert CopilotBackend._session_error_reason(out) == "session.error: quota — monthly quota exceeded (HTTP 402)"


def test_session_error_reason_from_code_and_error_field():
    """Fehlerart in `code`, Beschreibung in `error` (andere Schema-Variante)."""
    out = '{"type":"session.error","data":{"code":"402","error":"payment required"}}\n'
    assert CopilotBackend._session_error_reason(out) == "session.error: 402 — payment required"


def test_session_error_reason_type_only():
    """Nur Fehlerart, keine Beschreibung → nur die Art."""
    out = '{"type":"session.error","data":{"type":"quota"}}\n'
    assert CopilotBackend._session_error_reason(out) == "session.error: quota"


def test_session_error_reason_empty_without_event():
    """Kein `session.error`-Event → leer (der Aufrufer fällt auf stderr/stdout zurück)."""
    out = '{"type":"session.start","data":{"sessionId":"x"}}\n{"type":"result","exitCode":1}\n'
    assert CopilotBackend._session_error_reason(out) == ""
    assert CopilotBackend._session_error_reason("") == ""


def test_session_error_reason_ignores_broken_and_non_dict_lines():
    """Kaputte/nicht-Objekt-JSON-Zeilen werden übersprungen, das Event dahinter gefunden."""
    out = 'kein json\n[1, 2]\n42\n{"type":"session.error","data":{"type":"quota","message":"limit"}}\n'
    assert CopilotBackend._session_error_reason(out) == "session.error: quota — limit"


def test_session_error_reason_ignores_non_dict_data():
    """`data` ohne nützliche Felder (z. B. String) → kein Crash, nur der Event-Typ."""
    out = '{"type":"session.error","data":"quota"}\n'
    assert CopilotBackend._session_error_reason(out) == "session.error"


# ---------------------------------------------------------------- _parse: session.error im Fehler
def test_poll_failed_includes_session_error_reason():
    """Exit ≠ 0 + `session.error` (Quota) → der Grund landet in der Fehlermeldung,
    voran dem stderr/stdout-Kontext."""
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p.returncode = 1
    p._out = (
        b'{"type":"session.start","data":{"sessionId":"x"}}\n'
        b'{"type":"session.error","data":{"type":"quota","message":"monthly quota exceeded (HTTP 402)"}}\n'
    )
    p._err = b"copilot exited with code 1"
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert "session.error: quota" in r["text"]
    assert "monthly quota exceeded (HTTP 402)" in r["text"]
    # Der Grund steht vor dem Kontext.
    assert r["text"].index("session.error: quota") < r["text"].index("copilot exited with code 1")


def test_poll_failed_without_session_error_uses_stderr():
    """Exit ≠ 0 OHNE `session.error`-Event → wie bisher: stderr (oder stdout) als Text,
    kein `session.error`-Präfix."""
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p.returncode = 1
    p._out = b'{"type":"session.start","data":{"sessionId":"x"}}\n'
    p._err = b"boom"
    be._sessions[sid]["proc"] = p
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "failed"
    assert "session.error" not in r["text"]
    assert r["text"] == "boom"


def test_poll_after_send_prompt_no_flush_of_closed_stdin(monkeypatch):
    """Regression (issue: review-pr 'flush of closed file').

    `send_prompt` schreibt den Prompt, schließt stdin und setzt es auf None.
    `poll()` ruft danach `communicate()` — der muss den (None-)Stream NICHT
    flushen, sonst `ValueError: flush of closed file`."""
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    cap = _spawn(monkeypatch, be, sid, prompt="review the PR")
    p = cap["proc"]
    p._out = json.dumps({"type": "result", "result": "review done"}).encode()  # Output vor Exit (wie real)
    p.returncode = 0
    r = be.poll(sid, "/tmp/wd")
    assert r["status"] == "done"
    assert r["text"] == "review done"


# ---------------------------------------------------------------- cancel
def test_cancel_kills_process_group(monkeypatch):
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    be._sessions[sid]["proc"] = p
    calls = []
    monkeypatch.setattr(os, "getpgid", lambda pid: (calls.append(("getpgid", pid)), pid)[1])
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (calls.append(("killpg", pgid, sig)), p.kill())[1])
    be.cancel(sid, "/tmp/wd")
    assert p.killed
    assert ("killpg", 777, signal.SIGKILL) in calls


def test_cancel_unknown_session_is_noop():
    be = CopilotBackend()
    be.cancel("unbekannt", "/tmp/wd")  # keine Exception


def test_cancel_finished_proc_is_noop(monkeypatch):
    be = CopilotBackend()
    sid = be.create_task("/tmp/wd", "t")
    p = FakePopen([])
    p.returncode = 0
    be._sessions[sid]["proc"] = p
    be.cancel(sid, "/tmp/wd")  # keine Exception, kein kill
    assert p.killed is False


# ---------------------------------------------------------------- agent.py-Dispatch
def test_agent_backend_copilot_is_known(monkeypatch):
    # copilot ist registriert → kein ValueError, model ist optional.
    import shutil

    monkeypatch.delenv("FABRIKANT_AGENT_MODEL", raising=False)
    monkeypatch.delenv("FABRIKANT_COPILOT_TOOLS", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)  # health() → False
    r = _step_agent_backend({}, {}, lambda m: None, "copilot", "p")
    assert r["ok"] is False
    assert "copilot" in r["summary"]


def test_agent_backend_unknown_still_raises():
    with pytest.raises(ValueError, match="copilot"):
        _step_agent_backend({}, {}, lambda m: None, "bogus", "p")


def test_parse_reports_premium_requests_from_result_event():
    out = (
        '{"type":"assistant.message","data":{"content":"ok"}}\n'
        '{"type":"result","exitCode":0,"usage":{"premiumRequests":3}}\n'
    )
    r = CopilotBackend()._parse(out, "", 0)
    assert r["status"] == "done" and r["premium_requests"] == 3
    assert r["tokens"] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def test_parse_without_usage_has_no_premium_requests_key():
    out = '{"type":"assistant.message","data":{"content":"ok"}}\n{"type":"result","exitCode":0,"usage":{}}\n'
    assert "premium_requests" not in CopilotBackend()._parse(out, "", 0)


def test_premium_requests_survive_failure_and_ignore_bad_values():
    assert CopilotBackend._premium_requests('{"type":"result","usage":{"premiumRequests":"x"}}\nnope\n') is None
    out = '{"type":"result","usage":{"premiumRequests":2}}\n'
    assert CopilotBackend()._parse(out, "boom", 1)["premium_requests"] == 2


class _Log:
    def __init__(self):
        self.events = []

    def event(self, kind, **data):
        self.events.append((kind, data))


def test_record_cost_event_carries_premium_requests_only_when_reported():
    from fabrikant.steps.agent import _record_cost

    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    lg = _Log()
    _record_cost(lg, "s", usage, {"premium_requests": 2})
    _record_cost(lg, "s", usage, {})
    assert lg.events[0][1]["premium_requests"] == 2
    assert "premium_requests" not in lg.events[1][1]
