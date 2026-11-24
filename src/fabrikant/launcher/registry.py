"""Launcher registry: substrate name → adapter factory.

Lets ``engine.py`` stay port-only: the core only imports
:func:`create_launcher` (and the port :class:`RunLauncher`), never concrete
adapters. New substrates (``k8s``, ``ecs``) register here — the core
does not change (analogous to :mod:`fabrikant.backends.registry`).

The factory receives the dependencies injected by the core as keyword arguments
(in-process: ``pool_factory``/``pool_or_none``); substrates with different
requirements define their own parameters.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..i18n import t
from .inprocess import InProcessLauncher
from .port import RunLauncher

# name → Adapter-Factory. Reihenfolge = Reihenfolge in Fehlermeldungen.
ADAPTERS: dict[str, Callable[..., RunLauncher]] = {
    "inprocess": InProcessLauncher,
}


def known_launchers() -> list[str]:
    return sorted(ADAPTERS)


def create_launcher(name: str, **kwargs: Any) -> RunLauncher:
    """Instantiates the launcher for `name` (port-typed).

    Raises `ValueError` for unknown names — the core shows the available
    substrates. `kwargs` are passed on to the adapter factory."""
    factory = ADAPTERS.get(name)
    if factory is None:
        raise ValueError(t("launcher.registry.unknown_substrate", name=name, available=", ".join(known_launchers())))
    return factory(**kwargs)
