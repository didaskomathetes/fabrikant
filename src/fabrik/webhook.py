"""`on_webhook`-Trigger: HMAC-Prüfung, Trigger-Lookup, Payload→Variablen (ADR 0027).

Reine Funktionen ohne HTTP-Kontext; der Server ruft danach die gemeinsame
Run-Start-Funktion auf (kein eigener Webhook-Code-Pfad)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from urllib.parse import urlparse

SIGNATURE_HEADER = "X-Fabrik-Signature"
SIGNATURE_PREFIX = "sha256="
ORIGIN_ENV = "FABRIK_WEBHOOK_ORIGIN"
MAX_BODY_BYTES = 1_000_000
WEBHOOK_PATH_PREFIX = "/api/webhook/"


class WebhookError(Exception):
    """Fehler mit HTTP-Status (für die Server-Antwort)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def tunnel_origin() -> str:
    """Konfigurierter Tunnel-Origin (`https://host`), leer = Webhook-CORS aus."""
    return os.environ.get(ORIGIN_ENV, "").strip().rstrip("/")


def parse_content_length(raw: str | None) -> int:
    """Content-Length validieren: fehlend → 0; nicht numerisch/negativ → 400; zu groß → 413."""
    if raw is None:
        return 0
    try:
        length = int(raw)
    except ValueError:
        raise WebhookError(400, "Content-Length ungültig") from None
    if length < 0:
        raise WebhookError(400, "Content-Length ungültig")
    if length > MAX_BODY_BYTES:
        raise WebhookError(413, "Payload zu groß")
    return length


def tunnel_matches(host_header: str) -> bool:
    """Return whether a Host header names the configured tunnel endpoint.

    Hostnames are case-insensitive, and an omitted port is equivalent to the
    scheme's default port. Invalid or non-authority Host values never match.
    """
    origin = urlparse(tunnel_origin())
    try:
        origin_host = origin.hostname
        origin_port = origin.port
    except ValueError:
        return False
    if not origin_host or origin.username is not None or origin.password is not None:
        return False
    if origin_port is None:
        origin_port = 443 if origin.scheme.lower() == "https" else 80

    parsed = urlparse(f"//{host_header.strip()}")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    if port is None:
        port = 443 if origin.scheme.lower() == "https" else 80
    return _canonical_host(host) == _canonical_host(origin_host) and port == origin_port


def _canonical_host(host: str) -> str:
    """Lower-case a hostname and drop the optional trailing DNS root dot."""
    return host.lower().rstrip(".")


def sign(secret: str, body: bytes) -> str:
    """Signatur-Headerwert (`sha256=<hex>`) für `body`."""
    return SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Zeitkonstanter Vergleich; leeres Secret oder fehlender Header → False."""
    if not secret or not header:
        return False
    return hmac.compare_digest(header.strip(), sign(secret, body))


def find_trigger(spec: dict | None, lname: str, ttype: str = "on_webhook") -> dict | None:
    """Erster Trigger vom Typ `ttype` (Default `on_webhook`) des Loops, sonst None."""
    ldef = ((spec or {}).get("loops") or {}).get(lname) or {}
    for trig in ldef.get("triggers") or []:
        if isinstance(trig, dict) and trig.get("type") == ttype:
            return trig
    return None


def _lookup(payload: object, path: str) -> object:
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(path)
    return cur


def map_payload(body: bytes, mapping: dict) -> dict[str, str]:
    """JSON-Body gemäß `mapping` (Variable → Punkt-Pfad) auf Initial-Variablen abbilden.

    Ungültiges JSON / kein Objekt → 400, fehlendes Feld → 422. Werte werden zu
    Strings (Objekte/Listen als JSON); sie sind untrusted (framework §1.4)."""
    try:
        payload = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WebhookError(400, "ungültiges JSON") from e
    if not isinstance(payload, dict):
        raise WebhookError(400, "JSON-Objekt erwartet")
    out: dict[str, str] = {}
    for var, path in (mapping or {}).items():
        try:
            val = _lookup(payload, str(path))
        except KeyError:
            raise WebhookError(422, f"Feld fehlt: {path}") from None
        out[str(var)] = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
    return out


def authenticate(trigger: dict | None, body: bytes, header: str) -> dict:
    """Authentifizierten Trigger liefern; 401 bei fehlendem Trigger/Secret/Signatur
    (bewusst einheitlich, kein Orakel)."""
    secret = os.environ.get(str((trigger or {}).get("secret_env") or ""), "")
    if not trigger or not verify_signature(secret, body, header):
        raise WebhookError(401, "Signatur ungültig")
    return trigger


def validate_trigger(trig: dict, where: str) -> list[str]:
    """Spec-Validierung eines `on_webhook`-Triggers."""
    errs = []
    if not isinstance(trig.get("secret_env"), str) or not trig["secret_env"]:
        errs.append(f"{where}: on_webhook braucht secret_env (Name der Env-Variable mit dem Shared Secret)")
    m = trig.get("map")
    if m is not None and (not isinstance(m, dict) or not all(isinstance(v, str) for v in m.values())):
        errs.append(f"{where}: on_webhook map muss Variable → Pfad (String) sein")
    return errs
