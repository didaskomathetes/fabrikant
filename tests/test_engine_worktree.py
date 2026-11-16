"""Engine: Worktree pro Run, Cleanup und gemeinsamer Agent-State (ADR 0028)."""

from __future__ import annotations

import json
import subprocess
import threading

from fabrik import agent_state, config
from fabrik.engine import run_loop


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True)


def _setup(projects_dir, tmp_path, monkeypatch, steps_yaml, name="wtp"):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c1")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    body = "\n".join("      " + ln for ln in steps_yaml.strip().splitlines())
    (projects_dir / f"{name}.fabrik.yaml").write_text(
        f"name: {name}\nroot: {repo}\nrepo: {{branch: main}}\nloops:\n  s:\n    steps:\n{body}\n"
    )
    return name


def test_run_uses_own_worktree_and_cleans_up(runs_dir, projects_dir, tmp_path, monkeypatch):
    name = _setup(projects_dir, tmp_path, monkeypatch, "- {name: w, type: shell, cmd: 'pwd'}")
    rec = run_loop(name, "s")
    assert rec["status"] == "pass"
    assert rec["vars"]["workdir"] == str(tmp_path / "wt" / name / rec["id"])
    assert not (tmp_path / "wt" / name / rec["id"]).exists()


def test_failed_run_also_cleans_up(runs_dir, projects_dir, tmp_path, monkeypatch):
    name = _setup(projects_dir, tmp_path, monkeypatch, "- {name: w, type: shell, cmd: 'exit 1'}")
    rec = run_loop(name, "s")
    assert rec["status"] == "fail"
    assert not (tmp_path / "wt" / name / rec["id"]).exists()


def test_parallel_runs_get_distinct_worktrees(runs_dir, projects_dir, tmp_path, monkeypatch):
    name = _setup(projects_dir, tmp_path, monkeypatch, "- {name: w, type: shell, cmd: 'sleep 0.3; pwd'}")
    results = []
    threads = [threading.Thread(target=lambda: results.append(run_loop(name, "s"))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r["status"] for r in results] == ["pass"] * 3
    assert len({r["vars"]["workdir"] for r in results}) == 3


def test_pause_keeps_worktree_and_resume_reuses_then_cleans(runs_dir, projects_dir, tmp_path, monkeypatch):
    name = _setup(
        projects_dir,
        tmp_path,
        monkeypatch,
        "- {name: w, type: shell, cmd: 'echo pre > pre.txt'}\n"
        "- {name: gate, type: shell, cmd: 'cat pre.txt', approval: true}",
    )
    rec = run_loop(name, "s")
    assert rec["status"] == "needs_approval"
    wt = tmp_path / "wt" / name / rec["id"]
    assert (wt / "pre.txt").is_file()
    rec2 = run_loop(name, "s", resume_id=rec["id"])
    assert rec2["status"] == "pass"  # pre.txt aus dem Worktree vor der Pause ist noch da
    assert not wt.exists()


def test_agent_state_merged_into_shared_state(runs_dir, projects_dir, tmp_path, monkeypatch):
    # wie der Agent: den geseedeten Stand lesen und einen Key ergänzen
    py = "import json; p='state/reviewed-prs.json'; d=json.load(open(p)); d['7']='sha7'; json.dump(d, open(p, 'w'))"
    cmd = f'python3 -c "{py}"'
    step = json.dumps({"name": "w", "type": "shell", "cmd": cmd})  # JSON ist gültiges YAML
    name = _setup(projects_dir, tmp_path, monkeypatch, f"- {step}")
    shared = agent_state.shared_dir(name) / "reviewed-prs.json"
    shared.parent.mkdir(parents=True)
    shared.write_text(json.dumps({"1": "sha1"}))
    rec = run_loop(name, "s")
    assert rec["status"] == "pass"
    assert json.loads(shared.read_text()) == {"1": "sha1", "7": "sha7"}


def _stored(runs_dir, name, rec):
    return json.loads((runs_dir / name / f"{rec['id']}.json").read_text())


def test_worktree_cleanup_error_is_persisted_not_raised(runs_dir, projects_dir, tmp_path, monkeypatch):
    from fabrik import engine

    name = _setup(projects_dir, tmp_path, monkeypatch, "- {name: w, type: shell, cmd: 'true'}")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 120)

    monkeypatch.setattr(engine, "cleanup_run_worktree", boom)
    rec = run_loop(name, "s")
    assert rec["status"] == "pass"  # Cleanup-Fehler kippt den Run nicht
    assert "Worktree-Cleanup fehlgeschlagen" in rec["worktree_cleanup_error"]
    assert "Worktree-Cleanup fehlgeschlagen" in _stored(runs_dir, name, rec)["worktree_cleanup_error"]
    log = (runs_dir / name / f"{rec['id']}.log").read_text()
    assert "Worktree-Cleanup fehlgeschlagen" in log


