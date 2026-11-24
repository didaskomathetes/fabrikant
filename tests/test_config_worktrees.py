"""config: Worktree-Wurzel außerhalb des Quell-Checkouts (ADR 0047, Nachtrag 2026-11-03)."""

from __future__ import annotations

from pathlib import Path

from fabrikant import config


def test_explicit_env_wins(tmp_path):
    env = {config.WORKTREES_ENV: str(tmp_path / "wt"), "XDG_STATE_HOME": "/ignored"}
    assert config.default_worktrees_dir(tmp_path, env) == tmp_path / "wt"


def test_explicit_env_expands_user(tmp_path):
    env = {config.WORKTREES_ENV: "~/fabrikant-wt"}
    assert config.default_worktrees_dir(tmp_path, env) == Path.home() / "fabrikant-wt"


def test_xdg_state_home(tmp_path):
    home = tmp_path / "fabrikant"
    home.mkdir()
    got = config.default_worktrees_dir(home, {"XDG_STATE_HOME": str(tmp_path / "state")})
    assert got == tmp_path / "state" / "fabrikant" / "worktrees" / config.install_id(home)


def test_falls_back_to_local_state(tmp_path):
    got = config.default_worktrees_dir(tmp_path, {config.WORKTREES_ENV: "  ", "XDG_STATE_HOME": ""})
    assert got.parent == Path.home() / ".local" / "state" / "fabrikant" / "worktrees"


def test_default_is_outside_the_checkout(tmp_path):
    """Kern der Entscheidung: nicht unter FABRIKANT_HOME und nicht im Repo darüber (IDE-Index)."""
    repo = tmp_path / "repo"
    home = repo / "fabrikant"
    home.mkdir(parents=True)
    got = config.default_worktrees_dir(home, {"XDG_STATE_HOME": str(tmp_path / "state")})
    assert not got.is_relative_to(repo)


def test_install_id_stable_and_distinct(tmp_path):
    a, b = tmp_path / "a" / "fabrikant", tmp_path / "b" / "fabrikant"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    first = config.install_id(a)
    again = config.install_id(a)
    assert first == again  # stabil: zweiter Aufruf liefert dasselbe
    assert first != config.install_id(b)  # gleicher Ordnername, andere Installation
    assert first.startswith("fabrikant-")
