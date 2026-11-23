"""Projekt-eigene Token-Datei (`github_env_file`): Auflösung, Kontext, Subprozess-Env, Validierung."""

import threading

import pytest

from fabrik import gitops


@pytest.fixture
def files(tmp_path, monkeypatch):
    glob = tmp_path / "global.env"
    glob.write_text("GITHUB_TOKEN=global-dev\nGITHUB_REVIEWER_TOKEN=global-rev\n")
    proj = tmp_path / "proj.env"
    proj.write_text("GITHUB_TOKEN=proj-dev\nGITHUB_REVIEWER_TOKEN=proj-pat\n")
    monkeypatch.setenv(gitops.ENV_FILE_VAR, str(glob))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REVIEWER_TOKEN", raising=False)
    return glob, proj


def test_without_override_global_file_is_used(files):
    glob, _ = files
    assert gitops._gh_token_safe("dev") == "global-dev"
    assert gitops.token_env_file() == glob
    assert gitops.subprocess_env() == {}
    with gitops.project_github({"name": "x"}):  # Spec ohne Feld: unverändert
        assert gitops._gh_token_safe("reviewer") == "global-rev"


def test_project_file_wins_and_is_reset(files):
    _, proj = files
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        assert gitops._gh_token_safe("dev") == "proj-dev"
        assert gitops._gh_token_safe("reviewer") == "proj-pat"
        assert gitops.subprocess_env() == {gitops.ENV_FILE_VAR: str(proj), gitops.PROJECT_ENV_FILE_VAR: str(proj)}
    assert gitops._gh_token_safe("dev") == "global-dev"


def test_project_file_ignores_global_token_env(files, monkeypatch):
    _, proj = files
    monkeypatch.setenv("GITHUB_TOKEN", "env-dev")
    assert gitops._gh_token_safe("dev") == "env-dev"
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        assert gitops._gh_token_safe("dev") == "proj-dev"


def test_missing_key_in_project_file_raises_with_path(files, tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("")
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(empty)}):
        assert gitops._gh_token_safe("dev") == ""
        with pytest.raises(gitops.SetupError, match="empty.env"):
            gitops._gh_token("dev")


def test_tilde_and_env_vars_are_expanded(files, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: "~/proj.env"}):
        assert gitops._gh_token_safe("dev") == "proj-dev"


def test_override_is_per_thread(files):
    _, proj = files
    seen = {}
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        t = threading.Thread(target=lambda: seen.setdefault("tok", gitops._gh_token_safe("dev")))
        t.start()
        t.join()
        assert gitops._gh_token_safe("dev") == "proj-dev"
    assert seen["tok"] == "global-dev"  # neuer Thread erbt den Kontext nicht


def test_github_env_passes_project_file_to_agent(files, monkeypatch):
    _, proj = files
    monkeypatch.setattr(gitops, "_gh_wrapper_dir", lambda role, path: None)
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        env = gitops.github_env("reviewer")
    assert env["GH_TOKEN"] == "proj-pat" and env[gitops.ENV_FILE_VAR] == str(proj)


def test_shell_step_inherits_project_file(files, tmp_path):
    from fabrik.steps.shell import _step_shell

    _, proj = files
    ctx = {"workdir": str(tmp_path)}
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        res = _step_shell(ctx, {"cmd": 'echo "$FABRIK_ENV_FILE"'}, lambda m: None)
    assert res["summary"] == str(proj)


def test_subprocess_inherits_override_and_ignores_global_token_env(files, monkeypatch):
    """Subprozess (z. B. `python -m fabrik.push` aus einem Shell-Step) ohne Kontext:
    das geerbte Env markiert die Projekt-Datei, globale Token-Werte zählen nicht."""
    _, proj = files
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj), gitops.SPEC_COMMIT_VIA_KEY: "api"}):
        child_env = gitops.subprocess_env()
    for key, value in {**child_env, "GITHUB_TOKEN": "global-env-dev"}.items():
        monkeypatch.setenv(key, value)
    assert gitops._gh_token_safe("dev") == "proj-dev"
    assert gitops.commit_via() == gitops.COMMIT_VIA_API
    assert gitops.token_env_file() == proj


def test_process_env_replaces_inherited_tokens_with_project_identity(files):
    _, proj = files
    base = {"PATH": "/bin", "GH_TOKEN": "g", "GITHUB_TOKEN": "g", "GITHUB_REVIEWER_TOKEN": "g-rev"}
    assert gitops.process_env(base) == base  # ohne Projekt-Datei unverändert
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        env = gitops.process_env(base)
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "proj-dev"
    assert "GITHUB_REVIEWER_TOKEN" not in env
    assert env["PATH"] == "/bin" and env[gitops.PROJECT_ENV_FILE_VAR] == str(proj)


