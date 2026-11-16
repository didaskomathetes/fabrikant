# ruff: noqa: F811  (Fixtures aus test_server_api werden importiert und als Argumente genutzt)
"""OIDC-Login (ADR 0046): Fake-IdP mit signierten Test-Tokens, Rollen-Mapping, HTTP-Flow."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_server_api import _req, fabrik_home, fabrik_url  # noqa: F401

from fabrik import auth, oidc

CLIENT_ID = "fabrik-client"
KID = "test-key"
ROLE_MAP = "fab-admins=admin,fab-ops=operator,fab-view=viewer"
CALLBACK = "http://localhost/api/oidc/callback"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


class FakeIdP:
    """Minimaler IdP: Discovery, JWKS, Token-Endpoint (mit PKCE-Prüfung). Tokens per ``claims``/``key`` steuerbar."""

    def __init__(self):
        self.claims: dict = {}  # Overrides/Ergänzungen der ID-Token-Claims; Wert None → Claim entfernen
        self.key = _KEY
        self.drop_id_token = False
        self.codes: dict[str, dict] = {}  # code → {challenge, nonce}
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/.well-known/openid-configuration":
                    self._send(idp.discovery_doc())
                elif self.path == "/jwks":
                    pub = _KEY.public_key().public_numbers()
                    jwk = {"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256", "n": _b64(pub.n), "e": _b64(pub.e)}
                    self._send({"keys": [jwk]})
                else:
                    self._send({}, 404)

            def do_POST(self):
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                self._send(*idp.token(form))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.issuer_override: str | None = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def discovery_doc(self):
        return {
            "issuer": self.issuer_override or self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
        }

    def authorize(self, url: str) -> tuple[str, str]:
        """Simuliert den Browser-Besuch der Authorize-URL; liefert ``(code, state)``."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = {"challenge": q["code_challenge"], "nonce": q["nonce"]}
        return code, q["state"]

    def token(self, form):
        entry = self.codes.get(form.get("code", ""))
        if not entry or form.get("client_secret") != "s3cret" or form.get("client_id") != CLIENT_ID:
            return {"error": "invalid_grant"}, 400
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != entry["challenge"]:
            return {"error": "invalid_grant", "error_description": "PKCE"}, 400
        if self.drop_id_token:
            return {"access_token": "x"}, 200
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "u-1",
            "iat": now,
            "exp": now + 300,
            "nonce": entry["nonce"],
            "preferred_username": "anna",
            "groups": ["fab-ops"],
        } | self.claims
        claims = {k: v for k, v in claims.items() if v is not None}
        token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": KID})
        return {"id_token": token, "access_token": "x"}, 200

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdP()
    monkeypatch.setenv("FABRIK_OIDC_ISSUER", fake.issuer)
    monkeypatch.setenv("FABRIK_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("FABRIK_OIDC_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("FABRIK_OIDC_REDIRECT_URI", CALLBACK)
    monkeypatch.setenv("FABRIK_OIDC_ROLE_MAP", ROLE_MAP)
    monkeypatch.delenv("FABRIK_OIDC_GROUPS_CLAIM", raising=False)
    monkeypatch.delenv("FABRIK_OIDC_SCOPES", raising=False)
    oidc.reset_caches()
    auth._sessions.clear()
    yield fake
    fake.close()
    oidc.reset_caches()
    auth._sessions.clear()


def _start(idp: FakeIdP) -> tuple[oidc.OidcConfig, str, str]:
    """Login beginnen und beim Fake-IdP autorisieren; liefert ``(cfg, code, state)``."""
    cfg = oidc.OidcConfig.from_env()
    code, state = idp.authorize(oidc.begin_login(cfg).url)
    return cfg, code, state


def _login(idp: FakeIdP) -> tuple[str, str]:
    cfg, code, state = _start(idp)
    return oidc.complete_login(cfg, code, state, state)


# ---------------------------------------------------------------- Konfiguration
def test_parse_role_map():
    assert oidc.parse_role_map(" a=admin, b = viewer ,") == {"a": "admin", "b": "viewer"}
    assert oidc.parse_role_map("grp=with=eq=operator") == {"grp=with=eq": "operator"}


@pytest.mark.parametrize("spec", ["", " , ", "a", "a=root", "=admin"])
def test_parse_role_map_invalid(spec):
    with pytest.raises(oidc.OidcError):
        oidc.parse_role_map(spec)


def test_config_from_env(idp):
    cfg = oidc.OidcConfig.from_env()
    assert cfg.issuer == idp.issuer and cfg.groups_claim == "groups" and cfg.scopes == oidc.DEFAULT_SCOPES
    assert cfg.role_map["fab-admins"] == "admin"


def test_config_missing_vars(idp, monkeypatch):
    monkeypatch.delenv("FABRIK_OIDC_CLIENT_ID")
    monkeypatch.delenv("FABRIK_OIDC_ROLE_MAP")
    with pytest.raises(oidc.OidcError, match="FABRIK_OIDC_CLIENT_ID.*FABRIK_OIDC_ROLE_MAP"):
        oidc.OidcConfig.from_env()


def test_config_requires_https_for_remote_issuer(idp, monkeypatch):
    monkeypatch.setenv("FABRIK_OIDC_ISSUER", "http://idp.example.com")
    with pytest.raises(oidc.OidcError, match="https"):
        oidc.OidcConfig.from_env()


def test_auth_mode_oidc_accepted(monkeypatch):
    monkeypatch.setenv("FABRIK_AUTH", "oidc")
    assert auth.mode() == auth.MODE_OIDC
    auth.check_bind("0.0.0.0")  # oidc ist Auth → Nicht-Loopback-Bind erlaubt


def test_missing_extra_gives_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "jwt":
            raise ImportError("no jwt")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(oidc.OidcError, match=r"fabrik\[oidc\]"):
        oidc.check_ready()


# ---------------------------------------------------------------- Login-Ablauf
def test_happy_path_maps_role(idp):
    assert _login(idp) == ("anna", "operator")


def test_authorize_url_has_pkce_state_nonce(idp):
    start = oidc.begin_login(oidc.OidcConfig.from_env())
    url = start.url
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith(idp.issuer + "/authorize?")
    assert {"state", "nonce", "code_challenge"} <= q.keys() and q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == CALLBACK and q["scope"] == oidc.DEFAULT_SCOPES
    assert q["state"] == start.state


def test_state_cookie_binds_browser(monkeypatch):
    start = oidc.LoginRedirect("https://idp/authorize", "st")
    cookie = start.state_cookie()
    assert cookie.startswith(f"{oidc.STATE_COOKIE}=st;") and "SameSite=Lax" in cookie and "HttpOnly" in cookie
    assert f"Max-Age={oidc.PENDING_TTL_S}" in cookie and "Secure" not in cookie
    assert "Max-Age=0" in oidc.clear_state_cookie()
    monkeypatch.setenv("FABRIK_COOKIE_SECURE", "1")
    assert "Secure" in start.state_cookie()


def test_highest_mapped_role_wins(idp):
    idp.claims = {"groups": ["fab-view", "fab-admins", "unrelated"]}
    assert _login(idp)[1] == "admin"


def test_group_claim_as_string_and_custom_claim(idp, monkeypatch):
    monkeypatch.setenv("FABRIK_OIDC_GROUPS_CLAIM", "cognito:groups")
    idp.claims = {"groups": None, "cognito:groups": "fab-admins"}
    assert _login(idp)[1] == "admin"


@pytest.mark.parametrize("groups", [[], ["other"], None, 42, {"fab-admins": 1}])
def test_user_without_mapped_role_rejected(idp, groups):
    idp.claims = {"groups": groups}
    with pytest.raises(oidc.NoRoleError, match="Rolle"):
        _login(idp)


def test_username_falls_back_to_email_then_sub(idp):
    idp.claims = {"preferred_username": None, "email": "a@x.org"}
    assert _login(idp)[0] == "a@x.org"
    idp.claims = {"preferred_username": None}
    assert _login(idp)[0] == "u-1"


def test_expired_token_rejected(idp):
    idp.claims = {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}
    with pytest.raises(oidc.OidcError, match="ungültig"):
        _login(idp)


@pytest.mark.parametrize(
    "override",
    [{"aud": "someone-else"}, {"iss": "http://evil.example"}, {"exp": None}, {"sub": None}],
)
def test_invalid_claims_rejected(idp, override):
    idp.claims = override
    with pytest.raises(oidc.OidcError, match="ID-Token ungültig"):
        _login(idp)


@pytest.mark.parametrize(
    "override",
    [{"aud": [CLIENT_ID, "other"]}, {"aud": [CLIENT_ID, "other"], "azp": "other"}, {"azp": "other"}],
)
def test_azp_mismatch_rejected(idp, override):
    idp.claims = override
    with pytest.raises(oidc.OidcError, match="azp"):
        _login(idp)


@pytest.mark.parametrize("override", [{"aud": [CLIENT_ID]}, {"aud": [CLIENT_ID, "other"], "azp": CLIENT_ID}])
def test_azp_accepted(idp, override):
    idp.claims = override
    assert _login(idp) == ("anna", "operator")


def test_wrong_signature_rejected(idp):
    idp.key = _OTHER_KEY
    with pytest.raises(oidc.OidcError, match="ID-Token ungültig"):
        _login(idp)


def test_nonce_mismatch_rejected(idp):
    idp.claims = {"nonce": "attacker"}
    with pytest.raises(oidc.OidcError, match="nonce"):
        _login(idp)


def test_missing_id_token_rejected(idp):
    idp.drop_id_token = True
    with pytest.raises(oidc.OidcError, match="id_token"):
        _login(idp)


def test_unknown_state_rejected(idp):
    with pytest.raises(oidc.OidcError, match="state"):
        oidc.complete_login(oidc.OidcConfig.from_env(), "c", "never-issued", "never-issued")


@pytest.mark.parametrize("browser_state", [None, "", "other-browser"])
def test_state_from_other_browser_rejected(idp, browser_state):
    """Login-CSRF: code+state eines fremden Login-Versuchs ohne passendes State-Cookie."""
    cfg, code, state = _start(idp)
    with pytest.raises(oidc.OidcError, match="Browser"):
        oidc.complete_login(cfg, code, state, browser_state)
    # Fehlversuch verbraucht den Login-Versuch nicht; der richtige Browser kann ihn abschließen
    assert oidc.complete_login(cfg, code, state, state) == ("anna", "operator")


def test_state_is_single_use(idp):
    cfg, code, state = _start(idp)
    oidc.complete_login(cfg, code, state, state)
    with pytest.raises(oidc.OidcError, match="state"):
        oidc.complete_login(cfg, code, state, state)


def test_expired_state_rejected(idp, monkeypatch):
    cfg, code, state = _start(idp)
    real_time = time.time
    monkeypatch.setattr(oidc.time, "time", lambda: real_time() + oidc.PENDING_TTL_S + 1)
    with pytest.raises(oidc.OidcError, match="abgelaufen"):
        oidc.complete_login(cfg, code, state, state)


def test_pending_limit(idp, monkeypatch):
    monkeypatch.setattr(oidc, "MAX_PENDING", 2)
    cfg = oidc.OidcConfig.from_env()
    oidc.begin_login(cfg)
    oidc.begin_login(cfg)
    with pytest.raises(oidc.OidcError, match="zu viele"):
        oidc.begin_login(cfg)


def test_bad_code_surfaces_idp_error(idp):
    cfg, _, state = _start(idp)
    with pytest.raises(oidc.OidcError, match="fehlgeschlagen"):
        oidc.complete_login(cfg, "bogus", state, state)


def test_discovery_issuer_mismatch(idp):
    idp.issuer_override = "http://other.example"
    with pytest.raises(oidc.OidcError, match="passt nicht"):
        oidc.begin_login(oidc.OidcConfig.from_env())


def test_discovery_unreachable(idp, monkeypatch):
    monkeypatch.setenv("FABRIK_OIDC_ISSUER", "http://127.0.0.1:1")
    with pytest.raises(oidc.OidcError, match="fehlgeschlagen"):
        oidc.begin_login(oidc.OidcConfig.from_env())


def test_discovery_missing_endpoint(idp, monkeypatch):
    monkeypatch.setattr(idp, "discovery_doc", lambda: {"issuer": idp.issuer})
    with pytest.raises(oidc.OidcError, match="authorization_endpoint"):
        oidc.begin_login(oidc.OidcConfig.from_env())


def test_discovery_is_cached(idp):
    cfg = oidc.OidcConfig.from_env()
    first = oidc.discovery(cfg)
    idp.close()
    assert oidc.discovery(cfg) is first


# ---------------------------------------------------------------- HTTP-Flow
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _get_noredirect(url, headers=None):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=headers or {})) as r:
            return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


