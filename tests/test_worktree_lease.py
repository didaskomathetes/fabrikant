"""Worktree-Lease (ADR 0028): Ownership-Prüfung im Orphan-Cleanup."""

from __future__ import annotations

import json
import os
import threading
import time
import types

import pytest

from fabrik import config, engine, filelock, gitops

OTHER_PID = os.getpid() + 1


def _wt(tmp_path):
    wt = tmp_path / "proj" / "run1"
    wt.mkdir(parents=True)
    return wt


def _lease(wt, pid, heartbeat):
    gitops.lease_path(wt).write_text(json.dumps({"pid": pid, "heartbeat": heartbeat}))


@pytest.fixture
def alive(monkeypatch):
    monkeypatch.setattr(gitops, "_pid_alive", lambda pid: True)


def test_lease_timeout_exceeds_heartbeat_interval():
    """Invariante (ADR 0028): Timeout > Heartbeat-Intervall + Puffer.

    `lease_lock` wird nur für die kurze Lease-Prüfung/den Claim gehalten, nie über
    die `repo_lock`-Erwerbung in `remove_worktree`; Heartbeats lebender Runs können
    daher nie so lange blockiert werden, dass eine lebende Lease abläuft. Das Timeout
    muss nur das Heartbeat-Intervall plus einen Puffer für ein blockiertes Schreiben
    übersteigen — nicht den `repo_lock`-Halt."""
    assert config.WORKTREE_LEASE_HEARTBEAT_S * 2 < config.WORKTREE_LEASE_TIMEOUT_S


def test_active_lease_of_other_process(tmp_path, alive):
    wt = _wt(tmp_path)
    _lease(wt, OTHER_PID, time.time())
    assert gitops.lease_held_by_other(wt)


def test_expired_lease(tmp_path, alive):
    wt = _wt(tmp_path)
    _lease(wt, OTHER_PID, time.time() - config.WORKTREE_LEASE_TIMEOUT_S - 1)
    assert not gitops.lease_held_by_other(wt)


def test_dead_pid(tmp_path, monkeypatch):
    wt = _wt(tmp_path)
    monkeypatch.setattr(gitops, "_pid_alive", lambda pid: False)
    _lease(wt, OTHER_PID, time.time())
    assert not gitops.lease_held_by_other(wt)


def test_own_pid_is_not_protected(tmp_path, alive):
    wt = _wt(tmp_path)
    _lease(wt, os.getpid(), time.time())
    assert not gitops.lease_held_by_other(wt)


def test_missing_lease(tmp_path):
    assert not gitops.lease_held_by_other(_wt(tmp_path))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "{}",
        '{"pid": "x", "heartbeat": 1}',
        "[]",
        '{"pid": 0, "heartbeat": 1}',
        '{"pid": 1, "heartbeat": NaN}',
        '{"pid": 1, "heartbeat": Infinity}',
        '{"pid": 1, "heartbeat": -1}',
    ],
)
def test_corrupt_lease(tmp_path, alive, content):
    wt = _wt(tmp_path)
    gitops.lease_path(wt).write_text(content)
    assert not gitops.lease_held_by_other(wt)


def test_pid_alive_real_processes():
    assert gitops._pid_alive(os.getpid())
    assert not gitops._pid_alive(2**22 + 12345)


