"""scripts/gh-token-refresh.py (ADR 0010) — JWT-Struktur, Env-File-Write, Config-Checks,
Token-Request-Pfad (gemockt), HTTP-Retry, Dateirechte."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest
from helpers import http_error, load_script

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gh-token-refresh.py"


@pytest.fixture(scope="module")
def mod():
    m = load_script(SCRIPT.name, "gh_token_refresh")
    yield m
    sys.modules.pop("gh_token_refresh", None)


def _pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def test_b64url_no_padding(mod):
    assert mod._b64url(b"abc") == "YWJj"
    assert mod._b64url(b"abcde") == base64.urlsafe_b64encode(b"abcde").rstrip(b"=").decode()


@pytest.mark.parametrize("issuer", ["12345", "Iv23liAbCdEf0123"])
def test_make_jwt_structure(mod, tmp_path, issuer):
    """iss: numerische App-ID oder (von GitHub empfohlen) Client-ID — unverändert übernommen."""
    key = tmp_path / "key.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    jwt = mod._make_jwt(issuer, key)
    header_b64, payload_b64, sig_b64 = jwt.split(".")
    header = json.loads(base64.urlsafe_b64decode(_pad(header_b64)))
    payload = json.loads(base64.urlsafe_b64decode(_pad(payload_b64)))
    assert header == {"alg": "RS256", "typ": "JWT"}  # GitHub akzeptiert nur RS256
    assert payload["iss"] == issuer
    now = int(time.time())
    assert payload["exp"] - payload["iat"] == mod.JWT_LIFETIME_S + mod.JWT_SKEW_S
    assert payload["iat"] <= now - mod.JWT_SKEW_S + 2  # Skew in der Vergangenheit
    assert len(base64.urlsafe_b64decode(_pad(sig_b64))) == 256  # RSA-2048-Signatur


def test_write_token_replaces_existing(mod, tmp_path):
    f = tmp_path / ".env"
    f.write_text("OTHER=1\nGITHUB_TOKEN=old\nMORE=2\n")
    mod._write_token(f, "new-token")
    lines = f.read_text().splitlines()
    assert lines == ["OTHER=1", "GITHUB_TOKEN=new-token", "MORE=2"]


def test_write_token_appends_when_missing(mod, tmp_path):
    f = tmp_path / ".env"
    f.write_text("OTHER=1\n")
    mod._write_token(f, "tok")
    assert f.read_text().splitlines() == ["OTHER=1", "GITHUB_TOKEN=tok"]


def test_write_token_creates_file(mod, tmp_path):
    f = tmp_path / "sub" / ".env"
    mod._write_token(f, "tok")
    assert f.read_text().splitlines() == ["GITHUB_TOKEN=tok"]


def test_main_missing_config_fails(mod, monkeypatch, tmp_path, capsys):
    for k in ("BUILDER_APP_ID", "BUILDER_APP_KEY_FILE", "BUILDER_INSTALLATION_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(mod, "FABRIK_HOME", tmp_path)  # echtes fabrik.env nicht laden
    assert mod.main() == 1
    assert "Config fehlt" in capsys.readouterr().err


def test_main_missing_key_file_fails(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BUILDER_APP_ID", "1")
    monkeypatch.setenv("BUILDER_APP_KEY_FILE", str(tmp_path / "kein.pem"))
    monkeypatch.setenv("BUILDER_INSTALLATION_ID", "2")
    assert mod.main() == 1
    assert "Key-Datei fehlt" in capsys.readouterr().err


# ---------------------------------------------------------------- Dateirechte
def test_write_token_sets_0600(mod, tmp_path):
    """Env-File enthält den Token → nur Owner darf lesen (0600)."""
    f = tmp_path / ".env"
    mod._write_token(f, "tok")
    assert (f.stat().st_mode & 0o777) == 0o600


def test_write_token_tightens_existing_loose_file(mod, tmp_path):
    """Vorhandene 0644-Datei wird beim Rewrite auf 0600 gezogen."""
    f = tmp_path / ".env"
    f.write_text("GITHUB_TOKEN=old\n")
    f.chmod(0o644)
    mod._write_token(f, "new")
    assert (f.stat().st_mode & 0o777) == 0o600


# ------------------------------------------------- immutable .env (chattr +i)
class _FakeAttrTools:
    """Ersetzt `subprocess.run` für lsattr/chattr; protokolliert Aufrufe + os.replace in `events`."""

    def __init__(self, immutable: bool, chattr_rc: dict[str, int] | None = None, lsattr_missing: bool = False):
        self.immutable = immutable
        self.chattr_rc = chattr_rc or {}
        self.lsattr_missing = lsattr_missing
        self.events: list[str] = []

    def run(self, cmd, **kwargs):
        tool, *args = cmd
        if tool == "lsattr":
            if self.lsattr_missing:
                raise FileNotFoundError("lsattr")
            flags = "----i---------e-------" if self.immutable else "--------------e-------"
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{flags} {args[-1]}\n", stderr="")
        assert tool == "chattr", cmd
        op = args[0]
        self.events.append(f"chattr {op}")
        rc = self.chattr_rc.get(op, 0)
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="Operation not permitted" if rc else "")


@pytest.fixture
def attr_tools(mod, monkeypatch):
    def install(**kwargs) -> _FakeAttrTools:
        fake = _FakeAttrTools(**kwargs)
        monkeypatch.setattr(mod.subprocess, "run", fake.run)
        real_replace = mod.os.replace

        def replace(src, dst):
            fake.events.append("replace")
            real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", replace)
        return fake

    return install


def test_write_token_lifts_and_restores_immutable_flag(mod, tmp_path, attr_tools):
    """Immutable .env (chattr +i): -i vor dem Rename, +i danach."""
    f = tmp_path / ".env"
    f.write_text("GITHUB_REVIEWER_TOKEN=old\n")
    fake = attr_tools(immutable=True)
    mod._write_token(f, "new", "GITHUB_REVIEWER_TOKEN")
    assert fake.events == ["chattr -i", "replace", "chattr +i"]
    assert f.read_text() == "GITHUB_REVIEWER_TOKEN=new\n"


def test_write_token_leaves_mutable_file_mutable(mod, tmp_path, attr_tools):
    """Nicht-immutable Datei wird NICHT nachträglich +i gesetzt."""
    f = tmp_path / ".env"
    f.write_text("GITHUB_TOKEN=old\n")
    fake = attr_tools(immutable=False)
    mod._write_token(f, "new")
    assert fake.events == ["replace"]


def test_write_token_new_file_skips_attr_tools(mod, tmp_path, attr_tools):
    fake = attr_tools(immutable=True)
    mod._write_token(tmp_path / ".env", "tok")
    assert fake.events == ["replace"]


def test_write_token_without_lsattr_still_writes(mod, tmp_path, attr_tools):
    """Kein e2fsprogs (macOS/minimaler Container) → kein Crash, normaler Write."""
    f = tmp_path / ".env"
    f.write_text("GITHUB_TOKEN=old\n")
    fake = attr_tools(immutable=True, lsattr_missing=True)
    mod._write_token(f, "new")
    assert fake.events == ["replace"]
    assert f.read_text() == "GITHUB_TOKEN=new\n"


def test_write_token_restores_immutable_flag_when_replace_fails(mod, tmp_path, attr_tools, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("GITHUB_TOKEN=old\n")
    fake = attr_tools(immutable=True)

    def boom(src, dst):
        fake.events.append("replace")
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        mod._write_token(f, "new")
    assert fake.events == ["chattr -i", "replace", "chattr +i"]


def test_write_token_chattr_failure_is_loud(mod, tmp_path, attr_tools):
    """chattr -i scheitert (kein root) → klarer Fehler statt EPERM aus os.replace; Datei unverändert."""
    f = tmp_path / ".env"
    f.write_text("GITHUB_TOKEN=old\n")
    fake = attr_tools(immutable=True, chattr_rc={"-i": 1})
    with pytest.raises(RuntimeError, match="chattr -i"):
        mod._write_token(f, "new")
    assert fake.events == ["chattr -i"]
    assert f.read_text() == "GITHUB_TOKEN=old\n"


# ---------------------------------------------------------------- _http (Retry)
class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_retries_on_urlerror(mod, monkeypatch):
    """Transiente Netzwerkfehler (URLError) → Retry, danach Erfolg."""
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("dns flake")
        return _FakeResp(b'{"token": "t"}')

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    res = mod._http("GET", "https://x", "tok")
    assert res == {"token": "t"}
    assert len(calls) == 3


def test_http_gives_up_after_retries(mod, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(1)
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.URLError):
        mod._http("GET", "https://x", "tok")
    assert len(calls) == mod.RETRIES


def test_http_httperror_not_retried(mod, monkeypatch):
    """HTTP-Antwort (z. B. 401) ist KEIN Flake → sofort weiterreichen."""
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(1)
        raise http_error(401, "https://x", "unauthorized")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        mod._http("GET", "https://x", "bad-token")
    assert len(calls) == 1


# ---------------------------------------------------------------- main: Token-Request-Pfad
def _main_env(mod, monkeypatch, tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("dummy")
    monkeypatch.setenv("BUILDER_APP_ID", "1")
    monkeypatch.setenv("BUILDER_APP_KEY_FILE", str(key))
    monkeypatch.setenv("BUILDER_INSTALLATION_ID", "2")
    monkeypatch.setenv("FABRIK_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(mod, "_make_jwt", lambda app_id, key_path: "jwt")


def test_main_success_writes_token(mod, monkeypatch, tmp_path, capsys):
    _main_env(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(
        mod, "_http", lambda m, u, t, p=None: {"token": "ghs_abc", "expires_at": "2026-01-01T00:00:00Z"}
    )
    assert mod.main() == 0
    assert "GITHUB_TOKEN=ghs_abc" in (tmp_path / ".env").read_text()
    assert "OK" in capsys.readouterr().out


def test_main_token_request_network_error_fails_closed(mod, monkeypatch, tmp_path, capsys):
    """Netzwerkfehler → Exit 1 (fail-closed), keine Datei-Anfassung."""
    _main_env(mod, monkeypatch, tmp_path)

    def boom(method, url, token, payload=None):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(mod, "_http", boom)
    assert mod.main() == 1
    assert "Token-Request" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()


def test_main_token_request_missing_token_fails(mod, monkeypatch, tmp_path, capsys):
    _main_env(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_http", lambda m, u, t, p=None: {"expires_at": "2026-01-01"})
    assert mod.main() == 1
    assert "kein Token" in capsys.readouterr().err


def test_main_reviewer_role_writes_reviewer_token(mod, monkeypatch, tmp_path, capsys):
    """--role reviewer: REVIEWER_*-Config → GITHUB_REVIEWER_TOKEN; GITHUB_TOKEN bleibt."""
    key = tmp_path / "qa.pem"
    key.write_text("dummy")
    for k, v in (("REVIEWER_APP_ID", "3"), ("REVIEWER_APP_KEY_FILE", str(key)), ("REVIEWER_INSTALLATION_ID", "4")):
        monkeypatch.setenv(k, v)
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=dev-tok\n")
    monkeypatch.setenv("FABRIK_ENV_FILE", str(env))
    seen = {}
    monkeypatch.setattr(mod, "_make_jwt", lambda app_id, key_path: seen.setdefault("app", app_id) and "jwt")
    monkeypatch.setattr(mod, "_http", lambda m, u, t, p=None: seen.setdefault("url", u) and {"token": "ghs_qa"})
    assert mod.main(["--role", "reviewer"]) == 0
    assert env.read_text().splitlines() == ["GITHUB_TOKEN=dev-tok", "GITHUB_REVIEWER_TOKEN=ghs_qa"]
    assert seen["app"] == "3" and "/installations/4/" in seen["url"]
    assert "GITHUB_REVIEWER_TOKEN" in capsys.readouterr().out


def test_main_reviewer_role_reports_its_own_missing_keys(mod, monkeypatch, capsys):
    for k in ("REVIEWER_APP_ID", "REVIEWER_APP_KEY_FILE", "REVIEWER_INSTALLATION_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(mod, "_load_fabrik_env", lambda: None)
    assert mod.main(["--role", "reviewer"]) == 1
    assert "REVIEWER_APP_ID" in capsys.readouterr().err
