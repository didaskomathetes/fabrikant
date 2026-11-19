"""codex-Adapter: OpenAI Codex CLI (`codex exec`, One-Shot pro Prompt).

One-Shot-Modell (Mechanik in `OneShotCliBackend`): Prompt über STDIN
(keine ARG-Limits, kein Shell-Interpolation). Sandbox: Default
`workspace-write` (Codex-Sandbox, bubblewrap-basiert); `step.sandbox` /
`FABRIK_CODEX_SANDBOX` als Override — in Gateway-/Service-Kontexten ohne
bubblewrap oft `danger-full-access` notwendig (Containment dann über
User/VM, nicht über die Sandbox).

Usage: `codex exec --json` streamt JSONL; `turn.completed.usage`
(`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`) ist die
**kumulierte** Summe des Threads — es zählt das letzte Event, nicht die Summe (`input_tokens`
enthält den gecachten Anteil bereits). Credits/Limits (`rate_limits`) stehen nicht im
`--json`-Stream, nur in der Rollout-Datei unter `$CODEX_HOME/sessions`. Die finale
Antwort ist die letzte `item.completed`/`agent_message`; Fehler stehen in
`turn.failed`/`error`. Keine Kosten in USD — die CLI liefert keine (`cost`
bleibt 0.0). Ohne JSON im stdout (ältere CLI) gilt der rohe stdout als Antwort.
"""

from __future__ import annotations

import json
import os

from ..agentenv import CODEX_AUTH_VARS, passthrough
from ..billing import Billing, from_auth
from ..tokens import TokenCounts, as_count
from ._oneshot import OneShotCliBackend

# ChatGPT-Login als Env (base64 von auth.json); ohne ihn liest die CLI ~/.codex/auth.json — Art offen.
_CHATGPT_LOGIN_VAR = "CODEX_AUTH_JSON_B64"
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
        reason = error or err.strip() or out.strip() or "codex: kein Output"
        return {**base, "status": "failed", "text": reason[:300]}

    @staticmethod
    def _read_events(out: str) -> tuple[str, TokenCounts, str]:
        """JSONL-stdout → (finaler Agent-Text, Token-Summe, Fehlermeldung).

        Defensiv: Nicht-JSON-Zeilen und unbekannte Events werden ignoriert; liegt gar kein JSON
        vor, ist der rohe stdout der Text (Schema-Fallback)."""
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
                # Kumulativ je Thread (codex exec): das letzte Event ersetzt, nicht addieren.
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
