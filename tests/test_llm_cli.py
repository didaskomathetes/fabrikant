"""CLI-LLM-Provider: Registry-Parsing, argv ohne Tools, Poll-Loop, complete()-Dispatch."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from fabrikant import llm, llm_cli
from fabrikant.backends._oneshot import OneShotCliBackend


@pytest.fixture
def clean_llm_env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("FABRIKANT_LLM"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_ACTIVE_FILE", tmp_path / "active_llm")
    monkeypatch.setattr(llm_cli, "_WORKDIR", tmp_path / "llm-cli")
    llm.reset_cache()
    yield
    llm.reset_cache()


# ---------------------------------------------------------------- Registry
def test_cli_provider_listed(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "claude,copilot")
    monkeypatch.setenv("FABRIKANT_LLM_CLAUDE_CLI", "claude")
    monkeypatch.setenv("FABRIKANT_LLM_CLAUDE_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("FABRIKANT_LLM_COPILOT_CLI", "copilot")
    monkeypatch.setenv("FABRIKANT_LLM_COPILOT_MODEL", "gpt-5.6-luna")
    provs = {p["name"]: p for p in llm.list_providers()}
    assert provs["claude"] == {
        "name": "claude",
        "url": "",
        "cli": "claude",
        "model": "claude-sonnet-5-5",
        "key": "",
    }
    assert provs["copilot"]["cli"] == "copilot"


def test_cli_provider_unknown_cli_skipped(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "x")
    monkeypatch.setenv("FABRIKANT_LLM_X_CLI", "rm")
    monkeypatch.setenv("FABRIKANT_LLM_X_MODEL", "m")
    assert llm.list_providers() == []


# ---------------------------------------------------------------- argv
def test_claude_chat_cmd_has_no_tools():
    cmd = llm_cli._ClaudeChat("claude-sonnet-5-5", "/tmp/sys.md")._build_cmd()
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--system-prompt-file") + 1] == "/tmp/sys.md"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert "--allowedTools" not in cmd


def test_copilot_chat_cmd_has_no_tools():
    cmd = llm_cli._CopilotChat(model="gpt-5.6-luna")._build_cmd()
    assert cmd[cmd.index("--available-tools") + 1] == ""
    assert "--allow-all-tools" not in cmd
    assert "--disable-builtin-mcps" in cmd and "--no-custom-instructions" in cmd


# ---------------------------------------------------------------- _run
class _FakeBackend(OneShotCliBackend):
    """Poll-Sequenz aus ``states``; der letzte Zustand bleibt stehen."""

    def __init__(self, states):
        super().__init__()
        self.states = list(states)
        self.prompt = None
        self.cancelled = False

    def create_task(self, workdir, title, permissions=None):
        return "sid"

    def send_prompt(self, sid, prompt, directory):
        self.prompt = prompt

    def poll(self, sid, directory):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def cancel(self, sid, directory):
        self.cancelled = True


def test_run_returns_terminal_result(clean_llm_env, monkeypatch):
    monkeypatch.setattr(llm_cli, "_POLL_INTERVAL_S", 0)
    be = _FakeBackend([{"status": "running"}, {"status": "done", "text": "hi"}])
    assert llm_cli._run(be, "p", 5, None)["text"] == "hi"
    assert be.prompt == "p"


def test_run_cancel_kills_cli(clean_llm_env, monkeypatch):
    monkeypatch.setattr(llm_cli, "_POLL_INTERVAL_S", 0)
    ev = threading.Event()
    ev.set()
    be = _FakeBackend([{"status": "running"}])
    with pytest.raises(llm_cli.LlmCliError, match="abgebrochen"):
        llm_cli._run(be, "p", 5, ev)
    assert be.cancelled


def test_run_timeout_kills_cli(clean_llm_env, monkeypatch):
    monkeypatch.setattr(llm_cli, "_POLL_INTERVAL_S", 0)
    be = _FakeBackend([{"status": "running"}])
    with pytest.raises(llm_cli.LlmCliError, match="Timeout"):
        llm_cli._run(be, "p", 0, None)
    assert be.cancelled


# ---------------------------------------------------------------- complete
def test_complete_claude_uses_temp_system_file(clean_llm_env, monkeypatch):
    seen = {}

    def fake_run(backend, prompt, timeout, cancel_event):
        seen["sys"] = Path(backend.system_file).read_text()
        seen["path"] = backend.system_file
        seen["prompt"] = prompt
        return {"status": "done", "text": "antwort", "tokens": {"input": 3, "output": 2}}

    monkeypatch.setattr(llm_cli, "_run", fake_run)
    text, tokens = llm_cli.complete("claude", "m", "SYSTEM", "USER", 10)
    assert (text, tokens) == ("antwort", {"input": 3, "output": 2})
    assert seen["sys"] == "SYSTEM" and seen["prompt"] == "USER"
    assert not Path(seen["path"]).exists()  # Temp-Datei aufgeräumt


def test_complete_copilot_prepends_system(clean_llm_env, monkeypatch):
    seen = {}

    def fake_run(backend, prompt, timeout, cancel_event):
        seen["prompt"] = prompt
        return {"status": "done", "text": "ok"}

    monkeypatch.setattr(llm_cli, "_run", fake_run)
    assert llm_cli.complete("copilot", "m", "SYSTEM", "USER", 10) == ("ok", {})
    assert seen["prompt"].startswith("SYSTEM") and seen["prompt"].endswith("USER")


def test_complete_failed_raises(clean_llm_env, monkeypatch):
    monkeypatch.setattr(llm_cli, "_run", lambda *a: {"status": "failed", "text": "auth"})
    with pytest.raises(llm_cli.LlmCliError, match="copilot: auth"):
        llm_cli.complete("copilot", "m", "s", "u", 10)


def test_complete_unknown_cli_raises():
    with pytest.raises(llm_cli.LlmCliError, match="unknown CLI"):
        llm_cli.complete("bash", "m", "s", "u", 10)


def test_registry_complete_dispatches_to_cli(monkeypatch):
    calls = {}

    def fake(cli, model, system, user, timeout, cancel_event):
        calls.update(cli=cli, model=model)
        return "txt", {"input": 4, "output": 1}

    monkeypatch.setattr(llm_cli, "complete", fake)
    prov = {"name": "claude", "url": "", "cli": "claude", "model": "default-m", "key": ""}
    res = llm.complete(prov, "s", "u", model="override-m")
    assert calls == {"cli": "claude", "model": "override-m"}
    assert res == {
        "text": "txt",
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 1,
            "total_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
    }


@pytest.mark.parametrize("raw", [None, {"prompt_tokens": "x"}, "kaputt"])
def test_usage_malformed_is_zero(raw):
    assert llm._usage(raw) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_complete_errors_carry_failure_kind(clean_llm_env, monkeypatch):
    """LlmCliError trägt die Klasse (ADR 0035): CLI-Text → Fallback, unbekannte CLI → environment."""
    monkeypatch.setattr(llm_cli, "_run", lambda *a: {"status": "failed", "text": "429 Too Many Requests"})
    with pytest.raises(llm_cli.LlmCliError) as exc:
        llm_cli.complete("copilot", "m", "s", "u", 10)
    assert exc.value.failure.kind.value == "transient"
    with pytest.raises(llm_cli.LlmCliError) as exc:
        llm_cli.complete("bash", "m", "s", "u", 10)
    assert exc.value.failure.kind.value == "environment"
