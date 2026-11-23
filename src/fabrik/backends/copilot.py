"""copilot adapter: GitHub Copilot CLI (`copilot -p`, one-shot per prompt).

One-shot model (mechanics in `OneShotCliBackend`): prompt via STDIN
(`echo … | copilot` — no ARG limits, no shell interpolation; `-p`
would hit `MAX_ARG_STRLEN`). Output: `--output-format json` → JSONL
(one JSON line per event); the final result is extracted defensively
(schema not fully documented), fallback is the raw stdout.

Permissions: default `--allow-all-tools` (agent edits files + shell).
REAL isolation (non-root user, container, VM) is a DEPLOYMENT
prerequisite, not a config option. Override: `step.tools` /
`FABRIK_COPILOT_TOOLS` (e.g. `shell,write`) → `--allow-tool=<list>`.

Auth: `COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN` (fine-grained
PAT with "Copilot Requests") — deployment environment, not spec.
`COPILOT_AUTO_UPDATE=false` is set per process.

Usage: Copilot accounts in premium requests, not tokens. The `result` event
carries `usage.premiumRequests`; the adapter reports it as `premium_requests` in the
poll result (step summary + `cost` event in the session log). Tokens and USD costs
are not provided by the CLI — they stay 0 (documented gap, no pricing table).
"""

from __future__ import annotations

import json
import os

from ..agentenv import COPILOT_AUTH_VARS, passthrough
from ..billing import Billing
from ..i18n import t as _t
from ..tokens import TokenCounts
from ._oneshot import OneShotCliBackend

# JSONL event types of the Copilot CLI schema (read defensively — the schema is
# not fully documented; new/older variants are ignored).
_EVENT_SESSION_START = "session.start"
_EVENT_TURN_START = "assistant.turn_start"
_EVENT_RESULT = "result"
_EVENT_ASSISTANT_MESSAGE = "assistant.message"
_EVENT_ASSISTANT_DELTA = "assistant.message_delta"
_EVENT_MESSAGE = "message"
_EVENT_ASSISTANT = "assistant"
_EVENT_SESSION_ERROR = "session.error"