def test_pid_alive_permission_error(monkeypatch):
    def deny(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", deny)
    assert gitops._pid_alive(1)


def test_write_and_release_lease(tmp_path):
    wt = _wt(tmp_path)
    gitops.write_lease(wt)
    data = json.loads(gitops.lease_path(wt).read_text())
    assert data["pid"] == os.getpid()
    gitops.release_lease(wt)
    gitops.release_lease(wt)  # idempotent
    assert not gitops.lease_path(wt).exists()


def test_release_does_not_remove_replacement_lease(tmp_path):
    wt = _wt(tmp_path)
    old_token = gitops.write_lease(wt)
    new_token = gitops.write_lease(wt)
    gitops.release_lease(wt, old_token)
    assert json.loads(gitops.lease_path(wt).read_text())["token"] == new_token


def test_worktree_lease_heartbeat_and_release(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREE_LEASE_HEARTBEAT_S", 0.01)
    wt = _wt(tmp_path)
    lease = gitops.WorktreeLease(wt)
    lease.start()
    first = json.loads(gitops.lease_path(wt).read_text())["heartbeat"]
    time.sleep(0.1)
    assert json.loads(gitops.lease_path(wt).read_text())["heartbeat"] > first
    lease.release()
    lease.release()
    assert not gitops.lease_path(wt).exists()


def test_release_waits_for_inflight_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREE_LEASE_HEARTBEAT_S", 0.01)
    wt = _wt(tmp_path)
    lease = gitops.WorktreeLease(wt)
    lease.start()
    entered = threading.Event()
    unblock = threading.Event()
    original_write = gitops.write_lease

    def blocked_write(wt, token):
        entered.set()
        unblock.wait()
        return original_write(wt, token)

    monkeypatch.setattr(gitops, "write_lease", blocked_write)
    assert entered.wait(1)
    release_done = threading.Event()
    threading.Thread(target=lambda: (lease.release(), release_done.set())).start()
    assert not release_done.wait(0.05)
    unblock.set()
    assert release_done.wait(1)
    assert not gitops.lease_path(wt).exists()


def test_heartbeat_survives_write_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREE_LEASE_HEARTBEAT_S", 0.01)
    lease = gitops.WorktreeLease(_wt(tmp_path))
    gitops.write_lease(lease._wt, lease._token)  # Heartbeat erneuert nur eine eigene Lease
    calls = []

    def boom(wt, token):
        calls.append(wt)
        raise OSError("disk")

    monkeypatch.setattr(gitops, "write_lease", boom)
    lease._thread.start()
    time.sleep(0.1)
    assert len(calls) > 1
    lease._stop.set()
    lease._thread.join()


@pytest.fixture
def orphan_env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    monkeypatch.setattr("fabrik.specs.load_projects", lambda: {"proj": {"root": str(root), "repo": {"branch": "main"}}})
    removed = []
    monkeypatch.setattr(gitops, "remove_worktree", lambda r, wt: removed.append(wt.name) or True)
    for name in ("live", "stale", "paused", "plain"):
        (tmp_path / "wt" / "proj" / name).mkdir(parents=True)
    return tmp_path / "wt" / "proj", removed


def test_cleanup_respects_leases_and_approval(orphan_env, monkeypatch, alive):
    proj, removed = orphan_env
    _lease(proj / "live", OTHER_PID, time.time())
    _lease(proj / "stale", OTHER_PID, time.time() - config.WORKTREE_LEASE_TIMEOUT_S - 1)
    _lease(proj / "paused", OTHER_PID, 0)
    monkeypatch.setattr("fabrik.runs.get_run", lambda rid, p: {"status": "needs_approval"} if rid == "paused" else None)
    gitops.cleanup_orphaned_worktrees()
    assert sorted(removed) == ["plain", "stale"]


def test_remove_worktree_releases_lease(tmp_path):
    wt = _wt(tmp_path)
    gitops.write_lease(wt)
    wt.rmdir()
    assert gitops.remove_worktree(tmp_path, wt)
    assert not gitops.lease_path(wt).exists()


def _own_lease(tmp_path):
    wt = _wt(tmp_path)
    lease = gitops.WorktreeLease(wt)
    gitops.write_lease(wt, lease._token)
    return wt, lease


def test_renew_stops_when_lease_removed(tmp_path):
    wt, lease = _own_lease(tmp_path)
    gitops.lease_path(wt).unlink()  # z. B. abgelaufen und von fremdem Cleanup weggeräumt
    assert not lease._renew()
    assert not gitops.lease_path(wt).exists()


def test_renew_stops_when_lease_taken_over(tmp_path):
    wt, lease = _own_lease(tmp_path)
    other = gitops.write_lease(wt)
    assert not lease._renew()
    assert json.loads(gitops.lease_path(wt).read_text())["token"] == other


def test_renew_reclaims_corrupt_lease(tmp_path):
    wt, lease = _own_lease(tmp_path)
    gitops.lease_path(wt).write_text("not json")
    assert lease._renew()
    assert json.loads(gitops.lease_path(wt).read_text())["token"] == lease._token


def test_heartbeat_exits_after_lease_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREE_LEASE_HEARTBEAT_S", 0.01)
    wt, lease = _own_lease(tmp_path)
    gitops.lease_path(wt).unlink()
    lease._thread.start()
    lease._thread.join(1)
    assert not lease._thread.is_alive()
    assert not gitops.lease_path(wt).exists()


def test_renew_waits_while_cleanup_holds_lock(tmp_path):
    wt, lease = _own_lease(tmp_path)
    done = threading.Event()
    with gitops.lease_lock(wt):
        threading.Thread(target=lambda: (lease._renew(), done.set())).start()
        assert not done.wait(0.05)
    assert done.wait(1)


def test_cleanup_checks_under_lock_and_removes_outside(orphan_env, monkeypatch):
    """ADR 0028: Lease-Prüfung + Claim unter `lease_lock`, `remove_worktree` außerhalb."""
    proj, _ = orphan_env
    monkeypatch.setattr("fabrik.runs.get_run", lambda rid, p: None)
    lock_key = str(proj.with_name(proj.name + ".lock"))
    held: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        gitops, "lease_held_by_other", lambda wt: held.append(("check", filelock._thread_lock(lock_key).locked()))
    )
    monkeypatch.setattr(
        gitops, "remove_worktree", lambda r, wt: held.append(("remove", filelock._thread_lock(lock_key).locked()))
    )
    gitops.cleanup_orphaned_worktrees()
    checks = [locked for op, locked in held if op == "check"]
    removes = [locked for op, locked in held if op == "remove"]
    assert checks and all(checks)
    assert removes and not any(removes)


def test_cleanup_claims_lease_before_removal(orphan_env, monkeypatch, alive):
    """ADR 0028: Die Lease wird unter `lease_lock` entfernt (Claim), bevor `remove_worktree` läuft;
    eine frische Lease eines lebenden Prozesses bleibt unangetastet."""
    proj, _ = orphan_env
    monkeypatch.setattr("fabrik.runs.get_run", lambda rid, p: None)
    _lease(proj / "stale", OTHER_PID, time.time() - config.WORKTREE_LEASE_TIMEOUT_S - 1)
    _lease(proj / "live", OTHER_PID, time.time())
    seen: list[bool] = []

    def fake_remove(r, wt):
        seen.append(gitops.lease_path(wt).exists())
        return True

    monkeypatch.setattr(gitops, "remove_worktree", fake_remove)
    gitops.cleanup_orphaned_worktrees()
    assert seen and not any(seen)  # Claim: Lease weg, bevor `remove_worktree` aufgerufen wird
    assert gitops.lease_path(proj / "live").exists()


def test_build_ctx_writes_lease_before_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    seen = []

    def fake_ensure(spec, run_id):
        seen.append(gitops.lease_path(gitops.run_worktree_path("proj", run_id)).exists())
        raise RuntimeError("stop")

    monkeypatch.setattr(engine, "ensure_worktree", fake_ensure)
    rn = types.SimpleNamespace(
        spec={"repo": {"branch": "main"}}, worktree=True, project="proj", run_id="r1", lease=None
    )
    with pytest.raises(RuntimeError, match="stop"):
        engine._build_ctx(rn, "develop", "", False, None)  # type: ignore[reportArgumentType]
    assert seen == [True]
    rn.lease.release()


def test_finish_workdir_releases_lease_after_merge_and_cleanup(monkeypatch):
    order = []
    monkeypatch.setattr(engine, "_merge_agent_state", lambda rn: order.append("merge"))
    monkeypatch.setattr(engine, "_cleanup_worktree", lambda p, r, e: order.append("cleanup"))
    lease = types.SimpleNamespace(release=lambda: order.append("release"))
    rn = types.SimpleNamespace(
        record={},
        worktree=True,
        spec={"repo": {"branch": "main"}},
        project="proj",
        run_id="r1",
        emit=print,
        lease=lease,
    )
    engine._finish_workdir(rn, paused=False)  # type: ignore[reportArgumentType]
    assert order == ["merge", "cleanup", "release"]
    assert rn.lease is None


@pytest.mark.parametrize(
    "content",
    [
        '{"pid": true, "heartbeat": 1}',
        '{"pid": 1.5, "heartbeat": 1}',
        '{"pid": 1, "heartbeat": false}',
        '{"pid": 1, "heartbeat": "1"}',
        '{"pid": 1, "heartbeat": 1, "token": 7}',
    ],
)
def test_lease_info_rejects_invalid_types(content):
    assert gitops.LeaseInfo.parse(content) is None


def test_lease_info_roundtrip():
    lease = gitops.LeaseInfo(42, 1.5, "tok")
    assert gitops.LeaseInfo.parse(lease.to_json()) == lease
    assert gitops.LeaseInfo.parse('{"pid": 42, "heartbeat": 2}') == gitops.LeaseInfo(42, 2.0, "")


def test_heartbeat_slightly_in_future_still_protects(tmp_path, alive):
    """Uhr zurückgestellt (NTP): eine lebende Lease darf nicht ungeschützt werden."""
    wt = _wt(tmp_path)
    now = time.time()
    _lease(wt, OTHER_PID, now + config.WORKTREE_LEASE_TIMEOUT_S / 2)
    assert gitops.lease_held_by_other(wt, now=now)


def test_heartbeat_far_in_future_is_corrupt(tmp_path, alive):
    wt = _wt(tmp_path)
    now = time.time()
    _lease(wt, OTHER_PID, now + config.WORKTREE_LEASE_TIMEOUT_S + 1)
    assert not gitops.lease_held_by_other(wt, now=now)


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps({"pid": OTHER_PID, "heartbeat": 1.0})],
    ids=["corrupt", "stale-without-token"],
)
def test_remove_worktree_removes_foreign_or_corrupt_lease(tmp_path, content):
    wt = _wt(tmp_path)
    gitops.lease_path(wt).write_text(content)
    wt.rmdir()
    assert gitops.remove_worktree(tmp_path, wt)
    assert not gitops.lease_path(wt).exists()


