"""Launcher-Registry: Substrat-Name → Adapter-Factory.

Lässt ``engine.py`` port-only bleiben: der Core importiert nur
:func:`create_launcher` (und den Port :class:`RunLauncher`), nie konkrete
Adapter. Neue Substrate (``k8s``, ``ecs``) registrieren sich hier — der Core
ändert sich nicht (analog :mod:`fabrik.backends.registry`).

Die Factory erhält die vom Core injizierten Abhängigkeiten als Keyword-Argumente
(in-process: ``pool_factory``/``pool_or_none``); Substrate mit anderen
Anforderungen definieren ihre eigenen Parameter.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .inprocess import InProcessLauncher
from .port import RunLauncher

# name → Adapter-Factory. Reihenfolge = Reihenfolge in Fehlermeldungen.
ADAPTERS: dict[str, Callable[..., RunLauncher]] = {
    "inprocess": InProcessLauncher,
}


def known_launchers() -> list[str]:
    return sorted(ADAPTERS)


def create_launcher(name: str, **kwargs: Any) -> RunLauncher:
    """Instanziiert den Launcher für `name` (port-typisiert).

    Wirft `ValueError` für unbekannte Namen — der Core zeigt die verfügbaren
    Substrate an. `kwargs` werden an die Adapter-Factory weitergereicht."""
    factory = ADAPTERS.get(name)
    if factory is None:
        raise ValueError(f"run-launcher: unbekanntes Substrat '{name}' (verfügbar: {', '.join(known_launchers())})")
    return factory(**kwargs)
