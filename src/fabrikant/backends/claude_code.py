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
  Restricted per step: `tools` is the hard limit on which built-ins exist (`--tools`);
  `allowed_tools` are pre-approval rules (`--allowedTools`, e.g.
  `Edit(/.fabrikant-session/review/review.json)`) and switch the CLI to `dontAsk`,
  which denies whatever would otherwise prompt. Not an allowlist: calls that need no
  approval (reads in the workdir, read-only Bash) and calls approved by the user/
  `settings.json` still run. `tools` does not cover MCP tools.
  A restricted step also treats the working directory as untrusted (`RESTRICTED_FLAGS`):
  no project/local settings (hooks, env, `.mcp.json`), no MCP servers, no hooks at all.
- **Staleness:** disabled (`_stale_after() → None`) — no inactive
  server state as with opencode; the step timeout is the backstop.
"""

from __future__ import annotations

import json

from ..agentenv import CLAUDE_AUTH_VARS, passthrough
from ..billing import Billing, from_auth
from ..tokens import TokenCounts
from ._oneshot import OneShotCliBackend, csv_arg


def _opt_csv(value: object) -> str | None:
    """Like `csv_arg`, but an omitted key stays None — an explicit empty value must not mean "default"."""
    return None if value is None else csv_arg(value)


# Restricted steps (`tools`/`allowed_tools`) often run on repo content someone else wrote (a PR head).
# Headless `claude -p` ignores project `permissions.allow` rules, but still applies the project's hooks,
# `env` block and `.mcp.json` servers — a PR adding `.claude/settings.json` could run commands despite
# `tools`, e.g. write the reviewer's payload itself. Only user settings (the operator's) are read; MCP
# servers and hooks (the CLI flag outranks every settings file) stay off.
RESTRICTED_FLAGS = (
    "--setting-sources",
    "user",
    "--strict-mcp-config",
    "--settings",
    json.dumps({"disableAllHooks": True}),
)


class ClaudeCodeBackend(OneShotCliBackend):
    """Agent-Adapter: Claude Code CLI (`claude -p`, headless Print-Mode)."""

    name = "claude_code"

    def __init__(
        self,
        endpoint: str = "",
        model: str = "",
        stale_after: int = 900,
        tools: str | None = None,
        allowed_tools: str | None = None,
    ):
        # opencode uses `provider/model`; `claude --model` wants only the model.
        super().__init__(endpoint, model.removeprefix("anthropic/"), stale_after)
        # None = key omitted (backend default); "" = explicitly empty (no tools / no pre-approvals).
        self.tools = None if tools is None else tools.strip()
        self.allowed_tools = None if allowed_tools is None else allowed_tools.strip()

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> ClaudeCodeBackend:
        return cls(
            endpoint,
            model,
            stale_after=stale_after,
            tools=_opt_csv(step.get("tools")),
            allowed_tools=_opt_csv(step.get("allowed_tools")),
        )

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
        return None  # no staleness check — the step timeout is the backstop

    def _build_cmd(self) -> list[str]:
        cmd = ["claude", "-p", "--output-format", "json", "--model", self.model]
        if self.tools is not None or self.allowed_tools is not None:
            cmd += RESTRICTED_FLAGS
        if self.tools is not None:
            cmd += ["--tools", self.tools]  # "" = no built-in tools at all
        if self.allowed_tools is not None:
            # Restricted: whatever would prompt is denied (no prompt in a headless run).
            # Empty = no pre-approvals at all — never falls back to the Bash default.
            cmd += ["--permission-mode", "dontAsk"]
            return [*cmd, "--allowedTools", self.allowed_tools] if self.allowed_tools else cmd
        # Default containment: `acceptEdits` + `--allowedTools Bash` lets the agent
        # edit files in the workdir and run shell commands.
        return [*cmd, "--permission-mode", "acceptEdits", "--allowedTools", "Bash"]

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
