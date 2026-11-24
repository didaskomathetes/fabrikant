"""Pre-rename `fabrik` names keep working (ADR 0070)."""

from __future__ import annotations

import logging

import pytest

from fabrikant import config, legacy, spec_sources, specs, webhook


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(legacy, "_warned", set())


def test_adopt_legacy_env_copies_only_missing_and_warns_once(caplog):
    env = {"FABRIK_PORT": "1", "FABRIK_HOST": "old", "FABRIKANT_HOST": "new", "OTHER": "x"}
    with caplog.at_level(logging.WARNING, logger="fabrikant.legacy"):
        assert legacy.adopt_legacy_env(env) == ["FABRIK_PORT"]
        legacy.adopt_legacy_env({"FABRIK_A": "1"})
    assert env["FABRIKANT_PORT"] == "1" and env["FABRIKANT_HOST"] == "new" and "FABRIKANT_OTHER" not in env
    assert caplog.text.count("deprecated pre-rename") == 1


def test_adopt_legacy_env_noop_without_legacy_vars():
    assert legacy.adopt_legacy_env({"FABRIKANT_PORT": "1"}) == []


def test_env_file_falls_back_to_legacy_name(tmp_path):
    assert legacy.existing_env_file(tmp_path, "fabrikant.env") == tmp_path / "fabrikant.env"
    (tmp_path / "fabrik.env").write_text("")
    assert legacy.existing_env_file(tmp_path, "fabrikant.env") == tmp_path / "fabrik.env"
    (tmp_path / "fabrikant.env").write_text("")
    assert legacy.existing_env_file(tmp_path, "fabrikant.env") == tmp_path / "fabrikant.env"


def test_state_db_prefers_new_and_keeps_old_history(tmp_path):
    assert legacy.state_db_filename(tmp_path, "fabrikant.db") == "fabrikant.db"
    (tmp_path / "fabrik.db").write_text("")
    assert legacy.state_db_filename(tmp_path, "fabrikant.db") == "fabrik.db"
    (tmp_path / "fabrikant.db").write_text("")
    assert legacy.state_db_filename(tmp_path, "fabrikant.db") == "fabrikant.db"


def test_cwd_with_legacy_env_file_is_home(tmp_path):
    (tmp_path / "fabrik.env").write_text("")
    assert config.default_home({}, tmp_path / "site-packages", tmp_path) == tmp_path


def test_legacy_specs_are_discovered_and_named(tmp_path):
    (tmp_path / "a.fabrik.yaml").write_text("x: 1")
    (tmp_path / "b.fabrikant.yaml").write_text("x: 1")
    target = tmp_path / "repo" / ".fabrik"
    target.mkdir(parents=True)
    (target / "fabrik.yaml").write_text("x: 1")
    names = [p.name for p in spec_sources._dir_specs(tmp_path)]
    assert names == ["b.fabrikant.yaml", "a.fabrik.yaml"]
    assert [spec_sources.default_name(p) for p in spec_sources._dir_specs(tmp_path)] == ["b", "a"]
    assert spec_sources.default_name(target / "fabrik.yaml") == "repo"
    assert [p.name for p in spec_sources._dir_specs(tmp_path / "repo")] == ["fabrik.yaml"]


def test_register_target_accepts_legacy_spec_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REGISTRY_FILE", tmp_path / "reg.json")
    (tmp_path / "r" / ".fabrik").mkdir(parents=True)
    (tmp_path / "r" / ".fabrik" / "fabrik.yaml").write_text("x: 1")
    assert spec_sources.register_target(tmp_path / "r").name == "fabrik.yaml"
    assert specs  # imported for spec-name resolution coverage


def test_webhook_header_prefers_new_and_accepts_legacy(caplog):
    new = webhook.SIGNATURE_HEADER
    assert webhook.header_value({new: "n", "X-Fabrik-Signature": "o"}, new) == "n"
    with caplog.at_level(logging.WARNING, logger="fabrikant.legacy"):
        assert webhook.header_value({"X-Fabrik-Signature": "o"}, new) == "o"
    assert "X-Fabrik-Signature" in caplog.text
    assert webhook.header_value({}, new) == ""
    assert legacy.legacy_header("X-Fabrikant-Nonce") == "X-Fabrik-Nonce"
