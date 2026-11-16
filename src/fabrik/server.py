"""HTTP API + UI (ThreadingHTTPServer, Default 127.0.0.1; Auth/Rollen: ADR 0030)."""

from __future__ import annotations

import contextlib
import hmac
import json
import mimetypes
import os
import signal
import threading
import time
import urllib.parse
from dataclasses import asdict
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

from . import (
    alerts,
    auth,
    branches,
    config,
    connections,
    metrics,
    oidc,
    pause,
    procs,
    retry_chains,
    runsearch,
    sandbox,
    schedule,
    secrets,
    sessionlog,
    stream,
    webhook,
)
from . import llm as llm_registry
from .chat import _cancel_chat, _chat, _chat_resume, _finish_chat, _register_chat, requeue_approved_runs
from .engine import pool_status, queued_trigger_snapshot, requeue_queued_runs, start_priority_run
from .gitops import cleanup_orphaned_worktrees
from .runmeta import run_meta
from .runs import (
    LOCK_KEY_SEP,
    get_run,
    get_run_log,
    get_run_log_path,
    last_run_per_loop,
    list_runs,
    load_records,
    max_parallel_for,
    parse_runs,
    reap_orphaned_runs,
    select_pending_approvals,
    slot_snapshot,
)
from .scheduler import Scheduler
from .spec_expand import _load_template
from .specs import SpecConflict, SpecUpdate, load_projects, load_spec_raw, save_spec, spec_hash, spec_path

# Parameter, die GET /api/runs in den paginierten Such-Modus schalten.
_RUN_SEARCH_PARAMS = ("q", "from", "to", "status", "loop", "cursor", "failure_kind")
LEGACY_RUNS_LIMIT = 50  # Default `limit` der einfachen /api/runs-Liste


def _chains_or_empty() -> list[retry_chains.RetryChain]:
    """Alle Retry-Ketten für die Übersicht; ein unlesbarer Store (ADR 0036)
    legt das Dashboard nicht lahm — leer + Log."""
    try:
        return retry_chains.ChainStore().all()
    except (OSError, ValueError) as e:
        print(f"[fabrik] Retry-Ketten nicht lesbar: {e}")
        return []


# Routen mit eigener HMAC-Auth, die über den Tunnel-Host erreichbar bleiben.
INTAKE_PATH_PREFIXES = (webhook.WEBHOOK_PATH_PREFIX, alerts.ALERT_PATH_PREFIX)