def test_worktree_cleanup_incomplete_is_reported(runs_dir, projects_dir, tmp_path, monkeypatch):
    """Cleanup meldet „nicht entfernt“ (False) → Fehler im Record statt stillem Erfolg."""
    from fabrik import engine

    name = _setup(projects_dir, tmp_path, monkeypatch, "- {name: w, type: shell, cmd: 'true'}")
    monkeypatch.setattr(engine, "cleanup_run_worktree", lambda *a, **k: False)
    rec = run_loop(name, "s")
    assert rec["status"] == "pass"
    assert "unvollständig" in rec["worktree_cleanup_error"]


def test_unexpected_cleanup_error_is_not_swallowed(runs_dir, projects_dir, tmp_path, monkeypatch):
    """Kein breites except: ein Programmierfehler im Cleanup wird als Run-Fehler sichtbar."""
    from fabrik import engine

    name = _setup(projects_dir, tmp_path, monkeypatch, "- {name: w, type: shell, cmd: 'true'}")

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(engine, "cleanup_run_worktree", boom)
    try:
        run_loop(name, "s")
    except RuntimeError as e:
        assert "bug" in str(e)
    else:  # pragma: no cover
        raise AssertionError("RuntimeError muss durchschlagen")


def test_state_merge_error_is_persisted_and_keeps_worktree(runs_dir, projects_dir, tmp_path, monkeypatch):
    """Merge scheitert → Fehler im Record, Worktree (ungemergter Stand) bleibt, Cleanup wird nicht versucht."""
    from fabrik import engine

    name = _setup(projects_dir, tmp_path, monkeypatch, "- {name: w, type: shell, cmd: 'true'}")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(engine.agent_state, "merge_back", boom)
    cleanups = []
    monkeypatch.setattr(engine, "cleanup_run_worktree", lambda *a, **k: cleanups.append(a) or True)
    rec = run_loop(name, "s")
    assert rec["status"] == "pass"
    assert "Agent-State-Merge fehlgeschlagen" in rec["state_merge_error"]
    assert "Agent-State-Merge fehlgeschlagen" in _stored(runs_dir, name, rec)["state_merge_error"]
    assert "worktree_cleanup_error" not in rec
    assert cleanups == []
    assert (tmp_path / "wt" / name / rec["id"]).is_dir()


def test_cleanup_error_on_pause_is_persisted(runs_dir, projects_dir, tmp_path, monkeypatch):
    from fabrik import engine

    name = _setup(
        projects_dir,
        tmp_path,
        monkeypatch,
        "- {name: gate, type: shell, cmd: 'true', approval: true}",
    )

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(engine.agent_state, "merge_back", boom)
    rec = run_loop(name, "s")
    assert rec["status"] == "needs_approval"
    assert "Agent-State-Merge fehlgeschlagen" in _stored(runs_dir, name, rec)["state_merge_error"]
    assert (tmp_path / "wt" / name / rec["id"]).is_dir()  # Pause behält den Worktree


def test_cleanup_rejected_run_removes_worktree_and_reports_errors(runs_dir, projects_dir, tmp_path, monkeypatch):
    from fabrik import engine

    name = _setup(projects_dir, tmp_path, monkeypatch, "- {name: gate, type: shell, cmd: 'true', approval: true}")
    rec = run_loop(name, "s")
    wt = tmp_path / "wt" / name / rec["id"]
    assert wt.is_dir()
    msgs: list[str] = []
    engine.cleanup_rejected_run(name, rec["id"], msgs.append)
    assert not wt.exists() and msgs == []

    monkeypatch.setattr(engine, "cleanup_run_worktree", lambda *a, **k: False)
    engine.cleanup_rejected_run(name, rec["id"], msgs.append)
    assert len(msgs) == 1 and "unvollständig" in msgs[0]
