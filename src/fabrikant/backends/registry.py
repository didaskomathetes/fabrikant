"""Adapter registry: backend name → adapter class + factory.

Lets `steps/agent.py` stay port-only: the dispatcher only imports
`create_backend` (and the port `AgentBackend`), never concrete adapters. New
adapters register here — the dispatcher does not change.

Every adapter implements `create(step, endpoint, model, stale_after)`
(see `port.AgentBackend`), which extracts its specific options (e.g. codex
`sandbox`, copilot `tools`) from the step/env itself.
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


# name → variant for `FABRIKANT_SANDBOX=1` (ADR 0054): backends whose host adapter talks to a foreign
# process run in the sandbox as a one-shot in the container instead.
SANDBOX_ADAPTERS: dict[str, type[AgentBackend]] = {
    "opencode": OpenCodeCliBackend,
}


def known_backends() -> list[str]:
    return sorted(ADAPTERS)


def create_backend(backend_name: str, step: dict, endpoint: str, model: str, stale_after: int) -> AgentBackend:
    """Instantiates the adapter for `backend_name` (port-typed).

    Raises `ValueError` for unknown names — the dispatcher shows the
    available backends.
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
