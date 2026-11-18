"""`on_webhook`-Trigger: HMAC-Prüfung, Trigger-Lookup, Payload→Variablen (ADR 0027).

Reine Funktionen ohne HTTP-Kontext; der Server ruft danach die gemeinsame
Run-Start-Funktion auf (kein eigener Webhook-Code-Pfad)."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import threading
import time
from collections.abc import Mapping
from typing import Any, TypeAlias
from urllib.parse import urlparse

SIGNATURE_HEADER = "X-Fabrik-Signature"
SIGNATURE_PREFIX = "sha256="
ORIGIN_ENV = "FABRIK_WEBHOOK_ORIGIN"
MAX_BODY_BYTES = 1_000_000
WEBHOOK_PATH_PREFIX = "/api/webhook/"
TIMESTAMP_HEADER = "X-Fabrik-Timestamp"
NONCE_HEADER = "X-Fabrik-Nonce"
REPLAY_FLAG = "replay_protection"  # opt-in pro Trigger (abwärtskompatibel)
REPLAY_WINDOW_S = 300  # Toleranz für |now - timestamp|
MAX_NONCE_LEN = 128
DEFAULT_TRIGGER_TYPE = "on_webhook"

# Spec-Typen: ein Trigger ist ein YAML-Mapping, die Spec ein verschachteltes Dict.
Trigger: TypeAlias = dict[str, Any]
Spec: TypeAlias = Mapping[str, Any]
PayloadMap: TypeAlias = Mapping[str, str]


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


def find_trigger(spec: Spec | None, lname: str, ttype: str = DEFAULT_TRIGGER_TYPE) -> Trigger | None:
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


def map_payload(body: bytes, mapping: PayloadMap | None) -> dict[str, str]:
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


def sign_replay(secret: str, body: bytes, timestamp: str, nonce: str) -> str:
    """Signatur mit Replay-Schutz: HMAC über `<timestamp>.<nonce>.<body>`."""
    return sign(secret, f"{timestamp}.{nonce}.".encode() + body)


class NonceCache:
    """Thread-sicherer Speicher gesehener Nonces mit TTL (im Prozess, nicht persistent)."""

    def __init__(self, ttl_s: float = 2 * REPLAY_WINDOW_S):
        self._ttl = ttl_s
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def add(self, nonce: str, now: float) -> bool:
        """Nonce merken; False, wenn sie innerhalb der TTL schon gesehen wurde."""
        with self._lock:
            self._seen = {n: t for n, t in self._seen.items() if now - t < self._ttl}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


NONCES = NonceCache()


def _check_replay(secret: str, body: bytes, header: str, timestamp: str, nonce: str, now: float) -> None:
    """Signatur über Timestamp+Nonce prüfen, dann Fenster und Einmaligkeit (jeweils 401).

    Signatur zuerst: Fehlermeldungen zu Timestamp/Nonce gibt es nur für authentische Requests."""
    if not nonce or len(nonce) > MAX_NONCE_LEN or not timestamp:
        raise WebhookError(401, "Signatur ungültig")
    if not secret or not header or not hmac.compare_digest(header.strip(), sign_replay(secret, body, timestamp, nonce)):
        raise WebhookError(401, "Signatur ungültig")
    try:
        ts = float(timestamp)
    except ValueError:
        raise WebhookError(401, "Timestamp ungültig") from None
    # Nicht-endliche Werte (nan/inf) umgehen das Fenster: nan vergleicht nie >.
    if not math.isfinite(ts):
        raise WebhookError(401, "Timestamp ungültig")
    if abs(now - ts) > REPLAY_WINDOW_S:
        raise WebhookError(401, "Timestamp abgelaufen")
    if not NONCES.add(nonce, now):
        raise WebhookError(401, "Nonce bereits verwendet")


def authenticate(
    trigger: Trigger | None,
    body: bytes,
    header: str,
    *,
    timestamp: str = "",
    nonce: str = "",
    now: float | None = None,
) -> Trigger:
    """Authentifizierten Trigger liefern; 401 bei fehlendem Trigger/Secret/Signatur
    (bewusst einheitlich, kein Orakel).

    Mit `replay_protection: true` im Trigger sind Timestamp und Nonce Pflicht und
    Teil der Signatur (`sign_replay`)."""
    secret = os.environ.get(str((trigger or {}).get("secret_env") or ""), "")
    if trigger and trigger.get(REPLAY_FLAG):
        _check_replay(secret, body, header, timestamp, nonce, time.time() if now is None else now)
        return trigger
    if not trigger or not verify_signature(secret, body, header):
        raise WebhookError(401, "Signatur ungültig")
    return trigger


def validate_trigger(trig: Trigger, where: str) -> list[str]:
    """Spec-Validierung eines `on_webhook`-Triggers."""
    errs = []
    if not isinstance(trig.get("secret_env"), str) or not trig["secret_env"]:
        errs.append(f"{where}: on_webhook braucht secret_env (Name der Env-Variable mit dem Shared Secret)")
    m = trig.get("map")
    if m is not None and (not isinstance(m, dict) or not all(isinstance(v, str) for v in m.values())):
        errs.append(f"{where}: on_webhook map muss Variable → Pfad (String) sein")
    if REPLAY_FLAG in trig and not isinstance(trig[REPLAY_FLAG], bool):
        errs.append(f"{where}: on_webhook {REPLAY_FLAG} muss bool sein")
    return errs