def test_remove_worktree_releases_lease_on_git_failure(tmp_path, monkeypatch):
    wt = _wt(tmp_path)
    gitops.write_lease(wt)
    failed = types.SimpleNamespace(returncode=1, args=["git"], stdout="", stderr="boom")
    monkeypatch.setattr(gitops.subprocess, "run", lambda *a, **k: failed)
    with pytest.raises(gitops.subprocess.CalledProcessError):
        gitops.remove_worktree(tmp_path, wt)
    assert not gitops.lease_path(wt).exists()


def test_remove_worktree_keeps_lease_renewed_meanwhile(tmp_path, monkeypatch):
    wt = _wt(tmp_path)
    gitops.write_lease(wt)

    def renew_then_remove(*args, **kwargs):
        argv = list(args[0]) if args else []
        if "rev-parse" in argv:  # repo_lock → _git_common_dir: kein Git-Repo → kein Lock
            return types.SimpleNamespace(returncode=1, args=argv, stdout="", stderr="")
        gitops.write_lease(wt)  # z. B. Heartbeat des Besitzers während des git-Aufrufs
        wt.rmdir()
        return types.SimpleNamespace(returncode=0, args=argv, stdout="", stderr="")

    monkeypatch.setattr(gitops.subprocess, "run", renew_then_remove)
    assert gitops.remove_worktree(tmp_path, wt)
    assert gitops.lease_path(wt).exists()


