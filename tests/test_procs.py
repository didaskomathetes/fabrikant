"""procs: Prozessgruppen — Timeout/Shutdown beenden den ganzen Baum (keine Waisen)."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from fabrikant import procs

# Grandchild im Hintergrund starten, PID ablegen, dann warten (hält die Gruppe am Leben).
SPAWN_GRANDCHILD = 'sleep 30 & echo $! > "$PIDFILE"; wait'
DEATH_WAIT_S = 3.0


def _wait_dead(pid: int) -> bool:
    deadline = time.time() + DEATH_WAIT_S
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _grandchild_pid(pidfile) -> int:
    deadline = time.time() + DEATH_WAIT_S
    while not (pidfile.is_file() and pidfile.read_text().strip()):
        assert time.time() < deadline, "Grandchild hat keine PID geschrieben"
        time.sleep(0.02)
    return int(pidfile.read_text())


def test_run_group_returns_output_and_code():
    p = procs.run_group(["sh", "-c", "echo out; echo err >&2; exit 3"], timeout=10)
    assert (p.returncode, p.stdout, p.stderr) == (3, "out\n", "err\n")
    assert not procs._live  # nach Ende nicht mehr vorgemerkt


def test_run_group_timeout_kills_grandchildren(tmp_path):
    """Regression: subprocess.run tötete nur `sh`; der eigentliche Befehl lief weiter."""
    pidfile = tmp_path / "pid"
    env = {**os.environ, "PIDFILE": str(pidfile)}
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        procs.run_group(["sh", "-c", SPAWN_GRANDCHILD], timeout=1, env=env)
    assert time.time() - t0 < DEATH_WAIT_S
    assert _wait_dead(_grandchild_pid(pidfile))
    assert not procs._live


def test_kill_all_kills_tracked_groups():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    procs.track(proc)
    try:
        assert procs.kill_all() == 1
        assert proc.wait(timeout=DEATH_WAIT_S) != 0
        assert procs.kill_all() == 0  # Registry ist danach leer
    finally:
        procs.untrack(proc)
        if proc.poll() is None:
            proc.kill()


def test_kill_all_skips_finished():
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    procs.track(proc)
    assert procs.kill_all() == 0


def test_kill_group_tolerates_gone_process():
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    procs.kill_group(proc)  # kein Fehler, obwohl die Gruppe weg ist
