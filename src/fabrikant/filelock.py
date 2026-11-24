"""Exclusive lock for files: thread lock (in-process) + flock (cross-process).

The server and `fabrikant approve` (CLI) run as separate processes and share
state files — a plain thread lock is not enough for that."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: thread lock only
    fcntl = None  # type: ignore[assignment]

_thread_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _guard:
        return _thread_locks.setdefault(key, threading.Lock())


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Holds an exclusive lock on `path` (lock file `<path>.lock`, created if needed)."""
    lock_file = path.with_name(path.name + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(str(lock_file)), open(lock_file, "a") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)
