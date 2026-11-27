"""Agent-Env-Allowlist (ADR 0050): Agent-Prozesse erben kein `os.environ`."""

from __future__ import annotations

import pytest

from fabrikant.agentenv import AGENT_ENV_ALLOWLIST, build_agent_env, passthrough


@pytest.fixture
def secret_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    monkeypatch.setenv("GITHUB_REVIEWER_TOKEN", "ghs_reviewer")
    monkeypatch.setenv("FABRIKANT_LLM_KEY", "sk-llm")
    monkeypatch.setenv("FABRIKANT_ENV_FILE", "/root/.config/fabrikant/fabrikant.env")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/root")


def test_allowlist_keeps_only_safe_names(secret_env):
    env = build_agent_env()
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/root"
    for name in ("GITHUB_TOKEN", "GITHUB_REVIEWER_TOKEN", "FABRIKANT_LLM_KEY", "FABRIKANT_ENV_FILE"):
        assert name not in env


def test_extra_env_overlays_allowlist(secret_env):
    """Pro-Step-Variablen (z. B. gh-Identität) bleiben erlaubt (Issue #350)."""
    env = build_agent_env({"GH_TOKEN": "step-identity"})
    assert env["GH_TOKEN"] == "step-identity"
    assert "GITHUB_TOKEN" not in env


def test_extra_allowlist_env_var(secret_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_AGENT_ENV_EXTRA", "MY_CUSTOM_VAR,")
    monkeypatch.setenv("MY_CUSTOM_VAR", "value")
    env = build_agent_env()
    assert env["MY_CUSTOM_VAR"] == "value"


def test_allowlist_contains_no_secret_names():
    """Die Default-Allowlist darf keine Secret-Names tragen (Defence-in-Depth-Check)."""
    for name in AGENT_ENV_ALLOWLIST:
        assert not any(s in name for s in ("TOKEN", "KEY", "SECRET", "PASS", "CREDENTIAL")), name


def test_allowlist_has_no_command_or_stale_vars():
    """PAGER/EDITOR sind Befehls-Knöpfe, PWD/SHLVL im Kind bedeutungslos."""
    for name in ("PWD", "SHLVL", "EDITOR", "PAGER", "GIT_PAGER"):
        assert name not in AGENT_ENV_ALLOWLIST


def test_passthrough_returns_only_set_nonempty_names(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert passthrough(("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")) == {
        "ANTHROPIC_API_KEY": "sk-ant"
    }


def test_backends_pass_only_their_own_auth(monkeypatch):
    """Jede CLI bekommt ihre eigene Auth — nie fabrikants GITHUB_TOKEN/GH_TOKEN."""
    from fabrikant.backends.claude_code import ClaudeCodeBackend
    from fabrikant.backends.codex import CodexBackend
    from fabrikant.backends.copilot import CopilotBackend

    for var, val in (
        ("ANTHROPIC_API_KEY", "a"),
        ("OPENAI_API_KEY", "o"),
        ("COPILOT_GITHUB_TOKEN", "c"),
        ("GITHUB_TOKEN", "fabrikant-api"),
        ("GH_TOKEN", "fabrikant-gh"),
    ):
        monkeypatch.setenv(var, val)
    assert ClaudeCodeBackend()._env() == {"ANTHROPIC_API_KEY": "a"}
    assert CodexBackend()._env() == {"OPENAI_API_KEY": "o"}
    assert CopilotBackend()._env() == {"COPILOT_AUTO_UPDATE": "false", "COPILOT_GITHUB_TOKEN": "c"}
