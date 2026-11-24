#!/usr/bin/env python3
"""gh-token-refresh — renew the GitHub App installation token for fabrikant (ADR 0010).

Creates a fresh 1h installation token for a GitHub App and writes it
to FABRIKANT_ENV_FILE (default ~/.config/fabrikant/fabrikant.env). fabrikant re-reads the file on every
API call — no restart.

Roles (`--role`, default dev) = the two identities of the dual-identity pipeline:
  dev       builder app (creates PRs, pushes)  → GITHUB_TOKEN
            Config: BUILDER_APP_ID, BUILDER_APP_KEY_FILE, BUILDER_INSTALLATION_ID
  reviewer  QA app (reviews, comments)         → GITHUB_REVIEWER_TOKEN
            Config: REVIEWER_APP_ID, REVIEWER_APP_KEY_FILE, REVIEWER_INSTALLATION_ID

  <P>_APP_ID           client ID (`Iv23…`, recommended by GitHub) or numeric app ID —
                       ends up as `iss` in the JWT
  <P>_APP_KEY_FILE     path to the app private key (PEM, RSA)
  <P>_INSTALLATION_ID  installation ID in the target repo/org
  FABRIKANT_ENV_FILE      target file (default ~/.config/fabrikant/fabrikant.env)
Config from env OR fabrikant.env next to this script.

Cron (every 30 min, token lives 1h; one line per role):
  */30 * * * * /path/to/.venv/bin/python /path/to/fabrikant/scripts/gh-token-refresh.py >> /var/log/gh-token-refresh.log 2>&1
  */30 * * * * /path/to/.venv/bin/python /path/to/fabrikant/scripts/gh-token-refresh.py --role reviewer >> /var/log/gh-token-refresh.log 2>&1

Exit 0 = token written, != 0 = error (cron mail/alert).

Security:
  - FABRIKANT_ENV_FILE is written with 0600 (only the owner can read the token).
  - If FABRIKANT_ENV_FILE is `chattr +i` (e.g. via Terraform), the flag is briefly lifted
    for the rename and set again afterwards (needs root;
    `lsattr`/`chattr` from e2fsprogs).
  - Dependency: `openssl` (RS256 JWT signing) must be on the PATH —
    also in CI/deployment images that run the tests/script.
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
FABRIKANT_HOME = SCRIPT_DIR.parent
DEFAULT_ENV_FILE = str(Path.home() / ".config" / "fabrikant" / "fabrikant.env")
JWT_LIFETIME_S = 540  # 9 min — GitHub accepts max. 10 min
JWT_SKEW_S = 60  # iat in the past (clock-skew tolerance)
SUBPROCESS_TIMEOUT_S = 30
HTTP_TIMEOUT_S = 30
IMMUTABLE_ATTR = "i"  # ext attribute flag of `chattr +i` / column in `lsattr`


@dataclass(frozen=True)
class AppRole:
    """An app identity: prefix of its config keys + target key in the env file."""

    prefix: str
    token_key: str

    @property
    def config_keys(self) -> tuple[str, str, str]:
        return (f"{self.prefix}_APP_ID", f"{self.prefix}_APP_KEY_FILE", f"{self.prefix}_INSTALLATION_ID")


# Role names as in fabrikant.gitops.GH_TOKEN_ENV (dev/reviewer).
ROLES = {
    "dev": AppRole("BUILDER", "GITHUB_TOKEN"),
    "reviewer": AppRole("REVIEWER", "GITHUB_REVIEWER_TOKEN"),
}


def _load_fabrikant_env() -> None:
    """fabrikant.env as fallback config (os.environ wins)."""
    p = FABRIKANT_HOME / "fabrikant.env"
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
    """RS256 JWT for app authentication (GitHub accepts ONLY RS256).

    Signing via `openssl` (the stdlib has no RSA; openssl is on the
    deployment host)."""
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
        raise RuntimeError(f"openssl signing failed: {p.stderr.decode()[:200]}")
    return f"{header}.{payload}.{_b64url(p.stdout)}"


RETRIES = 3
RETRY_DELAY_S = 2.0


def _http(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    """GitHub REST call with retry on transient network errors (DNS flakes)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "fabrikant-gh-token-refresh",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
                body = r.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError:
            raise  # a response, not a flake
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt >= RETRIES - 1:
                raise
            time.sleep(RETRY_DELAY_S)
    raise AssertionError("unreachable")  # pragma: no cover


def _is_immutable(path: Path) -> bool:
    """True if `path` carries the immutable attribute (`chattr +i`).

    If `lsattr` is missing (not Linux/e2fsprogs) or the FS cannot do attributes
    (tmpfs, overlayfs, …), the file counts as NOT immutable."""
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
    """`chattr <op> path` (e.g. `-i`/`+i`); RuntimeError on failure (needs root/CAP_LINUX_IMMUTABLE)."""
    p = subprocess.run(
        ["chattr", op, str(path)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(f"chattr {op} {path} failed: {p.stderr.strip()[:200]}")


@contextmanager
def _immutable_lifted(path: Path) -> Iterator[None]:
    """Lift the immutable flag of `path` for the duration of the block.

    If the env file is `chattr +i` (immutable) via Terraform, `os.replace`
    on it otherwise fails with EPERM. The flag is only
    touched if it was set beforehand, and restored in `finally`
    (even if the block raises) — on the new inode after the rename."""
    if not path.exists() or not _is_immutable(path):
        yield
        return
    _chattr(path, f"-{IMMUTABLE_ATTR}")
    try:
        yield
    finally:
        _chattr(path, f"+{IMMUTABLE_ATTR}")


def _write_token(env_file: Path, token: str, key: str = "GITHUB_TOKEN") -> None:
    """Replace (or append) the `key` line, atomically via temp file + rename.

    The file gets 0600 (only the owner can read the token) — chmod on the
    temp file BEFORE the rename so the final path is never more permissive.
    An existing immutable attribute (`chattr +i`) is preserved."""
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
    _load_fabrikant_env()
    values = {k: os.environ.get(k, "").strip() for k in role.config_keys}
    app_id, key_file, installation_id = values.values()
    env_file = Path(os.environ.get("FABRIKANT_ENV_FILE", DEFAULT_ENV_FILE))
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
        # Targeted: transient network errors (after retries) + broken response.
        # Fail closed: no token → exit != 0 → cron alert, old token expires.
        print(f"FEHLER: Token-Request: {e}", file=sys.stderr)
        return 1
    token = res.get("token", "")
    if not token:
        print(f"FEHLER: kein Token in API-Antwort: {str(res)[:200]}", file=sys.stderr)
        return 1
    _write_token(env_file, token, role.token_key)
    expires = res.get("expires_at", "?")
    print(f"OK: {role.token_key} written to {env_file} (valid until {expires})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
