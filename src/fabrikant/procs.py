"""Child processes in their own process group — timeout and server end terminate the whole tree.

`subprocess.run(..., timeout=…)` kills only the direct child (`sh`) on expiry; what the
command started (Python CLI, `git push`, test runs) keeps running orphaned. Here
every command starts with `start_new_session=True` and is terminated as a group via `killpg`.

Own session also means: a Ctrl+C in the terminal no longer reaches the group. Therefore
the module remembers running groups and `kill_all()` terminates them at process end
(`atexit`; `fabrikant serve` translates SIGTERM/SIGHUP into a regular end).

Side effect of the import: `atexit.register(kill_all)` runs on `import fabrikant.procs` —
every process (also CLI commands, tests) thereby gets the safety-net handler. It is
idempotent and a no-op as long as no group is registered; `fabrikant serve` calls `kill_all()`
additionally and explicitly in `finally`.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Sequence

_live: set[subprocess.Popen] = set()
_lock = threading.Lock()


def track(proc: subprocess.Popen) -> None:
    """Register the group of `proc` (started with `start_new_session=True`) for `kill_all`."""
    with _lock:
        _live.add(proc)


def untrack(proc: subprocess.Popen) -> None:
    with _lock:
        _live.discard(proc)


def kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL to the process group of `proc`; no-op if it is already gone.

    Deliberately SIGKILL (not SIGTERM): the groups are test runs, `git` and agent CLIs —
    an orderly shutdown gains nothing, and an ignoring child would otherwise block
    the end of the server/step."""
    with contextlib.suppress(OSError):  # ProcessLookupError: Gruppe schon beendet
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def kill_all() -> int:
    """Terminate all registered, still running groups. Returns the count."""
    with _lock:
        procs = [p for p in _live if p.poll() is None]
        _live.clear()
    for proc in procs:
        kill_group(proc)
    return len(procs)


def run_group(argv: Sequence[str], *, timeout: float, **kwargs) -> subprocess.CompletedProcess[str]:
    """Like `subprocess.run(argv, capture_output=True, text=True, timeout=timeout)`, but in
    its own process group: on timeout (or abort) the whole group dies, afterwards
    `subprocess.TimeoutExpired` (with the output read so far) is re-raised."""
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **kwargs,
    ) as proc:
        track(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:  # Timeout, KeyboardInterrupt, …: leave no orphans behind
            kill_group(proc)
            proc.wait()
            raise
        finally:
            untrack(proc)
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


atexit.register(kill_all)
