"""OpenID-Connect-Login (Authorization Code Flow + PKCE) mit Rollen-Mapping (ADR 0046).

Generischer Client für jeden OIDC-IdP (Cognito, Entra ID, Keycloak, Okta, Google): Discovery über
``<issuer>/.well-known/openid-configuration``, ID-Token-Prüfung (Signatur per JWKS, ``iss``, ``aud``,
``exp``, ``nonce``) und Abbildung der Gruppen-Claims auf Fabrik-Rollen. Die Krypto kommt aus dem
optionalen Extra ``fabrik[oidc]`` (PyJWT); Konfiguration per ``FABRIK_OIDC_*``-Umgebungsvariablen.
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

DISCOVERY_PATH = "/.well-known/openid-configuration"
CALLBACK_PATH = "/api/oidc/callback"
LOGIN_PATH = "/api/login/oidc"
DEFAULT_SCOPES = "openid profile email"
DEFAULT_GROUPS_CLAIM = "groups"
HTTP_TIMEOUT_S = 10
PENDING_TTL_S = 600  # Zeit, in der der Nutzer den Login beim IdP abschließen muss
MAX_PENDING = 1000  # Obergrenze offener Login-Versuche (Schutz vor Speicherflut)
LEEWAY_S = 30  # Uhrenabweichung zum IdP
# Nur asymmetrische Verfahren: HS*/none würden Token-Fälschung mit dem Client-Secret erlauben.
SIGNING_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "ES256", "ES384"]
USERNAME_CLAIMS = ("preferred_username", "email", "sub")
INSTALL_HINT = "pip install 'fabrik[oidc]'"
# Bindet den Login-Versuch an den Browser, der ihn begonnen hat (Schutz vor Login-CSRF: sonst
# könnte ein Angreifer einem Opfer seinen eigenen code+state unterschieben). SameSite=Lax, weil
# der Callback eine Top-Level-Navigation vom IdP (cross-site) ist.
STATE_COOKIE = "fabrik_oidc_state"


class OidcError(auth.AuthError):
    """Fehler in OIDC-Konfiguration oder -Login (Meldung ist für den Benutzer bestimmt)."""


class NoRoleError(OidcError):
    """Benutzer ist am IdP authentifiziert, hat aber keine auf eine Fabrik-Rolle gemappte Gruppe."""


@dataclass(frozen=True)
class OidcConfig:
    """OIDC-Konfiguration (``from_env``). ``role_map`` bildet IdP-Gruppen auf Fabrik-Rollen ab."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    role_map: dict[str, str]
    scopes: str = DEFAULT_SCOPES
    groups_claim: str = DEFAULT_GROUPS_CLAIM

    @classmethod
    def from_env(cls) -> OidcConfig:
        """Aus ``FABRIK_OIDC_ISSUER/_CLIENT_ID/_CLIENT_SECRET/_REDIRECT_URI/_ROLE_MAP[/_SCOPES/_GROUPS_CLAIM]``."""
        env = {k: os.environ.get(f"FABRIK_OIDC_{k}", "").strip() for k in _ENV_KEYS}
        missing = [f"FABRIK_OIDC_{k}" for k in _REQUIRED_ENV if not env[k]]
        if missing:
            raise OidcError(f"OIDC-Konfiguration unvollständig: {', '.join(missing)} fehlt")
        issuer = env["ISSUER"].rstrip("/")
        _require_https(issuer, "FABRIK_OIDC_ISSUER")
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
            raise OidcError(
                f"FABRIK_OIDC_ROLE_MAP: '{part}' ungültig (Format gruppe=rolle, Rollen: {', '.join(auth.ROLES)})"
            )
        mapping[group] = role
    if not mapping:
        raise OidcError("FABRIK_OIDC_ROLE_MAP ist leer — ohne Mapping könnte sich niemand anmelden")
    return mapping


