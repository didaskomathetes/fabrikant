"""ADR 0020: Loops mit `worktree: false` — kein Slot, Scratch-Workdir."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from fabrikant import config, engine, runs
from fabrikant.specs import _validate_spec


@pytest.fixture
def scratch_dir(tmp_path, monkeypatch):
    d = tmp_path / "scratch"
    monkeypatch.setattr(config, "SCRATCH_DIR", d)
    return d


def _write_spec(projects_dir, tmp_path, name, loops_yaml, repo=True):
    root = tmp_path / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    head = [f"name: {name}", f"root: {root}"] + (["repo:", "  branch: main"] if repo else [])
    body = "\n".join("  " + ln for ln in loops_yaml.strip().splitlines())
    (projects_dir / f"{name}.fabrikant.yaml").write_text("\n".join([*head, "loops:", body]) + "\n")


_LOOPS = """
build:
  steps:
    - {type: shell, cmd: "true"}
triage:
  worktree: false
  steps:
    - {type: shell, cmd: pwd}
"""


# ---------------------------------------------------------------- uses_worktree / Scope
@pytest.mark.parametrize(("loop", "expected"), [({}, True), ({"worktree": True}, True), ({"worktree": False}, False)])
def test_uses_worktree(loop, expected):
    assert runs.uses_worktree(loop) is expected


def test_claim_worktree_loop_uses_project_slot(projects_dir, tmp_path):
    _write_spec(projects_dir, tmp_path, "lf_shared", _LOOPS)
    claim = runs.claim_run("lf_shared", "build")
    assert claim is not None and claim.scope == "lf_shared" and claim.uses_slot
    claim.release()


def test_claim_lock_free_loop_has_own_scope_and_no_slot(projects_dir, tmp_path, monkeypatch):
    _write_spec(projects_dir, tmp_path, "lf_own", _LOOPS)
    monkeypatch.setattr(runs, "max_parallel_for", lambda _p: 1)
    build = runs.claim_run("lf_own", "build")  # belegt den einzigen Slot
    assert build is not None
    triage = runs.claim_run("lf_own", "triage")
    assert triage is not None and triage.scope == "lf_own::triage" and not triage.uses_slot
    assert runs.claim_run("lf_own", "triage") is None  # nie doppelt: Dedup pro Loop
    triage.release()
    build.release()


@pytest.mark.parametrize(("project", "loop"), [("lf_unknown", "x"), ("lf_known", "nope")])
def test_lock_key_unknown_falls_back_to_project_scope(projects_dir, tmp_path, project, loop):
    _write_spec(projects_dir, tmp_path, "lf_known", _LOOPS)
    assert runs.lock_key(project, loop) == project


# ---------------------------------------------------------------- Spec-Validierung
def _spec(tmp_path, loop: dict) -> dict:
    return {"name": "v", "root": str(tmp_path), "loops": {"l": loop}}


def test_validate_worktree_false_with_shell_ok(tmp_path):
    _validate_spec(_spec(tmp_path, {"worktree": False, "steps": [{"type": "shell", "cmd": "true"}]}))


@pytest.mark.parametrize("step_type", config.WORKTREE_STEP_TYPES)
def test_validate_worktree_false_rejects_worktree_steps(tmp_path, step_type):
    loop = {"worktree": False, "steps": [{"type": "shell", "cmd": "true"}, {"type": step_type}]}
    with pytest.raises(ValueError, match=f"need the worktree \\({step_type}\\)"):
        _validate_spec(_spec(tmp_path, loop))


@pytest.mark.parametrize("step_type", config.WORKTREE_STEP_TYPES)
def test_validate_worktree_false_rejects_nested_verify_checks(tmp_path, step_type):
    """verify dispatcht seine Checks selbst — auch verschachtelt (verify in verify) geprüft."""
    inner = {"type": "verify", "checks": [{"type": step_type}]}
    loop = {"worktree": False, "steps": [{"type": "verify", "checks": [{"type": "shell", "cmd": "true"}, inner]}]}
    with pytest.raises(ValueError, match=f"need the worktree \\({step_type}\\)"):
        _validate_spec(_spec(tmp_path, loop))


def test_validate_worktree_false_allows_verify_with_shell_checks(tmp_path):
    loop = {"worktree": False, "steps": [{"type": "verify", "checks": [{"type": "shell", "cmd": "true"}]}]}
    _validate_spec(_spec(tmp_path, loop))


def test_validate_worktree_must_be_bool(tmp_path):
    with pytest.raises(ValueError, match="worktree muss true/false"):
        _validate_spec(_spec(tmp_path, {"worktree": "no", "steps": [{"type": "shell", "cmd": "true"}]}))


# ---------------------------------------------------------------- Engine
def test_lock_free_run_uses_scratch_dir(runs_dir, projects_dir, scratch_dir, tmp_path, monkeypatch):
    def _no_worktree(*a, **k):
        raise AssertionError("worktree: false darf den Projekt-Worktree nicht anfassen")

    monkeypatch.setattr(engine, "ensure_worktree", _no_worktree)
    monkeypatch.setattr(engine, "_worktree_sync_check", _no_worktree)
    _write_spec(projects_dir, tmp_path, "lf_run", _LOOPS)
    rec = engine.run_loop("lf_run", "triage")
    assert rec["status"] == "pass"
    log = (runs_dir / "lf_run" / f"{rec['id']}.log").read_text()
    assert f"workdir: {scratch_dir / rec['id']}" in log
    assert f"(cwd={scratch_dir / rec['id']}," in log  # Shell-Step lief in der Scratch-Workdir
    assert not (scratch_dir / rec["id"]).exists()  # am Run-Ende aufgeräumt


def test_lock_free_run_starts_with_empty_scratch_dir(runs_dir, projects_dir, scratch_dir, tmp_path):
    _write_spec(projects_dir, tmp_path, "lf_empty", _LOOPS)
    run_id = "stale-run"
    stale = scratch_dir / run_id
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("old")

    rec = engine.run_loop("lf_empty", "triage", run_id=run_id)

    assert rec["status"] == "pass"
    assert not stale.exists()


def test_safe_run_lock_free_loop_runs_while_project_busy(projects_dir, tmp_path, monkeypatch):
    """Kern von ADR 0020: belegte Slots blockieren worktree-freie Loops nicht."""
    _write_spec(projects_dir, tmp_path, "lf_busy", _LOOPS)
    monkeypatch.setattr(runs, "max_parallel_for", lambda _p: 1)
    ran: list[str] = []
    done = threading.Event()

    def fake_run_loop(project, loop, **k):
        ran.append(loop)
        done.set()

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    busy = runs.claim_run("lf_busy", "build")  # simuliert einen laufenden develop-Run
    assert busy is not None
    try:
        engine._safe_run("lf_busy", "triage", "cron")
        assert done.wait(5), "worktree-freier Loop lief nicht trotz belegtem Slot"
        done.clear()
        engine._safe_run("lf_busy", "build", "cron")
        assert not done.wait(0.3), "Worktree-Loop darf bei belegtem Slot nicht laufen"
    finally:
        busy.release()
    assert ran == ["triage"]


def test_scratch_dir_is_under_state():
    assert Path(config.SCRATCH_DIR).parent == config.STATE_DIR


# ---------------------------------------------------------------- Config-TOCTOU (Claim-Scope vs. Run-Spec)
def test_lock_key_matches_loop_definition():
    assert runs.loop_lock_key("p", "l", {"worktree": False}) == "p::l"
    assert runs.loop_lock_key("p", "l", {"steps": []}) == "p"
    assert runs.loop_lock_key("p", "l", None) == "p"


def test_run_loop_matching_scope_keeps_mode(runs_dir, projects_dir, scratch_dir, tmp_path, monkeypatch):
    _write_spec(projects_dir, tmp_path, "lf_match", _LOOPS)
    monkeypatch.setattr(engine, "ensure_worktree", lambda *a, **k: pytest.fail("kein Worktree erwartet"))
    rec = engine.run_loop("lf_match", "triage", held_scope="lf_match::triage")
    assert rec["status"] == "pass"
    log = (runs_dir / "lf_match" / f"{rec['id']}.log").read_text()
    assert "Config geändert" not in log


def test_run_loop_scope_mismatch_follows_current_spec(runs_dir, projects_dir, scratch_dir, tmp_path, monkeypatch):
    """Claim hielt den Projekt-Scope, der Loop ist inzwischen worktree-frei → Scratch, Notiz im Log."""
    _write_spec(projects_dir, tmp_path, "lf_toctou", _LOOPS)
    monkeypatch.setattr(engine, "ensure_worktree", lambda *a, **k: pytest.fail("kein Worktree erwartet"))
    rec = engine.run_loop("lf_toctou", "triage", held_scope="lf_toctou")
    assert rec["status"] == "pass"
    log = (runs_dir / "lf_toctou" / f"{rec['id']}.log").read_text()
    assert "Config geändert" in log
    assert f"workdir: {scratch_dir / rec['id']}" in log


# ---------------------------------------------------------------- Scratch-Cleanup
def test_cleanup_scratch_removes_dir(scratch_dir):
    (scratch_dir / "r1").mkdir(parents=True)
    msgs: list[str] = []
    engine._cleanup_scratch("r1", msgs.append)
    assert not (scratch_dir / "r1").exists() and msgs == []


def test_cleanup_scratch_missing_dir_is_silent(scratch_dir):
    msgs: list[str] = []
    engine._cleanup_scratch("nope", msgs.append)
    assert msgs == []


def test_cleanup_scratch_failure_is_logged_not_raised(scratch_dir, monkeypatch):
    msgs: list[str] = []

    def boom(path):
        raise PermissionError("denied")

    monkeypatch.setattr(engine.shutil, "rmtree", boom)
    engine._cleanup_scratch("r1", msgs.append)
    assert len(msgs) == 1 and "Scratch-Cleanup fehlgeschlagen" in msgs[0] and "denied" in msgs[0]


def test_run_survives_failing_scratch_cleanup(runs_dir, projects_dir, scratch_dir, tmp_path, monkeypatch):
    _write_spec(projects_dir, tmp_path, "lf_clean", _LOOPS)

    def boom(path):
        raise OSError("busy")

    monkeypatch.setattr(engine.shutil, "rmtree", boom)
    rec = engine.run_loop("lf_clean", "triage")
    assert rec["status"] == "pass"
    stored = (runs_dir / "lf_clean" / f"{rec['id']}.json").read_text()
    assert "Scratch-Cleanup fehlgeschlagen" in stored and "busy" in stored
    log = (runs_dir / "lf_clean" / f"{rec['id']}.log").read_text()
    assert "Scratch-Cleanup fehlgeschlagen" in log and "busy" in log


def test_startup_cleanup_failure_is_nonfatal(runs_dir, projects_dir, scratch_dir, tmp_path, monkeypatch):
    _write_spec(projects_dir, tmp_path, "lf_startup_clean", _LOOPS)
    run_id = "stale-run"
    stale = scratch_dir / run_id
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("old")

    def boom(path):
        raise OSError("busy")

    monkeypatch.setattr(engine.shutil, "rmtree", boom)
    rec = engine.run_loop("lf_startup_clean", "triage", run_id=run_id)

    assert rec["status"] == "pass"
    assert "Scratch-Cleanup fehlgeschlagen" in rec["scratch_cleanup_error"]
    stored = (runs_dir / "lf_startup_clean" / f"{run_id}.json").read_text()
    assert "Scratch-Cleanup fehlgeschlagen" in stored and "busy" in stored
    log = (runs_dir / "lf_startup_clean" / f"{run_id}.log").read_text()
    assert "Scratch-Cleanup fehlgeschlagen" in log and "busy" in log


def test_paused_run_cleans_scratch_dir(runs_dir, projects_dir, scratch_dir, tmp_path, monkeypatch):
    _write_spec(projects_dir, tmp_path, "lf_paused", _LOOPS)

    def pause(rn, *args):
        rn.record["status"] = "needs_approval"
        return rn.record

    monkeypatch.setattr(engine, "_run_steps", pause)
    rec = engine.run_loop("lf_paused", "triage")

    assert rec["status"] == "needs_approval"
    assert not (scratch_dir / rec["id"]).exists()


def test_paused_run_records_scratch_cleanup_failure(runs_dir, projects_dir, scratch_dir, tmp_path, monkeypatch):
    _write_spec(projects_dir, tmp_path, "lf_paused_error", _LOOPS)

    def pause(rn, *args):
        rn.record["status"] = "needs_approval"
        return rn.record

    def boom(path):
        raise OSError("busy")

    monkeypatch.setattr(engine, "_run_steps", pause)
    monkeypatch.setattr(engine.shutil, "rmtree", boom)
    rec = engine.run_loop("lf_paused_error", "triage")

    assert rec["status"] == "needs_approval"
    assert "Scratch-Cleanup fehlgeschlagen" in rec["scratch_cleanup_error"]
    stored = (runs_dir / "lf_paused_error" / f"{rec['id']}.json").read_text()
    assert "Scratch-Cleanup fehlgeschlagen" in stored


# ---------------------------------------------------------------- Prioritäts-Queue pro Scope
def test_priority_waiter_is_per_scope(projects_dir, tmp_path):
    _write_spec(projects_dir, tmp_path, "lf_prio", _LOOPS)
    runs.mark_priority_waiter("lf_prio::triage", +1)
    try:
        assert runs.claim_run("lf_prio", "triage") is None  # Vorrang-Warter auf dem Loop-Scope
        build = runs.claim_run("lf_prio", "build")  # Projekt-Scope unberührt
        assert build is not None
        build.release()
    finally:
        runs.mark_priority_waiter("lf_prio::triage", -1)


@pytest.mark.parametrize("name,loop", [("a::b", "c"), ("a", "b::c")])
def test_validate_rejects_lock_key_separator_in_names(tmp_path, name, loop):
    """Sonst könnten "a::b"+"c" und "a"+"b::c" denselben Lock-Schlüssel teilen."""
    spec = {"name": name, "root": str(tmp_path), "loops": {loop: {"steps": [{"type": "shell", "cmd": "true"}]}}}
    with pytest.raises(ValueError, match="Lock-Schlüssel"):
        _validate_spec(spec)
