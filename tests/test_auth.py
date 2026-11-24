# ruff: noqa: F811  (Fixtures aus test_server_api werden importiert und als Argumente genutzt)
"""Auth (ADR 0030): Passwörter, Benutzer-Store, Sessions, Rollenmatrix, CSRF, Bind-Check, CLI, Audit."""

from __future__ import annotations

import io
import json
import time

import pytest
from helpers import not_none
from test_server_api import RUN_ID, _req, fabrikant_home, fabrikant_url  # noqa: F401

from fabrikant import auth, chat, cli, config, engine, runs
from fabrikant.server import FabrikantHandler

PASSWORD = "correct-horse"


@pytest.fixture
def auth_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.delenv("FABRIKANT_AUTH", raising=False)
    monkeypatch.delenv("FABRIKANT_SESSION_TTL", raising=False)
    auth._sessions.clear()
    yield
    auth._sessions.clear()


@pytest.fixture
def local_url(fabrikant_url, monkeypatch):
    monkeypatch.setenv("FABRIKANT_AUTH", "local")
    auth._sessions.clear()
    for role in auth.ROLES:
        auth.add_user(role, PASSWORD, role)
    return fabrikant_url


def _login(url, user):
    status, body, headers = _req(url + "/api/login", "POST", {"user": user, "password": PASSWORD})
    assert status == 200, body
    cookie = headers["Set-Cookie"].split(";")[0]
    return {"Cookie": cookie, auth.CSRF_HEADER: json.loads(body)["csrf"]}


# ---------------------------------------------------------------- Passwörter / Store
def test_password_hash_roundtrip():
    h = auth.hash_password(PASSWORD)
    assert h.startswith("scrypt$") and PASSWORD not in h
    assert auth.verify_password(PASSWORD, h)
    assert not auth.verify_password("wrong", h)
    assert h != auth.hash_password(PASSWORD)  # Salt


@pytest.mark.parametrize("stored", ["", "garbage", "bcrypt$1$2$3$aa$bb", "scrypt$x$8$1$zz$zz"])
def test_verify_password_rejects_malformed(stored):
    assert auth.verify_password(PASSWORD, stored) is False


def test_user_store_validation(auth_state):
    auth.add_user("anna", PASSWORD, auth.OPERATOR)
    assert auth.load_users()["anna"].get("role") == auth.OPERATOR
    assert (config.STATE_DIR / "users.json").stat().st_mode & 0o777 == 0o600
    for name, pw, role in [("anna", PASSWORD, "viewer"), ("a b", PASSWORD, "viewer"), ("", PASSWORD, "viewer")]:
        with pytest.raises(auth.AuthError):
            auth.add_user(name, pw, role)
    with pytest.raises(auth.AuthError, match="kurz"):
        auth.add_user("bob", "short", "viewer")
    with pytest.raises(auth.AuthError, match="Rolle"):
        auth.add_user("bob", PASSWORD, "root")
    with pytest.raises(auth.AuthError, match="unbekannt"):
        auth.set_role("nobody", "admin")
    with pytest.raises(auth.AuthError):
        auth.set_password("nobody", PASSWORD)


def test_load_users_tolerates_broken_file(auth_state):
    config.STATE_DIR.mkdir(parents=True)
    (config.STATE_DIR / "users.json").write_text("[1")
    assert auth.load_users() == {}
    (config.STATE_DIR / "users.json").write_text("[]")
    assert auth.load_users() == {}


def test_load_users_drops_invalid_entries(auth_state):
    auth.add_user("ok", "pw-long-enough-1", auth.OPERATOR)
    # Absichtlich ungültige Einträge: Roh-JSON, nicht der UserEntry-Typ.
    users: dict[str, object] = {
        **auth.load_users(),
        "nohash": {"role": auth.ADMIN},
        "notdict": "x",
        "badrole": {"hash": "h", "role": "root"},
    }
    (config.STATE_DIR / "users.json").write_text(json.dumps(users))
    assert list(auth.load_users()) == ["ok"]
    assert auth.login("nohash", "whatever") is None
    assert auth.login("notdict", "whatever") is None


