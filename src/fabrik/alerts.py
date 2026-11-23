"""Alert intake for the incident round trip (ADR 0045): Sentry + CloudWatch.

Pure functions without HTTP context, following the `webhook.py` pattern: trigger
lookup (`on_alert`), HMAC auth, payload normalization to `Alert`, deterministic
routing `category → hotfix loop`. The server then starts the triage run via
the shared run-start function (no separate run path)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import webhook
from .i18n import t
from .webhook import WebhookError

ALERT_PATH_PREFIX = "/api/alert/"
TRIGGER_TYPE = "on_alert"
STEP_TYPE = "alert_route"
SOURCES = ("sentry", "cloudwatch")
SENTRY_SIGNATURE_HEADER = "Sentry-Hook-Signature"  # raw HMAC-SHA256 hex, without prefix
VAR_PREFIX = "alert_"
# Triage categories (enum in the llm schema). `unknown` is the defined catch-all class.
CATEGORY_UNKNOWN = "unknown"
CATEGORIES = ("bug", "infra", "noise", CATEGORY_UNKNOWN)
ROUTE_NONE = "none"  # no hotfix loop → run ends without a hotfix
CLOUDWATCH_ALARM_STATE = "ALARM"
FINGERPRINT_LEN = 16
_MAX_FIELD = 2000


@dataclass(frozen=True)
class Alert:
    """Normalized alert; all fields are untrusted (framework §1.4)."""

    source: str
    title: str
    message: str = ""
    level: str = ""
    service: str = ""
    url: str = ""

    @property
    def fingerprint(self) -> str:
        """Stable hash over source/service/title (same alert → same ID)."""
        raw = "\0".join((self.source, self.service, self.title))
        return hashlib.sha256(raw.encode()).hexdigest()[:FINGERPRINT_LEN]

    def to_vars(self) -> dict[str, str]:
        """Initial variables (`alert_*`) for the triage run."""
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
    """Sentry project as service name: `{slug, name}` object (issue webhook),
    string (legacy) or numeric ID (`data.event.project`)."""
    if isinstance(val, dict):
        return _str(val.get("slug")) or _str(val.get("name"))
    if isinstance(val, int) and not isinstance(val, bool):
        return str(val)
    return _str(val)


def _sentry_service(payload: dict, event: dict, issue: dict) -> str:
    """Service = project slug; slug sources before the numeric event project ID."""
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
        raise WebhookError(422, t("alerts.sentry_alert_without_title"))
    return Alert(
        source="sentry",
        title=title,
        message=_str(event.get("message")) or _str(payload.get("culprit")) or _str(issue.get("culprit")),
        level=_str(event.get("level")) or _str(issue.get("level")) or _str(payload.get("level")),
        service=_sentry_service(payload, event, issue),
        url=_str(event.get("web_url")) or _str(issue.get("web_url")) or _str(payload.get("url")),
    )


def parse_cloudwatch(payload: dict) -> Alert | None:
    """CloudWatch alarm (SNS envelope or direct). Non-`ALARM` state → None (no incident)."""
    msg = payload.get("Message")
    if isinstance(msg, str):  # SNS: the alarm is embedded as a JSON string in `Message`
        try:
            inner = json.loads(msg)
        except json.JSONDecodeError as e:
            raise WebhookError(422, t("alerts.cloudwatch_message_is_not_json")) from e
        payload = inner if isinstance(inner, dict) else {}
    name = _str(payload.get("AlarmName"))
    if not name:
        raise WebhookError(422, t("alerts.cloudwatch_alarm_without_alarmname"))
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
    """Normalize the body according to the source. 400 on invalid JSON, 422 on missing fields;
    None = alert without need for action (e.g. CloudWatch `OK`)."""
    try:
        payload = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WebhookError(400, t("alerts.invalid_json")) from e
    if not isinstance(payload, dict):
        raise WebhookError(400, t("alerts.json_object_expected"))
    return parse_sentry(payload) if source == "sentry" else parse_cloudwatch(payload)


def find_trigger(spec: dict | None, lname: str) -> dict | None:
    """The loop's first `on_alert` trigger, otherwise None."""
    return webhook.find_trigger(spec, lname, TRIGGER_TYPE)


