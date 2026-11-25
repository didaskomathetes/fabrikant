"""OpenID Connect login (authorization code flow + PKCE) with role mapping (ADR 0046).

Generic client for any OIDC IdP (Cognito, Entra ID, Keycloak, Okta, Google): discovery via
``<issuer>/.well-known/openid-configuration``, ID token verification (signature via JWKS, ``iss``, ``aud``,
``exp``, ``nonce``) and mapping of the group claims to fabrikant roles. The crypto comes from the
optional extra ``fabrikant[oidc]`` (PyJWT); configuration via ``FABRIKANT_OIDC_*`` environment variables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import auth
from .i18n import t

DISCOVERY_PATH = "/.well-known/openid-configuration"
CALLBACK_PATH = "/api/oidc/callback"
LOGIN_PATH = "/api/login/oidc"
DEFAULT_SCOPES = "openid profile email"
DEFAULT_GROUPS_CLAIM = "groups"
HTTP_TIMEOUT_S = 10
PENDING_TTL_S = 600  # Time within which the user must complete the login at the IdP
MAX_PENDING = 1000  # upper bound on pending login attempts (protects against memory flooding)
LEEWAY_S = 30  # clock skew vs. the IdP
# Asymmetric algorithms only: HS*/none would allow token forgery with the client secret.
SIGNING_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "ES256", "ES384"]
USERNAME_CLAIMS = ("preferred_username", "email", "sub")
INSTALL_HINT = "pip install 'fabrikant[oidc]'"
# Binds the login attempt to the browser that started it (protection against login CSRF: otherwise
# an attacker could slip a victim their own code+state). SameSite=Lax, because
# the callback is a top-level navigation from the IdP (cross-site).
STATE_COOKIE = "fabrikant_oidc_state"


class OidcError(auth.AuthError):
    """Error in OIDC configuration or login (the message is meant for the user)."""


class NoRoleError(OidcError):
    """User is authenticated at the IdP but has no group mapped to a fabrikant role."""


@dataclass(frozen=True)
class OidcConfig:
    """OIDC-Konfiguration (``from_env``). ``role_map`` bildet IdP-Gruppen auf Fabrikant-Rollen ab."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    role_map: dict[str, str]
    scopes: str = DEFAULT_SCOPES
    groups_claim: str = DEFAULT_GROUPS_CLAIM

    @classmethod
    def from_env(cls) -> OidcConfig:
        """Aus ``FABRIKANT_OIDC_ISSUER/_CLIENT_ID/_CLIENT_SECRET/_REDIRECT_URI/_ROLE_MAP[/_SCOPES/_GROUPS_CLAIM]``."""
        env = {k: os.environ.get(f"FABRIKANT_OIDC_{k}", "").strip() for k in _ENV_KEYS}
        missing = [f"FABRIKANT_OIDC_{k}" for k in _REQUIRED_ENV if not env[k]]
        if missing:
            raise OidcError(t("oidc.oidc_configuration_incomplete_missing", v1=", ".join(missing)))
        issuer = env["ISSUER"].rstrip("/")
        _require_https(issuer, "FABRIKANT_OIDC_ISSUER")
        return cls(
            issuer=issuer,
            client_id=env["CLIENT_ID"],
            client_secret=env["CLIENT_SECRET"],
            redirect_uri=env["REDIRECT_URI"],
            role_map=parse_role_map(env["ROLE_MAP"]),
            scopes=env["SCOPES"] or DEFAULT_SCOPES,
            groups_claim=env["GROUPS_CLAIM"] or DEFAULT_GROUPS_CLAIM,
        )


_ENV_KEYS = ("ISSUER", "CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI", "ROLE_MAP", "SCOPES", "GROUPS_CLAIM")
_REQUIRED_ENV = ("ISSUER", "CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI", "ROLE_MAP")


