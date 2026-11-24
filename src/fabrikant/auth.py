"""Authentication + authorization: local users, roles, sessions, CSRF (ADR 0030).

Modes (``FABRIKANT_AUTH``): ``none`` (default, loopback only), ``local`` and ``oidc`` (ADR 0046,
login flow in ``oidc.py``). The user store lives in ``state/users.json`` (scrypt hashes),
sessions only in memory.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from . import config
from .i18n import t

VIEWER, OPERATOR, ADMIN = "viewer", "operator", "admin"
ROLES = (VIEWER, OPERATOR, ADMIN)  # aufsteigende Rechte
MODE_NONE, MODE_LOCAL, MODE_OIDC = "none", "local", "oidc"
MODES = (MODE_NONE, MODE_LOCAL, MODE_OIDC)

COOKIE_NAME = "fabrikant_session"
CSRF_HEADER = "X-CSRF-Token"
DEFAULT_SESSION_TTL_S = 8 * 3600
MIN_PASSWORD_LEN = 8
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_SALT_BYTES = 2**14, 8, 1, 16
SCRYPT_MAXMEM = 64 * 1024 * 1024
TOKEN_USER = "token"  # Principal name for FABRIKANT_TOKEN callers  # nosec B105

# Routes without login (login form, liveness, session status). Static UI files
# (everything outside /api/) are also public and contain no data.
PUBLIC_API = ("/api/health", "/api/login", "/api/session", "/api/login/oidc", "/api/oidc/callback")
PUBLIC_API_PREFIXES = ("/api/webhook/",)
CONNECTIONS_PREFIX = "/api/connections"  # Schreiben (PUT/DELETE) nur admin (ADR 0052)
_ROLE_OVERRIDES = {"/api/logout": VIEWER}  # POST-Routes unterhalb von operator

_users_lock = threading.Lock()
_sessions_lock = threading.Lock()


class AuthError(ValueError):
    """Invalid auth configuration or user management."""


@dataclass(frozen=True)
class Principal:
    """Authentifizierter Aufrufer (Session-Benutzer oder API-Token)."""

    name: str
    role: str
    csrf: str | None = None  # only with session auth; token callers are CSRF-immune

    def allows(self, required: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(required)


# ---------------------------------------------------------------- Konfiguration
def mode() -> str:
    """Aktiver Auth-Modus aus ``FABRIKANT_AUTH`` (Default ``none``)."""
    value = os.environ.get("FABRIKANT_AUTH", MODE_NONE).strip().lower() or MODE_NONE
    if value not in MODES:
        raise AuthError(t("auth.fabrikant_auth_unknown_allowed", value=value, v2=", ".join(MODES)))
    return value


def session_ttl() -> int:
    """Session-Lebensdauer in Sekunden (``FABRIKANT_SESSION_TTL``)."""
    try:
        return max(60, int(os.environ.get("FABRIKANT_SESSION_TTL", DEFAULT_SESSION_TTL_S)))
    except ValueError:
        return DEFAULT_SESSION_TTL_S


def is_loopback(host: str) -> bool:
    """True for loopback bind addresses (``localhost``, 127.0.0.0/8, ::1)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str) -> None:
    """Refuse binding to non-loopback without auth (``AuthError``)."""
    if not is_loopback(host) and mode() == MODE_NONE:
        raise AuthError(t("auth.bind_to_refused_non_loopback", host=host))


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    """scrypt-Hash im Format ``scrypt$n$r$p$salt$hash`` (hex)."""
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM)


def verify_password(password: str, stored: str) -> bool:
    """Check a password against the stored hash (constant time, broken hash → False)."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = _scrypt(password, bytes.fromhex(salt), int(n), int(r), int(p))
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_hex(8))  # gleiche Rechenzeit bei unbekanntem Benutzer


# ---------------------------------------------------------------- Benutzer-Store
class UserEntry(TypedDict):
    """Persisted user entry; a missing ``role`` counts as ``viewer``."""

    hash: str
    role: NotRequired[str]


def _users_path() -> Path:
    return config.STATE_DIR / "users.json"


def load_users() -> dict[str, UserEntry]:
    """``{name: {"hash", "role"}}``; missing/broken file → empty.

    Invalid entries (not a mapping, ``hash`` missing/not a string, unknown role)
    are discarded — a login then fails cleanly instead of with a 500."""
    try:
        data = json.loads(_users_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        name: cast(UserEntry, user)
        for name, user in data.items()
        if isinstance(user, dict) and isinstance(user.get("hash"), str) and user.get("role", VIEWER) in ROLES
    }


def _save_users(users: dict[str, UserEntry]) -> None:
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=1), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise AuthError(t("auth.role_unknown_allowed", role=role, v2=", ".join(ROLES)))


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(t("auth.password_too_short_at_least", v1=MIN_PASSWORD_LEN))


def add_user(name: str, password: str, role: str = VIEWER) -> None:
    """Create a user (``AuthError`` on invalid name/password/role or duplicate)."""
    name = name.strip()
    if not name or any(c.isspace() or c in ":/" for c in name):
        raise AuthError(t("auth.invalid_username_empty_spaces_and"))
    _check_role(role)
    _check_password(password)
    with _users_lock:
        users = load_users()
        if name in users:
            raise AuthError(t("auth.user_already_exists", name=name))
        users[name] = {"hash": hash_password(password), "role": role}
        _save_users(users)


def _update_user(name: str, *, hash: str | None = None, role: str | None = None) -> None:
    """Update only the passed fields (``hash``/``role``) of the user."""
    with _users_lock:
        users = load_users()
        if name not in users:
            raise AuthError(t("auth.user_unknown", name=name))
        if hash is not None:
            users[name]["hash"] = hash
        if role is not None:
            users[name]["role"] = role
        _save_users(users)


def set_password(name: str, password: str) -> None:
    """Change the password and end all of the user's sessions."""
    _check_password(password)
    _update_user(name, hash=hash_password(password))
    drop_user_sessions(name)


