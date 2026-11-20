"""Replay (ADR 0061, Issue #129): deterministic re-execution of a finished run.

Covers `replay_run` (pinned inputs, terminal-status guard, spec/loop checks),
`_pinned_vars` (derived-key filtering), `diff_runs` (report rendering) and the
`fabrik replay` CLI command.
"""

from __future__ import annotations

import json

import pytest

from fabrik import cli, replay


def _write_spec(projects_dir, name="p", root=None, loops=None):
    """Minimal valid spec (same shape as test_cli._write_spec)."""
    root = root or projects_dir.parent / "proj"
    root.mkdir(parents=True, exist_ok=True)
    loops = loops or {"l": {"steps": [{"type": "shell", "cmd": "true"}]}}
    (projects_dir / f"{name}.fabrik.yaml").write_text(
        f"name: {name}\nroot: {root}\nloops:\n"
        + "\n".join(
            f"  {ln}:\n    steps:\n" + "\n".join(f"      - {json.dumps(s)}" for s in ldef["steps"])
            for ln, ldef in loops.items()
        )
    )


def _write_run(runs_dir, project, rid, status="pass", loop="l", **extra):
    d = runs_dir / project
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": rid,
        "project": project,
        "loop": loop,
        "status": status,
        "started": "2024-01-01T00:00:00Z",
        "spec_hash": "abc123def456",
        "vars": {},
        "steps": [],
    }
    rec.update(extra)
    (d / f"{rid}.json").write_text(json.dumps(rec))
    return rec


def _fake_replay_record(orig):
    return {
        "id": "replay-1",
        "project": orig["project"],
        "loop": orig["loop"],
        "status": "pass",
        "spec_hash": orig.get("spec_hash", ""),
        "replay_of": orig["id"],
        "steps": [dict(s) for s in orig.get("steps", [])],
    }


# ------------------------------------------------------------------ _pinned_vars
def test_pinned_vars_filters_derived_and_env():
    record = {
        "vars": {
            "issue_number": 42,
            "issue_title": "t",
            "workdir": "/old/workdir",
            "run_id": "orig-1",
            "branch": "main",
            "head": "sha",
            "project": "p",
            "root": "/root",
            "loop": "l",
            "trigger": "on_issue:#42",
            "note": "n",
            "fabrik_home": "/home",
            "spec_path": "/x.fabrik.yaml",
            "env_key": "from-env",
            "emitted": "value",
        }
    }
    spec = {"env": {"env_key": "from-env"}}
    pinned = replay._pinned_vars(record, spec)
    assert pinned == {"issue_number": 42, "issue_title": "t", "emitted": "value"}


def test_pinned_vars_empty_when_only_derived():
    record = {"vars": {"workdir": "/x", "run_id": "r", "project": "p"}}
    assert replay._pinned_vars(record, {}) == {}


# ------------------------------------------------------------------ replay_run
def test_replay_run_pins_inputs(projects_dir, runs_dir, monkeypatch):
    _write_spec(projects_dir, "p", loops={"l": {"steps": [{"type": "shell", "cmd": "true"}]}})
    orig = _write_run(
        runs_dir,
        "p",
        "orig-1",
        status="pass",
        loop="l",
        vars={"issue_number": 7, "workdir": "/old", "run_id": "orig-1"},
        steps=[{"name": "s", "type": "shell", "status": "pass", "summary": "ok"}],
    )
    calls = []

    def _fake_run_loop(*args, **kwargs):
        calls.append((args, kwargs))
        return _fake_replay_record(orig)

    monkeypatch.setattr(replay, "run_loop", _fake_run_loop)
    o, r = replay.replay_run("orig-1", project="p", actor="tester", held_scope="p")
    assert o["id"] == "orig-1" and r["id"] == "replay-1"
    args, kwargs = calls[0]
    assert args[:2] == ("p", "l")
    assert kwargs["trigger"] == "replay"
    assert kwargs["note"] == "replay of orig-1"
    assert kwargs["replay_of"] == "orig-1"
    assert kwargs["actor"] == "tester"
    assert kwargs["held_scope"] == "p"
    # Derived keys (workdir, run_id) are filtered out; trigger var is pinned.
    assert kwargs["initial_vars"] == {"issue_number": 7}


def test_replay_run_unknown_run(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    with pytest.raises(replay.ReplayError, match="not found"):
        replay.replay_run("nope", project="p")


@pytest.mark.parametrize("status", ["queued", "running", "needs_approval"])
def test_replay_run_non_terminal_refused(projects_dir, runs_dir, status):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status=status)
    with pytest.raises(replay.ReplayError, match="not terminal"):
        replay.replay_run("r1", project="p")


