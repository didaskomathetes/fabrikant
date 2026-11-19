"""opencode backend: opencode serve (HTTP API, 1.x and 2.x).

SPIKE: docs/spike-opencode.md. One serve process serves many projects —
the workdir goes per request (1.x: `?directory=`) or at
session creation (2.x: `location`). Polling instead of SSE: tick-oriented,
idempotent, enough for the conductor base.

Version detection (lazy, cached): `GET /api/info` → 2.x, otherwise
`GET /api/health` → 1.x. 2.x requires HTTP basic auth
(`FABRIK_AGENT_PASSWORD`; the server generates a password if
`OPENCODE_SERVER_PASSWORD` is not set). 2.x messages (flat:
`type`, `content`, top-level `tokens/cost/…`) are normalized into the 1.x envelope
(`{info, parts}`) so `poll()` has common
status logic.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import httpsafe
from ..failures import classify_exception
from ..i18n import t as _t
from ..runmodel import FailureKind


def _default_permissions() -> list[dict]:
    """Default permission rules (1.x format: `permission`/`pattern`/`action`).

    Containment model: `edit` patterns are RELATIVE to the session workdir;
    paths OUTSIDE trigger `external_directory` (default: ask → in headless
    serve = deny). File tools are thereby confined to the workdir.
    `bash "*` is deliberately wide (agents need a shell) — REAL isolation
    (non-root user, container, VM) is a DEPLOYMENT prerequisite,
    not a config option. Override: step.permissions.
    """
    return [
        {"permission": "edit", "pattern": "*", "action": "allow"},
        {"permission": "bash", "pattern": "*", "action": "allow"},
    ]


class _ServeApi:
    """Transport + shape normalization for opencode serve (1.x / 2.x).

    `fetch_messages` ALWAYS returns the 1.x envelope `[{info, parts}]` —
    2.x flat messages are converted in `_normalize_v2` so that
    `OpenCodeBackend.poll()` stays version-agnostic.

    Security: TLS verification always stays active (urllib default —
    never disable). Credentials go only in the Authorization header
    and are never logged.
    """

    def __init__(self, endpoint: str, version: str, password: str | None, username: str):
        self.endpoint = endpoint
        self.version = version
        self._auth = None
        if password:
            self._auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def _req(
        self, method: str, path: str, payload: dict | None = None, directory: str | None = None, timeout: int = 15
    ):
        url = self.endpoint + path
        if self.version == "1" and directory:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}directory={urllib.parse.quote(directory)}"
        headers = {"Content-Type": "application/json", "User-Agent": "fabrik"}
        if self._auth:
            headers["Authorization"] = self._auth
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers,
            method=method,
        )
        with httpsafe.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else {})

    # -- health ----------------------------------------------------------
    def health(self) -> bool:
        try:
            if self.version == "2":
                s, d = self._req("GET", "/api/info", timeout=5)
                return s == 200 and bool(d.get("version"))
            s, d = self._req("GET", "/api/health", timeout=5)
            return s == 200 and d.get("healthy") is True
        except Exception:
            return False

    # -- session ---------------------------------------------------------
    def create_session(self, workdir: str, title: str, model: dict, permissions: list[dict]) -> str:
        if self.version == "2":
            # 2.x: workdir via `location` (bound to the session), rules
            # are called `action`/`resource`/`effect`, response in a `data` wrapper.
            payload = {
                "title": title,
                "model": model,
                "location": {"directory": workdir},
                "permissions": [
                    {"action": p.get("permission"), "resource": p.get("pattern"), "effect": p.get("action")}
                    for p in permissions
                ],
            }
            _, d = self._req("POST", "/api/session", payload, timeout=30)
            d = d.get("data") or {}
        else:
            payload = {"title": title, "model": model, "permission": permissions}
            _, d = self._req("POST", "/session", payload, directory=workdir, timeout=30)
        if not d.get("id"):
            raise RuntimeError(_t("backends.opencode.opencode_session_create_without_id", v1=str(d)[:200]))
        return d["id"]

    def send_prompt(self, session_id: str, prompt: str, directory: str) -> None:
        if self.version == "2":
            s, _ = self._req("POST", f"/api/session/{session_id}/prompt", {"text": prompt}, timeout=30)
            ok = s == 200
        else:
            s, _ = self._req(
                "POST",
                f"/session/{session_id}/prompt_async",
                {"parts": [{"type": "text", "text": prompt}]},
                directory=directory,
                timeout=30,
            )
            ok = s in (200, 204)
        if not ok:
            raise RuntimeError(f"opencode prompt: HTTP {s}")

    def fetch_messages(self, session_id: str, directory: str) -> list[dict]:
        """→ 1.x envelope `[{info, parts}]`, ALWAYS oldest-first.

        2.x: paginated + normalized; the API pages are sorted
        newest-first and are reversed so both versions deliver the same
        order (`poll()` reads the last one as `a[-1]`).
        """
        if self.version == "2":
            msgs: list[dict] = []
            cursor = None
            while True:
                path = f"/api/session/{session_id}/message"
                if cursor:
                    path += f"?cursor={urllib.parse.quote(cursor)}"
                _, d = self._req("GET", path, timeout=15)
                msgs.extend(d.get("data") or [])
                cursor = (d.get("cursor") or {}).get("next")
                if not cursor:
                    break
            # 2.x returns the pages NEWEST-FIRST (the cursor goes into the
            # past). Reverse → oldest-first as with 1.x; `poll()`
            # expects the last assistant message as `a[-1]` (without reversal
            # the FIRST message would be read as "last" → finish=tool-calls
            # → "running" forever → step timeout, although the session was done).
            msgs.reverse()
            return [m for m in map(self._normalize_v2, msgs) if m is not None]
        _, d = self._req("GET", f"/session/{session_id}/message", directory=directory, timeout=15)
        return d if isinstance(d, list) else []

    def abort(self, session_id: str, directory: str) -> None:
        if self.version == "2":
            self._req("POST", f"/api/session/{session_id}/interrupt", {}, timeout=10)
        else:
            self._req("POST", f"/session/{session_id}/abort", {}, directory=directory, timeout=10)

    @staticmethod
    def _normalize_v2(m: dict) -> dict | None:
        """2.x flat message → 1.x envelope `{info, parts}` (assistant only).

        Tool parts: 2.x has `name` as a field; 1.x code reads the name from the
        JSON `text` field → it is synthesized. `state.status`
        (`completed`/`error`/`running`) is identical in both versions.
        """
        if m.get("type") != "assistant":
            return None
        parts: list[dict] = []
        for p in m.get("content") or []:
            t = p.get("type")
            if t == "text":
                parts.append({"type": "text", "text": p.get("text", "")})
            elif t == "tool":
                parts.append(
                    {
                        "type": "tool",
                        "text": json.dumps({"name": p.get("name", "tool")}),
                        "state": p.get("state") or {},
                    }
                )
        return {
            "info": {
                "role": "assistant",
                "tokens": m.get("tokens") or {},
                "cost": m.get("cost") or 0,
                "finish": m.get("finish"),
                "error": m.get("error"),
                "time": m.get("time") or {},
            },
            "parts": parts,
        }


class OpenCodeBackend:
    """Agent backend: opencode serve (HTTP API, 1.x and 2.x).

    SPIKE: docs/spike-opencode.md. One serve process serves many projects —
    the workdir goes per request (1.x) or at session creation (2.x).
    Polling instead of SSE: tick-oriented, idempotent, enough for the conductor
    base. The server version is detected lazily and cached; 2.x needs
    basic auth (`FABRIK_AGENT_PASSWORD`).
    """

    name = "opencode"

    def __init__(
        self,
        endpoint: str,
        model: str,
        stale_after: int = 900,
        password: str | None = None,
        username: str = "opencode",
        variant: str | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.stale_after = stale_after
        self.password = password
        self.username = username
        prov, _, mid = model.partition("/")
        self.model = {"providerID": prov, "id": mid} if prov and mid else {"providerID": "ollama", "id": model}
        if variant:  # Model variant from the opencode config (e.g. reasoning effort); otherwise server default
            self.model["variant"] = variant
        self._api: _ServeApi | None = None
        self._probe_errors: list[Exception] = []

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> OpenCodeBackend:
        password = str(step.get("password") or os.environ.get("FABRIK_AGENT_PASSWORD", "")).strip() or None
        username = str(step.get("username") or os.environ.get("FABRIK_AGENT_USERNAME", "")).strip() or "opencode"
        variant = str(step.get("variant") or "").strip() or None
        return cls(endpoint, model, stale_after=stale_after, password=password, username=username, variant=variant)

    # -- Versions-Erkennung ----------------------------------------------
    def _detect(self) -> _ServeApi | None:
        """Detects the server version (lazy, cached). None = not reachable.

        2.x: `GET /api/info` → `{"version": …}` (auth needed if a password is set).
        1.x: `GET /api/health` → `{"healthy": true}`.
        """
        if self._api is not None:
            return self._api
        self._probe_errors = []
        for version, path, check in (
            ("2", "/api/info", lambda d: bool(d.get("version"))),
            ("1", "/api/health", lambda d: d.get("healthy") is True),
        ):
            try:
                api = _ServeApi(self.endpoint, version, self.password, self.username)
                s, d = api._req("GET", path, timeout=5)
                if s == 200 and check(d):
                    self._api = api
                    return api
            except Exception as e:
                self._probe_errors.append(e)
        return None

    def health(self) -> bool:
        api = self._detect()
        return api is not None and api.health()

    def diagnose(self) -> str | None:
        """None = healthy; otherwise a human-readable diagnosis for the health gate.

        Distinguishes the two most common cases (from practice):
        - server not running (port not in use) → start hint incl. default port
        - server running, but auth fails (401) → password-mismatch hint
        """
        if self._detect() is not None:
            return None
        base = _t("backends.opencode.opencode_serve_not_reachable", v1=self.endpoint)
        if any(isinstance(e, urllib.error.HTTPError) and e.code == 401 for e in self._probe_errors):
            return _t("backends.opencode.server_answers_but_authentication_failed", base=base)
        return _t("backends.opencode.is_opencode_serve_running_default", base=base, v2=self.endpoint)

    def unhealthy_kind(self) -> FailureKind:
        """Abgelehnte Auth (401/403) → `environment`; Server nicht erreichbar → `transient`."""
        kinds = {classify_exception(e).kind for e in self._probe_errors}
        return FailureKind.ENVIRONMENT if FailureKind.ENVIRONMENT in kinds else FailureKind.TRANSIENT

    def _require_api(self) -> _ServeApi:
        api = self._detect()
        if api is None:
            hint = "" if self.password else " (2.x: FABRIK_AGENT_PASSWORD setzen?)"
            raise RuntimeError(_t("backends.opencode.opencode_serve_not_reachable_2", v1=self.endpoint, hint=hint))
        return api

    # -- Port-Interface ----------------------------------------------------
    def create_task(self, workdir: str, title: str, permissions: list | None = None) -> str:
        api = self._require_api()
        return api.create_session(workdir, title, self.model, permissions or _default_permissions())

    def send_prompt(self, session_id: str, prompt: str, directory: str) -> None:
        self._require_api().send_prompt(session_id, prompt, directory)

    def poll(self, session_id: str, directory: str) -> dict:
        """→ {status: running|done|failed, text, tokens, cost, tools}

        Status logic (empirically verified, opencode 1.19; 2.x shape is
        normalized beforehand):
        - no assistant message / finish=None / finish="tool-calls" → running
          (tool-calls = tool phase running, next message follows)
        - finish set (stop/length/…) OR info.error → message completed:
          substance (completed tool part OR real final text) → done,
          otherwise failed (e.g. LLM 500 → empty/wrong message).
        - A malformed tool call as text (3b model artifact) does not count as text.
        """
        msgs = self._require_api().fetch_messages(session_id, directory)
        a = [m for m in msgs if m.get("info", {}).get("role") == "assistant"]
        tin = sum(m.get("info", {}).get("tokens", {}).get("input", 0) for m in msgs)
        tout = sum(m.get("info", {}).get("tokens", {}).get("output", 0) for m in msgs)
        cost = sum(m.get("info", {}).get("cost") or 0 for m in msgs)
        tools = []
        for m in a:
            for p in m.get("parts", []):
                if p.get("type") == "tool":
                    try:
                        tools.append(json.loads(p.get("text", "{}")).get("name", "tool"))
                    except Exception:
                        tools.append("tool")
        base = {"tokens": {"input": tin, "output": tout}, "cost": cost, "tools": tools}
        if not a:
            return {**base, "status": "running", "text": ""}
        last = a[-1]
        info = last.get("info", {})
        last_txt = " ".join(x.get("text", "") for x in last.get("parts", []) if x.get("type") == "text").strip()
        if info.get("error"):
            return {**base, "status": "failed", "text": str(info["error"])[:300]}
        fin = info.get("finish")
        if fin is None or fin == "tool-calls":
            # finish=None WITHOUT parts = model never started → staleness check
            # (dead session, e.g. serve crash). tool-calls = tool phase running,
            # may take long → no staleness check (step timeout is the backstop).
            if fin is None and not last.get("parts"):
                created = (info.get("time") or {}).get("created")
                if created:
                    age = time.time() - created / 1000
                    # CPU-Boxen: erstes Token kann >5 Min dauern (Prompt-Processing) —
                    # Threshold konfigurierbar (step.stale_after / FABRIK_AGENT_STALE_AFTER)
                    if age > self.stale_after:
                        # `retryable`: a hanging model/server is transient —
                        # a fresh session heals it (agent step retry, ADR 0035).
                        return {
                            **base,
                            "status": "failed",
                            "retryable": True,
                            "text": _t("backends.opencode.session_inactive_for_s_no", v1=int(age)),
                        }
            return {**base, "status": "running", "text": last_txt}
        # abgeschlossene Message: Substanz-Check
        t = last_txt.strip()
        if t:
            # Tool-call artifact (also malformed, 3b model): text starts like
            # a tool-call JSON → no substance
            if t.startswith("{") and '"name"' in t[:80] and '"parameters"' in t[:160]:
                t = ""
            else:
                try:
                    j = json.loads(t)
                    if isinstance(j, dict) and "name" in j and "parameters" in j:
                        t = ""
                except (json.JSONDecodeError, ValueError):
                    pass
        # Substance check (tool-status based, verified on real sessions):
        # - completed tool part anywhere → done (action executed)
        # - NO completed tool, but tool error + final text → failed
        # (model "narrates" a failed action — not trustworthy)
        # - NO tool, but real final text → done
        # - otherwise → failed (empty/wrong messages, e.g. LLM 500)
        completed_tool = False
        err_tool = False
        for m2 in a:
            for p in m2.get("parts", []):
                if p.get("type") != "tool":
                    continue
                st = (p.get("state") or {}).get("status")
                if st == "completed":
                    completed_tool = True
                elif st == "error":
                    err_tool = True
        if completed_tool:
            return {**base, "status": "done", "text": last_txt}
        if err_tool and t:
            return {
                **base,
                "status": "failed",
                "text": "Tool-Call fehlgeschlagen (state=error), Final-Text unzuverlaessig",
            }
        if t:
            return {**base, "status": "done", "text": last_txt}
        return {
            **base,
            "status": "failed",
            "text": _t("backends.opencode.session_without_substance_empty_wrong"),
        }

    def transcript(self, session_id: str, directory: str) -> list[dict]:
        """Complete assistant dialog `[{info, parts}]` (oldest first).

        Serves the run log: the agent step emits new messages live, so
        prompt answers + tool calls end up in `runs/<project>/<id>.log`.
        """
        return self._require_api().fetch_messages(session_id, directory)

    def cancel(self, session_id: str, directory: str) -> None:
        with contextlib.suppress(Exception):
            self._require_api().abort(session_id, directory)