def test_load_users_keeps_entry_without_role_and_drops_non_string_hash(auth_state):
    (config.STATE_DIR).mkdir(parents=True, exist_ok=True)
    (config.STATE_DIR / "users.json").write_text(json.dumps({"norole": {"hash": "h"}, "inthash": {"hash": 1}}))
    assert list(auth.load_users()) == ["norole"]


def test_set_password_and_role_take_effect(auth_state):
    auth.add_user("anna", PASSWORD, "viewer")
    sid, _ = not_none(auth.login("anna", PASSWORD))
    auth.set_role("anna", "admin")
    assert auth.session_principal(sid).role == "admin"  # Rolle pro Request nachgeschlagen
    auth.set_password("anna", "another-pass")
    assert auth.session_principal(sid) is None  # alte Sessions beendet
    assert auth.login("anna", PASSWORD) is None
    assert auth.login("anna", "another-pass")


# ---------------------------------------------------------------- Sessions
def test_login_unknown_user_and_wrong_password(auth_state):
    auth.add_user("anna", PASSWORD)
    assert auth.login("ghost", PASSWORD) is None
    assert auth.login("anna", "nope") is None


def test_session_expires(auth_state, monkeypatch):
    auth.add_user("anna", PASSWORD)
    monkeypatch.setenv("FABRIKANT_SESSION_TTL", "120")
    now = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    sid, _ = not_none(auth.login("anna", PASSWORD))
    now[0] += 119
    assert auth.session_principal(sid).name == "anna"
    now[0] += 2
    assert auth.session_principal(sid) is None
    assert sid not in auth._sessions


def test_session_dropped_when_user_deleted(auth_state):
    auth.add_user("anna", PASSWORD)
    sid, _ = not_none(auth.login("anna", PASSWORD))
    (config.STATE_DIR / "users.json").write_text("{}")
    assert auth.session_principal(sid) is None
    assert auth.session_principal(None) is None


def test_logout_idempotent(auth_state):
    auth.add_user("anna", PASSWORD)
    sid, _ = not_none(auth.login("anna", PASSWORD))
    auth.logout(sid)
    auth.logout(sid)
    auth.logout(None)
    assert auth.session_principal(sid) is None


