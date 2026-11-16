"""agent_state + filelock: gemeinsamer, gelockter Agent-State über parallele Runs (ADR 0028)."""

from __future__ import annotations

import json
import subprocess
import threading

import pytest

from fabrik import agent_state, config
from fabrik.filelock import locked


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")


def _write(wd, name, data):
    (wd / "state").mkdir(parents=True, exist_ok=True)
    (wd / "state" / name).write_text(json.dumps(data))


def _read(path):
    return json.loads(path.read_text())


def test_seed_copies_shared_state(tmp_path):
    shared = agent_state.shared_dir("p")
    shared.mkdir(parents=True)
    (shared / "reviewed-prs.json").write_text(json.dumps({"1": "abc"}))
    wd = tmp_path / "wd"
    snap = agent_state.seed("p", wd)
    assert snap["reviewed-prs.json"] == {"1": "abc"}
    assert _read(wd / "state" / "reviewed-prs.json") == {"1": "abc"}
    assert not (wd / "state" / "replied-comments.json").exists()


def test_resume_seed_preserves_local_state_and_original_snapshot(tmp_path):
    shared = agent_state.shared_dir("p")
    shared.mkdir(parents=True)
    (shared / "reviewed-prs.json").write_text(json.dumps({"1": "old"}))
    wd = tmp_path / "wd"
    original = agent_state.seed("p", wd)
    _write(wd, "reviewed-prs.json", {"1": "local"})

    # Another run updates the canonical state while this run is paused.
    (shared / "reviewed-prs.json").write_text(json.dumps({"1": "old", "2": "other"}))
    resumed = agent_state.seed("p", wd)

    assert resumed == original
    assert _read(wd / "state" / "reviewed-prs.json") == {"1": "local"}
    agent_state.merge_back("p", wd, resumed)
    assert _read(shared / "reviewed-prs.json") == {"1": "local", "2": "other"}


def test_parallel_runs_lose_no_updates(tmp_path):
    """Zwei Runs seeden denselben Stand, ändern verschiedene Keys → beide bleiben erhalten."""
    wd1, wd2 = tmp_path / "wd1", tmp_path / "wd2"
    snap1 = agent_state.seed("p", wd1)
    snap2 = agent_state.seed("p", wd2)
    _write(wd1, "reviewed-prs.json", {"1": "sha1"})
    _write(wd2, "reviewed-prs.json", {"2": "sha2"})
    agent_state.merge_back("p", wd1, snap1)
    agent_state.merge_back("p", wd2, snap2)
    assert _read(agent_state.shared_dir("p") / "reviewed-prs.json") == {"1": "sha1", "2": "sha2"}


def test_merge_updates_and_deletions(tmp_path):
    shared = agent_state.shared_dir("p")
    shared.mkdir(parents=True)
    (shared / "replied-comments.json").write_text(json.dumps({"a": True, "b": True, "c": True}))
    wd = tmp_path / "wd"
    snap = agent_state.seed("p", wd)
    _write(wd, "replied-comments.json", {"a": True, "c": "neu", "d": True})  # b gelöscht, c geändert, d neu
    # zwischenzeitlich schreibt ein anderer Run "e"
    (shared / "replied-comments.json").write_text(json.dumps({"a": True, "b": True, "c": True, "e": True}))
    agent_state.merge_back("p", wd, snap)
    assert _read(shared / "replied-comments.json") == {"a": True, "c": "neu", "d": True, "e": True}


@pytest.mark.parametrize("merge_delete_first", [True, False])
def test_merge_delete_does_not_lose_concurrent_update(tmp_path, merge_delete_first):
    shared = agent_state.shared_dir("p")
    shared.mkdir(parents=True)
    (shared / "reviewed-prs.json").write_text(json.dumps({"a": "old"}))
    delete_wd, update_wd = tmp_path / "delete", tmp_path / "update"
    delete_snapshot = agent_state.seed("p", delete_wd)
    update_snapshot = agent_state.seed("p", update_wd)
    _write(delete_wd, "reviewed-prs.json", {})
    _write(update_wd, "reviewed-prs.json", {"a": "new"})

    first, second = (
        ((delete_wd, delete_snapshot), (update_wd, update_snapshot))
        if merge_delete_first
        else ((update_wd, update_snapshot), (delete_wd, delete_snapshot))
    )
    agent_state.merge_back("p", *first)
    agent_state.merge_back("p", *second)

    assert _read(shared / "reviewed-prs.json") == {"a": "new"}


