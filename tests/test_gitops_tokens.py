"""gitops: Dual-Identity-Token-Auflösung (ADR 0010) — dev (Builder-App) vs. reviewer (Reviewer-App)."""

from __future__ import annotations

import pytest

from fabrik import gitops


def test_dev_token_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "dev-tok")
    assert gitops._gh_token_safe("dev") == "dev-tok"


def test_reviewer_token_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_REVIEWER_TOKEN", "rev-tok")
    assert gitops._gh_token_safe("reviewer") == "rev-tok"


def test_roles_are_independent(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "dev-tok")
    monkeypatch.setenv("GITHUB_REVIEWER_TOKEN", "rev-tok")
    assert gitops._gh_token_safe("dev") != gitops._gh_token_safe("reviewer")


def test_reviewer_falls_back_to_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_REVIEWER_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    f = tmp_path / ".env"
    f.write_text('GITHUB_REVIEWER_TOKEN="file-tok"\nGITHUB_TOKEN=dev-file\n')
    monkeypatch.setenv("FABRIK_ENV_FILE", str(f))
    assert gitops._gh_token_safe("reviewer") == "file-tok"
    assert gitops._gh_token_safe("dev") == "dev-file"


def test_env_wins_over_file(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text("GITHUB_TOKEN=file-tok\n")
    monkeypatch.setenv("FABRIK_ENV_FILE", str(f))
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    assert gitops._gh_token_safe("dev") == "env-tok"


def test_missing_reviewer_token_raises(monkeypatch):
    monkeypatch.delenv("GITHUB_REVIEWER_TOKEN", raising=False)
    monkeypatch.setenv("FABRIK_ENV_FILE", "/nonexistent/.env")
    with pytest.raises(RuntimeError, match="GITHUB_REVIEWER_TOKEN"):
        gitops._gh_token("reviewer")


def test_missing_dev_token_raises(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("FABRIK_ENV_FILE", "/nonexistent/.env")
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        gitops._gh_token("dev")


def test_unknown_role_raises():
    with pytest.raises(ValueError, match="Rolle"):
        gitops._gh_token_safe("hacker")