def test_expired_sessions_are_pruned_on_login(auth_state, monkeypatch):
    auth.add_user("anna", PASSWORD)
    now = [0.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    old, _ = not_none(auth.login("anna", PASSWORD))
    now[0] += auth.session_ttl() + 1
    auth.login("anna", PASSWORD)
    assert old not in auth._sessions


def test_session_ttl_parsing(monkeypatch):
    monkeypatch.setenv("FABRIKANT_SESSION_TTL", "abc")
    assert auth.session_ttl() == auth.DEFAULT_SESSION_TTL_S
    monkeypatch.setenv("FABRIKANT_SESSION_TTL", "5")
    assert auth.session_ttl() == 60


def test_cookie_helpers(monkeypatch):
    assert auth.cookie_value(f"a=b; {auth.COOKIE_NAME}=xyz; c=d") == "xyz"
    assert auth.cookie_value("a=b") is None
    c = auth.set_cookie_header("sid")
    assert "HttpOnly" in c and "SameSite=Strict" in c and "Secure" not in c
    monkeypatch.setenv("FABRIKANT_COOKIE_SECURE", "1")
    assert "Secure" in auth.set_cookie_header("sid")
    assert "Max-Age=0" in auth.set_cookie_header("", clear=True)


# ---------------------------------------------------------------- Modus / Bind
def test_mode_default_and_invalid(monkeypatch):
    monkeypatch.delenv("FABRIKANT_AUTH", raising=False)
    assert auth.mode() == "none"
    monkeypatch.setenv("FABRIKANT_AUTH", "LOCAL")
    assert auth.mode() == "local"
    monkeypatch.setenv("FABRIKANT_AUTH", "oidc")
    assert auth.mode() == "oidc"
    monkeypatch.setenv("FABRIKANT_AUTH", "ldap")
    with pytest.raises(auth.AuthError):
        auth.mode()


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost"])
def test_loopback_hosts(host):
    assert auth.is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
def test_non_loopback_hosts(host):
    assert not auth.is_loopback(host)


def test_bind_check(monkeypatch):
    monkeypatch.delenv("FABRIKANT_AUTH", raising=False)
    auth.check_bind("127.0.0.1")
    with pytest.raises(auth.AuthError, match="verweigert"):
        auth.check_bind("0.0.0.0")
    monkeypatch.setenv("FABRIKANT_AUTH", "local")
    auth.check_bind("0.0.0.0")


def test_serve_refuses_non_loopback_without_auth(monkeypatch):
    from fabrikant import server

    monkeypatch.delenv("FABRIKANT_AUTH", raising=False)
    with pytest.raises(auth.AuthError):
        server.serve(0, "0.0.0.0")


def test_cli_serve_reports_bind_refusal(monkeypatch, capsys):
    monkeypatch.delenv("FABRIKANT_AUTH", raising=False)
    monkeypatch.setattr(cli, "serve", lambda port, host: auth.check_bind(host))
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", "--host", "0.0.0.0"])
    assert e.value.code == 1 and "verweigert" in capsys.readouterr().out


# ---------------------------------------------------------------- Modus none: rückwärtskompatibel
def test_none_mode_open_get_and_post(fabrikant_url, monkeypatch):
    monkeypatch.delenv("FABRIKANT_AUTH", raising=False)
    assert _req(f"{fabrikant_url}/api/runs")[0] == 200
    assert _req(f"{fabrikant_url}/api/projects/uitest/state", "POST", {"state": "active"})[0] == 200
    status, body, _ = _req(f"{fabrikant_url}/api/session")
    assert status == 200 and json.loads(body)["mode"] == "none"
    assert _req(f"{fabrikant_url}/api/login", "POST", {"user": "x", "password": "y"})[0] == 400


def test_none_mode_token_still_gates_posts_only(fabrikant_url, monkeypatch):
    monkeypatch.setenv("FABRIKANT_TOKEN", "sekret")
    assert _req(f"{fabrikant_url}/api/runs")[0] == 200
    assert _req(f"{fabrikant_url}/api/projects/uitest/state", "POST", {"state": "active"})[0] == 403


# ---------------------------------------------------------------- Rollenmatrix (Modus local)
VIEW, OPER, ADM = auth.ROLES
ROUTES = [  # (Methode, Pfad, Body, Mindestrolle)
    ("GET", "/api/projects", None, VIEW),
    ("GET", "/api/runs", None, VIEW),
    ("GET", f"/api/run/{RUN_ID}", None, VIEW),
    ("GET", f"/api/run/{RUN_ID}/log", None, VIEW),
    ("GET", "/api/metrics", None, VIEW),
    ("GET", "/api/llm", None, VIEW),
    ("GET", "/api/projects/uitest/spec", None, VIEW),
    ("GET", "/api/projects/uitest/spec.yaml", None, VIEW),
    ("GET", "/api/projects/uitest/branches", None, VIEW),
    ("POST", "/api/logout", {}, VIEW),
    ("POST", "/api/runs", {}, OPER),
    ("POST", "/api/chat", {"message": "help"}, OPER),
    ("POST", "/api/chat/cancel", {}, OPER),
    ("POST", "/api/approve", {}, OPER),
    ("POST", "/api/reject", {}, OPER),
    ("POST", "/api/projects/uitest/state", {"state": "active"}, OPER),
    ("POST", "/api/llm", {}, ADM),
    ("POST", "/api/projects/uitest/branch", {}, ADM),
]


@pytest.mark.parametrize("role", auth.ROLES)
@pytest.mark.parametrize(("method", "path", "body", "needed"), ROUTES)
def test_role_matrix(local_url, role, method, path, body, needed):
    headers = _login(local_url, role)
    status, _, _ = _req(local_url + path, method, body, headers)
    if auth.ROLES.index(role) < auth.ROLES.index(needed):
        assert status == 403
    else:
        assert status not in (401, 403)


@pytest.mark.parametrize(("method", "path", "body", "_needed"), ROUTES)
def test_anonymous_gets_401(local_url, method, path, body, _needed):
    assert _req(local_url + path, method, body)[0] == 401


def test_public_routes_without_login(local_url):
    assert _req(local_url + "/api/health")[0] == 200
    assert _req(local_url + "/")[0] == 200  # UI-Shell (Login-Maske)
    status, body, _ = _req(local_url + "/api/session")
    assert status == 200 and json.loads(body)["user"] is None


def test_login_failures(local_url):
    assert _req(local_url + "/api/login", "POST", {"user": "viewer", "password": "bad"})[0] == 401
    assert _req(local_url + "/api/login", "POST", {"user": "ghost", "password": PASSWORD})[0] == 401
    assert _req(local_url + "/api/login", "POST", {})[0] == 401


def test_session_endpoint_and_logout(local_url):
    headers = _login(local_url, "operator")
    _, body, _ = _req(local_url + "/api/session", headers=headers)
    info = json.loads(body)
    assert (info["user"], info["role"], info["csrf"]) == ("operator", "operator", headers[auth.CSRF_HEADER])
    status, _, resp = _req(local_url + "/api/logout", "POST", {}, headers)
    assert status == 200 and "Max-Age=0" in resp["Set-Cookie"]
    assert _req(local_url + "/api/runs", headers=headers)[0] == 401


def test_session_cookie_flags(local_url):
    _, _, headers = _req(local_url + "/api/login", "POST", {"user": "admin", "password": PASSWORD})
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie


# ---------------------------------------------------------------- CSRF / Origin / Token
def test_csrf_required_for_session_posts(local_url):
    headers = _login(local_url, "admin")
    cookie_only = {"Cookie": headers["Cookie"]}
    body = {"state": "active"}
    url = local_url + "/api/projects/uitest/state"
    assert _req(url, "POST", body, cookie_only)[0] == 403
    assert _req(url, "POST", body, {**cookie_only, auth.CSRF_HEADER: "wrong"})[0] == 403
    assert _req(url, "POST", body, headers)[0] == 200


def test_csrf_not_needed_for_get(local_url):
    headers = _login(local_url, "viewer")
    assert _req(local_url + "/api/runs", headers={"Cookie": headers["Cookie"]})[0] == 200


def test_cross_origin_post_rejected_even_with_session(local_url):
    headers = {**_login(local_url, "admin"), "Origin": "http://evil.example"}
    assert _req(local_url + "/api/projects/uitest/state", "POST", {"state": "active"}, headers)[0] == 403
    assert _req(local_url + "/api/login", "POST", {"user": "admin", "password": PASSWORD}, headers)[0] == 403


def test_api_token_is_admin_without_csrf(local_url, monkeypatch):
    monkeypatch.setenv("FABRIKANT_TOKEN", "sekret")
    bearer = {"Authorization": "Bearer sekret"}
    assert _req(local_url + "/api/runs", headers=bearer)[0] == 200
    assert _req(local_url + "/api/projects/uitest/state", "POST", {"state": "active"}, bearer)[0] == 200
    assert _req(local_url + "/api/llm", "POST", {}, bearer)[0] == 400  # admin, nur Body fehlt
    assert _req(local_url + "/api/runs", headers={"Authorization": "Bearer nope"})[0] == 401


def test_api_token_skips_origin_check(local_url, monkeypatch):
    monkeypatch.setenv("FABRIKANT_TOKEN", "sekret")
    headers = {"Authorization": "Bearer sekret", "Origin": "http://automation.example"}
    assert _req(local_url + "/api/projects/uitest/state", "POST", {"state": "active"}, headers)[0] == 200


def test_chat_apply_needs_admin(local_url):
    out = {}
    for role in (auth.OPERATOR, auth.ADMIN):
        _, body, _ = _req(local_url + "/api/chat", "POST", {"message": "apply uitest"}, _login(local_url, role))
        out[role] = json.loads(body)["reply"]
    assert "admin" in out[auth.OPERATOR]
    assert "admin" not in out[auth.ADMIN]  # läuft in den normalen "kein Vorschlag"-Pfad


# ---------------------------------------------------------------- Audit
def test_api_run_start_records_user(local_url, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: calls.append(k))
    headers = _login(local_url, "operator")
    status, _, _ = _req(local_url + "/api/runs", "POST", {"project": "uitest", "loop": "t"}, headers)
    assert status == 200
    for _ in range(100):
        if calls:
            break
        time.sleep(0.05)
    assert calls[0]["actor"] == "operator"


def test_init_record_stores_started_by(runs_dir, projects_dir):
    spec = {"_path": str(projects_dir / "none.yaml")}
    rec, _, _ = engine._init_record(spec, "p", {}, "l", "manual", "", None, "r1", runs_dir, "anna")
    assert rec["started_by"] == "anna"
    assert rec["approved_by"] is None and rec["rejected_by"] is None
    rec, _, _ = engine._init_record(spec, "p", {}, "l", "cron", "", None, "r2", runs_dir)
    assert rec["started_by"] is None
    assert rec["approved_by"] is None and rec["rejected_by"] is None


def test_queued_record_keeps_started_by(runs_dir):
    engine._write_queued_record("p", "l", "r1", "manual", "", None, "anna")
    rec = runs.get_run("r1", "p")
    assert rec["started_by"] == "anna"
    assert rec["approved_by"] is None and rec["rejected_by"] is None


def _approval_run(runs_dir):
    d = runs_dir / "p"
    d.mkdir()
    (d / "r1.json").write_text(
        json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "needs_approval", "steps": []})
    )


