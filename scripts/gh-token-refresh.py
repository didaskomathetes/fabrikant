#!/usr/bin/env python3
"""gh-token-refresh — GitHub-App-Installation-Token für fabrik erneuern (ADR 0010).

Erzeugt ein frisches 1h-Installation-Token für eine GitHub-App und schreibt es
in die FABRIK_ENV_FILE (Default /root/.hermes/.env). fabrik liest die Datei pro
API-Call neu — kein Restart.

Rollen (`--role`, Default dev) = die zwei Identitäten der Dual-Identity-Pipeline:
  dev       Builder-App (erstellt PRs, pusht)  → GITHUB_TOKEN
            Config: BUILDER_APP_ID, BUILDER_APP_KEY_FILE, BUILDER_INSTALLATION_ID
  reviewer  QA-App (reviewt, kommentiert)      → GITHUB_REVIEWER_TOKEN
            Config: REVIEWER_APP_ID, REVIEWER_APP_KEY_FILE, REVIEWER_INSTALLATION_ID

  <P>_APP_ID           Client-ID (`Iv23…`, von GitHub empfohlen) oder numerische App-ID —
                       landet als `iss` im JWT
  <P>_APP_KEY_FILE     Pfad zum App-Private-Key (PEM, RSA)
  <P>_INSTALLATION_ID  Installation-ID im Ziel-Repo/-Org
  FABRIK_ENV_FILE      Ziel-Datei (Default /root/.hermes/.env)
Config aus Env ODER fabrik.env neben diesem Script.

Cron (alle 30 min, Token lebt 1h; eine Zeile pro Rolle):
  */30 * * * * /path/to/.venv/bin/python /path/to/fabrik/scripts/gh-token-refresh.py >> /var/log/gh-token-refresh.log 2>&1
  */30 * * * * /path/to/.venv/bin/python /path/to/fabrik/scripts/gh-token-refresh.py --role reviewer >> /var/log/gh-token-refresh.log 2>&1

Exit 0 = Token geschrieben, != 0 = Fehler (Cron-Mail/Alert).

Sicherheit:
  - FABRIK_ENV_FILE wird mit 0600 geschrieben (nur Owner liest den Token).
  - Ist FABRIK_ENV_FILE `chattr +i` (hermes-VM, Terraform), wird das Flag für
    den Rename kurz aufgehoben und danach wieder gesetzt (braucht root;
    `lsattr`/`chattr` aus e2fsprogs).
  - Abhängigkeit: `openssl` (RS256-JWT-Signierung) muss im PATH sein —
    auch in CI/Deployment-Images, die die Tests/Script ausführen.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

API = "https://api.github.com"
SCRIPT_DIR = Path(__file__).resolve().parent
FABRIK_HOME = SCRIPT_DIR.parent
DEFAULT_ENV_FILE = "/root/.hermes/.env"
JWT_LIFETIME_S = 540  # 9 min — GitHub akzeptiert max. 10 min
JWT_SKEW_S = 60  # iat in der Vergangenheit (Clock-Skew-Toleranz)
SUBPROCESS_TIMEOUT_S = 30
HTTP_TIMEOUT_S = 30
IMMUTABLE_ATTR = "i"  # ext-Attribut-Flag von `chattr +i` / Spalte in `lsattr`


@dataclass(frozen=True)
class AppRole:
    """Eine App-Identität: Präfix ihrer Config-Keys + Ziel-Key in der Env-Datei."""

    prefix: str
    token_key: str

    @property
    def config_keys(self) -> tuple[str, str, str]:
        return (f"{self.prefix}_APP_ID", f"{self.prefix}_APP_KEY_FILE", f"{self.prefix}_INSTALLATION_ID")


# Rollen-Namen wie fabrik.gitops.GH_TOKEN_ENV (dev/reviewer).
ROLES = {
    "dev": AppRole("BUILDER", "GITHUB_TOKEN"),
    "reviewer": AppRole("REVIEWER", "GITHUB_REVIEWER_TOKEN"),
}


def _load_fabrik_env() -> None:
    """fabrik.env als Fallback-Config (os.environ gewinnt)."""
    p = FABRIK_HOME / "fabrik.env"
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_jwt(app_id: str, key_path: Path) -> str:
    """RS256-JWT für die App-Authentifizierung (GitHub akzeptiert NUR RS256).

    Signierung via `openssl` (Stdlib hat kein RSA; openssl ist auf dem
    Deployment-Host vorhanden)."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url(json.dumps({"iat": now - JWT_SKEW_S, "exp": now + JWT_LIFETIME_S, "iss": app_id}).encode())
    signing_input = f"{header}.{payload}".encode()
    p = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
        input=signing_input,
        capture_output=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    if p.returncode != 0:
        raise RuntimeError(f"openssl-Signierung fehlgeschlagen: {p.stderr.decode()[:200]}")
    return f"{header}.{payload}.{_b64url(p.stdout)}"