def test_replay_run_project_gone(projects_dir, runs_dir):
    # Run exists but its project spec is not loadable.
    _write_run(runs_dir, "ghost", "r1", status="pass")
    with pytest.raises(replay.ReplayError, match="not loadable"):
        replay.replay_run("r1", project="ghost")


def test_replay_run_loop_gone(projects_dir, runs_dir):
    _write_spec(projects_dir, "p", loops={"other": {"steps": [{"type": "shell", "cmd": "true"}]}})
    _write_run(runs_dir, "p", "r1", status="pass", loop="missing")
    with pytest.raises(replay.ReplayError, match="no longer exists"):
        replay.replay_run("r1", project="p")


# ------------------------------------------------------------------ diff_runs
def _orig_replay_pair(orig_overrides=None, replay_overrides=None):
    orig = {
        "id": "o",
        "status": "pass",
        "spec_hash": "h1",
        "steps": [{"name": "a", "type": "shell", "status": "pass", "summary": "x", "verdict": ""}],
    }
    rep = {
        "id": "r",
        "status": "pass",
        "spec_hash": "h1",
        "steps": [{"name": "a", "type": "shell", "status": "pass", "summary": "x", "verdict": ""}],
    }
    orig.update(orig_overrides or {})
    rep.update(replay_overrides or {})
    return orig, rep


def test_diff_runs_identical():
    o, r = _orig_replay_pair()
    out = replay.diff_runs(o, r)
    assert "spec_hash: h1 == h1  (match)" in out
    assert "status: pass == pass" in out
    assert "step 0 a [shell]: pass == pass" in out
    assert "!=" not in out


def test_diff_runs_status_differs():
    o, r = _orig_replay_pair(replay_overrides={"status": "fail", "failure_kind": "transient"})
    out = replay.diff_runs(o, r)
    assert "status: pass != fail" in out
    assert "failure_kind (replay): transient" in out


def test_diff_runs_spec_hash_mismatch():
    o, r = _orig_replay_pair(replay_overrides={"spec_hash": "h2"})
    out = replay.diff_runs(o, r)
    assert "MISMATCH" in out


def test_diff_runs_step_summary_differs():
    o, r = _orig_replay_pair(
        replay_overrides={"steps": [{"name": "a", "type": "shell", "status": "pass", "summary": "y", "verdict": ""}]}
    )
    out = replay.diff_runs(o, r)
    assert "step 0 a [shell]: pass != pass" in out
    assert "summary: 'x' != 'y'" in out


def test_diff_runs_step_count_differs():
    o, r = _orig_replay_pair()
    r["steps"].append({"name": "b", "type": "shell", "status": "pass", "summary": "", "verdict": ""})
    out = replay.diff_runs(o, r)
    assert "<missing in original>" in out


# ------------------------------------------------------------------ CLI
def test_cmd_replay_pass(projects_dir, runs_dir, monkeypatch, capsys):
    _write_spec(projects_dir, "p", loops={"l": {"steps": [{"type": "shell", "cmd": "true"}]}})
    orig = _write_run(runs_dir, "p", "orig-1", status="pass", loop="l")
    monkeypatch.setattr(
        cli,
        "replay_run",
        lambda run_id, project=None, actor=None, held_scope=None: (orig, _fake_replay_record(orig)),
    )
    monkeypatch.setattr(cli, "claim_run", lambda *a, **k: type("C", (), {"scope": "p", "release": lambda self: None})())
    with pytest.raises(SystemExit) as e:
        cli.cmd_replay(type("A", (), {"run_id": "orig-1", "project": None})())
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "[replay of orig-1]" in out
    assert "replay diff:" in out


def test_cmd_replay_not_found(projects_dir, runs_dir, capsys):
    with pytest.raises(SystemExit) as e:
        cli.cmd_replay(type("A", (), {"run_id": "nope", "project": None})())
    assert e.value.code == 1
    assert "nicht gefunden" in capsys.readouterr().out


def test_cmd_replay_replay_error(projects_dir, runs_dir, monkeypatch, capsys):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="running")
    with pytest.raises(SystemExit) as e:
        cli.cmd_replay(type("A", (), {"run_id": "r1", "project": None})())
    assert e.value.code == 1
    assert "not terminal" in capsys.readouterr().out
