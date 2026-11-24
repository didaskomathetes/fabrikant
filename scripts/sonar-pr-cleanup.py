#!/usr/bin/env python3
"""sonar-pr-cleanup — clean up orphaned SonarQube PR projects (ADR 0053).

Legacy cleanup: until native PR analysis (community branch plugin, PR #524),
`scripts/sonar-scan.sh --pr <n>` created a separate SonarQube project per PR,
`<base>:pr-<n>` (Community Edition could not do PR analysis). After merge or
close of the PR the project is orphaned — this script removes it. Since #524
no `:pr-<n>` projects are created any more; the run is therefore a self-
terminating backlog cleanup (as long as there are no matches, no GitHub calls):

  1. List all Sonar projects (`api/projects/search`, paginated)
  2. Filter out keys matching the pattern `<base>:pr-<n>`
  3. Per PR: status via GitHub (`gh api repos/<repo>/pulls/<n>`)
  4. Closed (merged or not) PRs → delete the project
     (`api/projects/delete`)

Fail-safe: if the PR status cannot be determined (GitHub unreachable, 404,
broken JSON), the project is NOT deleted. Every failure — Sonar or
GitHub API unreachable, deletion rejected — is only a warning: the
script always ends with exit 0 so that the calling main scan
(`sonar-scan.sh`) does not abort.

`--dry-run` lists what would be deleted without deleting anything.

Env: SONAR_HOST_URL (default http://127.0.0.1:9000), SONAR_TOKEN (otherwise
token file SONAR_TOKEN_FILE / state/sonar-token.txt), SONAR_PROJECT_KEY
(default fabrikant:fabrikant), GITHUB_REPOSITORY (default
didaskomathetes/fabrikant).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_REPO = "didaskomathetes/fabrikant"
DEFAULT_HOST_URL = "http://127.0.0.1:9000"
DEFAULT_BASE_KEY = "fabrikant:fabrikant"
PAGE_SIZE = 500  # api/projects/search: max. 500 per page
HTTP_TIMEOUT = 10  # seconds per Sonar request
FABRIKANT_DIR = Path(__file__).resolve().parent.parent


def _log(msg: str) -> None:
    print(f"[sonar-pr-cleanup] {msg}")


def _warn(msg: str) -> None:
    print(f"[sonar-pr-cleanup] WARNUNG: {msg}")


def _gh_json(*args: str):
    """Run `gh <args>`, stdout as JSON (error → exception)."""
    return json.loads(subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout)


def pr_key_pattern(base_key: str) -> re.Pattern[str]:
    """Regex for PR project keys: exactly `<base>:pr-<n>` (n = digits)."""
    return re.compile(rf"^{re.escape(base_key)}:pr-(\d+)$")


def pr_number(key: str, pattern: re.Pattern[str]) -> int | None:
    """PR number from a project key, None for non-PR keys (main, foreign keys)."""
    m = pattern.match(key)
    return int(m.group(1)) if m else None


def _http_json(url: str, token: str, method: str = "GET") -> dict:
    """Sonar REST request (basic auth with scanner token) → JSON. Error → exception."""
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{token}:".encode()).decode())
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def list_project_keys(host_url: str, token: str) -> list[str]:
    """Alle Projekt-Keys (`api/projects/search`, paginiert)."""
    keys: list[str] = []
    page = 1
    while True:
        data = _http_json(f"{host_url}/api/projects/search?p={page}&ps={PAGE_SIZE}", token)
        batch = [p.get("key", "") for p in data.get("projects") or []]
        keys.extend(batch)
        total = (data.get("p") or {}).get("total", 0)
        if not batch or len(keys) >= total:
            return keys
        page += 1


def pr_is_open(repo: str, number: int) -> bool | None:
    """PR status via GitHub: True = open, False = closed (merged or not),
    None = cannot be determined (fail-safe: then NOT deleted)."""
    try:
        pr = _gh_json("api", f"repos/{repo}/pulls/{number}")
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    state = pr.get("state")
    if state == "open":
        return True
    if state == "closed":
        return False
    return None


def delete_project(host_url: str, token: str, key: str) -> None:
    """Delete a Sonar project (`api/projects/delete`). Error → exception."""
    _http_json(f"{host_url}/api/projects/delete?projectKey={urllib.parse.quote(key)}", token, method="DELETE")


def cleanup(host_url: str, token: str, repo: str, base_key: str, dry_run: bool = False) -> dict:
    """Delete orphaned PR projects; returns statistics.

    Never raises: every failure (list, PR status, deletion) is only warned about.
    Projects of open PRs, the main project and foreign keys remain untouched."""
    stats = {"found": 0, "deleted": 0, "kept_open": 0, "skipped": 0, "failed": 0}
    pattern = pr_key_pattern(base_key)
    try:
        keys = list_project_keys(host_url, token)
    except Exception as e:  # noqa: BLE001 — fail-safe: cleanup must never break the scan
        _warn(f"Sonar projects cannot be listed ({e}) — cleanup skipped")
        return stats
    for key in keys:
        number = pr_number(key, pattern)
        if number is None:
            continue  # main-Projekt / fremde Keys bleiben unangetastet
        stats["found"] += 1
        open_ = pr_is_open(repo, number)
        if open_ is None:
            stats["skipped"] += 1
            _warn(f"PR #{number} status unknown — keeping {key} (fail-safe)")
            continue
        if open_:
            stats["kept_open"] += 1
            continue
        if dry_run:
            stats["deleted"] += 1
            _log(f"[dry-run] would delete: {key} (PR #{number} closed)")
            continue
        try:
            delete_project(host_url, token, key)
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            _warn(f"Deleting {key} failed ({e})")
            continue
        stats["deleted"] += 1
        _log(f"deleted: {key} (PR #{number} closed)")
    return stats


def read_token_file() -> str:
    """Scanner token from SONAR_TOKEN_FILE or state/sonar-token.txt, '' if absent."""
    path = Path(os.environ.get("SONAR_TOKEN_FILE") or FABRIKANT_DIR / "state" / "sonar-token.txt")
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO, help="GitHub-Repo (owner/name)"
    )
    p.add_argument("--base-key", default=os.environ.get("SONAR_PROJECT_KEY", DEFAULT_BASE_KEY), help="main-Projekt-Key")
    p.add_argument("--host-url", default=os.environ.get("SONAR_HOST_URL", DEFAULT_HOST_URL), help="SonarQube-URL")
    p.add_argument("--token", default=None, help="Scanner token (default: SONAR_TOKEN, otherwise token file)")
    p.add_argument("--dry-run", action="store_true", help="only list what would be deleted")
    args = p.parse_args(argv)

    token = args.token or os.environ.get("SONAR_TOKEN") or read_token_file()
    if not token:
        _warn("kein Scanner-Token (SONAR_TOKEN / Token-File) — Aufräumen übersprungen")
        return 0
    stats = cleanup(args.host_url, token, args.repo, args.base_key, dry_run=args.dry_run)
    _log(f"erledigt: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
