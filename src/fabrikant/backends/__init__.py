"""Agent backends (ports & adapters).

`port.AgentBackend` is the contract defined by the core. The adapters
implement it: `opencode` (HTTP API, structured), `claude_code` /
`codex` / `copilot` (CLI one-shot, inherit `OneShotCliBackend`).
`registry.create_backend` is the factory through which the dispatcher
stays port-only.
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
