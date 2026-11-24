"""Package-data and home resolution for installed wheels (ADR 0069)."""

from __future__ import annotations

from pathlib import Path

from fabrikant import config, spec_sources


def _checkout(root: Path) -> Path:
    (root / "src" / "fabrikant").mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    return root


def test_explicit_home_wins(tmp_path):
    env = {"FABRIKANT_HOME": str(tmp_path / "h")}
    assert config.default_home(env, _checkout(tmp_path / "co"), tmp_path) == tmp_path / "h"


def test_source_checkout_is_its_own_home(tmp_path):
    co = _checkout(tmp_path / "co")
    assert config.default_home({}, co, tmp_path) == co


def test_installed_wheel_uses_xdg_data_home(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path / "xdg")}
    assert config.default_home(env, tmp_path / "site-packages", tmp_path) == tmp_path / "xdg" / "fabrikant"


def test_installed_wheel_default_is_local_share(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config.default_home({}, tmp_path / "site-packages", tmp_path) == tmp_path / ".local" / "share" / "fabrikant"


def test_bundled_assets_live_inside_the_package():
    for d in (config.WEB_DIR, config.TEMPLATES_DIR):
        assert d.is_dir() and d.is_relative_to(config.PACKAGE_DIR)
    assert (config.WEB_DIR / "index.html").is_file()
    assert (config.PACKAGE_DIR / "prompts" / "relay" / "session.prompt.md").is_file()
    assert (config.PACKAGE_DIR / "examples" / "hello.fabrikant.yaml").is_file()


def test_relative_prompt_falls_back_to_bundled_package_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FABRIKANT_HOME", tmp_path / "empty-home")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "empty-data")
    got = spec_sources.resolve_relative("prompts/relay/session.prompt.md", None)
    assert got == config.PACKAGE_DIR / "prompts" / "relay" / "session.prompt.md"


def test_relative_prompt_prefers_data_dir_over_package(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "prompts" / "relay").mkdir(parents=True)
    mine = data / "prompts" / "relay" / "session.prompt.md"
    mine.write_text("custom")
    monkeypatch.setattr(config, "FABRIKANT_HOME", tmp_path / "empty-home")
    monkeypatch.setattr(config, "DATA_DIR", data)
    assert spec_sources.resolve_relative("prompts/relay/session.prompt.md", None) == mine


def test_cwd_with_fabrikant_env_is_the_home(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "fabrikant.env").write_text("")
    assert config.default_home({}, tmp_path / "site-packages", work) == work
    assert (
        config.default_home({"FABRIKANT_HOME": str(tmp_path / "h")}, tmp_path / "site-packages", work) == tmp_path / "h"
    )
