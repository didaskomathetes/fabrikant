"""Branch-Overrides (ADR 0004): State-Datei, Spec-Anwendung, git-Listing, API."""

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fabrik import branches, config, specs

# ---------------------------------------------------------------- State-Datei


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", d)
    return d


def test_load_overrides_missing_file(state_dir):
    assert branches.load_overrides() == {}


def test_load_overrides_corrupt_file(state_dir):
    (state_dir / "branch-overrides.json").write_text("{ nicht json")
    assert branches.load_overrides() == {}


def test_load_overrides_filters_non_strings(state_dir):
    (state_dir / "branch-overrides.json").write_text(json.dumps({"a": "main", "b": 1, "c": ""}))
    assert branches.load_overrides() == {"a": "main"}


def test_save_and_load_override(state_dir):
    branches.save_override("fabrik", "feature/x")
    assert branches.load_overrides() == {"fabrik": "feature/x"}
    assert (state_dir / "branch-overrides.json").is_file()


def test_save_override_updates_existing(state_dir):
    branches.save_override("fabrik", "main")
    branches.save_override("fabrik", "dev")
    assert branches.load_overrides() == {"fabrik": "dev"}


def test_clear_override(state_dir):
    branches.save_override("fabrik", "dev")
    branches.save_override("other", "main")
    branches.clear_override("fabrik")
    assert branches.load_overrides() == {"other": "main"}
    # Nicht existierendes Projekt löschen ist ein No-op.
    branches.clear_override("ghost")
    assert branches.load_overrides() == {"other": "main"}


# ---------------------------------------------------------------- Spec-Anwendung


def _write_spec(
    projects_dir: Path,
    name: str = "fabrik",
    branch: str = "main",
    with_repo: bool = True,
    root: Path | None = None,
):
    repo_yaml = f"repo:\n  branch: {branch}\n" if with_repo else ""
    (projects_dir / f"{name}.fabrik.yaml").write_text(
        f"name: {name}\nroot: {root or projects_dir}\n{repo_yaml}"
        "loops:\n  l:\n    tier: 1\n    steps:\n      - name: s\n        type: shell\n        cmd: echo hi\n"
    )


def test_load_projects_applies_override(state_dir, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    _write_spec(projects)
    branches.save_override("fabrik", "feature/x")
    loaded = specs.load_projects()
    assert loaded["fabrik"]["repo"]["branch"] == "feature/x"


def test_load_projects_without_override_keeps_default(state_dir, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    _write_spec(projects)
    loaded = specs.load_projects()
    assert loaded["fabrik"]["repo"]["branch"] == "main"


def test_load_projects_skips_specs_without_repo(state_dir, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    _write_spec(projects, with_repo=False)
    branches.save_override("fabrik", "feature/x")
    loaded = specs.load_projects()
    assert "repo" not in loaded["fabrik"]


def test_load_projects_skips_error_specs(state_dir, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    (projects / "broken.fabrik.yaml").write_text(
        "name: broken\nroot: /nonexistent/pfad\nloops:\n  l:\n    steps:\n      - name: s\n        type: shell\n        cmd: echo\n"
    )
    branches.save_override("broken", "feature/x")
    loaded = specs.load_projects()
    assert loaded["broken"].get("_error")
    assert "repo" not in loaded["broken"]


# ---------------------------------------------------------------- git-Listing


def _git(args: list[str], cwd: Path):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    """Repo mit 2 lokalen Branches + bare Remote (origin/*-Refs vorhanden)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "f.txt").write_text("x")
    _git(["add", "."], repo)
    _git(["commit", "-m", "init"], repo)
    _git(["branch", "feature/x"], repo)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "origin", "main", "feature/x"], repo)
    return repo


def test_list_branches_dedupes_local_and_remote(git_repo):
    assert branches.list_branches(git_repo) == ["feature/x", "main"]


def test_list_branches_accepts_str_path(git_repo):
    assert branches.list_branches(str(git_repo)) == ["feature/x", "main"]


def test_list_branches_non_repo(tmp_path):
    assert branches.list_branches(tmp_path) == []


# ---------------------------------------------------------------- API


@pytest.fixture
def fabrik_home(tmp_path, monkeypatch):
    """Isolierte FABRIK_HOME mit Test-Spec (toucht echte Daten nicht)."""
    home = tmp_path / "fabrik_home"
    (home / "projects").mkdir(parents=True)
    (home / "runs").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    _write_spec(home / "projects", name="uitest", branch="main")
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    return home


@pytest.fixture
def fabrik_url(fabrik_home, clean_token, live_server):
    """fabrik-HTTP-Server auf einem freien Port (ohne Scheduler)."""
    return live_server()


def _get(url: str):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read().decode())


def _post(url: str, payload: dict):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_get_branches_returns_current(fabrik_url):
    data = _get(f"{fabrik_url}/api/projects/uitest/branches")
    assert data["current"] == "main"
    assert isinstance(data["branches"], list)


def test_get_branches_unknown_project_404(fabrik_url):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(f"{fabrik_url}/api/projects/ghost/branches")
    assert ei.value.code == 404


def test_post_branch_persists_override(fabrik_url, fabrik_home):
    status, data = _post(f"{fabrik_url}/api/projects/uitest/branch", {"branch": "dev"})
    assert status == 200 and data["ok"] is True
    # Override ist persistiert und in load_projects sichtbar.
    assert branches.load_overrides() == {"uitest": "dev"}
    assert specs.load_projects()["uitest"]["repo"]["branch"] == "dev"
    # GET zeigt den neuen aktuellen Branch.
    assert _get(f"{fabrik_url}/api/projects/uitest/branches")["current"] == "dev"
    # State-Datei liegt unter STATE_DIR.
    assert (fabrik_home / "state" / "branch-overrides.json").is_file()


def test_post_branch_unknown_project_404(fabrik_url):
    status, data = _post(f"{fabrik_url}/api/projects/ghost/branch", {"branch": "dev"})
    assert status == 404
    assert "error" in data


def test_post_branch_empty_400(fabrik_url):
    status, data = _post(f"{fabrik_url}/api/projects/uitest/branch", {"branch": "  "})
    assert status == 400
    assert "error" in data


def test_post_branch_rejects_nonexistent_branch(fabrik_url, fabrik_home):
    # Repo mit genau einem Branch als Spec-Root → "dev" existiert nicht.
    repo = fabrik_home / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "f.txt").write_text("x")
    _git(["add", "."], repo)
    _git(["commit", "-m", "init"], repo)
    _write_spec(fabrik_home / "projects", name="uitest", branch="main", root=repo)
    status, data = _post(f"{fabrik_url}/api/projects/uitest/branch", {"branch": "dev"})
    assert status == 400
    assert "existiert nicht" in data["error"]
    assert branches.load_overrides() == {}


def test_post_branch_invalid_json_400(fabrik_url):
    req = urllib.request.Request(
        f"{fabrik_url}/api/projects/uitest/branch", data=b"{ kaputt", headers={"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req)
    assert ei.value.code == 400
