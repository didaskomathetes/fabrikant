"""Adapter-Registry: Backend-Name → Adapter-Klasse + Factory.

Lässt `steps/agent.py` port-only bleiben: der Dispatcher importiert nur
`create_backend` (und den Port `AgentBackend`), nie konkrete Adapter. Neue
Adapter registrieren sich hier — der Dispatcher ändert sich nicht.

Jeder Adapter implementiert `create(step, endpoint, model, stale_after)`
(see `port.AgentBackend`), der seine spezifischen Optionen (z. B. codex
`sandbox`, copilot `tools`) selbst aus dem Step/Env extrahiert.
"""

from __future__ import annotations

from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend
from .copilot import CopilotBackend
from .opencode import OpenCodeBackend
from .port import AgentBackend

# name → Adapter-Klasse. Reihenfolge = Reihenfolge in Fehlermeldungen.
ADAPTERS: dict[str, type[AgentBackend]] = {
    "opencode": OpenCodeBackend,
    "claude_code": ClaudeCodeBackend,
    "codex": CodexBackend,
    "copilot": CopilotBackend,
}


def known_backends() -> list[str]:
    return sorted(ADAPTERS)


def create_backend(backend_name: str, step: dict, endpoint: str, model: str, stale_after: int) -> AgentBackend:
    """Instanziiert den Adapter für `backend_name` (port-typisiert).

    Wirft `ValueError` für unbekannte Namen — der Dispatcher zeigt die
    verfügbaren Backends an.
    """
    cls = ADAPTERS.get(backend_name)
    if cls is None:
        raise ValueError(f"agent-step: unbekanntes Backend '{backend_name}' (verfügbar: {', '.join(known_backends())})")
    return cls.create(step, endpoint, model, stale_after)
