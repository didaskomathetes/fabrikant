"""`relay:`-Block: Validierung, generierte Loops, Einbindung ins Spec-Laden."""

import sys

import pytest
import yaml

from fabrikant import specs
from fabrikant.relay import config as relay_config
from fabrikant.relay.config import (
    LOOP_AUDIT,
    LOOP_DISPATCH,
    LOOP_SESSION,
    AgentRole,
    RelayConfig,
    build_loops,
    expand_loops,
)


def spec(**relay) -> dict:
    return {
        "name": "demo",
        "github": "o/r",
        "repo": {"branch": "develop"},
        "relay": {"allowed": ["me"], "bot_login": "my-builder-app", **relay},
    }


def test_defaults_come_from_spec_level():
    cfg = RelayConfig.from_spec(spec())
    assert (cfg.project, cfg.repo, cfg.base, cfg.label) == ("demo", "o/r", "develop", "fabrikant-relay")
    assert cfg.allowed == frozenset({"me"}) and cfg.audit is True
    assert cfg.session_timeout == cfg.max_session_minutes * 60 + relay_config.AGENT_TIMEOUT_GRACE_S


def test_full_block_is_parsed():
    cfg = RelayConfig.from_spec(
        spec(
            repo="x/y",
            base="main",
            label="relay",
            forbid=["infra/**"],
            dispatch="0 * * * *",
            builder={"backend": "claude_code", "model": "m1"},
            reviewer={"model": "m2"},
            max_session_minutes=20,
            verify_timeout=60,
            audit_timeout=600,
            bot_login="bot",
            audit=False,
        )
    )
    assert cfg.repo == "x/y" and cfg.forbid == ("infra/**",) and cfg.dispatch_cron == "0 * * * *"
    assert cfg.builder == AgentRole("claude_code", "m1") and cfg.reviewer == AgentRole(None, "m2")
    assert (cfg.max_session_minutes, cfg.verify_timeout, cfg.audit_timeout) == (20, 60, 600)
    assert cfg.labels.task == "relay" and cfg.bot_login == "bot" and cfg.audit is False


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"allowed": []}, "relay.allowed"),
        ({"allowed": [""]}, "relay.allowed"),
        ({"bogus": 1}, "relay.bogus"),
        ({"forbid": "infra"}, "relay.forbid"),
        ({"builder": "opus"}, "relay.builder"),
        ({"builder": {"model": "m", "x": 1}}, "relay.builder"),
        ({"max_session_minutes": 0}, "max_session_minutes"),
        ({"verify_timeout": True}, "verify_timeout"),
        ({"audit": "yes"}, "relay.audit"),
    ],
)
def test_invalid_blocks_are_rejected(raw, fragment):
    with pytest.raises(ValueError, match=fragment):
        RelayConfig.from_spec({"name": "d", "github": "o/r", "relay": {"allowed": ["me"], **raw}})


def test_repo_is_required_and_relay_must_be_mapping():
    with pytest.raises(ValueError, match="relay.repo"):
        RelayConfig.from_spec({"name": "d", "relay": {"allowed": ["me"], "bot_login": "b"}})
    with pytest.raises(ValueError, match="Mapping"):
        RelayConfig.from_spec({"name": "d", "relay": ["x"]})


@pytest.mark.parametrize("bot_login", [None, "", "  "])
def test_bot_login_is_required(bot_login):
    relay = {"allowed": ["me"]}
    if bot_login is not None:
        relay["bot_login"] = bot_login
    with pytest.raises(ValueError, match="relay.bot_login"):
        RelayConfig.from_spec({"name": "d", "github": "o/r", "relay": relay})


def test_build_loops_shapes():
    loops = build_loops(RelayConfig.from_spec(spec(builder={"model": "b"}, reviewer={"model": "r"})))
    assert set(loops) == {LOOP_DISPATCH, LOOP_SESSION, LOOP_AUDIT}
    assert loops[LOOP_DISPATCH]["worktree"] is False
    assert loops[LOOP_DISPATCH]["triggers"] == [{"type": "cron", "cron": relay_config.DEFAULT_DISPATCH_CRON}]
    session = {s["name"]: s for s in loops[LOOP_SESSION]["steps"]}
    assert list(session) == ["branch", "checkout", "session", "checkin", "publish"]
    assert session["branch"]["branch"] == "fabrikant/relay/{issue_number}"
    agent = session["session"]
    assert (agent["gate"], agent["github_identity"], agent["model"]) == (False, "dev", "b")
    assert "backend" not in agent  # nicht gesetzt → FABRIKANT_AGENT_BACKEND
    # Issue-Nummer nur über env: (ADR 0008), Interpreter = der des Servers.
    assert "{issue_number}" not in session["checkout"]["cmd"]
    assert session["checkout"]["env"] == {"RELAY_ISSUE": "{issue_number}"}
    assert session["checkout"]["cmd"].startswith(sys.executable)
    audit = [s["name"] for s in loops[LOOP_AUDIT]["steps"]]
    assert audit == ["branch", "audit-prepare", "audit-blind", "audit-reveal", "audit-review", "audit-publish"]
    assert all(s.get("github_identity") in (None, "reviewer") for s in loops[LOOP_AUDIT]["steps"])


def test_audit_false_omits_audit_loop():
    assert LOOP_AUDIT not in build_loops(RelayConfig.from_spec(spec(audit=False)))


def test_expand_loops_rejects_reserved_names_and_ignores_specs_without_relay():
    plain = {"name": "d", "loops": {"x": {}}}
    expand_loops(plain)
    assert plain["loops"] == {"x": {}}
    with pytest.raises(ValueError, match="reserviert"):
        expand_loops({**spec(), "loops": {LOOP_SESSION: {}}})


def test_spec_with_only_relay_block_loads_and_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(specs.config, "PROJECTS_DIR", tmp_path)
    data = {**spec(forbid=[".github/**"]), "root": str(tmp_path)}
    (tmp_path / "demo.fabrikant.yaml").write_text(yaml.safe_dump(data))
    loaded = specs.load_spec(tmp_path / "demo.fabrikant.yaml")
    assert {LOOP_DISPATCH, LOOP_SESSION, LOOP_AUDIT} <= set(loaded["loops"])
    loaded_spec, cfg = relay_config.load("demo")
    assert cfg.forbid == (".github/**",) and loaded_spec["name"] == "demo"


def test_load_without_relay_block_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(specs.config, "PROJECTS_DIR", tmp_path)
    data = {"name": "demo", "root": str(tmp_path), "loops": {"x": {"steps": [{"type": "shell", "cmd": "true"}]}}}
    (tmp_path / "demo.fabrikant.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="keinen relay"):
        relay_config.load("demo")


def test_fabrikant_url_env_override(monkeypatch):
    monkeypatch.setenv(relay_config.FABRIKANT_URL_ENV, "http://host:1/")
    assert relay_config.fabrikant_url() == "http://host:1"
    monkeypatch.delenv(relay_config.FABRIKANT_URL_ENV)
    assert relay_config.fabrikant_url().endswith(f":{relay_config.config.DEFAULT_PORT}")


def test_retry_block_is_parsed_and_validated():
    cfg = RelayConfig.from_spec(spec(retry={"max": 2, "backoff": ["10m"]}))
    assert cfg.retry.max == 2 and cfg.retry.delay(2).total_seconds() == 600
    assert RelayConfig.from_spec(spec()).retry is None
    with pytest.raises(ValueError, match="relay.retry.on"):
        RelayConfig.from_spec(spec(retry={"on": ["unknown"]}))
