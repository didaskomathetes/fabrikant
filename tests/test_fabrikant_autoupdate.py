"""scripts/fabrikant-autoupdate.py — ff-only-Pull, Neustart-Entscheidung
(serve-commit vs. HEAD), Vertagen bei aktiven Runs, uv sync bei Dependency-Änderung.
Gegen echte Git-Repos (tmp); systemctl/uv gemockt."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from helpers import load_script

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fabrikant-autoupdate.py"


@pytest.fixture
def mod():
    return load_script(SCRIPT.name, "fabrikant_autoupdate")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, rel: str, text: str) -> str:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", rel)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path, mod, monkeypatch):
    """upstream (bare) + Arbeitskopie `work` (Checkout, fabrikant/ = FABRIKANT_HOME) + `dev` (pusht Merges)."""
    upstream = tmp_path / "upstream.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    dev = tmp_path / "dev"
    _git(tmp_path, "clone", "-q", str(upstream), str(dev))
    _git(dev, "checkout", "-q", "-b", "main")
    _commit(dev, "fabrikant/projects/a.yaml", "v1")
    _git(dev, "push", "-q", "origin", "main")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(upstream), str(work))
    home = work / "fabrikant"
    monkeypatch.setattr(mod, "FABRIKANT_HOME", home)
    monkeypatch.setattr(mod, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(mod, "SERVE_COMMIT_FILE", home / "state" / "serve-commit")
    return dev, work, home


@pytest.fixture
def calls(mod, monkeypatch):
    """Zeichnet systemctl-/uv-Aufrufe auf, statt sie auszuführen; git läuft echt."""
    seen: list[tuple[str, ...]] = []
    real = mod.run

    def fake(*cmd: str) -> str:
        if cmd[0] in ("systemctl", "uv"):
            seen.append(cmd)
            return ""
        return real(*cmd)

    monkeypatch.setattr(mod, "run", fake)
    return seen


def _set_serve_commit(home: Path, sha: str) -> None:
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "serve-commit").write_text(sha + "\n")


def _write_run(home: Path, run_id: str, status: str) -> None:
    d = home / "runs" / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps({"status": status}))


def test_up_to_date_no_restart(mod, repos, calls):
    _, work, home = repos
    _set_serve_commit(home, _git(work, "rev-parse", "HEAD"))
    assert mod.main() == 0
    assert calls == []


def test_merge_pulls_and_restarts(mod, repos, calls):
    dev, work, home = repos
    _set_serve_commit(home, _git(work, "rev-parse", "HEAD"))
    new = _commit(dev, "fabrikant/projects/a.yaml", "v2")
    _git(dev, "push", "-q", "origin", "main")
    assert mod.main() == 0
    assert _git(work, "rev-parse", "HEAD") == new
    assert calls == [("systemctl", "--user", "restart", mod.SERVICE)]


def test_change_outside_fabrikant_no_restart(mod, repos, calls):
    dev, work, home = repos
    _set_serve_commit(home, _git(work, "rev-parse", "HEAD"))
    new = _commit(dev, "other/readme.md", "x")
    _git(dev, "push", "-q", "origin", "main")
    assert mod.main() == 0
    assert _git(work, "rev-parse", "HEAD") == new
    assert calls == []


def test_missing_serve_commit_restarts(mod, repos, calls):
    assert mod.main() == 0
    assert calls == [("systemctl", "--user", "restart", mod.SERVICE)]


def test_busy_run_defers_restart_then_restarts(mod, repos, calls):
    _, _, home = repos
    _write_run(home, "r1", "running")
    _write_run(home, "r2", "needs_approval")  # blockiert nicht
    assert mod.main() == 0
    assert calls == []
    _write_run(home, "r1", "pass")
    assert mod.main() == 0
    assert calls == [("systemctl", "--user", "restart", mod.SERVICE)]


def test_dependency_change_runs_uv_sync(mod, repos, calls):
    dev, work, home = repos
    _set_serve_commit(home, _git(work, "rev-parse", "HEAD"))
    _commit(dev, "fabrikant/uv.lock", "lock")
    _git(dev, "push", "-q", "origin", "main")
    assert mod.main() == 0
    assert calls == [("uv", "sync", "--quiet"), ("systemctl", "--user", "restart", mod.SERVICE)]


def test_wrong_branch_fails(mod, repos, calls):
    _, work, _ = repos
    _git(work, "checkout", "-q", "-b", "feature")
    assert mod.main() == 1
    assert calls == []


def test_diverged_main_fails_without_touching_checkout(mod, repos, calls):
    dev, work, _ = repos
    _commit(dev, "fabrikant/projects/a.yaml", "upstream")
    _git(dev, "push", "-q", "origin", "main")
    local = _commit(work, "fabrikant/projects/a.yaml", "local")
    assert mod.main() == 1
    assert _git(work, "rev-parse", "HEAD") == local
    assert calls == []


def test_busy_runs_ignores_broken_records(mod, tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "bad.json").write_text("{")
    (d / "list.json").write_text("[]")
    (d / "q.json").write_text(json.dumps({"status": "queued"}))
    assert mod.busy_runs(tmp_path) == ["q"]
