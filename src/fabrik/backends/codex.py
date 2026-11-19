"""codex adapter: OpenAI Codex CLI (`codex exec`, one-shot per prompt).

One-shot model (mechanics in `OneShotCliBackend`): prompt via STDIN
(no ARG limits, no shell interpolation). Sandbox: default
`workspace-write` (Codex sandbox, bubblewrap-based); `step.sandbox` /
`FABRIK_CODEX_SANDBOX` as override — in gateway/service contexts without
bubblewrap `danger-full-access` is often necessary (containment then via
user/VM, not via the sandbox).

Usage: `codex exec --json` streams JSONL; `turn.completed.usage`
(`input_tokens`, `cached_input_tokens`, `output_tokens`) is summed over all turns
(`input_tokens` already includes the cached share). The final
answer is the last `item.completed`/`agent_message`; errors are in
`turn.failed`/`error`. No cost in USD — the CLI provides none (`cost`
stays 0.0). Without JSON in stdout (older CLI) the raw stdout counts as the answer.
"""

from __future__ import annotations

import json
import os

from ..agentenv import CODEX_AUTH_VARS, passthrough
from ..i18n import t
from ._oneshot import OneShotCliBackend

_EVENT_TURN_COMPLETED = "turn.completed"
_EVENT_TURN_FAILED = "turn.failed"
_EVENT_ERROR = "error"
_EVENT_ITEM_COMPLETED = "item.completed"
_ITEM_AGENT_MESSAGE = "agent_message"


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
        cmd = ["codex", "exec", "--json", "--sandbox", self.sandbox]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        text, tokens, error = self._read_events(out)
        base = {"tokens": tokens, "cost": 0.0, "tools": []}
        if returncode == 0:
            return {**base, "status": "done", "text": text.strip()}
        reason = error or err.strip() or out.strip() or t("backends.codex.codex_no_output")
        return {**base, "status": "failed", "text": reason[:300]}

    @staticmethod
    def _read_events(out: str) -> tuple[str, dict, str]:
        """JSONL stdout → (final agent text, {input, output}, error message).

        Defensive: non-JSON lines and unknown events are ignored; if there is no JSON
        at all, the raw stdout is the text (schema fallback)."""
        text, error = "", ""
        tokens = {"input": 0, "output": 0}
        parsed_json = False
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            parsed_json = True
            kind = ev.get("type")
            if kind == _EVENT_TURN_COMPLETED and isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
                tokens["input"] += _count(usage.get("input_tokens"))
                tokens["output"] += _count(usage.get("output_tokens"))
            elif kind == _EVENT_ITEM_COMPLETED and isinstance(ev.get("item"), dict):
                item = ev["item"]
                if item.get("type") == _ITEM_AGENT_MESSAGE and isinstance(item.get("text"), str):
                    text = item["text"]
            elif kind in (_EVENT_TURN_FAILED, _EVENT_ERROR):
                err = ev.get("error") if kind == _EVENT_TURN_FAILED else ev
                msg = err.get("message") if isinstance(err, dict) else None
                error = str(msg or error)
        return (text if parsed_json else out), tokens, error


def _count(value) -> int:
    """Token counter from a JSON value (only non-negative ints, otherwise 0)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