def test_process_env_drops_tokens_when_project_file_has_none(files, tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("")
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(empty)}):
        env = gitops.process_env({"GH_TOKEN": "g", "GITHUB_TOKEN": "g"})
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env


def test_shell_step_gh_token_is_project_identity(files, monkeypatch, tmp_path):
    from fabrik.steps.shell import _step_shell

    _, proj = files
    monkeypatch.setenv("GH_TOKEN", "global-gh")
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        res = _step_shell({"workdir": str(tmp_path)}, {"cmd": 'echo "$GH_TOKEN"'}, lambda m: None)
    assert res["summary"] == "proj-dev"


def test_agent_without_identity_gets_project_settings(files):
    from fabrik.backends.claude_code import ClaudeCodeBackend
    from fabrik.steps.agent import _apply_github_identity

    _, proj = files
    be = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    be.extra_env = {}
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj), gitops.SPEC_COMMIT_VIA_KEY: "api"}):
        assert _apply_github_identity(be, {}) is None
    assert be.extra_env[gitops.COMMIT_VIA_VAR] == "api"
    assert be.extra_env[gitops.PROJECT_ENV_FILE_VAR] == str(proj)


def test_agent_without_identity_overrides_inherited_global_tokens(files, monkeypatch):
    """Geerbtes GH_TOKEN/GITHUB_TOKEN darf im Agent nicht die globale Identität behalten."""
    from fabrik.backends.claude_code import ClaudeCodeBackend
    from fabrik.steps.agent import _apply_github_identity

    _, proj = files
    be = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    be.extra_env = {}
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        assert _apply_github_identity(be, {}) is None
    assert be.extra_env["GH_TOKEN"] == be.extra_env["GITHUB_TOKEN"] == "proj-dev"
    assert be.extra_env["GITHUB_REVIEWER_TOKEN"] == ""


def test_token_overrides_empty_without_project_file(files):
    assert gitops.token_overrides() == {}


def test_spec_rejects_relative_github_env_file(tmp_path):
    from fabrik.specs import _validate_spec

    spec = {
        "name": "p",
        "root": str(tmp_path),
        gitops.SPEC_ENV_FILE_KEY: "rel/tokens.env",
        "loops": {"l": {"steps": [{"type": "shell", "cmd": "true"}]}},
    }
    with pytest.raises(ValueError, match="absolut"):
        _validate_spec(spec)


@pytest.mark.parametrize("value", [123, "", "  ", ["a"]])
def test_spec_rejects_invalid_github_env_file(value, tmp_path):
    from fabrik.specs import _validate_spec

    spec = {
        "name": "p",
        "root": str(tmp_path),
        gitops.SPEC_ENV_FILE_KEY: value,
        "loops": {"l": {"steps": [{"type": "shell", "cmd": "true"}]}},
    }
    with pytest.raises(ValueError, match=gitops.SPEC_ENV_FILE_KEY):
        _validate_spec(spec)


def test_run_loop_applies_project_token_file(runs_dir, projects_dir, tmp_path):
    import fabrik

    tokens = tmp_path / "p.env"
    tokens.write_text("GITHUB_TOKEN=x\n")
    (projects_dir / "ptok.fabrik.yaml").write_text(
        f"name: ptok\nroot: /tmp\n{gitops.SPEC_ENV_FILE_KEY}: {tokens}\n"
        "loops:\n  t:\n    steps:\n      - type: shell\n        name: s\n        cmd: 'echo \"$FABRIK_ENV_FILE\"'\n"
    )
    rec = fabrik.run_loop("ptok", "t")
    assert rec["steps"][0]["summary"] == str(tokens)
    assert gitops.subprocess_env() == {}  # nach dem Run zurückgesetzt


def test_sandboxed_agent_without_identity_gets_no_token(files, monkeypatch):
    """Sandbox (ADR 0054): ohne `github_identity` kein (Builder-)Token im Container."""
    from fabrik import sandbox
    from fabrik.backends.claude_code import ClaudeCodeBackend
    from fabrik.steps.agent import _apply_github_identity

    _, proj = files
    monkeypatch.setenv(sandbox.ENABLE_VAR, "1")
    monkeypatch.setenv("GH_TOKEN", "global-gh")
    be = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    be.extra_env = {}
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        assert _apply_github_identity(be, {}) is None
    assert be.extra_env == {"GH_TOKEN": "", "GITHUB_TOKEN": "", "GITHUB_REVIEWER_TOKEN": ""}
    with gitops.project_github({gitops.SPEC_ENV_FILE_KEY: str(proj)}):
        assert _apply_github_identity(be, {"github_identity": "dev"}) is None
    assert be.extra_env["GITHUB_TOKEN"] == "proj-dev"  # explizite Identität bleibt