@pytest.fixture
def oidc_url(fabrik_url, idp, monkeypatch):
    monkeypatch.setenv("FABRIK_AUTH", "oidc")
    return fabrik_url


def _callback(url, idp, *, with_state_cookie=True, **claims):
    idp.claims = claims
    status, _, headers = _get_noredirect(url + "/api/login/oidc")
    assert status == 302
    state_cookie = headers["Set-Cookie"].split(";")[0]
    assert state_cookie.startswith(oidc.STATE_COOKIE + "=")
    code, state = idp.authorize(headers["Location"])
    query = urllib.parse.urlencode({"code": code, "state": state})
    return _get_noredirect(
        f"{url}/api/oidc/callback?{query}", headers={"Cookie": state_cookie} if with_state_cookie else None
    )


def _session_cookie(headers) -> str:
    """Session-Cookie aus den (ggf. mehreren) Set-Cookie-Headern der Callback-Antwort."""
    cookies = [c.split(";")[0] for c in headers.get_all("Set-Cookie")]
    assert f"{oidc.STATE_COOKIE}=" in cookies  # State-Cookie wird gelöscht
    return next(c for c in cookies if c.startswith(auth.COOKIE_NAME + "="))


def test_http_flow_creates_session_with_role(oidc_url, idp):
    status, _, headers = _callback(oidc_url, idp, groups=["fab-admins"])
    assert status == 302 and headers["Location"] == "/"
    cookie = _session_cookie(headers)
    status, body, _ = _req(oidc_url + "/api/session", headers={"Cookie": cookie})
    session = json.loads(body)
    assert (session["mode"], session["user"], session["role"]) == ("oidc", "anna", "admin")
    assert session["csrf"]
    # Rolle gilt auch für geschützte Routes; ohne Cookie 401
    assert _req(oidc_url + "/api/projects", headers={"Cookie": cookie})[0] == 200
    assert _req(oidc_url + "/api/projects")[0] == 401


