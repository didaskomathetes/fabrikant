"""Benannte Verbindungen (ADR 0052): Persistenz, Verschlüsselung, Env-Merge, Step-Auflösung."""

from __future__ import annotations

import pytest
from helpers import not_none

from fabrik import config, connections, llm, secrets
from fabrik.connections import Connection, ConnectionKind, InvalidConnection, Source

AGENT = ConnectionKind.AGENT
LLM = ConnectionKind.LLM


def _agent(name="oc", **kw) -> Connection:
    base = {"driver": "opencode", "endpoint": "http://127.0.0.1:4096", "model": "llama/q", "secret": "pw-secret-123"}
    return Connection(name=name, kind=AGENT, **{**base, **kw})


def _llm(name="local", **kw) -> Connection:
    base = {"driver": "http", "endpoint": "http://x/v1/chat/completions", "model": "m", "secret": "key-secret-123"}
    return Connection(name=name, kind=LLM, **{**base, **kw})


@pytest.fixture(autouse=True)
def _no_agent_env(monkeypatch):
    for var in ("FABRIK_AGENT_BACKEND", "FABRIK_AGENT_ENDPOINT", "FABRIK_AGENT_MODEL", "FABRIK_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- Validierung
@pytest.mark.parametrize(
    "conn",
    [
        _agent(name="-bad"),
        _agent(name="a b"),
        _agent(driver="nope"),
        _agent(model=""),
        _agent(endpoint=""),
        _llm(driver="nope"),
        _llm(endpoint=""),
        _llm(driver="claude", model=""),
    ],
)
def test_validate_rejects(conn):
    with pytest.raises(InvalidConnection):
        conn.validate()


def test_validate_accepts_cli_and_oneshot_backend():
    _llm(name="c", driver="claude", endpoint="", secret="").validate()
    _agent(name="cc", driver="claude_code", endpoint="", model="m").validate()


def test_from_payload_rejects_unknown_kind():
    with pytest.raises(InvalidConnection):
        Connection.from_payload("x", "bogus", {})


def test_public_never_contains_secret():
    pub = _agent().public()
    assert pub["secret_set"] is True
    assert "pw-secret-123" not in str(pub) and "secret" not in {k for k in pub if k != "secret_set"}


# ---------------------------------------------------------------- Persistenz + Verschlüsselung
def test_save_and_read_roundtrip_encrypts_at_rest():
    connections.save_connection(_agent())
    assert connections.get_connection("oc", AGENT).secret == "pw-secret-123"
    with connections._store().read_tx() as db:
        raw = db.exec_driver_sql("SELECT secret FROM connections").scalar()
    assert raw and "pw-secret-123" not in raw


def test_key_file_created_with_0600():
    connections.save_connection(_agent())
    key_file = config.STATE_DIR / connections.SECRET_KEY_FILE
    assert key_file.is_file() and key_file.stat().st_mode & 0o777 == 0o600


def test_env_key_takes_precedence(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("FABRIK_SECRET_KEY", Fernet.generate_key().decode())
    connections.save_connection(_agent())
    assert not (config.STATE_DIR / connections.SECRET_KEY_FILE).exists()
    assert connections.get_connection("oc", AGENT).secret == "pw-secret-123"


def test_wrong_key_yields_empty_secret_not_crash(monkeypatch):
    from cryptography.fernet import Fernet

    connections.save_connection(_agent())
    monkeypatch.setenv("FABRIK_SECRET_KEY", Fernet.generate_key().decode())
    conn = connections.get_connection("oc", AGENT)
    assert conn.secret == "" and conn.secret_unreadable
    assert conn.public()["secret_set"] is True and conn.public()["secret_unreadable"] is True


def test_apply_rejects_unreadable_secret(monkeypatch):
    from cryptography.fernet import Fernet

    connections.save_connection(_agent())
    monkeypatch.setenv("FABRIK_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(connections.UndecryptableSecret):
        connections.apply_to_step({"type": "agent", "connection": "oc"})


def test_decrypt_raises_on_foreign_token():
    from cryptography.fernet import Fernet

    token = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
    with pytest.raises(connections.UndecryptableSecret):
        connections.decrypt(token)


def test_key_file_creation_survives_concurrent_winner(monkeypatch):
    """Ein anderer Prozess legt das Key-File zwischen exists() und link() an → dessen Schlüssel gilt."""
    key_file = config.STATE_DIR / connections.SECRET_KEY_FILE
    real_link = connections.os.link

    def racing_link(src, dst):
        key_file.write_bytes(b"winner-key")
        real_link(src, dst)  # → FileExistsError

    monkeypatch.setattr(connections.os, "link", racing_link)
    assert connections._load_key() == b"winner-key"
    assert not list(key_file.parent.glob("*.tmp"))


def test_concurrent_first_use_yields_one_valid_key():
    import threading

    keys: list[bytes] = []
    threads = [threading.Thread(target=lambda: keys.append(connections._load_key())) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(keys)) == 1 and len(keys[0]) == 44


def test_keep_secret_on_update_and_clear():
    connections.save_connection(_agent())
    connections.save_connection(_agent(secret="", model="other"), keep_secret=True)
    got = connections.get_connection("oc", AGENT)
    assert (got.secret, got.model) == ("pw-secret-123", "other")
    connections.save_connection(_agent(secret=""))
    assert connections.get_connection("oc", AGENT).secret == ""


def test_kind_clash_on_same_name_rejected():
    connections.save_connection(_agent(name="dup"))
    with pytest.raises(InvalidConnection):
        connections.save_connection(_llm(name="dup"))


def test_single_default_per_kind():
    connections.save_connection(_agent("a", is_default=True))
    connections.save_connection(_agent("b", is_default=True))
    connections.save_connection(_llm("l", is_default=True))
    assert connections.default_connection(AGENT).name == "b"
    assert connections.default_connection(LLM).name == "l"


def test_delete():
    connections.save_connection(_agent())
    assert connections.delete_connection("oc") is True
    assert connections.delete_connection("oc") is False
    assert connections.get_connection("oc", AGENT) is None


# ---------------------------------------------------------------- Env-Merge
def test_env_connections_are_read_only_sources(monkeypatch):
    monkeypatch.setenv("FABRIK_AGENT_BACKEND", "opencode")
    monkeypatch.setenv("FABRIK_AGENT_ENDPOINT", "http://e:1")
    monkeypatch.setenv("FABRIK_AGENT_MODEL", "p/m")
    monkeypatch.setenv("FABRIK_LLM_URL", "http://l/v1")
    monkeypatch.setenv("FABRIK_LLM_MODEL", "lm")
    by_name = {c.name: c for c in connections.list_connections()}
    assert by_name["env"].source is Source.ENV and by_name["env"].endpoint == "http://e:1"
    assert by_name["default"].kind is LLM and by_name["default"].source is Source.ENV


def test_db_connection_overrides_env_of_same_name(monkeypatch):
    monkeypatch.setenv("FABRIK_LLM_URL", "http://l/v1")
    monkeypatch.setenv("FABRIK_LLM_MODEL", "lm")
    connections.save_connection(_llm("default", model="from-ui"))
    assert [p["model"] for p in llm.list_providers() if p["name"] == "default"] == ["from-ui"]


def test_ui_llm_connection_listed_and_default_selected(monkeypatch):
    monkeypatch.setenv("FABRIK_LLM_URL", "http://l/v1")
    monkeypatch.setenv("FABRIK_LLM_MODEL", "lm")
    connections.save_connection(_llm("local"))
    assert {p["name"] for p in llm.list_providers()} == {"default", "local"}
    assert llm.default_name() == "default"
    connections.save_connection(_llm("local", is_default=True))
    assert llm.default_name() == "local"
    provider = llm.get_provider("local")
    assert provider["key"] == "key-secret-123" and provider["url"].startswith("http://x")


def test_llm_cli_connection_provider_shape():
    connections.save_connection(_llm("cc", driver="claude", endpoint="", secret=""))
    assert llm.get_provider("cc") == {"name": "cc", "url": "", "cli": "claude", "model": "m", "key": ""}


# ---------------------------------------------------------------- Step-Auflösung
def test_apply_ignores_non_agent_steps():
    step = {"type": "shell", "cmd": "true", "connection": "ignored"}
    assert connections.apply_to_step(step) is step


def test_apply_named_connection_fills_gaps_and_step_wins():
    connections.save_connection(_agent())
    out = connections.apply_to_step({"type": "agent", "connection": "oc", "model": "step/model"})
    assert (out["backend"], out["endpoint"], out["model"], out["password"]) == (
        "opencode",
        "http://127.0.0.1:4096",
        "step/model",
        "pw-secret-123",
    )


def test_apply_unknown_connection_or_backend_conflict_raises():
    connections.save_connection(_agent())
    with pytest.raises(InvalidConnection, match="unbekannt"):
        connections.apply_to_step({"type": "agent", "connection": "nope"})
    with pytest.raises(InvalidConnection, match="passt nicht"):
        connections.apply_to_step({"type": "review", "connection": "oc", "backend": "codex"})


def test_apply_default_connection_only_when_backend_matches():
    connections.save_connection(_agent(is_default=True))
    assert connections.apply_to_step({"type": "agent"})["endpoint"] == "http://127.0.0.1:4096"
    other = {"type": "agent", "backend": "claude_code"}
    assert connections.apply_to_step(other) is other


def test_apply_without_any_connection_is_noop():
    step = {"type": "agent", "prompt": "x"}
    assert connections.apply_to_step(step) is step


def test_env_agent_connection_is_selectable_by_name(monkeypatch):
    monkeypatch.setenv("FABRIK_AGENT_BACKEND", "opencode")
    monkeypatch.setenv("FABRIK_AGENT_ENDPOINT", "http://e:1")
    monkeypatch.setenv("FABRIK_AGENT_MODEL", "p/m")
    out = connections.apply_to_step({"type": "agent", "connection": "env"})
    assert out["endpoint"] == "http://e:1"


# ---------------------------------------------------------------- Masking
def test_connection_secrets_are_redacted_in_logs():
    connections.save_connection(_agent())
    connections._invalidate()
    assert secrets._redact("token=pw-secret-123 ok") == "token=[REDACTED] ok"


def test_secret_values_negative_cache_on_db_failure(monkeypatch):
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise RuntimeError("db down")

    connections._invalidate()
    monkeypatch.setattr(connections, "db_connections", boom)
    assert connections.secret_values() == [] and connections.secret_values() == []
    assert len(calls) == 1  # zweiter Aufruf trifft die DB nicht erneut
    # Fenster abgelaufen
    connections._secret_cache_failed_at = not_none(connections._secret_cache_failed_at) - connections.SECRET_RETRY_S - 1
    connections.secret_values()
    assert len(calls) == 2


def test_save_invalidates_failed_secret_cache():
    connections._secret_cache_failed_at = 1e12
    connections.save_connection(_agent())
    assert connections._secret_cache_failed_at is None


def test_secret_cache_expires_after_ttl(monkeypatch):
    """Änderungen anderer Prozesse werden nach der TTL sichtbar (Masking-Cache)."""
    connections._invalidate()
    connections.save_connection(_agent(secret="first-secret-1"))
    assert connections.secret_values() == ["first-secret-1"]
    # Anderer Prozess schreibt direkt in die DB, ohne unseren Cache zu invalidieren:
    with connections._store().write_tx() as db:
        db.execute(
            connections.update(connections.connections_table).values(secret=connections.encrypt("second-secret-2"))
        )
    assert connections.secret_values() == ["first-secret-1"]  # noch gecacht
    monkeypatch.setattr(
        connections, "_secret_cache_at", connections._secret_cache_at - connections.SECRET_CACHE_TTL_S - 1
    )
    assert connections.secret_values() == ["second-secret-2"]


def test_apply_without_secret_skips_decrypt(monkeypatch):
    connections.save_connection(_agent())

    def no_decrypt(token):
        raise AssertionError("decrypt darf für die Anzeige nicht laufen")

    monkeypatch.setattr(connections, "decrypt", no_decrypt)
    out = connections.apply_to_step({"type": "agent", "connection": "oc"}, with_secret=False)
    assert out["model"] == "llama/q" and "password" not in out


# ---------------------------------------------------------------- fabrik validate
def _spec(*steps: dict) -> dict:
    return {"loops": {"dev": {"steps": list(steps)}}}


def test_unknown_references_lists_only_missing_agent_connections(monkeypatch):
    connections.save_connection(_agent())
    monkeypatch.setenv("FABRIK_AGENT_BACKEND", "claude_code")  # Env-Verbindung `env` zählt als bekannt
    spec = _spec(
        {"type": "agent", "name": "ok", "connection": "oc"},
        {"type": "review", "name": "envref", "connection": "env"},
        {"type": "pr_review", "name": "typo", "connection": "ocx"},
        {"type": "agent", "name": "var", "connection": "{conn}"},  # erst zur Laufzeit aufgelöst
        {"type": "shell", "name": "sh", "connection": "nope"},  # kein Agent-Step
        {"type": "agent", "name": "plain"},
    )
    assert connections.unknown_references(spec) == ["dev/typo: Agent-Verbindung 'ocx' unbekannt"]


def test_unknown_references_without_refs_needs_no_db(monkeypatch):
    monkeypatch.setattr(connections, "list_connections", lambda *a, **k: pytest.fail("DB-Zugriff ohne Referenz"))
    assert connections.unknown_references(_spec({"type": "agent", "name": "a"})) == []


def test_min_masked_secret_len_matches_masking():
    """Die UI warnt ab derselben Grenze, ab der das Masking greift."""
    short = "x" * (secrets.MIN_SECRET_LEN - 1)
    connections.save_connection(_agent(secret=short))
    assert short not in connections.secret_values()
    connections.save_connection(_agent(secret=short + "y"))
    assert short + "y" in connections.secret_values()
