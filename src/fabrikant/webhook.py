"""`on_webhook` trigger: HMAC check, trigger lookup, payload → variables (ADR 0027).

Pure functions without HTTP context; the server then calls the shared
run-start function (no separate webhook code path)."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import threading
import time
from collections.abc import Mapping
from typing import Any, Protocol, TypeAlias
from urllib.parse import urlparse

from .i18n import t

SIGNATURE_HEADER = "X-Fabrikant-Signature"
SIGNATURE_PREFIX = "sha256="
ORIGIN_ENV = "FABRIKANT_WEBHOOK_ORIGIN"
MAX_BODY_BYTES = 1_000_000
WEBHOOK_PATH_PREFIX = "/api/webhook/"
TIMESTAMP_HEADER = "X-Fabrikant-Timestamp"
NONCE_HEADER = "X-Fabrikant-Nonce"


class HeaderSource(Protocol):
    """Anything with a header-style ``get`` (``dict``, ``http.client.HTTPMessage``)."""

    def get(self, name: str, failobj: Any = None, /) -> Any: ...


def header_value(headers: HeaderSource, name: str) -> str:
    """Value of the webhook header `name` (empty string if absent)."""
    return headers.get(name) or ""


REPLAY_FLAG = "replay_protection"  # opt-in per trigger (backward compatible)
REPLAY_WINDOW_S = 300  # Tolerance for |now - timestamp|
MAX_NONCE_LEN = 128
DEFAULT_TRIGGER_TYPE = "on_webhook"

# Spec types: a trigger is a YAML mapping, the spec a nested dict.
Trigger: TypeAlias = dict[str, Any]
Spec: TypeAlias = Mapping[str, Any]
PayloadMap: TypeAlias = Mapping[str, str]


class WebhookError(Exception):
    """Error with HTTP status (for the server response)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def tunnel_origin() -> str:
    """Configured tunnel origin (`https://host`); empty = webhook CORS off."""
    return os.environ.get(ORIGIN_ENV, "").strip().rstrip("/")


def parse_content_length(raw: str | None) -> int:
    """Validate Content-Length: missing → 0; non-numeric/negative → 400; too large → 413."""
    if raw is None:
        return 0
    try:
        length = int(raw)
    except ValueError:
        raise WebhookError(400, t("webhook.content_length_invalid")) from None
    if length < 0:
        raise WebhookError(400, t("webhook.content_length_invalid"))
    if length > MAX_BODY_BYTES:
        raise WebhookError(413, t("webhook.payload_too_large"))
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
    """Signature header value (`sha256=<hex>`) for `body`."""
    return SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Constant-time comparison; empty secret or missing header → False."""
    if not secret or not header:
        return False
    return hmac.compare_digest(header.strip(), sign(secret, body))


def find_trigger(spec: Spec | None, lname: str, ttype: str = DEFAULT_TRIGGER_TYPE) -> Trigger | None:
    """First trigger of type `ttype` (default `on_webhook`) of the loop, otherwise None."""
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
    """Map the JSON body according to `mapping` (variable → dot path) onto initial variables.

    Invalid JSON / not an object → 400, missing field → 422. Values become
    strings (objects/lists as JSON); they are untrusted (framework §1.4)."""
    try:
        payload = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WebhookError(400, t("webhook.invalid_json")) from e
    if not isinstance(payload, dict):
        raise WebhookError(400, t("webhook.json_object_expected"))
    out: dict[str, str] = {}
    for var, path in (mapping or {}).items():
        try:
            val = _lookup(payload, str(path))
        except KeyError:
            raise WebhookError(422, t("webhook.field_missing", path=path)) from None
        out[str(var)] = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
    return out


def sign_replay(secret: str, body: bytes, timestamp: str, nonce: str) -> str:
    """Signature with replay protection: HMAC over `<timestamp>.<nonce>.<body>`."""
    return sign(secret, f"{timestamp}.{nonce}.".encode() + body)


class NonceCache:
    """Thread-safe store of seen nonces with TTL (in-process, not persistent)."""

    def __init__(self, ttl_s: float = 2 * REPLAY_WINDOW_S):
        self._ttl = ttl_s
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def add(self, nonce: str, now: float) -> bool:
        """Remember a nonce; False if it was already seen within the TTL."""
        with self._lock:
            self._seen = {n: t for n, t in self._seen.items() if now - t < self._ttl}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


NONCES = NonceCache()


def _check_replay(secret: str, body: bytes, header: str, timestamp: str, nonce: str, now: float) -> None:
    """Verify the signature over timestamp+nonce, then window and uniqueness (each 401).

    Signature first: error messages about timestamp/nonce exist only for authentic requests."""
    if not nonce or len(nonce) > MAX_NONCE_LEN or not timestamp:
        raise WebhookError(401, t("api.invalid_signature"))
    if not secret or not header or not hmac.compare_digest(header.strip(), sign_replay(secret, body, timestamp, nonce)):
        raise WebhookError(401, t("api.invalid_signature"))
    try:
        ts = float(timestamp)
    except ValueError:
        raise WebhookError(401, t("webhook.timestamp_invalid")) from None
    # Non-finite values (nan/inf) bypass the window: nan never compares >.
    if not math.isfinite(ts):
        raise WebhookError(401, t("webhook.timestamp_invalid"))
    if abs(now - ts) > REPLAY_WINDOW_S:
        raise WebhookError(401, t("webhook.timestamp_expired"))
    if not NONCES.add(nonce, now):
        raise WebhookError(401, t("webhook.nonce_already_used"))


def authenticate(
    trigger: Trigger | None,
    body: bytes,
    header: str,
    *,
    timestamp: str = "",
    nonce: str = "",
    now: float | None = None,
) -> Trigger:
    """Return the authenticated trigger; 401 on missing trigger/secret/signature
    (deliberately uniform, no oracle).

    With `replay_protection: true` in the trigger, timestamp and nonce are mandatory and
    part of the signature (`sign_replay`)."""
    secret = os.environ.get(str((trigger or {}).get("secret_env") or ""), "")
    if trigger and trigger.get(REPLAY_FLAG):
        _check_replay(secret, body, header, timestamp, nonce, time.time() if now is None else now)
        return trigger
    if not trigger or not verify_signature(secret, body, header):
        raise WebhookError(401, t("webhook.invalid_signature"))
    return trigger


def validate_trigger(trig: Trigger, where: str) -> list[str]:
    """Spec validation of an `on_webhook` trigger."""
    errs = []
    if not isinstance(trig.get("secret_env"), str) or not trig["secret_env"]:
        errs.append(t("webhook.on_webhook_needs_secret_env", where=where))
    m = trig.get("map")
    if m is not None and (not isinstance(m, dict) or not all(isinstance(v, str) for v in m.values())):
        errs.append(t("webhook.on_webhook_map_must_be", where=where))
    if REPLAY_FLAG in trig and not isinstance(trig[REPLAY_FLAG], bool):
        errs.append(t("webhook.on_webhook_flag_must_be_bool", where=where, flag=REPLAY_FLAG))
    return errs
