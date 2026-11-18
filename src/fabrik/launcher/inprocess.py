"""In-Prozess-Run-Launcher: Worker-Pool + Daemon-Threads (heutiges Verhalten).

Das Substrat, das fabrik heute nutzt: Trigger-Runs laufen in einem bounded
Worker-Pool (``FABRIK_WORKERS``), Vorrang-Runs in eigenen Daemon-Threads. Der
Launcher ist ein dünner Wrapper um den bestehenden Pool des Core
(``engine._get_run_pool``) — er besitzt den Pool nicht, sondern delegiert, damit
die Scheduling-Policy und die bestehenden Tests (``engine._run_pool``) unverändert
bleiben.

Kein Import von ``engine`` (Import-Zyklus): der Core injiziert die Pool-Zugriffe
als Callables beim Aufbau des Launchers (``engine._get_launcher``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .. import config
from .port import LauncherStatus


class InProcessLauncher:
    """Run-Launcher, der Work in diesem Prozess ausführt (Port: :class:`RunLauncher`)."""

    name = "inprocess"

    def __init__(self, pool_factory: Callable[[], Any], pool_or_none: Callable[[], Any | None]):
        # pool_factory: liefert den (ggf. neu angelegten) Pool — für submit.
        # pool_or_none: liefert den aktuellen Pool oder None — für status/shutdown
        #               (legt bei None bewusst keinen Pool an).
        self._pool_factory = pool_factory
        self._pool_or_none = pool_or_none

    def submit(self, work: Callable[[], None]) -> None:
        self._pool_factory().submit(work)

    def submit_priority(self, work: Callable[[], None]) -> None:
        # RuntimeError (z. B. Thread-Start scheitert) wird an den Core
        # weitergereicht, der dort Claim/Wartezähler aufräumt.
        threading.Thread(target=work, daemon=True).start()

    def status(self) -> LauncherStatus:
        pool = self._pool_or_none()
        if pool is None:
            # Pool noch nicht angelegt: konfigurierte Größe melden (nicht 0).
            return {"workers": config.configured_workers(), "busy": 0, "queue": 0}
        return {"workers": pool.size, "busy": pool.busy, "queue": pool.waiting}

    def shutdown(self) -> None:
        pool = self._pool_or_none()
        if pool is not None:
            pool.shutdown()
