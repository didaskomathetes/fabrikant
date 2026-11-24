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
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

import yaml

from . import (
    alerts,
    auth,
    branches,
    config,
    connections,
    costs,
    kpis,
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
from .i18n import t
from .i18n import t as _t
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

# Parameters that switch GET /api/runs into the paginated search mode.
_RUN_SEARCH_PARAMS = ("q", "from", "to", "status", "loop", "cursor", "failure_kind")
LEGACY_RUNS_LIMIT = 50  # Default `limit` der einfachen /api/runs-Liste


def _chains_or_empty() -> list[retry_chains.RetryChain]:
    """All retry chains for the overview; an unreadable store (ADR 0036)
    does not cripple the dashboard — empty + log."""
    try:
        return retry_chains.ChainStore().all()
    except (OSError, ValueError) as e:
        print(t("server.chains_unreadable", error=e))
        return []


# Routes with their own HMAC auth that stay reachable via the tunnel host.
INTAKE_PATH_PREFIXES = (webhook.WEBHOOK_PATH_PREFIX, alerts.ALERT_PATH_PREFIX)


class RunStartResult(NamedTuple):
    """Response of `_start_run`: JSON payload and HTTP status."""

    payload: dict[str, Any]
    status: int


class FabrikHandler(BaseHTTPRequestHandler):
    server_version = "fabrikant/0.3"

    def log_message(self, format: str, *args: object) -> None:  # quieter (name as in the base class)
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
        # Client has already closed the connection (e.g. cancel)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _read_json(self) -> dict[str, Any] | None:
        """Read the request body as a JSON object (empty → ``{}``).

        On invalid JSON or an invalid ``Content-Length`` (non-numeric/negative)
        a 400 is sent itself and ``None`` returned."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                raise ValueError(length)
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except (ValueError, UnicodeDecodeError):  # JSONDecodeError is a ValueError
            self._json({"error": t("api.invalid_json")}, 400)
            return None
        if not isinstance(payload, dict):
            self._json({"error": t("api.invalid_json")}, 400)
            return None
        return payload

    def _query_params(self) -> dict[str, str]:
        """Query string of the request as dict (URL-decoded, last value wins)."""
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query, keep_blank_values=True))

    def _origin_ok(self) -> bool:
        """Allow POST requests only from the own origin (or with token).

        Protects the local API against drive-by RCE: any web page in the
        operator's browser could otherwise fire `fetch('http://localhost:PORT/api/runs')`
        and trigger shell steps. Without CORS headers the browser blocks
        cross-origin POSTs (same-origin policy)."""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True  # CLI / curl / Same-Origin ohne Origin-Header
        try:
            host = self.headers.get("Host", "")
            return origin.rstrip("/") in (f"http://{host}", f"https://{host}")
        except Exception:
            return False

    def _tunnel_blocked(self, path: str) -> bool:
        """Requests via the tunnel host reach only the webhook route."""
        return webhook.tunnel_matches(self.headers.get("Host", "")) and not path.startswith(INTAKE_PATH_PREFIXES)

    def _cors_headers(self):
        """CORS only for the configured tunnel origin (never `*`)."""
        allowed = webhook.tunnel_origin()
        on_webhook = self.path.startswith(webhook.WEBHOOK_PATH_PREFIX)
        if allowed and on_webhook and self.headers.get("Origin", "").rstrip("/") == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        """CORS preflight: only for the webhook route and the tunnel origin."""
        path = self.path.split("?")[0]
        if self._tunnel_blocked(path):
            self._json({"error": t("api.unknown_route")}, 404)
            return
        self.send_response(204)
        if path.startswith(webhook.WEBHOOK_PATH_PREFIX):
            self._cors_headers()
            self.send_header("Access-Control-Allow-Methods", "POST")
            allow = (webhook.SIGNATURE_HEADER, webhook.TIMESTAMP_HEADER, webhook.NONCE_HEADER)
            self.send_header("Access-Control-Allow-Headers", ", ".join(("Content-Type", *allow)))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_intake(self, path: str, prefix: str) -> tuple[str, str, bytes]:
        """Shared intake head for webhook/alert: split `<prefix><project>/<loop>`
        and read the raw body. Invalid path → 401 (no oracle, like missing auth)."""
        parts = path[len(prefix) :].split("/")
        length = webhook.parse_content_length(self.headers.get("Content-Length"))
        body = self.rfile.read(length) if length else b""
        if len(parts) != 2 or not all(parts):
            raise webhook.WebhookError(401, t("api.invalid_signature"))
        return parts[0], parts[1], body

    def _webhook(self, path: str):
        """POST /api/webhook/<project>/<loop>: verify HMAC, map payload, start run."""
        try:
            name, lname, body = self._read_intake(path, webhook.WEBHOOK_PATH_PREFIX)
            found = webhook.find_trigger(load_projects().get(name), lname)
            trig = webhook.authenticate(
                found,
                body,
                self.headers.get(webhook.SIGNATURE_HEADER, ""),
                timestamp=self.headers.get(webhook.TIMESTAMP_HEADER, ""),
                nonce=self.headers.get(webhook.NONCE_HEADER, ""),
            )
            vars_ = webhook.map_payload(body, trig.get("map") or {})
        except (webhook.WebhookError, ValueError) as e:
            code = e.status if isinstance(e, webhook.WebhookError) else 400
            self._json({"error": str(e)}, code)
            return
        self._json(*self._start_run(name, lname, "webhook", vars_ or None, "on_webhook", actor="webhook"))

    def _alert(self, path: str):
        """POST /api/alert/<project>/<loop>: verify HMAC, normalize alert, start triage run."""
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
            self._json({"ignored": True, "reason": t("api.not_an_incident")}, 200)
            return
        self._json(*self._start_run(name, lname, "alert", alert.to_vars(), alerts.TRIGGER_TYPE, actor="alert"))

    def _auth_ok(self) -> bool:
        """Optionales Bearer-Token (FABRIKANT_TOKEN). Leert → nur Origin-Check."""
        token = os.environ.get("FABRIKANT_TOKEN", "").strip()
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(auth, f"Bearer {token}")

    @property
    def _actor(self) -> str | None:
        """Audit name of the caller (``started_by``/``approved_by``); None without auth."""
        return self._who.name if self._who else None

    @property
    def _audit_actor(self) -> str:
        """Trigger for audit logs: ``user:<name>``, without auth ``api:<ip>``."""
        return f"user:{self._actor}" if self._actor else f"api:{self.client_address[0]}"

    def _gate(self, method: str, path: str) -> bool:
        """Check auth + role (ADR 0030) and set ``self._who``.

        Sends 401/403 itself on rejection and then returns False.
        Mode ``none``: GET open, POST as before origin + optional token."""
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
        # Token callers (CLI/automation) are not a browser: no origin/CSRF requirement.
        if writes and self._who is None and not self._origin_ok():
            self._json({"error": "zugriff verweigert (Origin)"}, 403)
            return False
        self._who = self._who or self._session()
        required = auth.required_role(method, path)
        if required is None:
            return True
        if self._who is None:
            self._json({"error": t("api.login_required")}, 401)
        elif not self._who.allows(required):
            self._json({"error": t("api.role_required", role=required)}, 403)
        elif writes and not auth.csrf_ok(self._who, self.headers.get(auth.CSRF_HEADER, "")):
            self._json({"error": t("api.csrf_invalid")}, 403)
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
            self._json({"error": _t("server.login_only_in_fabrikant_auth")}, 400)
            return
        result = auth.login(str(payload.get("user", "")), str(payload.get("password", "")))
        if result is None:
            self._json({"error": t("api.bad_credentials")}, 401)
            return
        sid, who = result
        self._json(
            {"user": who.name, "role": who.role, "csrf": who.csrf},
            headers={"Set-Cookie": auth.set_cookie_header(sid)},
        )

    def _redirect(self, location: str, cookies: tuple[str, ...] = ()):
        """302 to ``location``; ``cookies`` are ``Set-Cookie`` values (one header each)."""
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
        """GET /api/oidc/callback?code&state → session cookie + 302 to the UI."""
        params = self._query_params()
        if params.get("error"):
            self._json(
                {
                    "error": t(
                        "api.idp_error", error=params["error"], description=params.get("error_description", "")
                    ).strip()
                },
                401,
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
        self,
        name: str,
        lname: str,
        note: str,
        initial_vars: dict[str, str] | None,
        trigger: str,
        actor: str | None = None,
    ) -> RunStartResult:
        """Start a run with initial variables (the only internal start function).

        Shared for `POST /api/runs` and `on_webhook` (no separate webhook
        code path, framework-v1-draft §1.3). Returns `(payload, http_code)`."""
        # Generate the run id BEFORE the worker start: the response goes out
        # before the worker writes the record to disk. The UI needs the
        # id to follow the new run via /api/run/<id> — otherwise its
        # "newest run of the loop" finds the OLD run in a race.
        run_id = f"{config.now_id()}-{lname}"
        # Busy project → queue instead of 409; the run waits with priority
        # over trigger runs (ADR 0015).
        try:
            queued = start_priority_run(
                name, lname, trigger, run_id=run_id, note=note, initial_vars=initial_vars, actor=actor
            )
        except RuntimeError as e:
            # 5xx with a clear message so clients react deterministically.
            return RunStartResult({"error": f"Worker-Start fehlgeschlagen: {e}"}, 500)
        payload = {"started": not queued, "queued": queued, "project": name, "loop": lname, "run_id": run_id}
        return RunStartResult(payload, 202 if queued else 200)

    def _static(self, rel: str):
        f = config.WEB_DIR / (rel or "index.html")
        # is_relative_to: `parent !=` would also refuse subdirectories under WEB_DIR
        # (e.g. web/css/app.css).
        if not f.is_file() or not f.resolve().is_relative_to(config.WEB_DIR.resolve()):
            self.send_response(404)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Dev tool: never cache UI files (app.js, style.css, …) heuristically,
        # otherwise the browser keeps running the old version after a code change
        # until someone hard-reloads manually. no-cache → revalidate on every load
        # (without a validator here: complete re-fetch, fast locally).
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if self._tunnel_blocked(path):
            self._json({"error": t("api.unknown_route")}, 404)
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
        elif path.startswith("/api/run/") and path.endswith("/events"):
            self._run_events(path[len("/api/run/") : -len("/events")])
        elif path.startswith("/api/run/") and path.endswith("/log"):
            # Raw run log (from runs/<project>/<id>.log) as plain text.
            # Must be checked BEFORE the generic /api/run/<id> branch.
            # Optional ?lines=N → only the last N lines (tail).
            rid = path[len("/api/run/") : -len("/log")]
            log = get_run_log(rid, self._query_param("project"))
            if log is None:
                self._json({"error": t("api.log_not_found")}, 404)
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
                parsed = parse_runs([r], str(r.get("project") or ""))
                if parsed:
                    r = {**r, "cost": costs.run_cost(parsed[0]).as_dict()}
            self._json(r, 200 if r else 404)
        elif path in ("/api/kpis", "/api/kpis/costs"):
            report = costs.costs_report if path == "/api/kpis/costs" else kpis.kpis_report
            try:
                self._json(
                    report(
                        self._query_param("project"),
                        date_from=self._query_param("from") or "",
                        date_to=self._query_param("to") or "",
                    )
                )
            except ValueError as e:
                self._json({"error": str(e)}, 400)
        elif path == "/api/health":
            self._json({"ok": True, "time": config.utcnow().isoformat()})
        elif path == "/api/metrics":
            # KPI export (Prometheus text format) for scrape tools.

            body = (metrics.render() + kpis.prometheus_text() + "\n").encode("utf-8")
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
                self._json({"error": t("api.unknown_project")}, 404)
        elif path.startswith("/api/projects/") and path.endswith("/spec"):
            name = path[len("/api/projects/") : -len("/spec")]
            specs = load_projects()
            if name not in specs:
                self._json({"error": t("api.unknown_project")}, 404)
                return
            p = spec_path(name, specs)
            if self._query_params().get("raw") == "1":
                # Raw (non-expanded) spec as draft source for the
                # UI graph editor: vars literal, use:/insert: un-inlined, all
                # loop keys preserved. + _hash for the optimistic lock.
                if not p.is_file():
                    self._json({"error": t("api.unknown_project")}, 404)
                    return
                raw = load_spec_raw(p)
                raw["_hash"] = spec_hash(p)
                self._json(raw)
            else:
                expanded = dict(specs[name])
                expanded["_hash"] = spec_hash(p)
                self._json(expanded)
        elif path.startswith("/api/templates/") and path.endswith("/params"):
            # Template parameters (params block) for the UI graph editor:
            # shows which with: keys a template knows + their defaults.
            # ?project=<name> → template resolution as in expansion
            # (project-local <spec folder>/templates take precedence).
            tname = path[len("/api/templates/") : -len("/params")]
            q = self._query_params()
            proj = q.get("project")
            if proj and proj not in load_projects():
                self._json({"error": t("api.unknown_project")}, 404)
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
                self._json({"error": t("api.unknown_project")}, 404)
                return
            repo = spec.get("repo") or {}
            b = branches.list_branches(spec["root"]) if repo else []
            self._json({"branches": b, "current": repo.get("branch", "-")})
        elif path == "/api/retry-chains":
            # Retry chains (ADR 0036) for the UI ("Filed away" list). Optional
            # ?project=<name> filters; ?state=<state> filters the state.
            params = self._query_params()
            proj = params.get("project")
            state = params.get("state")
            if state is not None:
                try:
                    state = retry_chains.ChainState(state)
                except ValueError:
                    self._json({"error": t("api.unknown_state", state=repr(state))}, 400)
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
            # Own auth (HMAC) instead of origin/token — callers are servers, not browsers.
            self._webhook(path)
            return
        if path.startswith(alerts.ALERT_PATH_PREFIX):
            self._alert(path)  # eigene HMAC-Auth, wie Webhooks (ADR 0045)
            return
        if self._tunnel_blocked(path):
            self._json({"error": t("api.unknown_route")}, 404)
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
                self._json({"error": t("api.project_loop_required")}, 400)
                return
            note = payload.get("note", "")
            # Optional initial variables (K=V), e.g. issue_number for
            # issue-driven loops — manual runs otherwise have no
            # issue context (the on_issue trigger provides the variables).
            # Validation BEFORE the claim: a 400 must never hold a run slot
            # (the handler thread would otherwise die without release).
            raw_vars = payload.get("vars")
            if raw_vars is not None and not isinstance(raw_vars, dict):
                self._json({"error": _t("server.vars_must_be_an_object")}, 400)
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
                self._json({"error": t("api.project_loop_required")}, 400)
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
                self._json({"error": t("api.message_required")}, 400)
                return
            # The client may send its own req_id (so it can cancel immediately
            # before the response is there); otherwise we generate one.
            req_id = str(payload.get("req_id", "")).strip() or config.now_id()
            project = str(payload.get("project", ""))
            cancel_event = _register_chat(req_id)  # cancellable immediately, even before the LLM start
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
            # Wait for the worker, but exit immediately if the user cancels.
            # (The LLM call runs to completion in the background — daemon thread, result discarded.)
            while not done.wait(timeout=0.2):
                if cancel_event.is_set():
                    self._json({"reply": t("chat.cancelled"), "req_id": req_id})
                    return
            self._json({"reply": reply, "req_id": req_id})
        elif path == "/api/chat/cancel":
            payload = self._read_json()
            if payload is None:
                return
            rid = str(payload.get("req_id", "")).strip()
            if not rid:
                self._json({"error": t("api.req_id_required")}, 400)
                return
            ok = _cancel_chat(rid)
            self._json({"ok": ok, "cancelled": ok})
        elif path in ("/api/approve", "/api/reject"):
            payload = self._read_json()
            if payload is None:
                return
            name, run_id = payload.get("project"), payload.get("run_id")
            if not name or not run_id:
                self._json({"error": t("api.project_run_required")}, 400)
                return
            try:
                reply, resumed = _chat_resume(name, run_id, path == "/api/approve", actor=self._actor)
            except Exception as e:
                self._json({"error": str(e)}, 400)
                return
            # resumed: True only if the resume worker was started.
            # The UI then polls the run (with needs_approval grace), because the
            # worker writes "running" to disk only shortly after this response.
            # Project at its limit (max_parallel) → approval is queued (ADR 0015), resumed
            # stays True. False (thread start error) → the UI shows the reason.
            self._json({"ok": True, "reply": reply, "resumed": resumed})
        elif path == "/api/llm":
            payload = self._read_json()
            if payload is None:
                return
            name = str(payload.get("provider", "")).strip()
            if not name:
                self._json({"error": t("api.provider_required")}, 400)
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
            except pause.UnknownTargetError as e:
                self._json({"error": str(e)}, 404)
                return
            except ValueError as e:
                self._json({"error": str(e)}, 400)
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
                self._json({"error": t("api.unknown_project")}, 404)
                return
            if not spec.get("repo"):
                self._json({"error": t("api.no_repo")}, 400)
                return
            if not branch:
                self._json({"error": t("api.branch_required")}, 400)
                return
            available = branches.list_branches(spec["root"])
            if available and branch not in available:
                self._json({"error": t("api.branch_missing", branch=branch, available=", ".join(available))}, 400)
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
                self._json({"error": t("api.chain_key_required")}, 400)
                return
            if path.endswith("/dismiss"):
                chain = retry_chains.dismiss_chain(name, lname, tkey, actor=self._audit_actor)
                if chain is None:
                    self._json({"error": t("api.chain_not_retryable")}, 404)
                    return
                self._json({"ok": True, "chain": asdict(chain)})
            else:
                try:
                    # Bare name as with POST /api/runs: ends up in started_by (ADR 0030).
                    run_id = retry_chains.retry_now(name, lname, tkey, actor=self._actor)
                except retry_chains.RetryBlocked as e:
                    self._json({"error": str(e)}, 409)
                    return
                if run_id is None:
                    self._json({"error": t("api.chain_not_exhausted")}, 404)
                    return
                self._json({"ok": True, "run_id": run_id})
        else:
            self._json({"error": t("api.unknown_route")}, 404)

    def do_PUT(self):
        path = self.path.split("?")[0]
        if self._tunnel_blocked(path):
            self._json({"error": t("api.unknown_route")}, 404)
            return
        if not self._gate("PUT", path):
            return
        if path.startswith("/api/projects/") and path.endswith("/spec"):
            # Spec edit (graph view): merge loops per loop + delete +
            # update top-level fields (description, max_parallel).
            # Validation + atomic write + optimistic lock live in
            # specs.save_spec (comments are preserved).
            name = path[len("/api/projects/") : -len("/spec")]
            payload = self._read_json()
            if payload is None:
                return
            base_hash = payload.get("base_hash")
            if base_hash is not None and not isinstance(base_hash, str):
                self._json({"error": _t("server.base_hash_must_be_a")}, 400)
                return
            try:
                update = SpecUpdate.from_payload({k: v for k, v in payload.items() if k != "base_hash"})
                saved = save_spec(name, update, base_hash=base_hash)
            except KeyError:
                self._json({"error": t("api.unknown_project")}, 404)
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
            self._json({"error": t("api.unknown_route")}, 404)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if self._tunnel_blocked(path):
            self._json({"error": t("api.unknown_route")}, 404)
            return
        if not self._gate("DELETE", path):
            return
        if path == "/api/schedule/onetime":
            payload = self._read_json()
            if payload is None:
                return
            name, lname = payload.get("project"), payload.get("loop")
            if not name or not lname:
                self._json({"error": t("api.project_loop_required")}, 400)
                return
            entry = schedule.remove_onetime(name, lname)
            if entry is None:
                self._json({"error": t("api.no_onetime")}, 404)
                return
            self._json({"ok": True, "onetime": entry})
        elif path.startswith(auth.CONNECTIONS_PREFIX + "/"):
            name = path[len(auth.CONNECTIONS_PREFIX) + 1 :]
            if connections.delete_connection(name):
                self._json({"ok": True})
            else:
                self._json({"error": t("api.unknown_connection")}, 404)
        else:
            self._json({"error": t("api.unknown_route")}, 404)

    def _connection_put(self, name: str) -> None:
        """PUT /api/connections/<name>: create/change. A missing ``secret`` keeps the stored one,
        ``clear_secret: true`` deletes it."""
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
        """Connection list (never emit secrets) + selectable drivers per kind for the UI."""
        from . import llm_cli
        from .backends.registry import known_backends

        return {
            # Decrypted (only this way `secret_unreadable` is detectable); public() emits no secret.
            "connections": [c.public() for c in connections.list_connections()],
            "min_masked_secret_len": secrets.MIN_SECRET_LEN,
            "drivers": {
                connections.ConnectionKind.AGENT.value: known_backends(),
                connections.ConnectionKind.LLM.value: [connections.DRIVER_HTTP, *llm_cli.CLIS],
            },
        }

    def _run_stream(self, rid: str):
        """GET /api/run/<id>/stream: log + status as server-sent events.

        Reconnect via ``Last-Event-ID`` (header, otherwise ``?offset=``) = byte offset."""
        project = self._query_param("project")
        # Only check existence (path), do not read the whole log file.
        if get_run(rid, project) is None and get_run_log_path(rid, project) is None:
            self._json({"error": t("api.run_not_found")}, 404)
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
        """GET /api/run/<id>/transcript?after=<event-id>&limit=N: agent turns from the SessionLog."""
        if get_run(rid, self._query_param("project")) is None:
            self._json({"error": t("api.run_not_found")}, 404)
            return
        params = self._query_params()
        try:
            after = max(0, int(params.get("after") or 0))
            limit = int(params.get("limit") or sessionlog.TRANSCRIPT_PAGE)
        except ValueError:
            self._json({"error": t("api.after_limit_numeric")}, 400)
            return
        if limit < 1:
            self._json({"error": _t("server.limit_must_be_1")}, 400)
            return
        entries = sessionlog.transcript(rid, after, min(limit, sessionlog.TRANSCRIPT_PAGE))
        self._json({"entries": [e.to_dict() for e in entries], "next_after": entries[-1].id if entries else after})

    def _run_events(self, rid: str):
        """GET /api/run/<id>/events: full session log (events, checkpoints, artifacts).

        Backs the web UI trace view (#128). Works for both the legacy JSON store
        and the SQL run store — ``session_log_dump`` abstracts the backend. The
        ``data`` field of each event is the redacted JSON string as stored."""
        if get_run(rid, self._query_param("project")) is None:
            self._json({"error": "Run not found"}, 404)
            return
        self._json(sessionlog.session_log_dump(rid))

    def _query_param(self, name: str) -> str | None:
        """Last value of the query parameter `name` (URL-decoded) or None."""
        vals = parse_qs(urlsplit(self.path).query).get(name)
        return vals[-1] or None if vals else None

    def _runs_response(self):
        """GET /api/runs. Without search/filter/cursor parameters the simple list (`limit`);
        otherwise the paginated search (`runsearch`) → {"runs", "next_cursor"}."""
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
        """Provider list (never emit keys) + active provider for the UI."""
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
        """A retry chain for the UI (ADR 0036, decision 6).

        The chain itself holds only state; class, last error and
        attempt count are on the last run (``last_run_id``) and are reloaded
        here — the "Filed away" list shows them without a second fetch."""
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
        """Retry-chain info for a run (ADR 0036, decision 6).

        State + next attempt, so the modal can show "Attempt n/max, next
        at …". None if the run has no chain (no
        canonical trigger), the chain has already ended or the run
        is not the chain's current attempt (``last_run_id``/
        ``claimed_run_id``) — older attempts of the same key show
        no chain info."""
        project = str(r.get("project") or "")
        loop = str(r.get("loop") or "")
        spec = load_projects().get(project)
        key = retry_chains.key_for_record(spec, loop, r)
        if not key:
            return None
        try:
            chain = retry_chains.ChainStore().get(project, loop, key)
        except (OSError, ValueError) as e:  # defekter Store: Run-Ansicht ohne Ketten-Info
            print(t("server.chains_unreadable", error=e))
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
            # Read records once (the poll runs every few seconds per tab):
            # last run per loop typed, open approvals raw — the approval
            # context contains fields (pr, pr_title) that runmodel.Approval does not carry.
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
            # Open approval requests (needs_approval) for the project view:
            # count + expandable list (newest first). Compact projection
            # — the full record comes via /api/run/<id> when the modal opens.
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
            # Exhausted retry chains ("Filed away", ADR 0036, decision 6):
            # analogous to approvals — count + expandable list with actions.
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
        """Queue snapshot (schedule view): cron times are only the plan — this shows
        what is actually waiting.

        `running` = active runs (elapsed), `queued` = queued runs with
        submission time + rough start estimate: `now` (slot/worker free),
        `worker` (queue in the worker pool), `slot` (claim: max_parallel or
        active run of the same loop/issue). `approval` = paused runs waiting for
        approval. `pool`/`projects` = worker and slot context.
        `records`/`specs` are passed through by the caller (one load per poll)."""
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

        # Pool queue: trigger runs without a record (cron/file/issue/PR waiting for a worker).
        # FIFO: position + busy workers < pool size → starts immediately, otherwise queue.
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
        """GET /api/schedule: cron outlook + one-time overrides (schedule view).

        `loops` = all loops with a cron trigger: next fire times (cron semantics
        like the scheduler), last run, effective state, scheduled one-time.
        Sorted by next run; `onetime` = all scheduled overrides."""
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
    """SIGTERM/SIGHUP (systemd stop, terminal closed) → `SystemExit`, so `finally`/
    `atexit` run; the Python default would end the process without cleanup."""

    def _exit(signum, _frame):
        raise SystemExit(128 + signum)

    for sig in EXIT_SIGNALS:
        signal.signal(sig, _exit)


def serve(port: int = config.DEFAULT_PORT, host: str | None = None):
    """Server starten. Nicht-Loopback-Bind ohne Auth wirft ``auth.AuthError`` (ADR 0030)."""
    # Signal handlers as early as possible: even a SIGTERM/SIGHUP during setup
    # (auth check, worktree cleanup, requeue) should trigger an orderly end.
    _exit_on_signals()
    host = host or os.environ.get("FABRIKANT_HOST", "").strip() or config.DEFAULT_HOST
    auth.check_bind(host)
    if auth.mode() == auth.MODE_OIDC:
        oidc.check_ready()  # report a missing extra/config early instead of at the first login
    server = ThreadingHTTPServer((host, port), FabrikHandler)
    if sandbox.sandbox_enabled() and (n := sandbox.sweep_stale_containers()):
        print(f"[fabrikant] {n} verwaiste Sandbox-Container entfernt")
    reap_orphaned_runs()  # before scheduler/worker: no 'running' corpses from the previous run
    for wt in cleanup_orphaned_worktrees():  # Reste gecrashter Runs (ADR 0028)
        print(f"[fabrikant] verwaisten Worktree entfernt: {wt}")
    # Register the priority waiters first, THEN start the scheduler — otherwise
    # its first poll grabs the lock before the queued runs.
    for rid in requeue_approved_runs():
        print(t("server.approval_still_waiting", run_id=rid))
    for rid in requeue_queued_runs():
        print(t("server.queued_still_waiting", run_id=rid))
    sched = Scheduler()
    sched.start()
    workers = config.configured_workers()
    print(
        f"fabrikant: http://{host}:{port}  (Auth: {auth.mode()}, Specs: {config.PROJECTS_DIR} (+ FABRIKANT_PROJECTS_PATH, registrierte Zielrepos), Runs: {config.RUNS_DIR}, Workers: {workers})"
    )
    try:
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
    finally:
        # Step processes run in their own process group (procs): without that they would keep running
        # orphaned after Ctrl+C/terminal end.
        if n := procs.kill_all():
            print(f"[fabrikant] {n} laufende Step-Prozessgruppe(n) beendet")
