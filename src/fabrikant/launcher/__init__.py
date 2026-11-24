"""Run launcher (ports & adapters): substrate that executes run work.

The port (:class:`RunLauncher`) separates the core's scheduling policy
(``engine.py``) from the execution substrate. Today: :class:`InProcessLauncher`
(worker pool + daemon threads). Later: k8s jobs, ECS tasks — registered in
:mod:`fabrikant.launcher.registry` without changing the core.
"""

from __future__ import annotations

from .inprocess import InProcessLauncher
from .port import LauncherStatus, RunLauncher
from .registry import ADAPTERS, create_launcher, known_launchers

__all__ = [
    "ADAPTERS",
    "InProcessLauncher",
    "LauncherStatus",
    "RunLauncher",
    "create_launcher",
    "known_launchers",
]