def parse_role_map(spec: str) -> dict[str, str]:
    """``"gruppe=rolle,gruppe2=rolle2"`` → Dict; unbekannte Rollen/Syntaxfehler → ``OidcError``."""
    mapping: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        group, sep, role = part.rpartition("=")
        group, role = group.strip(), role.strip()
        if not (sep and group) or role not in auth.ROLES:
            raise OidcError(t("oidc.fabrikant_oidc_role_map_invalid", part=part, v2=", ".join(auth.ROLES)))
        mapping[group] = role
    if not mapping:
        raise OidcError(t("oidc.fabrikant_oidc_role_map_is"))
    return mapping


def _require_https(url: str, what: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" or (parsed.scheme == "http" and auth.is_loopback(parsed.hostname or "")):
        return
    raise OidcError(t("oidc.must_be_https_http_only", what=what, url=url))


def _jwt() -> Any:
    """Import PyJWT; without the extra, a clear error message."""
    try:
        import jwt
    except ImportError as e:
        raise OidcError(t("oidc.fabrikant_auth_oidc_needs_the", v1=INSTALL_HINT)) from e
    return jwt


def check_ready() -> OidcConfig:
    """Check configuration + dependencies (at server start); returns the configuration."""
    _jwt()
    return OidcConfig.from_env()


# ---------------------------------------------------------------- IdP-Kommunikation
def _http_json(url: str, form: dict[str, str] | None = None) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:  # noqa: S310  # nosec: B310 — scheme checked
            doc = json.loads(r.read())
    except (OSError, ValueError) as e:  # URLError/HTTPError are OSError
        raise OidcError(f"IdP-Anfrage an {url} fehlgeschlagen: {e}") from e
    if not isinstance(doc, dict):
        raise OidcError(t("oidc.idp_response_from_is_not", url=url))
    return doc


_discovery_cache: dict[str, dict[str, Any]] = {}
_jwks_clients: dict[str, Any] = {}
_cache_lock = threading.Lock()


def discovery(cfg: OidcConfig) -> dict[str, Any]:
    """Discovery document of the IdP (cached); ``issuer`` must match the configuration exactly."""
    with _cache_lock:
        doc = _discovery_cache.get(cfg.issuer)
    if doc is None:
        doc = _http_json(cfg.issuer + DISCOVERY_PATH)
        if doc.get("issuer") != cfg.issuer:
            raise OidcError(t("oidc.discovery_issuer_does_not_match", v1=doc.get("issuer"), v2=cfg.issuer))
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(doc.get(key), str):
                raise OidcError(t("oidc.discovery_document_without", key=key))
            _require_https(doc[key], key)
        with _cache_lock:
            _discovery_cache[cfg.issuer] = doc
    return doc


def _signing_key(jwks_uri: str, token: str) -> Any:
    jwt = _jwt()
    with _cache_lock:
        client = _jwks_clients.get(jwks_uri)
        if client is None:
            client = _jwks_clients[jwks_uri] = jwt.PyJWKClient(jwks_uri, timeout=HTTP_TIMEOUT_S)
    return client.get_signing_key_from_jwt(token).key


def reset_caches() -> None:
    """Discovery-/JWKS-/Login-Zustand verwerfen (Tests, Konfigurationswechsel)."""
    with _cache_lock:
        _discovery_cache.clear()
        _jwks_clients.clear()
    with _pending_lock:
        _pending.clear()


# ---------------------------------------------------------------- Login-Ablauf
@dataclass
class _Pending:
    verifier: str  # PKCE code_verifier
    nonce: str
    expires: float


_pending: dict[str, _Pending] = {}  # state → laufender Login-Versuch
_pending_lock = threading.Lock()


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass(frozen=True)
class LoginRedirect:
    """Start of a login attempt: authorize URL at the IdP and ``state`` for the browser cookie."""

    url: str
    state: str

    def state_cookie(self) -> str:
        """``Set-Cookie`` for :data:`STATE_COOKIE` (lifetime like the login attempt)."""
        return auth.cookie_header(STATE_COOKIE, self.state, PENDING_TTL_S, same_site="Lax")


def clear_state_cookie() -> str:
    """``Set-Cookie`` that deletes :data:`STATE_COOKIE` after the callback."""
    return auth.cookie_header(STATE_COOKIE, "", 0, same_site="Lax")


def begin_login(cfg: OidcConfig) -> LoginRedirect:
    """Neuen Login-Versuch anlegen (state, nonce, PKCE); liefert Authorize-URL + ``state``."""
    endpoint = discovery(cfg)["authorization_endpoint"]
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    now = time.time()
    with _pending_lock:
        for key in [k for k, p in _pending.items() if p.expires <= now]:
            del _pending[key]
        if len(_pending) >= MAX_PENDING:
            raise OidcError(t("oidc.too_many_open_login_attempts"))
        _pending[state] = _Pending(verifier, nonce, now + PENDING_TTL_S)
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "scope": cfg.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    sep = "&" if "?" in endpoint else "?"
    return LoginRedirect(f"{endpoint}{sep}{query}", state)


def _take_pending(state: str, browser_state: str | None) -> _Pending:
    """Take the login attempt for ``state`` (once only); unknown/expired → ``OidcError``.

    ``browser_state`` (value of :data:`STATE_COOKIE`) must match ``state``: the callback
    must arrive in the same browser that started the login (login CSRF)."""
    if not (browser_state and hmac.compare_digest(browser_state, state)):
        raise OidcError(t("oidc.login_attempt_does_not_belong"))
    with _pending_lock:
        found = _pending.pop(state, None)  # state is 256 bits of randomness → dict lookup suffices
    if found is None or found.expires <= time.time():
        raise OidcError(t("oidc.login_attempt_unknown_or_expired"))
    return found


def _groups(raw: Any) -> list[str]:
    """Normalize the group claim: string (e.g. a single group) or list of strings; otherwise empty."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [g for g in raw if isinstance(g, str)]
    return []


def map_role(claims: dict[str, Any], cfg: OidcConfig) -> str:
    """Highest fabrikant role from the group claims; no mapped group → ``NoRoleError``."""
    roles = [cfg.role_map[g] for g in _groups(claims.get(cfg.groups_claim)) if g in cfg.role_map]
    if not roles:
        raise NoRoleError(t("oidc.access_denied_none_of_your"))
    return max(roles, key=auth.ROLES.index)


def validate_id_token(cfg: OidcConfig, doc: dict[str, Any], id_token: str, nonce: str) -> dict[str, Any]:
    """Verify the ID token (signature/JWKS, ``iss``, ``aud``, ``exp``, ``iat``, ``nonce``) and return the claims."""
    jwt = _jwt()
    try:
        key = _signing_key(doc["jwks_uri"], id_token)
        claims = jwt.decode(
            id_token,
            key,
            algorithms=SIGNING_ALGORITHMS,
            audience=cfg.client_id,
            issuer=doc["issuer"],
            leeway=LEEWAY_S,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise OidcError(t("oidc.id_token_invalid", e=e)) from e
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcError(t("oidc.id_token_invalid_nonce_does"))
    # OIDC Core 3.1.3.7: with several audiences azp (if present) must be the own client.
    aud, azp = claims["aud"], claims.get("azp")
    if (azp is not None or (isinstance(aud, list) and len(aud) > 1)) and azp != cfg.client_id:
        raise OidcError(t("oidc.id_token_invalid_azp_is"))
    return claims


def complete_login(cfg: OidcConfig, code: str, state: str, browser_state: str | None) -> tuple[str, str]:
    """Complete the callback: check state against browser, exchange code for token, verify ID token, map role.

    ``browser_state`` is the value of :data:`STATE_COOKIE`. Returns ``(username, role)``;
    errors as ``OidcError`` (``NoRoleError`` without a role)."""
    pending = _take_pending(state, browser_state)
    doc = discovery(cfg)
    tokens = _http_json(
        doc["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code_verifier": pending.verifier,
        },
    )
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        raise OidcError(t("oidc.token_response_without_id_token"))
    claims = validate_id_token(cfg, doc, id_token, pending.nonce)
    role = map_role(claims, cfg)
    name = next((str(claims[c]) for c in USERNAME_CLAIMS if claims.get(c)), str(claims["sub"]))
    return name, role
