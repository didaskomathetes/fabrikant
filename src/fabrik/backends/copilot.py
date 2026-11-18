"""copilot-Adapter: GitHub Copilot CLI (`copilot -p`, One-Shot pro Prompt).

One-Shot-Modell (Mechanik in `OneShotCliBackend`): Prompt über STDIN
(`echo … | copilot` — keine ARG-Limits, keine Shell-Interpolation; `-p`
würde an `MAX_ARG_STRLEN` stoßen). Output: `--output-format json` → JSONL
(eine JSON-Zeile pro Event); das finale Ergebnis wird defensiv extrahiert
(Schema nicht vollständig dokumentiert), Fallback ist der rohe stdout.

Permissions: Default `--allow-all-tools` (Agent editiert Dateien + Shell).
ECHTE Isolation (non-root User, Container, VM) ist eine DEPLOYMENT-
Voraussetzung, keine Config-Option. Override: `step.tools` /
`FABRIK_COPILOT_TOOLS` (z. B. `shell,write`) → `--allow-tool=<list>`.

Auth: `COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN` (fine-grained
PAT mit "Copilot Requests") — Deployment-Umgebung, nicht Spec.
`COPILOT_AUTO_UPDATE=false` wird pro Prozess gesetzt.

Usage: Copilot rechnet mit Premium Requests, nicht mit Tokens. Das `result`-Event
trägt `usage.premiumRequests`; der Adapter meldet ihn als `premium_requests` im
Poll-Ergebnis (Step-Summary + `cost`-Event im Session-Log). Tokens und USD-Kosten
liefert die CLI nicht — sie bleiben 0 (dokumentierte Lücke, kein Pricing-Table).
"""

from __future__ import annotations

import json
import os

from ..agentenv import COPILOT_AUTH_VARS, passthrough
from ._oneshot import OneShotCliBackend

# JSONL-Event-Typen des Copilot-CLI-Schemas (defensiv gelesen — das Schema ist
# nicht vollständig dokumentiert; neue/ältere Varianten werden ignoriert).
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
        # opencode nutzt `provider/model`; `copilot --model` will nur das Model.
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

    def _env(self) -> dict:
        """Adapter-Zusatz auf der Agent-Env-Allowlist (ADR 0050) — kein `os.environ`-Klon.

        Nur `COPILOT_GITHUB_TOKEN` (fine-grained PAT, „Copilot Requests“) wird durchgereicht —
        die eigene Arbeits-Identität der CLI. `GITHUB_TOKEN`/`GH_TOKEN` sind fabriks
        API-Token und bleiben draußen; ein Step bekommt sie nur per `github_identity:`."""
        return {"COPILOT_AUTO_UPDATE": "false", **passthrough(COPILOT_AUTH_VARS)}

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        base = {"tokens": {"input": 0, "output": 0}, "cost": 0.0, "tools": []}
        premium = self._premium_requests(out)
        if premium is not None:
            base["premium_requests"] = premium
        if returncode == 0:
            text, found = self._extract_result(out)
            if not found:
                # Stille Fehlschlag: CLI exit 0, aber der Stream enthält keine
                # finale Antwort — entweder stirbt der Model-Call (z. B. nach
                # model.call_start) oder der JSONL-Stream wird beim Shutdown
                # abgeschnitten (stdout-Dispose-Timeout; dann fehlt das
                # terminierende result-Event und nur ein Zwischen-Turn-Text
                # wie ein Plan bleibt). Ohne diesen Check würde der Run als
                # "pass" enden, obwohl der Agent nichts getan hat
                # (Regression: review-pr 2026-10-18).
                # `retryable`: typischerweise transienter API-Ausfall → der
                # Agent-Step darf die Session neu starten (statt den Run zu killen).
                return {
                    **base,
                    "status": "failed",
                    "retryable": True,
                    "text": "copilot: Session endete ohne finale Antwort (JSONL ohne result-Event) — möglicher API-Ausfall oder abgeschnittener Stream",
                }
            return {**base, "status": "done", "text": text}
        # Exit ≠ 0: der eigentliche Grund steht oft im `session.error`-Event des
        # JSONL-Streams (z. B. Quota/HTTP 402), nicht in stderr — den voranstellen,
        # damit die Fehlermeldung die Ursache zeigt statt nur der ersten stdout-Zeile.
        detail = (err.strip() or out.strip() or "copilot: kein Output")[:300]
        reason = self._session_error_reason(out)
        text = f"copilot: {reason}\n{detail}" if reason else detail
        return {**base, "status": "failed", "text": text}

    @staticmethod
    def _premium_requests(out: str) -> int | None:
        """``usage.premiumRequests`` des letzten ``result``-Events; None, wenn nicht vorhanden."""
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
        """Assistant-Content → Text: String direkt, Liste von Text-Blöcken
        `[{"type":"text","text":…}]` wird konkateniert."""
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return "".join(b.get("text", "") for b in v if isinstance(b, dict) and b.get("type") == "text")
        return ""

    @staticmethod
    def _session_error_reason(out: str) -> str:
        """Grund aus dem `session.error`-Event des JSONL-Streams (z. B. Quota/HTTP 402).

        Defensive: das Copilot-CLI-Schema ist nicht vollständig dokumentiert, daher
        werden mehrere plausible Felder gelesen (`type`/`code` für die Fehlerart,
        `message`/`error`/`reason` für die Beschreibung). Liefert "" ohne
        `session.error`-Event im Stream."""
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
        """Finale Antwort aus JSONL-stdout extrahieren (defensiv).

        Liefert `(text, found)`. `found=False`: Der Stream enthält JSONL-Zeilen,
        aber KEINE finale Antwort (kein result/assistant.message/Delta mit
        Content) — die Session ist ohne Ergebnis beendet (stiller Fehlschlag,
        z. B. Model-Call-Abbruch). Liegt gar KEIN JSON vor, ist der rohe
        stdout die Antwort (Schema-Fallback, `found=True`).

        Echtes Copilot-CLI-Schema (verifiziert 1.0.98): die finale Antwort liegt
        in `assistant.message` → `data.content`; Streaming-Deltas in
        `assistant.message_delta` → `data.deltaContent`. Die `result`-Zeile hat
        NUR `exitCode`/`usage` — keinen Text. Priorität:
        1. explizites `result`-Feld (ältere/andere Schemas),
        2. finale `assistant.message` (data.content),
        3. akkumulierte Deltas (data.deltaContent),
        4. generischer Assistant-Text (role=assistant / type=message),
        5. roher stdout (Fallback, z. B. wenn JSON ausfällt).

        Abgeschnittener Stream: Session-Events (session.start/turn_start) sind
        vorhanden, aber das terminierende `result`-Event fehlt (CLI-stdout-
        Dispose-Timeout beim Shutdown) → der "finale" Text stammt aus einem
        Zwischen-Turn (z. B. ein Plan), nicht aus der echten Antwort →
        `found=False` (stiller Fehlschlag, retryable).
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
                # Abgeschnitten: Session lief, aber kein terminierendes
                # `result`-Event (und kein result-Feld älterer Schemas) →
                # der Text ist ein Zwischenstand, keine finale Antwort.
                if saw_session and not saw_result_event and result_val is None:
                    return "", False
                return val.strip(), True
        if not parsed_json:
            return out.strip(), bool(out.strip())
        return "", False