class CopilotBackend(OneShotCliBackend):
    """Agent-Adapter: GitHub Copilot CLI (`copilot -p`, One-Shot)."""

    name = "copilot"

    def __init__(self, endpoint: str = "", model: str = "", stale_after: int = 900, tools: str | None = None):
        # opencode uses `provider/model`; `copilot --model` wants only the model.
        super().__init__(endpoint, model.removeprefix("anthropic/").strip(), stale_after)
        self.tools = (tools or os.environ.get("FABRIK_COPILOT_TOOLS", "")).strip()

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> CopilotBackend:
        tools = (step.get("tools") or os.environ.get("FABRIK_COPILOT_TOOLS", "")).strip()
        return cls(endpoint, model, stale_after=stale_after, tools=tools or None)

    # ------------------------------------------------------------------ Hooks
    def _cli(self) -> str:
        return "copilot"

    def _build_cmd(self) -> list[str]:
        cmd = ["copilot", "--output-format", "json", "--no-ask-user"]
        if self.model:
            cmd += ["--model", self.model]
        if self.tools:
            cmd += [f"--allow-tool={self.tools}"]
        else:
            cmd += ["--allow-all-tools"]
        return cmd

    def billing(self) -> Billing:
        """Copilot rechnet Premium Requests des GitHub-Plans ab — nie pro Token."""
        return Billing.SUBSCRIPTION

    def _env(self) -> dict:
        """Adapter addition on the agent env allowlist (ADR 0050) — no `os.environ` clone.

        Only `COPILOT_GITHUB_TOKEN` (fine-grained PAT, "Copilot Requests") is passed through —
        the CLI's own working identity. `GITHUB_TOKEN`/`GH_TOKEN` are fabrik's
        API token and stay out; a step only gets them via `github_identity:`."""
        return {"COPILOT_AUTO_UPDATE": "false", **passthrough(COPILOT_AUTH_VARS)}

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        base = {"tokens": TokenCounts().as_poll(), "cost": 0.0, "tools": []}
        premium = self._premium_requests(out)
        if premium is not None:
            base["premium_requests"] = premium
        if returncode == 0:
            text, found = self._extract_result(out)
            if not found:
                # Silent failure: CLI exit 0, but the stream contains no
                # final answer — either the model call dies (e.g. after
                # model.call_start) or the JSONL stream is cut off at shutdown
                # (stdout dispose timeout; then the terminating result event
                # is missing and only an intermediate-turn text such as a plan
                # remains). Without this check the run would end as
                # "pass" although the agent did nothing
                # (regression: review-pr 2026-10-18).
                # `retryable`: typically a transient API outage → the
                # agent step may restart the session (instead of killing the run).
                return {
                    **base,
                    "status": "failed",
                    "retryable": True,
                    "text": _t("backends.copilot.copilot_session_ended_without_a"),
                }
            return {**base, "status": "done", "text": text}
        # Exit ≠ 0: the actual reason is often in the `session.error` event of the
        # JSONL stream (e.g. quota/HTTP 402), not in stderr — put it first,
        # so the error message shows the cause instead of just the first stdout line.
        detail = (err.strip() or out.strip() or _t("backends.copilot.copilot_no_output"))[:300]
        reason = self._session_error_reason(out)
        text = f"copilot: {reason}\n{detail}" if reason else detail
        return {**base, "status": "failed", "text": text}

    @staticmethod
    def _premium_requests(out: str) -> int | None:
        """``usage.premiumRequests`` of the last ``result`` event; None if not present."""
        found = None
        for ln in out.splitlines():
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            usage = d.get("usage") if isinstance(d, dict) and d.get("type") == _EVENT_RESULT else None
            n = usage.get("premiumRequests") if isinstance(usage, dict) else None
            if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0:
                found = int(n)
        return found

    @staticmethod
    def _content_text(v) -> str:
        """Assistant content → text: a string directly, a list of text blocks
        `[{"type":"text","text":…}]` is concatenated."""
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return "".join(b.get("text", "") for b in v if isinstance(b, dict) and b.get("type") == "text")
        return ""

    @staticmethod
    def _session_error_reason(out: str) -> str:
        """Reason from the `session.error` event of the JSONL stream (e.g. quota/HTTP 402).

        Defensive: the Copilot CLI schema is not fully documented, so
        several plausible fields are read (`type`/`code` for the error kind,
        `message`/`error`/`reason` for the description). Returns "" without a
        `session.error` event in the stream."""
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(d, dict) or d.get("type") != _EVENT_SESSION_ERROR:
                continue
            data = d.get("data")
            data = data if isinstance(data, dict) else {}
            etype = str(data.get("type") or data.get("code") or "").strip()
            msg = str(data.get("message") or data.get("error") or data.get("reason") or "").strip()
            if etype and msg:
                return f"session.error: {etype} — {msg}"
            if etype or msg:
                return f"session.error: {etype or msg}"
            return "session.error"
        return ""

    @staticmethod
    def _extract_result(out: str) -> tuple[str, bool]:
        """Extract the final answer from JSONL stdout (defensive).

        Returns `(text, found)`. `found=False`: the stream contains JSONL lines
        but NO final answer (no result/assistant.message/delta with
        content) — the session ended without a result (silent failure,
        e.g. model call abort). If there is NO JSON at all, the raw
        stdout is the answer (schema fallback, `found=True`).

        Real Copilot CLI schema (verified 1.0.98): the final answer is in
        `assistant.message` → `data.content`; streaming deltas in
        `assistant.message_delta` → `data.deltaContent`. The `result` line has
        ONLY `exitCode`/`usage` — no text. Priority:
        1. explicit `result` field (older/other schemas),
        2. final `assistant.message` (data.content),
        3. accumulated deltas (data.deltaContent),
        4. generic assistant text (role=assistant / type=message),
        5. raw stdout (fallback, e.g. if JSON fails).

        Truncated stream: session events (session.start/turn_start) are
        present, but the terminating `result` event is missing (CLI stdout
        dispose timeout at shutdown) → the "final" text comes from an
        intermediate turn (e.g. a plan), not from the real answer →
        `found=False` (silent failure, retryable).
        """
        lines = [ln for ln in out.splitlines() if ln.strip()]
        result_val = None
        last_content = ""
        last_delta = ""
        parsed_json = False
        saw_session = False
        saw_result_event = False
        for ln in lines:
            try:
                d = json.loads(ln)
            except (json.JSONDecodeError, ValueError):
                continue
            parsed_json = True
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t in (_EVENT_SESSION_START, _EVENT_TURN_START):
                saw_session = True
            if t == _EVENT_RESULT:
                saw_result_event = True
            raw_data = d.get("data")
            data = raw_data if isinstance(raw_data, dict) else {}
            if t == _EVENT_ASSISTANT_MESSAGE:
                c = CopilotBackend._content_text(data.get("content"))
                if c.strip():
                    last_content = c
            elif t == _EVENT_ASSISTANT_DELTA:
                dc = data.get("deltaContent")
                if isinstance(dc, str):
                    last_delta += dc
            if isinstance(d.get("result"), str):
                result_val = d["result"]
            role = d.get("role")
            if role == "assistant" or (role is None and t in (_EVENT_MESSAGE, _EVENT_ASSISTANT)):
                for k in ("content", "text", "message"):
                    v = CopilotBackend._content_text(d.get(k))
                    if v.strip():
                        last_content = v
        for val in (result_val, last_content, last_delta):
            if val and val.strip():
                # Truncated: the session ran, but no terminating
                # `result` event (and no result field of older schemas) →
                # the text is an intermediate state, not a final answer.
                if saw_session and not saw_result_event and result_val is None:
                    return "", False
                return val.strip(), True
        if not parsed_json:
            return out.strip(), bool(out.strip())
        return "", False
