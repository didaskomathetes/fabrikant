"""codex-Adapter: OpenAI Codex CLI (`codex exec`, One-Shot pro Prompt).

One-Shot-Modell (Mechanik in `OneShotCliBackend`): Prompt über STDIN
(keine ARG-Limits, kein Shell-Interpolation). Sandbox: Default
`workspace-write` (Codex-Sandbox, bubblewrap-basiert); `step.sandbox` /
`FABRIK_CODEX_SANDBOX` als Override — in Gateway-/Service-Kontexten ohne
bubblewrap oft `danger-full-access` notwendig (Containment dann über
User/VM, nicht über die Sandbox).

Keine Token-/Cost-Daten: `codex exec` ohne `--json` liefert kein Usage —
die Step-Summary zeigt 0 tokens (wie alle CLI-Backends).
"""

from __future__ import annotations

import os

from ..agentenv import CODEX_AUTH_VARS, passthrough
from ._oneshot import OneShotCliBackend


class CodexBackend(OneShotCliBackend):
    """Agent-Adapter: OpenAI Codex CLI (`codex exec`)."""

    name = "codex"

    def __init__(self, endpoint: str = "", model: str = "", stale_after: int = 900, sandbox: str | None = None):
        super().__init__(endpoint, model.strip(), stale_after)
        self.sandbox = (sandbox or os.environ.get("FABRIK_CODEX_SANDBOX", "workspace-write")).strip()

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> CodexBackend:
        sandbox = (step.get("sandbox") or os.environ.get("FABRIK_CODEX_SANDBOX", "")).strip()
        return cls(endpoint, model, stale_after=stale_after, sandbox=sandbox or None)

    # ------------------------------------------------------------------ Hooks
    def _env(self) -> dict:
        """Eigene Auth der Codex-CLI explizit durchreichen (ADR 0050)."""
        return passthrough(CODEX_AUTH_VARS)

    def _cli(self) -> str:
        return "codex"

    def _build_cmd(self) -> list[str]:
        cmd = ["codex", "exec", "--sandbox", self.sandbox]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        base = {"tokens": {}, "cost": 0.0, "tools": []}
        if returncode == 0:
            return {**base, "status": "done", "text": out.strip()}
        return {**base, "status": "failed", "text": (err.strip() or out.strip() or "codex: kein Output")[:300]}
