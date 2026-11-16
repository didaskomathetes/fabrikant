"""Alert-Intake für den Incident-Roundtrip (ADR 0045): Sentry + CloudWatch.

Reine Funktionen ohne HTTP-Kontext, nach dem Muster von `webhook.py`: Trigger-
Lookup (`on_alert`), HMAC-Auth, Payload-Normalisierung auf `Alert`, deterministisches
Routing `Kategorie → Hotfix-Loop`. Der Server startet danach den Triage-Run über
die gemeinsame Run-Start-Funktion (kein eigener Run-Pfad)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import webhook
from .webhook import WebhookError

ALERT_PATH_PREFIX = "/api/alert/"
TRIGGER_TYPE = "on_alert"
STEP_TYPE = "alert_route"
SOURCES = ("sentry", "cloudwatch")
SENTRY_SIGNATURE_HEADER = "Sentry-Hook-Signature"  # rohes HMAC-SHA256-Hex, ohne Präfix
VAR_PREFIX = "alert_"
# Triage-Kategorien (Enum im llm-Schema). `unknown` ist die definierte Auffangklasse.
CATEGORY_UNKNOWN = "unknown"
CATEGORIES = ("bug", "infra", "noise", CATEGORY_UNKNOWN)
ROUTE_NONE = "none"  # kein Hotfix-Loop → Run endet ohne Hotfix
CLOUDWATCH_ALARM_STATE = "ALARM"
FINGERPRINT_LEN = 16
_MAX_FIELD = 2000


@dataclass(frozen=True)
class Alert:
    """Normalisierter Alert; alle Felder sind untrusted (framework §1.4)."""

    source: str
    title: str
    message: str = ""
    level: str = ""
    service: str = ""
    url: str = ""

    @property
    def fingerprint(self) -> str:
        """Stabiler Hash über Quelle/Service/Titel (gleicher Alert → gleiche ID)."""
        raw = "\0".join((self.source, self.service, self.title))
        return hashlib.sha256(raw.encode()).hexdigest()[:FINGERPRINT_LEN]

    def to_vars(self) -> dict[str, str]:
        """Initial-Variablen (`alert_*`) für den Triage-Run."""
        fields = {
            "source": self.source,
            "title": self.title,
            "message": self.message,
            "level": self.level,
            "service": self.service,
            "url": self.url,
            "fingerprint": self.fingerprint,
        }
        return {VAR_PREFIX + k: v[:_MAX_FIELD] for k, v in fields.items()}


def _str(val: object) -> str:
    return val.strip() if isinstance(val, str) else ""


def _dict(val: object) -> dict:
    return val if isinstance(val, dict) else {}


def _project_name(val: object) -> str:
    """Sentry-Projekt als Service-Name: `{slug, name}`-Objekt (Issue-Webhook),
    String (Legacy) oder numerische ID (`data.event.project`)."""
    if isinstance(val, dict):
        return _str(val.get("slug")) or _str(val.get("name"))
    if isinstance(val, int) and not isinstance(val, bool):
        return str(val)
    return _str(val)


def _sentry_service(payload: dict, event: dict, issue: dict) -> str:
    """Service = Projekt-Slug; Slug-Quellen vor der numerischen Event-Projekt-ID."""
    candidates = (
        issue.get("project"),
        payload.get("project_slug"),
        event.get("project"),
        payload.get("project"),
        payload.get("project_name"),
    )
    return next((name for name in map(_project_name, candidates) if name), "")


def parse_sentry(payload: dict) -> Alert:
    """Sentry-Webhook (`data.event`/`data.issue`; Legacy: Top-Level-Felder)."""
    data = _dict(payload.get("data"))
    event = _dict(data.get("event")) or _dict(payload.get("event"))
    issue = _dict(data.get("issue"))
    title = _str(event.get("title")) or _str(issue.get("title")) or _str(payload.get("message"))
    if not title:
        raise WebhookError(422, "Sentry-Alert ohne Titel")
    return Alert(
        source="sentry",
        title=title,
        message=_str(event.get("message")) or _str(payload.get("culprit")) or _str(issue.get("culprit")),
        level=_str(event.get("level")) or _str(issue.get("level")) or _str(payload.get("level")),
        service=_sentry_service(payload, event, issue),
        url=_str(event.get("web_url")) or _str(issue.get("web_url")) or _str(payload.get("url")),
    )


def parse_cloudwatch(payload: dict) -> Alert | None:
    """CloudWatch-Alarm (SNS-Envelope oder direkt). Nicht-`ALARM`-Zustand → None (kein Incident)."""
    msg = payload.get("Message")
    if isinstance(msg, str):  # SNS: der Alarm steckt als JSON-String in `Message`
        try:
            inner = json.loads(msg)
        except json.JSONDecodeError as e:
            raise WebhookError(422, "CloudWatch-Message kein JSON") from e
        payload = inner if isinstance(inner, dict) else {}
    name = _str(payload.get("AlarmName"))
    if not name:
        raise WebhookError(422, "CloudWatch-Alarm ohne AlarmName")
    if _str(payload.get("NewStateValue")) != CLOUDWATCH_ALARM_STATE:
        return None
    trigger = _dict(payload.get("Trigger"))
    return Alert(
        source="cloudwatch",
        title=name,
        message=_str(payload.get("NewStateReason")) or _str(payload.get("AlarmDescription")),
        level=CLOUDWATCH_ALARM_STATE,
        service=_str(trigger.get("Namespace")),
        url=_str(payload.get("AlarmArn")),
    )


def parse_alert(source: str, body: bytes) -> Alert | None:
    """Body gemäß Quelle normalisieren. 400 bei ungültigem JSON, 422 bei fehlenden Feldern;
    None = Alert ohne Handlungsbedarf (z. B. CloudWatch `OK`)."""
    try:
        payload = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WebhookError(400, "ungültiges JSON") from e
    if not isinstance(payload, dict):
        raise WebhookError(400, "JSON-Objekt erwartet")
    return parse_sentry(payload) if source == "sentry" else parse_cloudwatch(payload)


def find_trigger(spec: dict | None, lname: str) -> dict | None:
    """Erster `on_alert`-Trigger des Loops, sonst None."""
    return webhook.find_trigger(spec, lname, TRIGGER_TYPE)


def authenticate(trigger: dict | None, body: bytes, fabrik_signature: str, sentry_signature: str = "") -> dict:
    """Authentifizierten Trigger liefern; 401 einheitlich (kein Orakel).

    Akzeptiert `X-Fabrik-Signature: sha256=<hex>` (Relay/Lambda für CloudWatch, generisch)
    und für Quelle `sentry` zusätzlich Sentrys `Sentry-Hook-Signature` (rohes Hex). Die
    Header-Werte liest der Aufrufer (HTTP-Header sind case-insensitiv)."""
    try:
        return webhook.authenticate(trigger, body, fabrik_signature)
    except WebhookError:
        if not (trigger and trigger.get("source") == "sentry" and sentry_signature.strip()):
            raise
    # Sentry signiert ohne `sha256=`-Präfix → auf das Fabrik-Format normalisieren.
    return webhook.authenticate(trigger, body, webhook.SIGNATURE_PREFIX + sentry_signature.strip())


def route(category: str, routes: dict[str, str] | None, fallback: str = ROUTE_NONE) -> str:
    """Deterministisches Mapping Kategorie → Hotfix-Loop.

    Unbekannte/ungemappte Kategorie → `fallback` (Default `none` = kein Hotfix)."""
    return str((routes or {}).get(category) or fallback or ROUTE_NONE)


def _route_targets(step: dict) -> list[tuple[str, str]]:
    """Ziele eines `alert_route`-Steps als `(Schlüssel, Loop-Name)`-Paare
    (`routes.<kategorie>` + `fallback`), ohne `none`."""
    targets = [(f"routes.{k}", v) for k, v in (step.get("routes") or {}).items() if isinstance(v, str)]
    if step.get("fallback") is not None:
        targets.append(("fallback", step["fallback"]))
    return [(k, v) for k, v in targets if v != ROUTE_NONE]


def _route_edges(spec: dict) -> dict[str, tuple[str, ...]]:
    """Route-Graph des Projekts: Loop → sortierte Ziel-Loops über alle
    `alert_route`-Steps (nur Ziele, die existierende Loops sind)."""
    loops = spec.get("loops") or {}
    edges: dict[str, tuple[str, ...]] = {}
    for lname, ldef in loops.items():
        if not isinstance(ldef, dict):
            continue
        targets: set[str] = set()
        for st in ldef.get("steps") or []:
            if not isinstance(st, dict) or st.get("type") != STEP_TYPE:
                continue
            for _, target in _route_targets(st):
                if target in loops:
                    targets.add(target)
        if targets:
            edges[lname] = tuple(sorted(targets))
    return edges


def _route_cycle_errors(spec: dict) -> list[str]:
    """Zyklen in der Route-Kette des Projekts (alle `alert_route`-Steps).

    Ein Zyklus — auch über mehrere Loops, z. B. `a → b → a` — würde beim
    Ausführen unendlich verschachtelte Runs starten (RecursionError)."""
    edges = _route_edges(spec)
    errs: list[str] = []
    state: dict[str, int] = {}  # 1 = auf dem aktuellen Pfad, 2 = abgeschlossen

    def dfs(node: str, path: list[str]) -> None:
        state[node] = 1
        path.append(node)
        for nxt in edges.get(node, ()):
            mark = state.get(nxt, 0)
            if mark == 1:
                cycle = path[path.index(nxt) :] + [nxt]
                if len(cycle) == 2:
                    errs.append(
                        f"alert_route: Route {cycle[0]!r} → {cycle[1]!r} zeigt auf den eigenen Loop (Endlosschleife)"
                    )
                else:
                    errs.append(f"alert_route: Route-Zyklus {' → '.join(cycle)} (verschachtelte Runs ohne Ende)")
            elif mark == 0:
                dfs(nxt, path)
        path.pop()
        state[node] = 2

    for node in sorted(edges):
        if state.get(node, 0) == 0:
            dfs(node, [])
    return errs


def validate_route_step(step: dict, spec: dict, where: str) -> list[str]:
    """Spec-Validierung eines `alert_route`-Steps (ADR 0045).

    `routes` ist `{kategorie: loop}`; jedes Ziel (und `fallback`) ist `none`
    oder ein Loop desselben Projekts. Zusätzlich muss die Route-Kette des
    ganzen Projekts zyklusfrei sein — ein Zyklus (auch über mehrere Loops)
    würde unendlich verschachtelte Runs starten."""
    routes = step.get("routes")
    if not isinstance(routes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in routes.items()):
        return [f"{where}: alert_route routes muss Mapping Kategorie → Loop (Strings) sein"]
    fallback = step.get("fallback")
    if fallback is not None and not isinstance(fallback, str):
        return [f"{where}: alert_route fallback muss ein Loop-Name (String) sein"]
    loops = spec.get("loops") or {}
    errs = []
    for key, target in _route_targets(step):
        if target not in loops:
            errs.append(f"{where}: alert_route {key} → unbekannter Loop {target!r}")
    errs += _route_cycle_errors(spec)
    return errs


def validate_trigger(trig: dict, where: str) -> list[str]:
    """Spec-Validierung eines `on_alert`-Triggers."""
    errs = []
    if trig.get("source") not in SOURCES:
        errs.append(f"{where}: on_alert source muss einer von {list(SOURCES)} sein (ist {trig.get('source')!r})")
    if not isinstance(trig.get("secret_env"), str) or not trig["secret_env"]:
        errs.append(f"{where}: on_alert braucht secret_env (Name der Env-Variable mit dem Shared Secret)")
    return errs