def _require_https(url: str, what: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" or (parsed.scheme == "http" and auth.is_loopback(parsed.hostname or "")):
        return
    raise OidcError(f"{what} muss https:// sein (http nur für Loopback): {url}")


def _jwt() -> Any:
    """PyJWT importieren; ohne Extra verständliche Fehlermeldung."""
    try:
        import jwt
    except ImportError as e:
        raise OidcError(
            f"FABRIK_AUTH=oidc braucht das Extra fabrik[oidc] (PyJWT + cryptography): {INSTALL_HINT}"
        ) from e
    return jwt


def check_ready() -> OidcConfig:
    """Konfiguration + Abhängigkeiten prüfen (beim Serverstart); liefert die Konfiguration."""
    _jwt()
    return OidcConfig.from_env()


# ---------------------------------------------------------------- IdP-Kommunikation
def _http_json(url: str, form: dict[str, str] | None = None) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:  # noqa: S310  # nosec: B310 — Schema geprüft
            doc = json.loads(r.read())
    except (OSError, ValueError) as e:  # URLError/HTTPError sind OSError
        raise OidcError(f"IdP-Anfrage an {url} fehlgeschlagen: {e}") from e
    if not isinstance(doc, dict):
        raise OidcError(f"IdP-Antwort von {url} ist kein JSON-Objekt")
    return doc


_discovery_cache: dict[str, dict[str, Any]] = {}
_jwks_clients: dict[str, Any] = {}
_cache_lock = threading.Lock()


def discovery(cfg: OidcConfig) -> dict[str, Any]:
    """Discovery-Dokument des IdP (gecacht); ``issuer`` muss exakt zur Konfiguration passen."""
    with _cache_lock:
        doc = _discovery_cache.get(cfg.issuer)
    if doc is None:
        doc = _http_json(cfg.issuer + DISCOVERY_PATH)
        if doc.get("issuer") != cfg.issuer:
            raise OidcError(f"Discovery-issuer {doc.get('issuer')!r} passt nicht zu FABRIK_OIDC_ISSUER {cfg.issuer!r}")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(doc.get(key), str):
                raise OidcError(f"Discovery-Dokument ohne '{key}'")
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
    """Start eines Login-Versuchs: Authorize-URL beim IdP und ``state`` für das Browser-Cookie."""

    url: str
    state: str

    def state_cookie(self) -> str:
        """``Set-Cookie`` für :data:`STATE_COOKIE` (Laufzeit wie der Login-Versuch)."""
        return auth.cookie_header(STATE_COOKIE, self.state, PENDING_TTL_S, same_site="Lax")


def clear_state_cookie() -> str:
    """``Set-Cookie``, der :data:`STATE_COOKIE` nach dem Callback löscht."""
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
            raise OidcError("zu viele offene Login-Versuche — bitte später erneut versuchen")
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
    """Login-Versuch zum ``state`` entnehmen (einmalig); unbekannt/abgelaufen → ``OidcError``.

    ``browser_state`` (Wert von :data:`STATE_COOKIE`) muss zum ``state`` passen: der Callback
    muss im selben Browser ankommen, der den Login begonnen hat (Login-CSRF)."""
    if not (browser_state and hmac.compare_digest(browser_state, state)):
        raise OidcError("Login-Versuch gehört nicht zu diesem Browser (state) — bitte erneut anmelden")
    with _pending_lock:
        found = _pending.pop(state, None)  # state ist 256 Bit zufällig → Dict-Lookup genügt
    if found is None or found.expires <= time.time():
        raise OidcError("Login-Versuch unbekannt oder abgelaufen (state) — bitte erneut anmelden")
    return found


def _groups(raw: Any) -> list[str]:
    """Gruppen-Claim normalisieren: String (z. B. einzelne Gruppe) oder Liste von Strings; sonst leer."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [g for g in raw if isinstance(g, str)]
    return []


def map_role(claims: dict[str, Any], cfg: OidcConfig) -> str:
    """Höchste Fabrik-Rolle aus den Gruppen-Claims; keine gemappte Gruppe → ``NoRoleError``."""
    roles = [cfg.role_map[g] for g in _groups(claims.get(cfg.groups_claim)) if g in cfg.role_map]
    if not roles:
        raise NoRoleError("Zugriff verweigert: keine Ihrer IdP-Gruppen ist einer Fabrik-Rolle zugeordnet")
    return max(roles, key=auth.ROLES.index)


def validate_id_token(cfg: OidcConfig, doc: dict[str, Any], id_token: str, nonce: str) -> dict[str, Any]:
    """ID-Token prüfen (Signatur/JWKS, ``iss``, ``aud``, ``exp``, ``iat``, ``nonce``) und Claims liefern."""
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
        raise OidcError(f"ID-Token ungültig: {e}") from e
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcError("ID-Token ungültig: nonce stimmt nicht")
    # OIDC Core 3.1.3.7: bei mehreren Audiences muss azp (falls vorhanden) der eigene Client sein.
    aud, azp = claims["aud"], claims.get("azp")
    if (azp is not None or (isinstance(aud, list) and len(aud) > 1)) and azp != cfg.client_id:
        raise OidcError("ID-Token ungültig: azp ist nicht dieser Client")
    return claims


def complete_login(cfg: OidcConfig, code: str, state: str, browser_state: str | None) -> tuple[str, str]:
    """Callback abschließen: state an Browser prüfen, Code gegen Token tauschen, ID-Token prüfen, Rolle mappen.

    ``browser_state`` ist der Wert von :data:`STATE_COOKIE`. Liefert ``(benutzername, rolle)``;
    Fehler als ``OidcError`` (``NoRoleError`` ohne Rolle)."""
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
        raise OidcError("Token-Antwort ohne id_token")
    claims = validate_id_token(cfg, doc, id_token, pending.nonce)
    role = map_role(claims, cfg)
    name = next((str(claims[c]) for c in USERNAME_CLAIMS if claims.get(c)), str(claims["sub"]))
    return name, role