def test_merge_rejects_corrupt_local_file_without_touching_shared_state(tmp_path):
    wd = tmp_path / "wd"
    agent_state.merge_back("p", wd)  # keine Dateien: No-Op
    (wd / "state").mkdir(parents=True)
    (wd / "state" / "reviewed-prs.json").write_text("{kaputt")
    shared = agent_state.shared_dir("p") / "reviewed-prs.json"
    shared.parent.mkdir(parents=True)
    shared.write_text(json.dumps({"valid": True}))
    with pytest.raises(agent_state.AgentStateError):
        agent_state.merge_back("p", wd, {})
    assert _read(shared) == {"valid": True}


def test_merge_rejects_corrupt_shared_file_without_overwriting_it(tmp_path):
    shared = agent_state.shared_dir("p") / "reviewed-prs.json"
    shared.parent.mkdir(parents=True)
    shared.write_text("{kaputt")
    wd = tmp_path / "wd"
    _write(wd, "reviewed-prs.json", {"new": True})
    with pytest.raises(agent_state.AgentStateError):
        agent_state.merge_back("p", wd, {})
    assert shared.read_text() == "{kaputt"


def test_concurrent_merges_threads(tmp_path):
    n = 12
    wds = [tmp_path / f"wd{i}" for i in range(n)]
    snaps = [agent_state.seed("p", wd) for wd in wds]
    for i, wd in enumerate(wds):
        _write(wd, "reviewed-prs.json", {str(i): f"sha{i}"})
    threads = [
        threading.Thread(target=agent_state.merge_back, args=("p", wd, s)) for wd, s in zip(wds, snaps, strict=True)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _read(agent_state.shared_dir("p") / "reviewed-prs.json") == {str(i): f"sha{i}" for i in range(n)}


def test_locked_is_exclusive(tmp_path):
    target = tmp_path / "x.json"
    counter = {"n": 0, "max": 0}

    def work():
        with locked(target):
            counter["n"] += 1
            counter["max"] = max(counter["max"], counter["n"])
            counter["n"] -= 1

    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert counter["max"] == 1
    assert (tmp_path / "x.json.lock").exists()


def test_state_step_concurrent_add_loses_nothing(tmp_path):
    """state-Step: paralleles `add` auf denselben Zähler (Read-Modify-Write unter Lock)."""
    from fabrik.steps.state import _step_state

    root = tmp_path / "root"
    root.mkdir()
    step = {"action": "add", "key": "n", "value": "1", "file": "state/c.json"}

    def work():
        _step_state({"root": str(root), "project": "p"}, step, lambda _m: None)

    ts = [threading.Thread(target=work) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert _read(root / "state" / "c.json") == {"n": 20}


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.mark.parametrize("subpath", ["", "fabrik"])
def test_seed_excludes_state_dir_anchored_to_workdir(tmp_path, subpath):
    """state/ landet nie im Commit — gleichnamige Verzeichnisse woanders bleiben sichtbar."""
    repo = tmp_path / "repo"
    workdir = repo / subpath if subpath else repo
    workdir.mkdir(parents=True)
    _git(repo, "init", "-q")
    agent_state.seed("p", workdir)
    agent_state.seed("p", workdir)  # idempotent
    (repo / "infra" / "state").mkdir(parents=True)
    (repo / "infra" / "state" / "prod.backend").write_text("x")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").split()
    assert untracked == ["infra/state/prod.backend"]
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(f"/{subpath + '/' if subpath else ''}state/") == 1


def test_seed_outside_git_is_noop_for_exclude(tmp_path):
    agent_state.seed("p", tmp_path)  # kein Git-Checkout → kein Fehler
    assert (tmp_path / "state").is_dir()