def test_approve_records_approved_by(runs_dir, monkeypatch):
    _approval_run(runs_dir)
    monkeypatch.setattr(chat, "_start_resume_worker", lambda project, run: True)
    chat._chat_resume("p", "r1", True, actor="anna")
    assert runs.get_run("r1", "p")["approved_by"] == "anna"


def test_reject_records_rejected_by(runs_dir, monkeypatch):
    _approval_run(runs_dir)
    monkeypatch.setattr(engine, "cleanup_rejected_run", lambda *a, **k: {})
    chat._chat_resume("p", "r1", False, actor="anna")
    rec = runs.get_run("r1", "p")
    assert rec["status"] == "rejected" and rec["rejected_by"] == "anna"


def test_chat_commands_pass_actor(monkeypatch):
    seen = []
    monkeypatch.setattr(chat, "start_priority_run", lambda *a, **k: seen.append(k["actor"]) or False)
    chat._chat_start_run("p", "l", "", actor="anna")
    assert seen == ["anna"]


# ---------------------------------------------------------------- CLI
def test_cli_user_add_role_passwd(auth_state, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    cli.main(["user", "add", "anna", "operator", "--password-stdin"])
    assert auth.load_users()["anna"].get("role") == "operator"
    cli.main(["user", "role", "anna", "admin"])
    assert auth.load_users()["anna"].get("role") == "admin"
    monkeypatch.setattr("sys.stdin", io.StringIO("new-password\n"))
    cli.main(["user", "passwd", "anna", "--password-stdin"])
    assert auth.login("anna", "new-password")
    assert "✔ user add: anna" in capsys.readouterr().out


def test_cli_user_add_defaults_to_viewer(auth_state, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    cli.main(["user", "add", "bob", "--password-stdin"])
    assert auth.load_users()["bob"].get("role") == "viewer"


def test_cli_user_interactive_password(auth_state, monkeypatch):
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    cli.main(["user", "add", "carl"])
    assert auth.login("carl", PASSWORD)


def test_cli_user_errors(auth_state, monkeypatch, capsys):
    answers = iter(["a-password", "different"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    with pytest.raises(SystemExit):
        cli.main(["user", "add", "carl"])
    assert "stimmen nicht überein" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["user", "role", "carl"])  # Rolle fehlt
    with pytest.raises(SystemExit):
        cli.main(["user", "role", "ghost", "admin"])  # unbekannter Benutzer


def test_handler_class_exposes_actor_none_by_default():
    h = object.__new__(FabrikantHandler)
    assert h._actor is None