def authenticate(trigger: dict | None, body: bytes, fabrik_signature: str, sentry_signature: str = "") -> dict:
    """Return the authenticated trigger; 401 uniformly (no oracle).

    Accepts `X-Fabrik-Signature: sha256=<hex>` (relay/Lambda for CloudWatch, generic)
    and, for source `sentry`, additionally Sentry's `Sentry-Hook-Signature` (raw hex). The
    caller reads the header values (HTTP headers are case-insensitive)."""
    try:
        return webhook.authenticate(trigger, body, fabrik_signature)
    except WebhookError:
        if not (trigger and trigger.get("source") == "sentry" and sentry_signature.strip()):
            raise
    # Sentry signs without the `sha256=` prefix → normalize to the fabrik format.
    return webhook.authenticate(trigger, body, webhook.SIGNATURE_PREFIX + sentry_signature.strip())


def route(category: str, routes: dict[str, str] | None, fallback: str = ROUTE_NONE) -> str:
    """Deterministisches Mapping Kategorie → Hotfix-Loop.

    Unbekannte/ungemappte Kategorie → `fallback` (Default `none` = kein Hotfix)."""
    return str((routes or {}).get(category) or fallback or ROUTE_NONE)


def _route_targets(step: dict) -> list[tuple[str, str]]:
    """Targets of an `alert_route` step as `(key, loop name)` pairs
    (`routes.<category>` + `fallback`), without `none`."""
    targets = [(f"routes.{k}", v) for k, v in (step.get("routes") or {}).items() if isinstance(v, str)]
    if step.get("fallback") is not None:
        targets.append(("fallback", step["fallback"]))
    return [(k, v) for k, v in targets if v != ROUTE_NONE]


def _route_edges(spec: dict) -> dict[str, tuple[str, ...]]:
    """The project's route graph: loop → sorted target loops across all
    `alert_route` steps (only targets that are existing loops)."""
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
    """Cycles in the project's route chain (all `alert_route` steps).

    A cycle — even across several loops, e.g. `a → b → a` — would start
    infinitely nested runs on execution (RecursionError)."""
    edges = _route_edges(spec)
    errs: list[str] = []
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished

    def dfs(node: str, path: list[str]) -> None:
        state[node] = 1
        path.append(node)
        for nxt in edges.get(node, ()):
            mark = state.get(nxt, 0)
            if mark == 1:
                cycle = path[path.index(nxt) :] + [nxt]
                if len(cycle) == 2:
                    errs.append(t("alerts.alert_route_route_points_to", v1=cycle[0], v2=cycle[1]))
                else:
                    errs.append(t("alerts.alert_route_route_cycle_nested", v1=" → ".join(cycle)))
            elif mark == 0:
                dfs(nxt, path)
        path.pop()
        state[node] = 2

    for node in sorted(edges):
        if state.get(node, 0) == 0:
            dfs(node, [])
    return errs


def validate_route_step(step: dict, spec: dict, where: str) -> list[str]:
    """Spec validation of an `alert_route` step (ADR 0045).

    `routes` is `{category: loop}`; every target (and `fallback`) is `none`
    or a loop of the same project. In addition, the route chain of the
    whole project must be cycle-free — a cycle (even across several loops)
    would start infinitely nested runs."""
    routes = step.get("routes")
    if not isinstance(routes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in routes.items()):
        return [t("alerts.alert_route_routes_must_be", where=where)]
    fallback = step.get("fallback")
    if fallback is not None and not isinstance(fallback, str):
        return [t("alerts.alert_route_fallback_must_be", where=where)]
    loops = spec.get("loops") or {}
    errs = []
    for key, target in _route_targets(step):
        if target not in loops:
            errs.append(f"{where}: alert_route {key} → unbekannter Loop {target!r}")
    errs += _route_cycle_errors(spec)
    return errs


def validate_trigger(trig: dict, where: str) -> list[str]:
    """Spec validation of an `on_alert` trigger."""
    errs = []
    if trig.get("source") not in SOURCES:
        errs.append(t("alerts.on_alert_source_must_be", where=where, v2=list(SOURCES), v3=trig.get("source")))
    if not isinstance(trig.get("secret_env"), str) or not trig["secret_env"]:
        errs.append(t("alerts.on_alert_needs_secret_env", where=where))
    return errs
