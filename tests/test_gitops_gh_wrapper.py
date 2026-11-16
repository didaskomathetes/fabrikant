"""`gh`-Wrapper: frisches Identity-Token pro Call aus FABRIK_ENV_FILE (ADR 0010)."""

import os
import stat
import subprocess

import pytest

from fabrik import gitops


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """Fake-`gh` im PATH, das das effektive GH_TOKEN ausgibt."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text('#!/bin/sh\necho "token=$GH_TOKEN/$GITHUB_TOKEN"\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(gitops, "GH_WRAPPER_DIR", tmp_path / "wrap")
    env_file = tmp_path / "env"
    monkeypatch.setenv("FABRIK_ENV_FILE", str(env_file))
    return env_file


def _run_gh(env: dict[str, str]) -> str:
    return subprocess.run(["gh"], env={**os.environ, **env}, capture_output=True, text=True, check=True).stdout.strip()


def test_wrapper_picks_up_refreshed_token_after_spawn(fake_gh):
    fake_gh.write_text("GITHUB_TOKEN=old\n")
    env = gitops.github_env("dev")
    assert _run_gh(env) == "token=old/old"
    # Cron-Refresh nach > 1h überschreibt die Datei — die Prozess-Env bleibt fix
    fake_gh.write_text('OTHER=1\nGITHUB_TOKEN="new"\n')
    assert env["GH_TOKEN"] == "old"
    assert _run_gh(env) == "token=new/new"


@pytest.mark.parametrize("content", [None, "", "GITHUB_TOKEN=\n", "OTHER=1\n"])
def test_wrapper_falls_back_to_spawn_token_if_env_file_unusable(fake_gh, content):
    fake_gh.write_text("GITHUB_TOKEN=old\n")
    env = gitops.github_env("dev")
    if content is None:
        fake_gh.unlink()
    else:
        fake_gh.write_text(content)
    assert _run_gh(env) == "token=old/old"


def test_wrapper_uses_role_specific_key(fake_gh):
    fake_gh.write_text("GITHUB_TOKEN=dev\nGITHUB_REVIEWER_TOKEN=qa\n")
    assert _run_gh(gitops.github_env("reviewer")) == "token=qa/qa"


def test_wrapper_rewrite_is_idempotent_and_skipped_without_gh(fake_gh, monkeypatch):
    fake_gh.write_text("GITHUB_TOKEN=t\n")
    first = gitops.github_env("dev")["PATH"]
    assert gitops.github_env("dev")["PATH"] == first
    monkeypatch.setenv("PATH", "/nonexistent")
    assert "PATH" not in gitops.github_env("dev")
