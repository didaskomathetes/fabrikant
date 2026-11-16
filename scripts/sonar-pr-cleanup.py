#!/usr/bin/env python3
"""sonar-pr-cleanup — verwaiste SonarQube-PR-Projekte aufräumen (ADR 0053).

Legacy-Aufräumung: Bis zur nativen PR-Analyse (Community-Branch-Plugin, PR #524)
legte `scripts/sonar-scan.sh --pr <n>` pro PR ein eigenes SonarQube-Projekt
`<base>:pr-<n>` an (Community Edition konnte keine PR-Analyse). Nach Merge oder
Close des PRs ist das Projekt verwaist — dieses Script entfernt es. Seit #524
entstehen keine `:pr-<n>`-Projekte mehr; der Lauf ist damit eine sich selbst
beendende Bestandsaufräumung (solange keine Treffer, keine GitHub-Calls):

  1. Alle Sonar-Projekte auflisten (`api/projects/search`, paginiert)
  2. Keys des Musters `<base>:pr-<n>` herausfiltern
  3. Pro PR: Status via GitHub (`gh api repos/<repo>/pulls/<n>`)
  4. Geschlossene (gemergt oder nicht) PRs → Projekt löschen
     (`api/projects/delete`)

Fail-safe: Ist der PR-Status nicht ermittelbar (GitHub nicht erreichbar, 404,
kaputtes JSON), wird das Projekt NICHT gelöscht. Jeder Fehlschlag — Sonar- oder
GitHub-API nicht erreichbar, Löschen abgelehnt — ist nur eine Warnung: Das
Script endet immer mit Exit 0, damit der aufrufende main-Scan
(`sonar-scan.sh`) nicht abbricht.

`--dry-run` listet, was gelöscht würde, ohne etwas zu löschen.

Env: SONAR_HOST_URL (Default http://127.0.0.1:9000), SONAR_TOKEN (sonst
Token-File SONAR_TOKEN_FILE / state/sonar-token.txt), SONAR_PROJECT_KEY
(Default quark-playground:fabrik), GITHUB_REPOSITORY (Default
didaskomathetes/quark-playground).
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

DEFAULT_REPO = "didaskomathetes/quark-playground"
DEFAULT_HOST_URL = "http://127.0.0.1:9000"
DEFAULT_BASE_KEY = "quark-playground:fabrik"
PAGE_SIZE = 500  # api/projects/search: max. 500 pro Seite
HTTP_TIMEOUT = 10  # Sekunden pro Sonar-Request
FABRIK_DIR = Path(__file__).resolve().parent.parent


def _log(msg: str) -> None:
    print(f"[sonar-pr-cleanup] {msg}")


def _warn(msg: str) -> None:
    print(f"[sonar-pr-cleanup] WARNUNG: {msg}")


def _gh_json(*args: str):
    """`gh <args>` ausführen, stdout als JSON (Fehler → Exception)."""
    return json.loads(subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout)


def pr_key_pattern(base_key: str) -> re.Pattern[str]:
    """Regex für PR-Projekt-Keys: exakt `<base>:pr-<n>` (n = Ziffern)."""
    return re.compile(rf"^{re.escape(base_key)}:pr-(\d+)$")


def pr_number(key: str, pattern: re.Pattern[str]) -> int | None:
    """PR-Nummer aus einem Projekt-Key, None für Nicht-PR-Keys (main, fremde Keys)."""
    m = pattern.match(key)
    return int(m.group(1)) if m else None


def _http_json(url: str, token: str, method: str = "GET") -> dict:
    """Sonar-REST-Request (Basic-Auth mit Scanner-Token) → JSON. Fehler → Exception."""
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
    """PR-Status via GitHub: True = offen, False = geschlossen (gemergt oder nicht),
    None = nicht ermittelbar (fail-safe: dann wird NICHT gelöscht)."""
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
    """Sonar-Projekt löschen (`api/projects/delete`). Fehler → Exception."""
    _http_json(f"{host_url}/api/projects/delete?projectKey={urllib.parse.quote(key)}", token, method="DELETE")


def cleanup(host_url: str, token: str, repo: str, base_key: str, dry_run: bool = False) -> dict:
    """Verwaiste PR-Projekte löschen; liefert eine Statistik.

    Wirft nie: Jeder Fehlschlag (Liste, PR-Status, Löschen) wird nur gewarnt.
    Projekte offener PRs, das main-Projekt und fremde Keys bleiben unangetastet."""
    stats = {"found": 0, "deleted": 0, "kept_open": 0, "skipped": 0, "failed": 0}
    pattern = pr_key_pattern(base_key)
    try:
        keys = list_project_keys(host_url, token)
    except Exception as e:  # noqa: BLE001 — fail-safe: Aufräumen darf den Scan nie brechen
        _warn(f"Sonar-Projekte nicht auflistbar ({e}) — Aufräumen übersprungen")
        return stats
    for key in keys:
        number = pr_number(key, pattern)
        if number is None:
            continue  # main-Projekt / fremde Keys bleiben unangetastet
        stats["found"] += 1
        open_ = pr_is_open(repo, number)
        if open_ is None:
            stats["skipped"] += 1
            _warn(f"PR #{number} nicht ermittelbar — {key} bleibt (fail-safe)")
            continue
        if open_:
            stats["kept_open"] += 1
            continue
        if dry_run:
            stats["deleted"] += 1
            _log(f"[dry-run] würde löschen: {key} (PR #{number} geschlossen)")
            continue
        try:
            delete_project(host_url, token, key)
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            _warn(f"Löschen von {key} fehlgeschlagen ({e})")
            continue
        stats["deleted"] += 1
        _log(f"gelöscht: {key} (PR #{number} geschlossen)")
    return stats


def read_token_file() -> str:
    """Scanner-Token aus SONAR_TOKEN_FILE bzw. state/sonar-token.txt, '' wenn nicht da."""
    path = Path(os.environ.get("SONAR_TOKEN_FILE") or FABRIK_DIR / "state" / "sonar-token.txt")
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
    p.add_argument("--token", default=None, help="Scanner-Token (Default: SONAR_TOKEN, sonst Token-File)")
    p.add_argument("--dry-run", action="store_true", help="nur auflisten, was gelöscht würde")
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
