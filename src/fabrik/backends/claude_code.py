"""claude_code-Adapter: Claude Code CLI (`claude -p`, headless Print-Mode).

One-Shot-Modell (Mechanik in `OneShotCliBackend`): `send_prompt` startet
den Subprozess (Prompt via Stdin), `poll` meldet running bis der Prozess
endet, dann done/failed. Ergebnis ist ein finales JSON auf Stdout
(`--output-format json`: `result`, `usage`, `total_cost_usd`, `subtype`,
`is_error`).

- **Model:** `--model <alias|name>` (z. B. `sonnet`, `claude-sonnet-4-5`);
  ein `anthropic/`-Prefix (opencode-Konvention) wird abgestreift.
- **Permissions:** Default `--permission-mode acceptEdits` +
  `--allowedTools Bash` (Agent editiert Dateien, darf Shell). Echte
  Isolation (non-root/Container/VM) ist Deployment-Voraussetzung.
- **Staleness:** deaktiviert (`_stale_after() → None`) — kein inaktiver
  Server-State wie bei opencode; der Step-Timeout ist der Backstop.
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
        # opencode nutzt `provider/model`; `claude --model` will nur das Model.
        super().__init__(endpoint, model.removeprefix("anthropic/"), stale_after)

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> ClaudeCodeBackend:
        return cls(endpoint, model, stale_after=stale_after)

    # ------------------------------------------------------------------ Hooks
    def _env(self) -> dict:
        """Eigene Auth der Claude-Code-CLI explizit durchreichen (ADR 0050)."""
        return passthrough(CLAUDE_AUTH_VARS)

    def billing(self) -> Billing:
        """API-Key → ``api`` (gewinnt im ``-p``-Modus immer), OAuth-Token des Abos → ``subscription``."""
        return from_auth(
            self._env(), api=("ANTHROPIC_API_KEY",), subscription=("CLAUDE_CODE_OAUTH_TOKEN",), api_wins=True
        )

    def _cli(self) -> str:
        return "claude"

    def _stale_after(self) -> int | None:
        return None  # kein Staleness-Check — Step-Timeout ist der Backstop

    def _build_cmd(self) -> list[str]:
        # Containment: `acceptEdits` + `--allowedTools Bash` lässt den Agenten
        # Dateien in der Workdir editieren und Shell ausführen.
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
            # JSON-Ergebnis trotz Exit ≠ 0 (z. B. Nutzungslimit): `result` ist die eigentliche
            # Meldung — der rohe JSON-Anfang (duration_api_ms, …) sagt nichts (ADR 0035).
            return {**base, "status": "failed", "text": str(d.get("result") or err or out)[:300]}
        usage = d.get("usage") or {}
        # Anthropic: input_tokens ist nur der ungecachte Rest — Cache-Read/-Write kommen dazu.
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