def set_role(name: str, role: str) -> None:
    """Change the role (takes effect immediately, the role is looked up per request)."""
    _check_role(role)
    _update_user(name, role=role)


# ---------------------------------------------------------------- Sessions
@dataclass
class _Session:
    user: str
    csrf: str
    expires: float
    role: str | None = None  # set for OIDC sessions (no entry in the user store)


_sessions: dict[str, _Session] = {}


def login(name: str, password: str) -> tuple[str, Principal] | None:
    """Check credentials; on success ``(session_id, principal)``, otherwise ``None``."""
    user = load_users().get(name)
    ok = verify_password(password, user["hash"] if user else _DUMMY_HASH)
    if not (user and ok):
        return None
    return create_session(name, user.get("role", VIEWER), stored=True)


def create_session(name: str, role: str, *, stored: bool = False) -> tuple[str, Principal]:
    """Create a session and return ``(session_id, principal)``.

    ``stored=True``: the user is in the local store, the role is looked up there per
    request. Otherwise (OIDC) the role is fixed in the session."""
    sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = time.time()
    with _sessions_lock:
        for key in [k for k, s in _sessions.items() if s.expires <= now]:
            del _sessions[key]
        _sessions[sid] = _Session(name, csrf, now + session_ttl(), None if stored else role)
    return sid, Principal(name, role, csrf)


def session_principal(sid: str | None) -> Principal | None:
    """Principal for the session id; expired/unknown/user deleted → ``None``."""
    if not sid:
        return None
    with _sessions_lock:
        sess = _sessions.get(sid)
        if sess and sess.expires <= time.time():
            del _sessions[sid]
            sess = None
    if sess and sess.role is not None:
        return Principal(sess.user, sess.role, sess.csrf)
    user = load_users().get(sess.user) if sess else None
    if not (sess and user):
        return None
    return Principal(sess.user, user.get("role", VIEWER), sess.csrf)


def logout(sid: str | None) -> None:
    """Session beenden (idempotent)."""
    with _sessions_lock, contextlib.suppress(KeyError):
        del _sessions[sid or ""]


def drop_user_sessions(name: str) -> None:
    """End all sessions of a local user (effective only in the own process)."""
    with _sessions_lock:
        for key in [k for k, s in _sessions.items() if s.user == name and s.role is None]:
            del _sessions[key]


# ---------------------------------------------------------------- Autorisierung
def required_role(method: str, path: str) -> str | None:
    """Minimum role for a route; ``None`` = public.

    GET → viewer; write routes → operator; settings (LLM provider, branch, connections) → admin."""
    if not path.startswith("/api/") or path in PUBLIC_API or path.startswith(PUBLIC_API_PREFIXES):
        return None
    if method in ("GET", "HEAD"):
        return VIEWER
    if path in _ROLE_OVERRIDES:
        return _ROLE_OVERRIDES[path]
    if path == "/api/llm" or path.endswith("/branch") or path.startswith(CONNECTIONS_PREFIX):
        return ADMIN
    return OPERATOR


def token_principal(authorization: str) -> Principal | None:
    """``Authorization: Bearer <FABRIKANT_TOKEN>`` → admin-Principal (API-Token), sonst ``None``."""
    token = os.environ.get("FABRIKANT_TOKEN", "").strip()
    if token and hmac.compare_digest(authorization, f"Bearer {token}"):
        return Principal(TOKEN_USER, ADMIN)
    return None


def csrf_ok(principal: Principal, supplied: str) -> bool:
    """Check the CSRF header; token callers (no cookie) do not need one."""
    return principal.csrf is None or hmac.compare_digest(principal.csrf, supplied)


def cookie_value(cookie_header: str, name: str = COOKIE_NAME) -> str | None:
    """Extract the value of cookie ``name`` (default: session id) from the ``Cookie`` header."""
    for part in cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and value:
            return value
    return None


def cookie_header(name: str, value: str, max_age: int, *, same_site: str = "Strict") -> str:
    """``Set-Cookie`` value (HttpOnly; ``Secure`` via ``FABRIKANT_COOKIE_SECURE=1``); ``max_age=0`` deletes."""
    attrs = f"{name}={value}; Path=/; HttpOnly; SameSite={same_site}; Max-Age={max_age}"
    if os.environ.get("FABRIKANT_COOKIE_SECURE") == "1":
        attrs += "; Secure"
    return attrs


def set_cookie_header(sid: str, *, clear: bool = False) -> str:
    """Session-``Set-Cookie`` (SameSite=Strict, Laufzeit ``session_ttl()``)."""
    return cookie_header(COOKIE_NAME, "" if clear else sid, 0 if clear else session_ttl())
