"""codex adapter: OpenAI Codex CLI (`codex exec`, one-shot per prompt).

One-shot model (mechanics in `OneShotCliBackend`): prompt via STDIN
(no ARG limits, no shell interpolation). Sandbox: default
`workspace-write` (Codex sandbox, bubblewrap-based); `step.sandbox` /
`FABRIKANT_CODEX_SANDBOX` as override — in gateway/service contexts without
bubblewrap `danger-full-access` is often necessary (containment then via
user/VM, not via the sandbox).

Usage: `codex exec --json` streams JSONL; `turn.completed.usage`
(`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`) is the
**cumulative** total of the thread — the last event counts, not the sum (`input_tokens`
already includes the cached share). Credits/limits (`rate_limits`) are not in the
`--json` stream, only in the rollout file under `$CODEX_HOME/sessions`. The final
answer is the last `item.completed`/`agent_message`; errors are in
`turn.failed`/`error`. No cost in USD — the CLI provides none (`cost`
stays 0.0). Without JSON in stdout (older CLI) the raw stdout counts as the answer.
"""

from __future__ import annotations

import json
import os
import re

from ..agentenv import CODEX_AUTH_VARS, passthrough
from ..billing import Billing, from_auth
from ..i18n import t
from ..tokens import TokenCounts, as_count
from ._oneshot import OneShotCliBackend

# ChatGPT login as env (base64 of auth.json); without it the CLI reads ~/.codex/auth.json — billing mode unknown.
_CHATGPT_LOGIN_VAR = "CODEX_AUTH_JSON_B64"
_FAILURE_TEXT_MAX = 300
# `codex exec` writes banners, warnings and the prompt to stderr before the actual error;
# the cause is in `ERROR: …` lines (e.g. spend cap, auth) — only those count.
_ERROR_LINE = re.compile(r"^ERROR:\s*(.+)$", re.MULTILINE)
_EVENT_TURN_COMPLETED = "turn.completed"
_EVENT_TURN_FAILED = "turn.failed"
_EVENT_ERROR = "error"
_EVENT_ITEM_COMPLETED = "item.completed"
_ITEM_AGENT_MESSAGE = "agent_message"


def _failure_text(out: str, err: str) -> str:
    """Error text of a failed `codex exec` session: the `ERROR:` lines (deduplicated), otherwise
    stderr/stdout as delivered — truncated, so the run log and the classification (ADR 0035) see the
    cause instead of the banner."""
    errors = list(dict.fromkeys(m.strip() for m in _ERROR_LINE.findall(err)))
    text = "\n".join(errors) or err.strip() or out.strip() or t("backends.codex.codex_no_output")
    return text[:_FAILURE_TEXT_MAX]


class CodexBackend(OneShotCliBackend):
    """Agent-Adapter: OpenAI Codex CLI (`codex exec`)."""

    name = "codex"

    def __init__(self, endpoint: str = "", model: str = "", stale_after: int = 900, sandbox: str | None = None):
        super().__init__(endpoint, model.strip(), stale_after)
        self.sandbox = (sandbox or os.environ.get("FABRIKANT_CODEX_SANDBOX", "workspace-write")).strip()

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> CodexBackend:
        sandbox = (step.get("sandbox") or os.environ.get("FABRIKANT_CODEX_SANDBOX", "")).strip()
        return cls(endpoint, model, stale_after=stale_after, sandbox=sandbox or None)

    # ------------------------------------------------------------------ Hooks
    def _env(self) -> dict:
        """Explicitly pass through the Codex CLI's own auth (ADR 0050)."""
        return passthrough(CODEX_AUTH_VARS)

    def _cli(self) -> str:
        return "codex"

    def billing(self) -> Billing:
        """ChatGPT-Login (``CODEX_AUTH_JSON_B64``) → ``subscription``, ``OPENAI_API_KEY`` → ``api``."""
        return from_auth(
            passthrough((*CODEX_AUTH_VARS, _CHATGPT_LOGIN_VAR)),
            api=("OPENAI_API_KEY",),
            subscription=(_CHATGPT_LOGIN_VAR,),
        )

    def _build_cmd(self) -> list[str]:
        cmd = ["codex", "exec", "--json", "--sandbox", self.sandbox]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        text, tokens, error = self._read_events(out)
        base = {"tokens": tokens.as_poll(), "cost": 0.0, "tools": []}
        if returncode == 0:
            return {**base, "status": "done", "text": text.strip()}
        if error:
            return {**base, "status": "failed", "text": error[:_FAILURE_TEXT_MAX]}
        return {**base, "status": "failed", "text": _failure_text(out, err)}

    @staticmethod
    def _read_events(out: str) -> tuple[str, TokenCounts, str]:
        """JSONL stdout → (final agent text, token total, error message).


        Defensive: non-JSON lines and unknown events are ignored; if there is no JSON
        at all, the raw stdout is the text (schema fallback)."""
        text, error = "", ""
        tokens = TokenCounts()
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
                # Cumulative per thread (codex exec): the last event replaces, do not add.

                tokens = TokenCounts(
                    input=as_count(usage.get("input_tokens")),
                    output=as_count(usage.get("output_tokens")),
                    cache_read=as_count(usage.get("cached_input_tokens")),
                    cache_write=as_count(usage.get("cache_write_input_tokens")),
                )
            elif kind == _EVENT_ITEM_COMPLETED and isinstance(ev.get("item"), dict):
                item = ev["item"]
                if item.get("type") == _ITEM_AGENT_MESSAGE and isinstance(item.get("text"), str):
                    text = item["text"]
            elif kind in (_EVENT_TURN_FAILED, _EVENT_ERROR):
                err = ev.get("error") if kind == _EVENT_TURN_FAILED else ev
                msg = err.get("message") if isinstance(err, dict) else None
                error = str(msg or error)
        return (text if parsed_json else out), tokens, error
