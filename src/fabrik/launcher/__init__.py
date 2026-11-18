"""Run-Launcher (Ports & Adapters): Substrat, das Run-Work ausführt.

Der Port (:class:`RunLauncher`) trennt die Scheduling-Policy des Core
(``engine.py``) vom Ausführungs-Substrat. Heute: :class:`InProcessLauncher`
(Worker-Pool + Daemon-Threads). Später: k8s-Jobs, ECS-Tasks — registriert in
:mod:`fabrik.launcher.registry`, ohne den Core zu ändern.
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
