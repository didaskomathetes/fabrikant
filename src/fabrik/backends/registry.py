"""Adapter-Registry: Backend-Name → Adapter-Klasse + Factory.

Lässt `steps/agent.py` port-only bleiben: der Dispatcher importiert nur
`create_backend` (und den Port `AgentBackend`), nie konkrete Adapter. Neue
Adapter registrieren sich hier — der Dispatcher ändert sich nicht.

Jeder Adapter implementiert `create(step, endpoint, model, stale_after)`
(see `port.AgentBackend`), der seine spezifischen Optionen (z. B. codex
`sandbox`, copilot `tools`) selbst aus dem Step/Env extrahiert.
"""

from __future__ import annotations

from .. import sandbox
from ..i18n import t
from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend
from .copilot import CopilotBackend
from .opencode import OpenCodeBackend
from .opencode_cli import OpenCodeCliBackend
from .port import AgentBackend

# name → Adapter-Klasse. Reihenfolge = Reihenfolge in Fehlermeldungen.
ADAPTERS: dict[str, type[AgentBackend]] = {
    "opencode": OpenCodeBackend,
    "claude_code": ClaudeCodeBackend,
    "codex": CodexBackend,
    "copilot": CopilotBackend,
}


# name → Variante für `FABRIK_SANDBOX=1` (ADR 0054): Backends, deren Host-Adapter einen fremden
# Prozess anspricht, laufen in der Sandbox stattdessen als One-Shot im Container.
SANDBOX_ADAPTERS: dict[str, type[AgentBackend]] = {
    "opencode": OpenCodeCliBackend,
}


def known_backends() -> list[str]:
    return sorted(ADAPTERS)


def create_backend(backend_name: str, step: dict, endpoint: str, model: str, stale_after: int) -> AgentBackend:
    """Instanziiert den Adapter für `backend_name` (port-typisiert).

    Wirft `ValueError` für unbekannte Namen — der Dispatcher zeigt die
    verfügbaren Backends an.
    """
    cls = ADAPTERS.get(backend_name)
    if cls is not None and sandbox.sandbox_enabled():
        cls = SANDBOX_ADAPTERS.get(backend_name, cls)
    if cls is None:
        raise ValueError(
            t(
                "backends.registry.agent_step_unknown_backend_available",
                backend_name=backend_name,
                v2=", ".join(known_backends()),
            )
        )
    return cls.create(step, endpoint, model, stale_after)
