"""Agent-Backends (Ports & Adapters).

`port.AgentBackend` ist der vom Core definierte Vertrag. Die Adapter
implementieren ihn: `opencode` (HTTP-API, strukturell), `claude_code` /
`codex` / `copilot` (CLI-One-Shot, erben `OneShotCliBackend`).
`registry.create_backend` ist die Factory, über die der Dispatcher
port-only bleibt.
"""

from ._oneshot import OneShotCliBackend
from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend
from .copilot import CopilotBackend
from .opencode import OpenCodeBackend
from .port import AgentBackend
from .registry import ADAPTERS, create_backend, known_backends

__all__ = [
    "ADAPTERS",
    "AgentBackend",
    "ClaudeCodeBackend",
    "CodexBackend",
    "CopilotBackend",
    "OneShotCliBackend",
    "OpenCodeBackend",
    "create_backend",
    "known_backends",
]