def test_cleanup_removes_orphaned_lease_files(orphan_env, monkeypatch, alive):
    proj, _ = orphan_env
    monkeypatch.setattr("fabrik.runs.get_run", lambda rid, p: None)
    _lease(proj / "gone-stale", OTHER_PID, time.time() - config.WORKTREE_LEASE_TIMEOUT_S - 1)
    _lease(proj / "starting", OTHER_PID, time.time())  # Run legt Worktree gerade an
    _lease(proj / "active", OTHER_PID, 1.0)
    gitops.cleanup_orphaned_worktrees(active_run_ids={"active"})
    assert not gitops.lease_path(proj / "gone-stale").exists()
    assert gitops.lease_path(proj / "starting").exists()
    assert gitops.lease_path(proj / "active").exists()


def test_finish_workdir_releases_lease_on_exception(monkeypatch):
    released = []

    def boom(rn):
        raise RuntimeError("merge kaputt")

    monkeypatch.setattr(engine, "_merge_agent_state", boom)
    rn = types.SimpleNamespace(
        record={},
        worktree=True,
        spec={"repo": {"branch": "main"}},
        project="proj",
        run_id="r1",
        emit=print,
        lease=types.SimpleNamespace(release=lambda: released.append(True)),
    )
    with pytest.raises(RuntimeError, match="merge kaputt"):
        engine._finish_workdir(rn, paused=False)  # type: ignore[reportArgumentType]
    assert released == [True]
    assert rn.lease is None
