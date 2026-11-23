"""opencode-Backend: opencode serve (HTTP-API, 1.x und 2.x).

SPIKE: docs/spike-opencode.md. Ein serve-Prozess bedient viele Projekte —
das Workdir geht pro Request (1.x: `?directory=`) bzw. bei der
Session-Erstellung (2.x: `location`). Polling statt SSE: tick-orientiert,
idempotent, für die Conductor-Basis genug.

Versions-Erkennung (lazy, gecacht): `GET /api/info` → 2.x, sonst
`GET /api/health` → 1.x. 2.x verlangt HTTP-Basic-Auth
(`FABRIK_AGENT_PASSWORD`; der Server generiert ein Passwort, wenn
`OPENCODE_SERVER_PASSWORD` nicht gesetzt ist). 2.x-Messages (flach:
`type`, `content`, top-level `tokens/cost/…`) werden in die 1.x-Envelope
(`{info, parts}`) normalisiert, damit `poll()` eine gemeinsame
Statuslogik hat.

Mit `FABRIK_SANDBOX=1` wählt die Registry stattdessen den One-Shot-Adapter
:class:`~fabrik.backends.opencode_cli.OpenCodeCliBackend` (eigene opencode-Instanz im
Sandbox-Container, Config aus einer LLM-Verbindung) — kein geteilter `opencode serve` auf dem Host.
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
from ..runmodel import FailureKind
from ..tokens import TokenCounts


def _default_permissions() -> list[dict]:
    """Default-Permission-Rules (1.x-Format: `permission`/`pattern`/`action`).

    Containment-Modell: `edit`-Patterns sind RELATIV zur Session-Workdir;
    Pfade DRAUSSEN triggern `external_directory` (Default: ask → in headless
    serve = deny). File-Tools sind damit auf die Workdir gefangen.
    `bash "*` ist bewusst weit (Agenten brauchen Shell) — ECHTE Isolation
    (non-root User, Container, VM) ist eine DEPLOYMENT-Voraussetzung,
    keine Config-Option. Override: step.permissions.
    """
    return [
        {"permission": "edit", "pattern": "*", "action": "allow"},
        {"permission": "bash", "pattern": "*", "action": "allow"},
    ]


def opencode_tokens(t: object) -> TokenCounts:
    """opencode-``tokens`` (``input``, ``output``, ``reasoning``, ``cache.read``/``cache.write``) → TokenCounts.

    opencode zählt Cache-Tokens nicht in ``input`` — sie kommen dazu; Reasoning zählt zum Output."""
    if not isinstance(t, dict):
        return TokenCounts()
    raw_cache = t.get("cache")
    cache: dict = raw_cache if isinstance(raw_cache, dict) else {}
    read, write = int(cache.get("read") or 0), int(cache.get("write") or 0)
    return TokenCounts(
        input=int(t.get("input") or 0) + read + write,
        output=int(t.get("output") or 0) + int(t.get("reasoning") or 0),
        cache_read=read,
        cache_write=write,
    )


class _ServeApi:
    """Transport + Shape-Normalisierung für opencode serve (1.x / 2.x).

    `fetch_messages` liefert IMMER die 1.x-Envelope `[{info, parts}]` —
    2.x-Flach-Messages werden in `_normalize_v2` umgebogen, damit
    `OpenCodeBackend.poll()` versionsoagnostisch bleibt.

    Sicherheit: TLS-Verifikation bleibt immer aktiv (urllib-Default —
    niemals deaktivieren). Credentials gehen nur im Authorization-Header
    und werden nie geloggt.
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
            # 2.x: Workdir über `location` (an die Session gebunden), Rules
            # heißen `action`/`resource`/`effect`, Response in `data`-Wrapper.
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
            raise RuntimeError(f"opencode: Session-Create ohne id: {str(d)[:200]}")
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
        """→ 1.x-Envelope `[{info, parts}]`, IMMER älteste-zuerst.

        2.x: paginiert + normalisiert; die API-Seiten sind neueste-zuerst
        sortiert und werden umgekehrt, damit beide Versionen dieselbe
        Reihenfolge liefern (`poll()` liest die letzte als `a[-1]`).
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
            # 2.x liefert die Seiten NEUESTE-ZUERST (der Cursor geht in die
            # Vergangenheit). Umkehren → älteste-zuerst wie bei 1.x; `poll()`
            # erwartet die letzte Assistant-Message als `a[-1]` (ohne Umkehr
            # würde die ERSTE Message als "letzte" gelesen → finish=tool-calls
            # → ewig "running" → Step-Timeout, obwohl die Session fertig war).
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
        """2.x-Flach-Message → 1.x-Envelope `{info, parts}` (nur Assistant).

        Tool-Parts: 2.x hat `name` als Feld; 1.x-Code liest den Namen aus dem
        JSON-`text`-Feld → wird synthetisiert. `state.status`
        (`completed`/`error`/`running`) ist in beiden Versionen identisch.
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
    """Agent-Backend: opencode serve (HTTP-API, 1.x und 2.x).

    SPIKE: docs/spike-opencode.md. Ein serve-Prozess bedient viele Projekte —
    das Workdir geht pro Request (1.x) bzw. bei der Session-Erstellung (2.x).
    Polling statt SSE: tick-orientiert, idempotent, für die Conductor-Basis
    genug. Die Server-Version wird lazy erkannt und gecacht; 2.x braucht
    Basic-Auth (`FABRIK_AGENT_PASSWORD`).
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
        if variant:  # Modell-Variante aus der opencode-Config (z. B. Reasoning-Aufwand); sonst Server-Default
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
        """Erkennt die Server-Version (lazy, gecacht). None = nicht erreichbar.

        2.x: `GET /api/info` → `{"version": …}` (Auth nötig, falls Passwort).
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
        """None = gesund; sonst menschenlesbare Diagnose für das Health-Gate.

        Unterscheidet die zwei häufigsten Fälle (aus der Praxis):
        - Server läuft nicht (Port nicht belegt) → Start-Hint inkl. Default-Port
        - Server läuft, aber Auth schlägt fehl (401) → Passwort-Mismatch-Hint
        """
        if self._detect() is not None:
            return None
        base = f"opencode serve nicht erreichbar: {self.endpoint}"
        if any(isinstance(e, urllib.error.HTTPError) and e.code == 401 for e in self._probe_errors):
            return f"{base} — Server antwortet, aber Auth fehlgeschlagen (401): FABRIK_AGENT_PASSWORD prüfen"
        return f"{base} — läuft `opencode serve`? (Default-Port 4096, konfiguriert: {self.endpoint})"

    def unhealthy_kind(self) -> FailureKind:
        """Abgelehnte Auth (401/403) → `environment`; Server nicht erreichbar → `transient`."""
        kinds = {classify_exception(e).kind for e in self._probe_errors}
        return FailureKind.ENVIRONMENT if FailureKind.ENVIRONMENT in kinds else FailureKind.TRANSIENT

    def _require_api(self) -> _ServeApi:
        api = self._detect()
        if api is None:
            hint = "" if self.password else " (2.x: FABRIK_AGENT_PASSWORD setzen?)"
            raise RuntimeError(f"opencode serve nicht erreichbar: {self.endpoint}{hint}")
        return api

    # -- Port-Interface ----------------------------------------------------
    def create_task(self, workdir: str, title: str, permissions: list | None = None) -> str:
        api = self._require_api()
        return api.create_session(workdir, title, self.model, permissions or _default_permissions())

    def send_prompt(self, session_id: str, prompt: str, directory: str) -> None:
        self._require_api().send_prompt(session_id, prompt, directory)

    def poll(self, session_id: str, directory: str) -> dict:
        """→ {status: running|done|failed, text, tokens, cost, tools}

        Statuslogik (empirisch verifiziert, opencode 1.19; 2.x-Shape wird
        vorab normalisiert):
        - keine Assistant-Message / finish=None / finish="tool-calls" → running
          (tool-calls = Tool-Phase läuft, nächste Message folgt)
        - finish gesetzt (stop/length/…) ODER info.error → Message abgeschlossen:
          Substanz (erledigtes Tool-Teil ODER echter Final-Text) → done,
          sonst failed (z. B. LLM-500 → leere/falsche Message).
        - Malformated Tool-Call als Text (3b-Modell-Artefakt) zählt nicht als Text.
        """
        msgs = self._require_api().fetch_messages(session_id, directory)
        a = [m for m in msgs if m.get("info", {}).get("role") == "assistant"]
        tokens = sum((opencode_tokens(m.get("info", {}).get("tokens")) for m in msgs), TokenCounts())
        cost = sum(m.get("info", {}).get("cost") or 0 for m in msgs)
        tools = []
        for m in a:
            for p in m.get("parts", []):
                if p.get("type") == "tool":
                    try:
                        tools.append(json.loads(p.get("text", "{}")).get("name", "tool"))
                    except Exception:
                        tools.append("tool")
        base = {"tokens": tokens.as_poll(), "cost": cost, "tools": tools}
        if not a:
            return {**base, "status": "running", "text": ""}
        last = a[-1]
        info = last.get("info", {})
        last_txt = " ".join(x.get("text", "") for x in last.get("parts", []) if x.get("type") == "text").strip()
        if info.get("error"):
            return {**base, "status": "failed", "text": str(info["error"])[:300]}
        fin = info.get("finish")
        if fin is None or fin == "tool-calls":
            # finish=None OHNE Parts = Modell hat nie angefangen → Staleness-Check
            # (tote Session, z. B. serve-Absturz). tool-calls = Tool-Phase läuft,
            # darf lange dauern → kein Staleness-Check (Step-Timeout ist Backstop).
            if fin is None and not last.get("parts"):
                created = (info.get("time") or {}).get("created")
                if created:
                    age = time.time() - created / 1000
                    # CPU-Boxen: erstes Token kann >5 Min dauern (Prompt-Processing) —
                    # Threshold konfigurierbar (step.stale_after / FABRIK_AGENT_STALE_AFTER)
                    if age > self.stale_after:
                        # `retryable`: hängendes Modell/Server ist transient —
                        # eine frische Session heilt es (Agent-Step-Retry, ADR 0035).
                        return {
                            **base,
                            "status": "failed",
                            "retryable": True,
                            "text": f"Session inaktiv seit {int(age)}s (keine Aktivität)",
                        }
            return {**base, "status": "running", "text": last_txt}
        # abgeschlossene Message: Substanz-Check
        t = last_txt.strip()
        if t:
            # Tool-Call-Artefakt (auch malformiert, 3b-Modell): Text beginnt wie
            # ein Tool-Call-JSON → keine Substanz
            if t.startswith("{") and '"name"' in t[:80] and '"parameters"' in t[:160]:
                t = ""
            else:
                try:
                    j = json.loads(t)
                    if isinstance(j, dict) and "name" in j and "parameters" in j:
                        t = ""
                except (json.JSONDecodeError, ValueError):
                    pass
        # Substanz-Check (Tool-Status-basiert, an realen Sessions verifiziert):
        # - completed-Tool-Part irgendwo → done (Action ausgeführt)
        # - KEIN completed-Tool, aber Tool-Error + Final-Text → failed
        #   (Modell "erzaehlt" eine fehlgeschlagene Aktion — nicht vertrauenswuerdig)
        # - KEIN Tool, aber echter Final-Text → done
        # - sonst → failed (leere/falsche Messages, z. B. LLM-500)
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
            "text": "Session ohne Substanz (leere/falsche Assistant-Messages, z. B. LLM-500)",
        }

    def transcript(self, session_id: str, directory: str) -> list[dict]:
        """Vollständiger Assistant-Dialog `[{info, parts}]` (älteste zuerst).

        Dient dem Run-Log: der Agent-Step emittet neue Messages live, damit
        Prompt-Antworten + Tool-Calls in `runs/<projekt>/<id>.log` landen.
        """
        return self._require_api().fetch_messages(session_id, directory)

    def cancel(self, session_id: str, directory: str) -> None:
        with contextlib.suppress(Exception):
            self._require_api().abort(session_id, directory)