def test_http_viewer_cannot_write(oidc_url, idp):
    _, _, headers = _callback(oidc_url, idp, groups=["fab-view"])
    cookie = _session_cookie(headers)
    session = json.loads(_req(oidc_url + "/api/session", headers={"Cookie": cookie})[1])
    hdrs = {"Cookie": cookie, "X-CSRF-Token": session["csrf"], "Origin": oidc_url}
    assert _req(oidc_url + "/api/runs", "POST", {}, headers=hdrs)[0] == 403


def test_http_user_without_role_gets_403_and_no_session(oidc_url, idp):
    status, body, headers = _callback(oidc_url, idp, groups=["nobody"])
    assert status == 403 and "Rolle" in json.loads(body)["error"]
    assert "Set-Cookie" not in headers


def test_http_invalid_token_gets_401(oidc_url, idp):
    status, body, headers = _callback(oidc_url, idp, aud="other")
    assert status == 401 and "Set-Cookie" not in headers


def test_http_callback_without_state_cookie_rejected(oidc_url, idp):
    """Login-CSRF: Callback in einem Browser, der den Login nicht begonnen hat → keine Session."""
    status, body, headers = _callback(oidc_url, idp, with_state_cookie=False)
    assert status == 401 and "Browser" in json.loads(body)["error"]
    assert "Set-Cookie" not in headers


def test_http_idp_error_param(oidc_url):
    status, body, _ = _get_noredirect(oidc_url + "/api/oidc/callback?error=access_denied&error_description=nope")
    assert status == 401 and "access_denied" in json.loads(body)["error"]


def test_http_login_route_reports_misconfiguration(oidc_url, monkeypatch):
    monkeypatch.setenv("FABRIK_OIDC_CLIENT_ID", "")
    status, body, _ = _get_noredirect(oidc_url + "/api/login/oidc")
    assert status == 502 and "FABRIK_OIDC_CLIENT_ID" in json.loads(body)["error"]


def test_oidc_routes_inactive_in_other_modes(fabrik_url, idp, monkeypatch):
    monkeypatch.setenv("FABRIK_AUTH", "none")
    assert _get_noredirect(fabrik_url + "/api/login/oidc")[0] == 404


def test_oidc_session_survives_local_user_store_changes(idp):
    """OIDC-Sessions hängen nicht am lokalen Benutzer-Store (Rolle in der Session fixiert)."""
    sid, who = auth.create_session("anna", "operator")
    assert auth.session_principal(sid) == who
    auth.drop_user_sessions("anna")  # lokale Operation darf OIDC-Session nicht beenden
    assert auth.session_principal(sid) == who
