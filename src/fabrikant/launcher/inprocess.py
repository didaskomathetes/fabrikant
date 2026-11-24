"""In-process run launcher: worker pool + daemon threads (today's behavior).

The substrate fabrikant uses today: trigger runs run in a bounded
worker pool (``FABRIKANT_WORKERS``), priority runs in their own daemon threads. The
launcher is a thin wrapper around the core's existing pool
(``engine._get_run_pool``) — it does not own the pool but delegates, so
the scheduling policy and the existing tests (``engine._run_pool``) stay
unchanged.

No import of ``engine`` (import cycle): the core injects the pool accessors
as callables when building the launcher (``engine._get_launcher``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .. import config
from .port import LauncherStatus


class InProcessLauncher:
    """Run launcher that executes work in this process (port: :class:`RunLauncher`)."""

    name = "inprocess"

    def __init__(self, pool_factory: Callable[[], Any], pool_or_none: Callable[[], Any | None]):
        # pool_factory: returns the (possibly newly created) pool — for submit.
        # pool_or_none: returns the current pool or None — for status/shutdown
        # (deliberately creates no pool if None).
        self._pool_factory = pool_factory
        self._pool_or_none = pool_or_none

    def submit(self, work: Callable[[], None]) -> None:
        self._pool_factory().submit(work)

    def submit_priority(self, work: Callable[[], None]) -> None:
        # RuntimeError (e.g. thread start fails) is passed on to the core,
        # which cleans up claim/wait counter there.
        threading.Thread(target=work, daemon=True).start()

    def status(self) -> LauncherStatus:
        pool = self._pool_or_none()
        if pool is None:
            # Pool not yet created: report the configured size (not 0).
            return {"workers": config.configured_workers(), "busy": 0, "queue": 0}
        return {"workers": pool.size, "busy": pool.busy, "queue": pool.waiting}

    def shutdown(self) -> None:
        pool = self._pool_or_none()
        if pool is not None:
            pool.shutdown()
