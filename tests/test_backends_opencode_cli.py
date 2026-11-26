"""opencode als One-Shot-CLI in der Sandbox (ADR 0054): Config aus der LLM-Verbindung (ADR 0052),
restriktive Permissions, JSONL-Auswertung, Umschaltung in `OpenCodeBackend.create`."""

from __future__ import annotations

import json

import pytest

from fabrikant import connections, egress, sandbox
from fabrikant.backends import opencode_cli
from fabrikant.backends.opencode import OpenCodeBackend
from fabrikant.backends.opencode_cli import OpenCodeCliBackend, OpenCodeConfig, parse_events, permission_rules
from fabrikant.backends.registry import create_backend

ENDPOINT = "https://llm.example/v1/chat/completions"


def _conn(**kw) -> connections.Connection:
    base = {
        "name": "lan",
        "kind": connections.ConnectionKind.LLM,
        "driver": "http",
        "endpoint": ENDPOINT,
        "model": "qwen",
    }
    return connections.Connection(**(base | kw))


@pytest.fixture
def conn(monkeypatch):
    """`get_connection` liefert die Verbindung aus `holder['conn']` (None = unbekannt).
    Der Test-Endpoint-Host steht auf der Egress-Allowlist (sonst fail-closed)."""
    holder = {"conn": _conn()}
    monkeypatch.setattr(
        connections, "get_connection", lambda name, kind, **_: holder["conn"] if name == "lan" else None
    )
    monkeypatch.setenv(egress.ALLOWLIST_VAR, "llm.example")
    return holder


@pytest.mark.parametrize(
    ("endpoint", "base"),
    [(ENDPOINT, "https://llm.example/v1"), ("https://llm.example/v1/", "https://llm.example/v1")],
)
def test_provider_base_url(endpoint, base):
    assert opencode_cli.provider_base_url(endpoint) == base


def test_permission_rules_are_restrictive():
    rules = [
        {"permission": "edit", "pattern": "review.json", "action": "allow"},
        {"permission": "bash", "pattern": "*", "action": "deny"},
    ]
    assert permission_rules(rules) == {
        "edit": {"*": "deny", "review.json": "allow"},
        "bash": {"*": "deny"},
        "read": {"*": "deny"},
        "webfetch": {"*": "deny"},
    }
    # ein explizites `*` des Steps gewinnt
    assert permission_rules([{"permission": "edit", "pattern": "*", "action": "allow"}]) == {
        "edit": {"*": "allow"},
        "bash": {"*": "deny"},
        "read": {"*": "deny"},
        "webfetch": {"*": "deny"},
    }
    assert permission_rules(None) == opencode_cli.DEFAULT_PERMISSIONS
    with pytest.raises(ValueError, match="permission \\+ action"):
        permission_rules([{"pattern": "*"}])


def test_permission_rules_deny_unmentioned_builtins():
    """opencode 2.x Basis-Policy erlaubt alles: nennt der Step nur `edit`, muss `bash`
    (und Co.) trotzdem `deny` sein — sonst umgeht der Agent die Einschränkung über die Shell."""
    rules = [{"permission": "edit", "pattern": "review.json", "action": "allow"}]
    out = permission_rules(rules)
    for perm in opencode_cli.BUILTIN_PERMISSIONS:
        assert out[perm]["*"] == "deny", perm
    assert out["edit"]["review.json"] == "allow"


