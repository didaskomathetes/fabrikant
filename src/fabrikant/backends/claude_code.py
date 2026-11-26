"""claude_code adapter: Claude Code CLI (`claude -p`, headless print mode).

One-shot model (mechanics in `OneShotCliBackend`): `send_prompt` starts
the subprocess (prompt via stdin), `poll` reports running until the process
ends, then done/failed. The result is a final JSON on stdout
(`--output-format json`: `result`, `usage`, `total_cost_usd`, `subtype`,
`is_error`).

- **Model:** `--model <alias|name>` (e.g. `sonnet`, `claude-sonnet-4-5`);
  an `anthropic/` prefix (opencode convention) is stripped.
- **Permissions:** default `--permission-mode acceptEdits` +
  `--allowedTools Bash` (agent edits files, may use shell). Real
  isolation (non-root/container/VM) is a deployment prerequisite.
- **Staleness:** disabled (`_stale_after() → None`) — no inactive
  server state as with opencode; the step timeout is the backstop.
"""

from __future__ import annotations

import json

from ..agentenv import CLAUDE_AUTH_VARS, passthrough
from ..billing import Billing, from_auth
from ..tokens import TokenCounts
from ._oneshot import OneShotCliBackend


class ClaudeCodeBackend(OneShotCliBackend):
    """Agent-Adapter: Claude Code CLI (`claude -p`, headless Print-Mode)."""

    name = "claude_code"

    def __init__(self, endpoint: str = "", model: str = "", stale_after: int = 900):
        # opencode uses `provider/model`; `claude --model` wants only the model.
        super().__init__(endpoint, model.removeprefix("anthropic/"), stale_after)

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> ClaudeCodeBackend:
        return cls(endpoint, model, stale_after=stale_after)

    # ------------------------------------------------------------------ Hooks
    def _env(self) -> dict:
        """Explicitly pass through the Claude Code CLI's own auth (ADR 0050)."""
        return passthrough(CLAUDE_AUTH_VARS)

    def billing(self) -> Billing:
        """API key → ``api`` (always wins in ``-p`` mode), subscription OAuth token → ``subscription``."""
        return from_auth(
            self._env(), api=("ANTHROPIC_API_KEY",), subscription=("CLAUDE_CODE_OAUTH_TOKEN",), api_wins=True
        )

    def _cli(self) -> str:
        return "claude"

    def _stale_after(self) -> int | None:
        return None  # no staleness check — the step timeout is the backstop

    def _build_cmd(self) -> list[str]:
        # Containment: `acceptEdits` + `--allowedTools Bash` lets the agent
        # edit files in the workdir and run shell commands.
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Bash",
        ]

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        base = {"tokens": TokenCounts().as_poll(), "cost": 0.0, "tools": []}
        try:
            d = json.loads(out)
        except (json.JSONDecodeError, ValueError):
            d = None
        if not isinstance(d, dict):
            return {**base, "status": "failed", "text": (err or out)[:300]}
        if returncode != 0:
            # JSON result despite exit ≠ 0 (e.g. usage limit): `result` is the actual
            # message — the raw JSON beginning (duration_api_ms, …) says nothing (ADR 0035).
            return {**base, "status": "failed", "text": str(d.get("result") or err or out)[:300]}
        usage = d.get("usage") or {}
        # Anthropic: input_tokens is only the uncached remainder — cache read/write come on top.
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        tokens = TokenCounts(
            input=int(usage.get("input_tokens") or 0) + cache_read + cache_write,
            output=int(usage.get("output_tokens") or 0),
            cache_read=cache_read,
            cache_write=cache_write,
        )
        return {
            "status": "done" if (d.get("subtype") == "success" and not d.get("is_error")) else "failed",
            "text": d.get("result", ""),
            "tokens": tokens.as_poll(),
            "cost": float(d.get("total_cost_usd", 0.0) or 0.0),
            "tools": [],
        }