RETRIES = 3
RETRY_DELAY_S = 2.0


def _http(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    """GitHub-REST-Call mit Retry bei transienten Netzwerkfehlern (DNS-Flakes)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "fabrik-gh-token-refresh",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
                body = r.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError:
            raise  # Antwort, kein Flake
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt >= RETRIES - 1:
                raise
            time.sleep(RETRY_DELAY_S)
    raise AssertionError("unreachable")  # pragma: no cover


def _is_immutable(path: Path) -> bool:
    """True, wenn `path` das immutable-Attribut (`chattr +i`) trägt.

    Fehlt `lsattr` (kein Linux/e2fsprogs) oder kann das FS keine Attribute
    (tmpfs, overlayfs, …), gilt die Datei als NICHT immutable."""
    try:
        p = subprocess.run(
            ["lsattr", "-d", str(path)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    flags = p.stdout.split(maxsplit=1)[0] if p.returncode == 0 and p.stdout.strip() else ""
    return IMMUTABLE_ATTR in flags


def _chattr(path: Path, op: str) -> None:
    """`chattr <op> path` (z.B. `-i`/`+i`); RuntimeError bei Fehlschlag (braucht root/CAP_LINUX_IMMUTABLE)."""
    p = subprocess.run(
        ["chattr", op, str(path)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(f"chattr {op} {path} fehlgeschlagen: {p.stderr.strip()[:200]}")


@contextmanager
def _immutable_lifted(path: Path) -> Iterator[None]:
    """Immutable-Flag von `path` für die Dauer des Blocks aufheben.

    Auf dem hermes-VM ist /root/.hermes/.env per Terraform `chattr +i` —
    `os.replace` darauf schlägt sonst mit EPERM fehl. Das Flag wird nur
    angefasst, wenn es vorher gesetzt war, und im `finally` wiederhergestellt
    (auch wenn der Block wirft) — auf dem neuen Inode nach dem Rename."""
    if not path.exists() or not _is_immutable(path):
        yield
        return
    _chattr(path, f"-{IMMUTABLE_ATTR}")
    try:
        yield
    finally:
        _chattr(path, f"+{IMMUTABLE_ATTR}")


def _write_token(env_file: Path, token: str, key: str = "GITHUB_TOKEN") -> None:
    """`key`-Zeile ersetzen (oder anhängen), atomar via Temp-File+Rename.

    Die Datei erhält 0600 (nur Owner liest den Token) — chmod auf dem
    Temp-File VOR dem Rename, damit der finale Pfad nie lockerer ist.
    Ein gesetztes immutable-Attribut (`chattr +i`) bleibt erhalten."""
    lines = env_file.read_text().splitlines() if env_file.is_file() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={token}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={token}")
    env_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_file.with_suffix(env_file.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    with _immutable_lifted(env_file):
        os.replace(tmp, env_file)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--role", choices=sorted(ROLES), default="dev")
    role = ROLES[p.parse_args(argv or []).role]
    _load_fabrik_env()
    values = {k: os.environ.get(k, "").strip() for k in role.config_keys}
    app_id, key_file, installation_id = values.values()
    env_file = Path(os.environ.get("FABRIK_ENV_FILE", DEFAULT_ENV_FILE))
    missing = [k for k, v in values.items() if not v]
    if missing:
        print(f"FEHLER: Config fehlt: {', '.join(missing)}", file=sys.stderr)
        return 1
    key_path = Path(key_file)
    if not key_path.is_file():
        print(f"FEHLER: Key-Datei fehlt: {key_path}", file=sys.stderr)
        return 1

    jwt = _make_jwt(app_id, key_path)
    try:
        res = _http("POST", f"{API}/app/installations/{installation_id}/access_tokens", jwt)
    except urllib.error.HTTPError as e:
        print(f"FEHLER: Token-Request HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError) as e:
        # Gezielt: transiente Netzwerkfehler (nach Retries) + defekte Antwort.
        # Fail-closed: kein Token → Exit != 0 → Cron-Alert, altes Token läuft aus.
        print(f"FEHLER: Token-Request: {e}", file=sys.stderr)
        return 1
    token = res.get("token", "")
    if not token:
        print(f"FEHLER: kein Token in API-Antwort: {str(res)[:200]}", file=sys.stderr)
        return 1
    _write_token(env_file, token, role.token_key)
    expires = res.get("expires_at", "?")
    print(f"OK: {role.token_key} nach {env_file} geschrieben (gültig bis {expires})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