class FabrikHandler(BaseHTTPRequestHandler):
    server_version = "fabrik/0.3"

    def log_message(self, format: str, *args: object) -> None:  # leiser (Name wie in der Basisklasse)
        pass

    _who: auth.Principal | None = None  # Aufrufer des aktuellen Requests (None = auth none/anonym)

    def _json(self, obj, code=200, headers: dict[str, str] | None = None):
        body = json.dumps(obj, indent=1, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self._cors_headers()
        self.end_headers()
        # Client hat die Verbindung schon geschlossen (z. B. Cancel)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _read_json(self) -> dict[str, Any] | None:
        """Request-Body als JSON-Objekt lesen (leer → ``{}``).

        Bei ungültigem JSON oder ungültigem ``Content-Length`` (nichtnumerisch/negativ)
        wird selbst ein 400 gesendet und ``None`` geliefert."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                raise ValueError(length)
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except (ValueError, UnicodeDecodeError):  # JSONDecodeError ist ein ValueError
            self._json({"error": "ungültiges JSON"}, 400)
            return None
        if not isinstance(payload, dict):
            self._json({"error": "ungültiges JSON"}, 400)
            return None
        return payload

    def _query_params(self) -> dict[str, str]:
        """Query-String des Requests als dict (URL-dekodiert, letzter Wert gewinnt)."""
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query, keep_blank_values=True))

    def _origin_ok(self) -> bool:
        """POST-Requests nur aus dem eigenen Origin (oder mit Token) zulassen.

        Schützt die lokale API gegen Drive-by-RCE: eine beliebige Webseite im
        Browser des Operators könnte sonst `fetch('http://localhost:PORT/api/runs')`
        feuern und Shell-Steps auslösen. Ohne CORS-Header blockiert der Browser
        Cross-Origin-POSTs (same-origin policy)."""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True  # CLI / curl / Same-Origin ohne Origin-Header
        try:
            host = self.headers.get("Host", "")
            return origin.rstrip("/") in (f"http://{host}", f"https://{host}")
        except Exception:
            return False

    def _tunnel_blocked(self, path: str) -> bool:
        """Requests über den Tunnel-Host erreichen nur die Webhook-Route."""
        return webhook.tunnel_matches(self.headers.get("Host", "")) and not path.startswith(INTAKE_PATH_PREFIXES)

    def _cors_headers(self):
        """CORS nur für den konfigurierten Tunnel-Origin (nie `*`)."""
        allowed = webhook.tunnel_origin()
        on_webhook = self.path.startswith(webhook.WEBHOOK_PATH_PREFIX)
        if allowed and on_webhook and self.headers.get("Origin", "").rstrip("/") == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        """CORS-Preflight: nur für die Webhook-Route und den Tunnel-Origin."""
        path = self.path.split("?")[0]
        if self._tunnel_blocked(path):
            self._json({"error": "unbekannte Route"}, 404)
            return
        self.send_response(204)
        if path.startswith(webhook.WEBHOOK_PATH_PREFIX):
            self._cors_headers()
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", f"Content-Type, {webhook.SIGNATURE_HEADER}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_intake(self, path: str, prefix: str) -> tuple[str, str, bytes]:
        """Gemeinsamer Intake-Kopf für Webhook/Alert: `<prefix><projekt>/<loop>` zerlegen
        und den Raw-Body lesen. Ungültiger Pfad → 401 (kein Orakel, wie fehlende Auth)."""
        parts = path[len(prefix) :].split("/")
        length = webhook.parse_content_length(self.headers.get("Content-Length"))
        body = self.rfile.read(length) if length else b""
        if len(parts) != 2 or not all(parts):
            raise webhook.WebhookError(401, "Signatur ungültig")
        return parts[0], parts[1], body

    def _webhook(self, path: str):
        """POST /api/webhook/<projekt>/<loop>: HMAC prüfen, Payload mappen, Run starten."""
        try:
            name, lname, body = self._read_intake(path, webhook.WEBHOOK_PATH_PREFIX)
            found = webhook.find_trigger(load_projects().get(name), lname)
            trig = webhook.authenticate(found, body, self.headers.get(webhook.SIGNATURE_HEADER, ""))
            vars_ = webhook.map_payload(body, trig.get("map") or {})
        except (webhook.WebhookError, ValueError) as e:
            code = e.status if isinstance(e, webhook.WebhookError) else 400
            self._json({"error": str(e)}, code)
            return
        self._json(*self._start_run(name, lname, "webhook", vars_ or None, "on_webhook", actor="webhook"))

    def _alert(self, path: str):
        """POST /api/alert/<projekt>/<loop>: HMAC prüfen, Alert normalisieren, Triage-Run starten."""
        try:
            name, lname, body = self._read_intake(path, alerts.ALERT_PATH_PREFIX)
            found = alerts.find_trigger(load_projects().get(name), lname)
            trig = alerts.authenticate(
                found,
                body,
                self.headers.get(webhook.SIGNATURE_HEADER, ""),
                self.headers.get(alerts.SENTRY_SIGNATURE_HEADER, ""),
            )
            alert = alerts.parse_alert(trig["source"], body)
        except webhook.WebhookError as e:
            self._json({"error": str(e)}, e.status)
            return
        if alert is None:
            self._json({"ignored": True, "reason": "kein Incident (Zustand != ALARM)"}, 200)
            return
        self._json(*self._start_run(name, lname, "alert", alert.to_vars(), alerts.TRIGGER_TYPE, actor="alert"))

    def _auth_ok(self) -> bool:
        """Optionales Bearer-Token (FABRIK_TOKEN). Leert → nur Origin-Check."""
        token = os.environ.get("FABRIK_TOKEN", "").strip()
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(auth, f"Bearer {token}")

    @property
    def _actor(self) -> str | None:
        """Audit-Name des Aufrufers (``started_by``/``approved_by``); None ohne Auth."""
        return self._who.name if self._who else None

    @property
    def _audit_actor(self) -> str:
        """Auslöser für Audit-Logs: ``user:<name>``, ohne Auth ``api:<ip>``."""
        return f"user:{self._actor}" if self._actor else f"api:{self.client_address[0]}"

    def _gate(self, method: str, path: str) -> bool:
        """Auth + Rolle prüfen (ADR 0030) und ``self._who`` setzen.

        Sendet bei Ablehnung selbst 401/403 und liefert dann False.
        Modus ``none``: GET offen, POST wie bisher Origin + optionales Token."""
        self._who = auth.token_principal(self.headers.get("Authorization", ""))
        if path.startswith(INTAKE_PATH_PREFIXES):
            # Webhooks/Alerts use their own raw-body HMAC authentication (ADR 0027/0030),
            # not browser/session authentication.
            return True
        writes = method not in ("GET", "HEAD")
        if auth.mode() == auth.MODE_NONE:
            if writes and not (self._origin_ok() and self._auth_ok()):
                self._json({"error": "zugriff verweigert (Origin/Token)"}, 403)
                return False
            return True
        # Token-Aufrufer (CLI/Automation) sind kein Browser: kein Origin-/CSRF-Zwang.
        if writes and self._who is None and not self._origin_ok():
            self._json({"error": "zugriff verweigert (Origin)"}, 403)
            return False
        self._who = self._who or self._session()
        required = auth.required_role(method, path)
        if required is None:
            return True
        if self._who is None:
            self._json({"error": "login nötig"}, 401)
        elif not self._who.allows(required):
            self._json({"error": f"Rolle '{required}' nötig"}, 403)
        elif writes and not auth.csrf_ok(self._who, self.headers.get(auth.CSRF_HEADER, "")):
            self._json({"error": "CSRF-Token ungültig"}, 403)
        else:
            return True
        return False

    def _session(self) -> auth.Principal | None:
        return auth.session_principal(auth.cookie_value(self.headers.get("Cookie", "")))

    def _session_payload(self) -> dict:
        who = self._session() if auth.mode() != auth.MODE_NONE else None
        return {
            "mode": auth.mode(),
            "user": who.name if who else None,
            "role": who.role if who else None,
            "csrf": who.csrf if who else None,
        }

    def _login(self):
        """POST /api/login {user, password} → Session-Cookie + CSRF-Token."""
        payload = self._read_json()
        if payload is None:
            return
        if auth.mode() != auth.MODE_LOCAL:
            self._json({"error": "Login nur im Modus FABRIK_AUTH=local"}, 400)
            return
        result = auth.login(str(payload.get("user", "")), str(payload.get("password", "")))
        if result is None:
            self._json({"error": "Zugangsdaten ungültig"}, 401)
            return
        sid, who = result
        self._json(
            {"user": who.name, "role": who.role, "csrf": who.csrf},
            headers={"Set-Cookie": auth.set_cookie_header(sid)},
        )

    def _redirect(self, location: str, cookies: tuple[str, ...] = ()):
        """302 auf ``location``; ``cookies`` sind ``Set-Cookie``-Werte (je ein Header)."""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _oidc_login(self):
        """GET /api/login/oidc → 302 zum IdP (Authorization Code + PKCE, ADR 0046)."""
        try:
            start = oidc.begin_login(oidc.OidcConfig.from_env())
            self._redirect(start.url, (start.state_cookie(),))
        except oidc.OidcError as e:
            self._json({"error": str(e)}, 502)

    def _oidc_callback(self):
        """GET /api/oidc/callback?code&state → Session-Cookie + 302 auf die UI."""
        params = self._query_params()
        if params.get("error"):
            self._json(
                {"error": f"IdP meldet Fehler: {params['error']} {params.get('error_description', '')}".strip()}, 401
            )
            return
        try:
            cfg = oidc.OidcConfig.from_env()
            browser_state = auth.cookie_value(self.headers.get("Cookie", ""), oidc.STATE_COOKIE)
            name, role = oidc.complete_login(cfg, params.get("code", ""), params.get("state", ""), browser_state)
        except oidc.NoRoleError as e:
            self._json({"error": str(e)}, 403)
            return
        except oidc.OidcError as e:
            self._json({"error": str(e)}, 401)
            return
        sid, _ = auth.create_session(name, role)
        self._redirect("/", (auth.set_cookie_header(sid), oidc.clear_state_cookie()))

    def _logout(self):
        auth.logout(auth.cookie_value(self.headers.get("Cookie", "")))
        self._json({"ok": True}, headers={"Set-Cookie": auth.set_cookie_header("", clear=True)})

    def _start_run(
        self, name: str, lname: str, note: str, initial_vars: dict | None, trigger: str, actor: str | None = None
    ):
        """Run mit Initial-Variablen starten (einzige interne Start-Funktion).

        Gemeinsam für `POST /api/runs` und `on_webhook` (kein eigener Webhook-
        Code-Pfad, framework-v1-draft §1.3). Gibt `(payload, http_code)` zurück."""
        # Run-Id VOR dem Worker-Start generieren: der Response geht raus,
        # bevor der Worker den Record auf Disk schreibt. Die UI braucht die
        # Id, um den neuen Run per /api/run/<id> zu verfolgen — sonst
        # findet ihr "neuester Run des Loops" im Race den ALTEN Run.
        run_id = f"{config.now_id()}-{lname}"
        # Belegtes Projekt → einreihen statt 409; der Run wartet mit Vorrang
        # vor Trigger-Runs (ADR 0015).
        try:
            queued = start_priority_run(
                name, lname, trigger, run_id=run_id, note=note, initial_vars=initial_vars, actor=actor
            )
        except RuntimeError as e:
            # 5xx mit klarer Nachricht, damit Clients deterministisch reagieren.
            return {"error": f"Worker-Start fehlgeschlagen: {e}"}, 500
        payload = {"started": not queued, "queued": queued, "project": name, "loop": lname, "run_id": run_id}
        return payload, 202 if queued else 200

    def _static(self, rel: str):
        f = config.WEB_DIR / (rel or "index.html")
        # is_relative_to: `parent !=` würde auch Unterverzeichnisse unter WEB_DIR
        # verweigern (z. B. web/css/app.css).
        if not f.is_file() or not f.resolve().is_relative_to(config.WEB_DIR.resolve()):
            self.send_response(404)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Dev-Tool: UI-Dateien (app.js, style.css, …) nie heuristisch cachen,
        # sonst läuft der Browser nach einem Code-Change weiter mit der alten
        # Version, bis man manuell hart neu lädt. no-cache → bei jedem Load
        # neu validieren (ohne Validator hier: kompletter Re-Fetch, lokal schnell).
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if self._tunnel_blocked(path):
            self._json({"error": "unbekannte Route"}, 404)
            return
        if not self._gate("GET", path):
            return
        if path == "/api/session":
            self._json(self._session_payload())
        elif path == oidc.LOGIN_PATH and auth.mode() == auth.MODE_OIDC:
            self._oidc_login()
        elif path == oidc.CALLBACK_PATH and auth.mode() == auth.MODE_OIDC:
            self._oidc_callback()
        elif path == "/api/projects":
            self._json(self._projects_payload())
        elif path == "/api/schedule":
            self._json(self._schedule_payload())
        elif path == "/api/runs":
            self._runs_response()
        elif path.startswith("/api/run/") and path.endswith("/stream"):
            self._run_stream(path[len("/api/run/") : -len("/stream")])
        elif path.startswith("/api/run/") and path.endswith("/transcript"):
            self._run_transcript(path[len("/api/run/") : -len("/transcript")])
        elif path.startswith("/api/run/") and path.endswith("/log"):
            # Rohes Run-Log (aus runs/<projekt>/<id>.log) als Plain-Text.
            # Muss VOR dem generischen /api/run/<id>-Zweig geprüft werden.
            # Optional ?lines=N → nur die letzten N Zeilen (Tail).
            rid = path[len("/api/run/") : -len("/log")]
            log = get_run_log(rid, self._query_param("project"))
            if log is None:
                self._json({"error": "Log nicht gefunden"}, 404)
                return
            params = self._query_params()
            if params.get("lines"):
                with contextlib.suppress(ValueError):
                    log = "\n".join(log.splitlines()[-max(1, int(params["lines"])) :])
            body = log.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)
        elif path.startswith("/api/run/"):
            rid = path[len("/api/run/") :]
            r = get_run(rid, self._query_param("project"))
            if r:
                retry = self._run_retry_info(r)
                if retry:
                    r = {**r, "retry": retry}
                r = {**r, "meta": run_meta(load_projects().get(str(r.get("project") or "")), r)}
            self._json(r, 200 if r else 404)
        elif path == "/api/health":
            self._json({"ok": True, "time": config.utcnow().isoformat()})
        elif path == "/api/metrics":
            # KPI-Export (Prometheus-Textformat) für Scrape-Tools.
            body = metrics.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)
        elif path == "/api/llm":
            self._json(self._llm_payload())
        elif path == auth.CONNECTIONS_PREFIX:
            self._json(self._connections_payload())
        elif path.startswith("/api/projects/") and path.endswith("/spec.yaml"):
            name = path[len("/api/projects/") : -len("/spec.yaml")]
            p = spec_path(name)
            if p.is_file():
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/yaml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)
            else:
                self._json({"error": "unbekanntes Projekt"}, 404)
        elif path.startswith("/api/projects/") and path.endswith("/spec"):
            name = path[len("/api/projects/") : -len("/spec")]
            specs = load_projects()
            if name not in specs:
                self._json({"error": "unbekanntes Projekt"}, 404)
                return
            p = spec_path(name, specs)
            if self._query_params().get("raw") == "1":
                # Rohe (nicht expandierte) Spec als Draft-Quelle für den
                # UI-Graph-Editor: vars wörtlich, use:/insert: un-inlined, alle
                # Loop-Schlüssel erhalten. + _hash für den Optimistischen Lock.
                if not p.is_file():
                    self._json({"error": "unbekanntes Projekt"}, 404)
                    return
                raw = load_spec_raw(p)
                raw["_hash"] = spec_hash(p)
                self._json(raw)
            else:
                expanded = dict(specs[name])
                expanded["_hash"] = spec_hash(p)
                self._json(expanded)
        elif path.startswith("/api/templates/") and path.endswith("/params"):
            # Template-Parameter (params-Block) für den UI-Graph-Editor:
            # zeigt welche with:-Keys ein Template kennt + deren Defaults.
            # ?project=<name> → Template-Auflösung wie bei der Expansion
            # (projektlokale <spec-ordner>/templates haben Vorrang).
            tname = path[len("/api/templates/") : -len("/params")]
            q = self._query_params()
            proj = q.get("project")
            if proj and proj not in load_projects():
                self._json({"error": "unbekanntes Projekt"}, 404)
                return
            proj_path = spec_path(proj) if proj else None
            try:
                tpl = _load_template(tname, proj_path)
            except (ValueError, OSError, yaml.YAMLError) as e:
                self._json({"error": str(e)}, 404)
                return
            self._json({"name": tname, "params": tpl.get("params") or {}})
        elif path.startswith("/api/projects/") and path.endswith("/branches"):
            name = path[len("/api/projects/") : -len("/branches")]
            spec = load_projects().get(name)
            if not spec or spec.get("_error"):
                self._json({"error": "unbekanntes Projekt"}, 404)
                return
            repo = spec.get("repo") or {}
            b = branches.list_branches(spec["root"]) if repo else []
            self._json({"branches": b, "current": repo.get("branch", "-")})
        elif path == "/api/retry-chains":
            # Retry-Ketten (ADR 0036) für die UI ("Abgelegt"-Liste). Optional
            # ?project=<name> filtert; ?state=<state> filtert den Zustand.
            params = self._query_params()
            proj = params.get("project")
            state = params.get("state")
            if state is not None:
                try:
                    state = retry_chains.ChainState(state)
                except ValueError:
                    self._json({"error": f"unbekannter state: {state!r}"}, 400)
                    return
            chains = [
                asdict(c)
                for c in _chains_or_empty()
                if (proj is None or c.project == proj) and (state is None or c.state is state)
            ]
            self._json({"chains": chains})
        else:
            self._static(path.lstrip("/"))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.startswith(webhook.WEBHOOK_PATH_PREFIX):
            # Eigene Auth (HMAC) statt Origin/Token — Aufrufer sind Server, keine Browser.
            self._webhook(path)
            return
        if path.startswith(alerts.ALERT_PATH_PREFIX):
            self._alert(path)  # eigene HMAC-Auth, wie Webhooks (ADR 0045)
            return
        if self._tunnel_blocked(path):
            self._json({"error": "unbekannte Route"}, 404)
            return
        if not self._gate("POST", path):
            return
        if path == "/api/login":
            self._login()
        elif path == "/api/logout":
            self._logout()
        elif path == "/api/runs":
            payload = self._read_json()
            if payload is None:
                return
            name, lname = payload.get("project"), payload.get("loop")
            if not name or not lname:
                self._json({"error": "project + loop nötig"}, 400)
                return
            note = payload.get("note", "")
            # Optionale Initiale-Variablen (K=V), z. B. issue_number für
            # issue-gesteuerte Loops — manuelle Runs haben sonst keinen
            # Issue-Kontext (der on_issue-Trigger liefert die Variablen).
            # Validierung VOR dem Claim: ein 400 darf nie einen Run-Slot
            # halten (der Handler-Thread würde sonst ohne Release sterben).
            raw_vars = payload.get("vars")
            if raw_vars is not None and not isinstance(raw_vars, dict):
                self._json({"error": "vars muss ein Objekt (K=V) sein"}, 400)
                return
            initial_vars = {str(k): str(v) for k, v in (raw_vars or {}).items()} or None

            blocked = pause.manual_block_reason(name, lname)
            if blocked:
                self._json({"error": blocked}, 409)
                return

            self._json(*self._start_run(name, lname, note, initial_vars, "manual", actor=self._actor))
        elif path == "/api/schedule/onetime":
            payload = self._read_json()
            if payload is None:
                return
            name, lname = payload.get("project"), payload.get("loop")
            if not name or not lname:
                self._json({"error": "project + loop nötig"}, 400)
                return
            try:
                when = schedule.parse_when(payload.get("when", ""), datetime.now())
                entry = schedule.add_onetime(name, lname, when, str(payload.get("note", "")))
            except schedule.NotFoundError as e:
                self._json({"error": str(e)}, 404)
                return
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"ok": True, "onetime": entry})
        elif path == "/api/chat":
            payload = self._read_json()
            if payload is None:
                return
            msg = str(payload.get("message", "")).strip()
            if not msg:
                self._json({"error": "message nötig"}, 400)
                return
            # Client kann eigene req_id mitschicken (damit er sofort canceln kann,
            # bevor die Antwort da ist); sonst generieren wir eine.
            req_id = str(payload.get("req_id", "")).strip() or config.now_id()
            project = str(payload.get("project", ""))
            cancel_event = _register_chat(req_id)  # sofort cancelbar, auch vor dem LLM-Start
            reply: str | None = None
            who = self._who
            done = threading.Event()

            def work():
                nonlocal reply
                try:
                    reply = _chat(msg, project, req_id=req_id, who=who)
                except Exception as e:
                    reply = f"✖ {e}"
                finally:
                    done.set()
                    _finish_chat(req_id)

            threading.Thread(target=work, daemon=True).start()
            # Auf den Worker warten, aber sofort aussteigen, wenn der User cancelt.
            # (Der LLM-Call läuft im Hintergrund zu Ende — Daemon-Thread, Ergebnis verworfen.)
            while not done.wait(timeout=0.2):
                if cancel_event.is_set():
                    self._json({"reply": "⊘ abgebrochen", "req_id": req_id})
                    return
            self._json({"reply": reply, "req_id": req_id})
        elif path == "/api/chat/cancel":
            payload = self._read_json()
            if payload is None:
                return
            rid = str(payload.get("req_id", "")).strip()
            if not rid:
                self._json({"error": "req_id nötig"}, 400)
                return
            ok = _cancel_chat(rid)
            self._json({"ok": ok, "cancelled": ok})
        elif path in ("/api/approve", "/api/reject"):
            payload = self._read_json()
            if payload is None:
                return
            name, run_id = payload.get("project"), payload.get("run_id")
            if not name or not run_id:
                self._json({"error": "project + run_id nötig"}, 400)
                return
            try:
                reply, resumed = _chat_resume(name, run_id, path == "/api/approve", actor=self._actor)
            except Exception as e:
                self._json({"error": str(e)}, 400)
                return
            # resumed: True nur, wenn der Resume-Worker gestartet wurde.
            # Die UI pollt den Run danach (mit needs_approval-Grace), weil der
            # Worker "running" erst kurz nach diesem Response auf Disk schreibt.
            # Projekt am Limit (max_parallel) → Freigabe wird eingereiht (ADR 0015), resumed
            # bleibt True. False (Thread-Start-Fehler) → UI zeigt die Begründung.
            self._json({"ok": True, "reply": reply, "resumed": resumed})
        elif path == "/api/llm":
            payload = self._read_json()
            if payload is None:
                return
            name = str(payload.get("provider", "")).strip()
            if not name:
                self._json({"error": "provider nötig"}, 400)
                return
            try:
                llm_registry.set_active(name)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json(self._llm_payload())
        elif path.startswith("/api/projects/") and path.endswith("/state"):
            name = path[len("/api/projects/") : -len("/state")]
            payload = self._read_json()
            if payload is None:
                return
            loop = str(payload.get("loop") or "") or None
            try:
                res = pause.set_state(
                    name,
                    loop,
                    str(payload.get("state", "")),
                    until=payload.get("until"),
                    actor=self._audit_actor,
                )
            except ValueError as e:
                self._json({"error": str(e)}, 404 if "unbekannt" in str(e) else 400)
                return
            self._json({"ok": True, **res})
        elif path.startswith("/api/projects/") and path.endswith("/branch"):
            name = path[len("/api/projects/") : -len("/branch")]
            payload = self._read_json()
            if payload is None:
                return
            branch = str(payload.get("branch", "")).strip()
            spec = load_projects().get(name)
            if not spec or spec.get("_error"):
                self._json({"error": "unbekanntes Projekt"}, 404)
                return
            if not spec.get("repo"):
                self._json({"error": "Projekt hat kein repo (kein Branch-Wechsel)"}, 400)
                return
            if not branch:
                self._json({"error": "branch nötig"}, 400)
                return
            available = branches.list_branches(spec["root"])
            if available and branch not in available:
                self._json({"error": f"Branch '{branch}' existiert nicht (verfügbar: {', '.join(available)})"}, 400)
                return
            branches.save_override(name, branch)
            self._json({"ok": True, "project": name, "branch": branch})
        elif path in ("/api/retry-chains/dismiss", "/api/retry-chains/retry-now"):
            # UI-Aktionen an einer Retry-Kette (ADR 0036, Entscheidung 4):
            # *verwerfen* (pending/exhausted → dismissed) bzw. *jetzt erneut*
            # (exhausted → sofortiger Run, volles Budget).
            payload = self._read_json()
            if payload is None:
                return
            name, lname, tkey = payload.get("project"), payload.get("loop"), payload.get("trigger_key")
            if not name or not lname or not tkey:
                self._json({"error": "project + loop + trigger_key nötig"}, 400)
                return
            if path.endswith("/dismiss"):
                chain = retry_chains.dismiss_chain(name, lname, tkey, actor=self._audit_actor)
                if chain is None:
                    self._json({"error": "Kette nicht gefunden (oder nicht pending/exhausted)"}, 404)
                    return
                self._json({"ok": True, "chain": asdict(chain)})
            else:
                try:
                    # Bare Name wie bei POST /api/runs: landet in started_by (ADR 0030).
                    run_id = retry_chains.retry_now(name, lname, tkey, actor=self._actor)
                except retry_chains.RetryBlocked as e:
                    self._json({"error": str(e)}, 409)
                    return
                if run_id is None:
                    self._json({"error": "Kette nicht gefunden (oder nicht exhausted)"}, 404)
                    return
                self._json({"ok": True, "run_id": run_id})
        else:
            self._json({"error": "unbekannte Route"}, 404)

    def do_PUT(self):
        path = self.path.split("?")[0]
        if self._tunnel_blocked(path):
            self._json({"error": "unbekannte Route"}, 404)
            return
        if not self._gate("PUT", path):
            return
        if path.startswith("/api/projects/") and path.endswith("/spec"):
            # Spec-Edit (Graph-View): Loops pro Loop mergen + löschen +
            # Top-Level-Felder (description, max_parallel) aktualisieren.
            # Validierung + atomarer Write + Optimistischer Lock liegen in
            # specs.save_spec (Kommentare bleiben erhalten).
            name = path[len("/api/projects/") : -len("/spec")]
            payload = self._read_json()
            if payload is None:
                return
            base_hash = payload.get("base_hash")
            if base_hash is not None and not isinstance(base_hash, str):
                self._json({"error": "base_hash muss ein String sein"}, 400)
                return
            try:
                update = SpecUpdate.from_payload({k: v for k, v in payload.items() if k != "base_hash"})
                saved = save_spec(name, update, base_hash=base_hash)
            except KeyError:
                self._json({"error": "unbekanntes Projekt"}, 404)
                return
            except SpecConflict as e:
                self._json({"error": str(e)}, 409)
                return
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"ok": True, "project": name, "spec": saved})
        elif path.startswith(auth.CONNECTIONS_PREFIX + "/"):
            self._connection_put(path[len(auth.CONNECTIONS_PREFIX) + 1 :])
        else:
            self._json({"error": "unbekannte Route"}, 404)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if self._tunnel_blocked(path):
            self._json({"error": "unbekannte Route"}, 404)
            return
        if not self._gate("DELETE", path):
            return
        if path == "/api/schedule/onetime":
            payload = self._read_json()
            if payload is None:
                return
            name, lname = payload.get("project"), payload.get("loop")
            if not name or not lname:
                self._json({"error": "project + loop nötig"}, 400)
                return
            entry = schedule.remove_onetime(name, lname)
            if entry is None:
                self._json({"error": "kein One-time-Run angesetzt"}, 404)
                return
            self._json({"ok": True, "onetime": entry})
        elif path.startswith(auth.CONNECTIONS_PREFIX + "/"):
            name = path[len(auth.CONNECTIONS_PREFIX) + 1 :]
            if connections.delete_connection(name):
                self._json({"ok": True})
            else:
                self._json({"error": "unbekannte Verbindung (Env-Verbindungen sind nicht löschbar)"}, 404)
        else:
            self._json({"error": "unbekannte Route"}, 404)

    def _connection_put(self, name: str) -> None:
        """PUT /api/connections/<name>: anlegen/ändern. Fehlendes ``secret`` behält das gespeicherte,
        ``clear_secret: true`` löscht es."""
        payload = self._read_json()
        if payload is None:
            return
        keep_secret = payload.get("secret") in (None, "") and not payload.get("clear_secret")
        try:
            conn = connections.Connection.from_payload(name, str(payload.get("kind", "")), payload)
            saved = connections.save_connection(conn, keep_secret=keep_secret)
        except connections.InvalidConnection as e:
            self._json({"error": str(e)}, 400)
            return
        self._json({"ok": True, "connection": saved.public()})

    @staticmethod
    def _connections_payload() -> dict:
        """Verbindungsliste (Secrets nie ausgeben) + wählbare Treiber je Art für die UI."""
        from . import llm_cli
        from .backends.registry import known_backends

        return {
            # Entschlüsselt (nur so ist `secret_unreadable` erkennbar); public() gibt kein Secret aus.
            "connections": [c.public() for c in connections.list_connections()],
            "min_masked_secret_len": secrets.MIN_SECRET_LEN,
            "drivers": {
                connections.ConnectionKind.AGENT.value: known_backends(),
                connections.ConnectionKind.LLM.value: [connections.DRIVER_HTTP, *llm_cli.CLIS],
            },
        }

    def _run_stream(self, rid: str):
        """GET /api/run/<id>/stream: Log + Status als Server-Sent Events.

        Reconnect über ``Last-Event-ID`` (Header, sonst ``?offset=``) = Byte-Offset."""
        project = self._query_param("project")
        # Nur Existenz prüfen (Pfad), nicht die ganze Log-Datei einlesen.
        if get_run(rid, project) is None and get_run_log_path(rid, project) is None:
            self._json({"error": "Run nicht gefunden"}, 404)
            return
        offset = stream.parse_offset(self.headers.get("Last-Event-ID") or self._query_param("offset"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            for chunk in stream.iter_run_events(rid, project, offset):
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()

    def _run_transcript(self, rid: str):
        """GET /api/run/<id>/transcript?after=<event-id>&limit=N: Agent-Turns aus dem SessionLog."""
        if get_run(rid, self._query_param("project")) is None:
            self._json({"error": "Run nicht gefunden"}, 404)
            return
        params = self._query_params()
        try:
            after = max(0, int(params.get("after") or 0))
            limit = int(params.get("limit") or sessionlog.TRANSCRIPT_PAGE)
        except ValueError:
            self._json({"error": "after/limit müssen Zahlen sein"}, 400)
            return
        if limit < 1:
            self._json({"error": "limit muss >= 1 sein"}, 400)
            return
        entries = sessionlog.transcript(rid, after, min(limit, sessionlog.TRANSCRIPT_PAGE))
        self._json({"entries": [e.to_dict() for e in entries], "next_after": entries[-1].id if entries else after})

    def _query_param(self, name: str) -> str | None:
        """Letzter Wert des Query-Parameters `name` (URL-dekodiert) oder None."""
        vals = parse_qs(urlsplit(self.path).query).get(name)
        return vals[-1] or None if vals else None

    def _runs_response(self):
        """GET /api/runs. Ohne Such-/Filter-/Cursor-Parameter die einfache Liste (`limit`);
        sonst die paginierte Suche (`runsearch`) → {"runs", "next_cursor"}."""
        qs = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        params = {k: v[-1] for k, v in qs.items()}
        proj = params.get("project") or None
        if not any(k in params for k in _RUN_SEARCH_PARAMS):
            try:
                limit = int(params.get("limit") or LEGACY_RUNS_LIMIT)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json(list_runs(proj, limit))
            return
        try:
            limit = int(params.get("limit") or runsearch.DEFAULT_PAGE_SIZE)
            self._json(
                runsearch.search_runs(
                    proj,
                    q=params.get("q", ""),
                    date_from=params.get("from", ""),
                    date_to=params.get("to", ""),
                    status=params.get("status", ""),
                    loop=params.get("loop", ""),
                    cursor=params.get("cursor", ""),
                    limit=limit,
                    failure_kind=params.get("failure_kind", ""),
                )
            )
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def _llm_payload(self):
        """Provider-Liste (Key nie ausgeben) + aktiver Provider für die UI."""
        active = llm_registry.active_name()
        providers = [
            {
                "name": p["name"],
                "url": p["url"],
                "cli": p.get("cli", ""),
                "model": p["model"],
                "has_key": bool(p.get("key")),
            }
            for p in llm_registry.list_providers()
        ]
        return {"providers": providers, "active": active}

    def _chain_payload(self, chain: retry_chains.RetryChain) -> dict:
        """Eine Retry-Kette für die UI (ADR 0036, Entscheidung 6).

        Die Kette selbst hält nur Zustand; Klasse, letzter Fehler und
        Versuchszahl stehen am letzten Run (``last_run_id``) und werden hier
        nachgeladen — die „Abgelegt“-Liste zeigt sie ohne zweiten Fetch."""
        last = get_run(chain.last_run_id, chain.project) if chain.last_run_id else None
        return {
            "loop": chain.loop,
            "trigger_key": chain.trigger_key,
            "state": chain.state,
            "next_attempt_at": chain.next_attempt_at,
            "last_run_id": chain.last_run_id,
            "created": chain.created,
            "updated": chain.updated,
            "failure_kind": (last or {}).get("failure_kind"),
            "error": (last or {}).get("error", ""),
            "attempt": (last or {}).get("attempt", 0),
        }

    def _run_retry_info(self, r: dict) -> dict | None:
        """Retry-Ketten-Info für einen Run (ADR 0036, Entscheidung 6).

        Zustand + nächster Versuch, damit das Modal „Versuch n/max, nächster
        um …“ zeigen kann. None, wenn der Run keine Kette hat (kein
        kanonischer Trigger), die Kette bereits beendet ist oder der Run
        nicht der aktuelle Versuch der Kette ist (``last_run_id``/
        ``claimed_run_id``) — ältere Versuche desselben Schlüssels zeigen
        keine Ketten-Info."""
        project = str(r.get("project") or "")
        loop = str(r.get("loop") or "")
        spec = load_projects().get(project)
        key = retry_chains.key_for_record(spec, loop, r)
        if not key:
            return None
        try:
            chain = retry_chains.ChainStore().get(project, loop, key)
        except (OSError, ValueError) as e:  # defekter Store: Run-Ansicht ohne Ketten-Info
            print(f"[fabrik] Retry-Ketten nicht lesbar: {e}")
            return None
        if chain is None or not chain.is_active:
            return None
        if r.get("id") not in (chain.last_run_id, chain.claimed_run_id):
            return None
        policy = retry_chains.policy_for(spec, loop) if spec else None
        return {
            "state": chain.state,
            "next_attempt_at": chain.next_attempt_at,
            "max": policy.max if policy else 0,
        }

    def _projects_payload(self):
        projects = []
        exhausted_by_project: dict[str, list[retry_chains.RetryChain]] = {}
        for c in _chains_or_empty():  # einmal lesen, nicht je Projekt
            if c.state is retry_chains.ChainState.EXHAUSTED:
                exhausted_by_project.setdefault(c.project, []).append(c)
        for name, spec in load_projects().items():
            if spec.get("_error"):
                projects.append({"name": name, "error": spec["_error"]})
                continue
            # Records einmal lesen (der Poll läuft alle paar Sekunden pro Tab):
            # letzter Run je Loop typisiert, offene Freigaben roh — der Approval-
            # Kontext enthält Felder (pr, pr_title), die runmodel.Approval nicht führt.
            records = load_records(name)
            last = last_run_per_loop(parse_runs(records, name))
            loops = []
            for lname, ldef in spec.get("loops", {}).items():
                loops.append(
                    {
                        "name": lname,
                        "description": ldef.get("description", ""),
                        "triggers": [t.get("type") for t in ldef.get("triggers") or []],
                        "steps": [s.get("name", s.get("type")) for s in ldef.get("steps", [])],
                        "last": last.get(lname),
                        "state": pause.get_state(name, lname),
                        "effective_state": pause.effective_state(name, lname),
                    }
                )
            repo = spec.get("repo") or {}
            # Offene Freigabe-Anfragen (needs_approval) für den Projekt-View:
            # Anzahl + aufklappbare Liste (neueste zuerst). Kompakte Projektion
            # — das volle Record kommt per /api/run/<id>, wenn das Modal öffnet.
            approvals = [
                {
                    "id": r["id"],
                    "loop": r.get("loop", ""),
                    "started": r.get("started", ""),
                    "approval": r.get("approval") or {},
                    "approval_queued": r.get("approval_queued", ""),
                }
                for r in select_pending_approvals(records)
            ]
            # Erschöpfte Retry-Ketten („Abgelegt“, ADR 0036, Entscheidung 6):
            # analog Freigaben — Anzahl + aufklappbare Liste mit Aktionen.
            shelved = [self._chain_payload(c) for c in exhausted_by_project.get(name, [])]
            projects.append(
                {
                    "name": name,
                    "description": spec.get("description", ""),
                    "root": spec["root"],
                    "branch": repo.get("branch", "-"),
                    "github": spec.get("github", ""),
                    "notify": sorted((spec.get("notify") or {}).keys()),
                    "state": pause.get_state(name),
                    "loops": loops,
                    "pending_approvals": approvals,
                    "shelved": shelved,
                }
            )
        return projects

    def _queue_payload(self, records: list[dict], specs: dict[str, dict]) -> dict:
        """Queue-Snapshot (Schedule-View): Cron-Zeiten sind nur der Plan — hier ist
        zu sehen, was tatsächlich wartet.

        `running` = aktive Runs (Elapsed), `queued` = eingereihte Runs mit
        Einreichungszeit + grober Start-Einschätzung: `now` (Slot/Worker frei),
        `worker` (Warteschleife im Worker-Pool), `slot` (Claim: max_parallel oder
        aktiver Run desselben Loops/Issues). `approval` = pausierte Runs, die auf
        Freigabe warten. `pool`/`projects` = Worker- und Slot-Kontext.
        `records`/`specs` reicht der Aufrufer durch (ein Ladevorgang pro Poll)."""
        pool = pool_status()
        snap = slot_snapshot()
        now_ts = time.time()

        def _local_ts(value: Any) -> str:
            """Record-`started` (naive UTC-ISO) → lokale ISO-String; defekt → ''."""
            try:
                return datetime.fromisoformat(str(value)).replace(tzinfo=UTC).astimezone().isoformat(timespec="seconds")
            except (TypeError, ValueError):
                return ""

        running: list[dict] = []
        queued: list[dict] = []
        approval: list[dict] = []
        for rec in records:
            status = rec.get("status")
            if status == "running":
                try:
                    started_ts = datetime.fromisoformat(str(rec["started"])).replace(tzinfo=UTC).timestamp()
                    elapsed = max(0, int(now_ts - started_ts))
                except (KeyError, TypeError, ValueError):
                    elapsed = 0
                running.append(
                    {
                        "id": str(rec.get("id") or ""),
                        "project": str(rec.get("project") or ""),
                        "loop": str(rec.get("loop") or ""),
                        "trigger": str(rec.get("trigger") or ""),
                        "started": _local_ts(rec.get("started")),
                        "elapsed_s": elapsed,
                        "note": str(rec.get("note") or ""),
                    }
                )
            elif status == "queued":
                queued.append(
                    {
                        "id": str(rec.get("id") or ""),
                        "project": str(rec.get("project") or ""),
                        "loop": str(rec.get("loop") or ""),
                        "trigger": str(rec.get("trigger") or ""),
                        "queued_at": _local_ts(rec.get("started")),
                        "est": "slot",
                        "note": str(rec.get("note") or ""),
                    }
                )
            elif status == "needs_approval":
                approval.append(
                    {
                        "id": str(rec.get("id") or ""),
                        "project": str(rec.get("project") or ""),
                        "loop": str(rec.get("loop") or ""),
                        "trigger": str(rec.get("trigger") or ""),
                        "note": str(rec.get("note") or ""),
                    }
                )

        running.sort(key=lambda e: e["started"], reverse=True)

        # Pool-Queue: Trigger-Runs ohne Record (Cron/Datei/Issue/PR warten auf Worker).
        # FIFO: Position + belegte Worker < Pool-Größe → startet sofort, sonst Warteschlange.
        seen = {(e["project"], e["loop"], e["trigger"]) for e in queued}
        pos = 0
        for entry in queued_trigger_snapshot():
            key = (entry["project"], entry["loop"], entry["trigger"])
            if key in seen:
                continue
            seen.add(key)
            est = "now" if pool["busy"] + pos < pool["workers"] else "worker"
            pos += 1
            queued.append(
                {
                    "id": "",
                    "project": entry["project"],
                    "loop": entry["loop"],
                    "trigger": entry["trigger"],
                    "queued_at": datetime.fromtimestamp(entry["enqueued_at"])
                    .astimezone()
                    .isoformat(timespec="seconds"),
                    "est": est,
                    "note": "",
                }
            )
        queued.sort(key=lambda e: (e["queued_at"], e["project"], e["loop"]))

        names = {e["project"] for e in running + queued + approval}
        for scope in snap["priority_waiters"]:
            names.add(scope.split(LOCK_KEY_SEP, 1)[0])
        projects = []
        for name in sorted(names):
            spec = specs.get(name) or {}
            if spec.get("_error"):
                continue
            projects.append(
                {
                    "name": name,
                    "slots_used": snap["slots_used"].get(name, 0),
                    "slots_limit": max_parallel_for(name, spec),
                }
            )
        return {
            "pool": pool,
            "projects": projects,
            "running": running,
            "queued": queued,
            "approval": approval,
            "priority_waiters": snap["priority_waiters"],
        }

    def _schedule_payload(self) -> dict:
        """GET /api/schedule: Cron-Ausblick + One-time-Overrides (Schedule-View).

        `loops` = alle Loops mit cron-Trigger: nächste Fire-Zeiten (Cron-Semantik
        wie der Scheduler), letzter Run, effektiver State, angesetzter One-time.
        Sortiert nach nächstem Run; `onetime` = alle angesetzt Overrides."""
        now_local = datetime.now()
        onetime = schedule.list_onetimes()
        onetime_by_loop = {(e["project"], e["loop"]): e for e in onetime}
        specs = load_projects()
        records = load_records()
        by_project: dict[str, list[dict]] = {}
        for rec in records:
            by_project.setdefault(str(rec.get("project") or ""), []).append(rec)
        loops: list[dict] = []
        for name, spec in specs.items():
            if spec.get("_error"):
                continue
            last = last_run_per_loop(parse_runs(by_project.get(name, []), name))
            for lname, ldef in spec.get("loops", {}).items():
                cron = next((str(t.get("cron", "")) for t in ldef.get("triggers") or [] if t.get("type") == "cron"), "")
                if not cron:
                    continue
                loops.append(
                    {
                        "project": name,
                        "loop": lname,
                        "cron": cron,
                        "next": [d.isoformat(timespec="seconds") for d in schedule.next_cron_times(cron, now_local, 5)],
                        "last": last.get(lname),
                        "effective_state": pause.effective_state(name, lname),
                        "onetime": onetime_by_loop.get((name, lname)),
                    }
                )
        loops.sort(
            key=lambda loop: (loop["next"][0] if loop["next"] else "9999-12-31T23:59:59", loop["project"], loop["loop"])
        )
        return {
            "now": now_local.isoformat(timespec="seconds"),
            "loops": loops,
            "onetime": onetime,
            "queue": self._queue_payload(records, specs),
        }


EXIT_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _exit_on_signals() -> None:
    """SIGTERM/SIGHUP (systemd-Stop, Terminal geschlossen) → `SystemExit`, damit `finally`/
    `atexit` laufen; der Python-Default würde den Prozess ohne Aufräumen beenden."""

    def _exit(signum, _frame):
        raise SystemExit(128 + signum)

    for sig in EXIT_SIGNALS:
        signal.signal(sig, _exit)


def serve(port: int = config.DEFAULT_PORT, host: str | None = None):
    """Server starten. Nicht-Loopback-Bind ohne Auth wirft ``auth.AuthError`` (ADR 0030)."""
    # Signal-Handler so früh wie möglich: auch ein SIGTERM/SIGHUP während des Setups
    # (Auth-Check, Worktree-Cleanup, Requeue) soll ein geregeltes Ende auslösen.
    _exit_on_signals()
    host = host or os.environ.get("FABRIK_HOST", "").strip() or config.DEFAULT_HOST
    auth.check_bind(host)
    if auth.mode() == auth.MODE_OIDC:
        oidc.check_ready()  # fehlendes Extra/Config früh melden statt beim ersten Login
    server = ThreadingHTTPServer((host, port), FabrikHandler)
    if sandbox.sandbox_enabled() and (n := sandbox.sweep_stale_containers()):
        print(f"[fabrik] {n} verwaiste Sandbox-Container entfernt")
    reap_orphaned_runs()  # vor Scheduler/Worker: keine 'running'-Leichen aus dem Vorlauf
    for wt in cleanup_orphaned_worktrees():  # Reste gecrashter Runs (ADR 0028)
        print(f"[fabrik] verwaisten Worktree entfernt: {wt}")
    # Erst die Vorrang-Warter registrieren, DANN den Scheduler starten — sonst
    # schnappt dessen erster Poll den Lock vor den eingereihten Runs.
    for rid in requeue_approved_runs():
        print(f"[fabrik] Freigabe {rid} wartet weiter auf einen freien Run-Slot")
    for rid in requeue_queued_runs():
        print(f"[fabrik] Eingereihter Run {rid} wartet weiter auf einen freien Run-Slot")
    sched = Scheduler()
    sched.start()
    workers = config.configured_workers()
    print(
        f"fabrik: http://{host}:{port}  (Auth: {auth.mode()}, Specs: {config.PROJECTS_DIR} (+ FABRIK_PROJECTS_PATH, registrierte Zielrepos), Runs: {config.RUNS_DIR}, Workers: {workers})"
    )
    try:
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
    finally:
        # Step-Prozesse laufen in eigener Prozessgruppe (procs): ohne das liefen sie
        # nach Ctrl+C/Terminal-Ende verwaist weiter.
        if n := procs.kill_all():
            print(f"[fabrik] {n} laufende Step-Prozessgruppe(n) beendet")