def test_config_from_connection(conn):
    cfg = OpenCodeConfig.from_connection("lan/qwen-big", [{"permission": "bash", "pattern": "*", "action": "deny"}])
    data = json.loads(cfg.content())
    provider = data["provider"]["lan"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {"baseURL": "https://llm.example/v1"}  # kein apiKey ohne Secret
    assert list(provider["models"]) == ["qwen-big"] and data["model"] == "lan/qwen-big"
    assert (
        data["permission"]
        == {
            "edit": {"*": "deny"},
            "bash": {"*": "deny"},
            "read": {"*": "deny"},
            "webfetch": {"*": "deny"},
        }
        and data["share"] == "disabled"
    )
    assert OpenCodeConfig.from_connection("lan").model == "qwen"  # Modell der Verbindung als Default


def test_config_carries_secret_only_in_content(conn):
    conn["conn"] = _conn(secret="sk-123")
    cfg = OpenCodeConfig.from_connection("lan/qwen")
    assert json.loads(cfg.content())["provider"]["lan"]["options"]["apiKey"] == "sk-123"
    assert "sk-123" not in repr(cfg)


@pytest.mark.parametrize(
    ("conn_value", "ref", "error"),
    [
        (_conn(), "other/qwen", "unknown"),
        (_conn(driver="claude", endpoint=""), "lan/qwen", "not an OpenAI-compatible"),
        (_conn(secret_unreadable=True), "lan/qwen", "cannot be decrypted"),
    ],
)
def test_config_errors(conn, conn_value, ref, error):
    conn["conn"] = conn_value
    with pytest.raises(connections.InvalidConnection, match=error):
        OpenCodeConfig.from_connection(ref)


def test_config_rejects_endpoint_host_off_allowlist(conn, monkeypatch):
    """Endpoint-Host ohne Egress-Allowlist-Eintrag → fail-closed mit Hinweis (ADR 0051)."""
    monkeypatch.delenv(egress.ALLOWLIST_VAR, raising=False)  # nur Basis-Allowlist (github.com, …)
    with pytest.raises(connections.InvalidConnection, match="egress allowlist"):
        OpenCodeConfig.from_connection("lan/qwen")
    # Subdomäne eines erlaubten Hosts zählt mit
    conn["conn"] = _conn(endpoint="https://api.github.com/v1/chat/completions")
    assert OpenCodeConfig.from_connection("lan/qwen").base_url == "https://api.github.com/v1"


def _event(type_: str, **part) -> str:
    return json.dumps({"type": type_, "part": part})


def test_parse_events_sums_usage_and_collects_text():
    out = "\n".join(
        [
            _event("step_start", type="step-start"),
            _event("tool_use", type="tool", tool="read"),
            _event(
                "step_finish",
                type="step-finish",
                cost=0.25,
                tokens={"input": 100, "output": 5, "reasoning": 3, "cache": {"read": 10, "write": 2}},
            ),
            "kein json",
            _event("text", type="text", text="Fertig."),
            _event("step_finish", type="step-finish", tokens={"input": 50, "output": 7}),
        ]
    )
    text, tokens, cost, tools, error = parse_events(out)
    assert text == "Fertig." and tools == ["read"] and error == ""
    assert tokens == {"input": 162, "output": 15, "cache_read": 10, "cache_write": 2} and cost == 0.25


def test_parse_events_error_message():
    out = json.dumps({"type": "error", "error": {"name": "APIError", "data": {"message": "model not found"}}})
    assert parse_events(out)[4] == "model not found"


def test_parse_events_error_message_v2_2_5_shape():
    """opencode 2.2.5: `message` liegt direkt im Error-Objekt (kein `data`, kein `name`) —
    die saubere Meldung, nicht der Dict-Repr, muss extrahiert werden."""
    out = json.dumps({"type": "error", "error": {"type": "provider.error", "message": "model not found"}})
    assert parse_events(out)[4] == "model not found"


def test_backend_cmd_env_and_parse(conn):
    be = OpenCodeCliBackend(model="lan/qwen", variant="high")
    be.create_task("/tmp", "t", [{"permission": "bash", "pattern": "*", "action": "deny"}])
    assert be._build_cmd() == ["opencode", "run", "--standalone", "--format", "json", "--model", "lan/qwen#high"]
    env = be._env()
    assert json.loads(env[opencode_cli.CONFIG_CONTENT_VAR])["permission"] == {
        perm: {"*": "deny"} for perm in opencode_cli.BUILTIN_PERMISSIONS
    }
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert be._stale_after() is None and be.health() and be.diagnose() is None
    done = be._parse(_event("text", type="text", text="ok"), "", 0)
    assert done["status"] == "done" and done["text"] == "ok"
    failed = be._parse("", "boom\n", 1)
    assert failed["status"] == "failed" and failed["text"] == "boom"
    err = json.dumps({"type": "error", "error": "rate limited"})
    assert be._parse(err, "", 0) == {**be._parse(err, "", 0), "status": "failed", "text": "rate limited"}


def test_env_requires_create_task():
    with pytest.raises(RuntimeError, match="create_task"):
        OpenCodeCliBackend(model="lan/qwen")._env()


def test_create_switches_to_cli_in_sandbox(monkeypatch):
    monkeypatch.setenv(sandbox.ENABLE_VAR, "1")
    assert isinstance(create_backend("opencode", {}, "http://x", "lan/qwen", 900), OpenCodeCliBackend)
    assert type(create_backend("codex", {}, "", "m", 900)).__name__ == "CodexBackend"  # ohne Variante: unverändert
    monkeypatch.setenv(sandbox.ENABLE_VAR, "0")
    assert isinstance(create_backend("opencode", {}, "http://x", "lan/qwen", 900), OpenCodeBackend)
