"""Einmalige SonarQube-Initialisierung via Playwright (First-Login + Token).

Ein frische SonarQube-Community-Instanz sperrt die API, bis der Default-Admin
(admin/admin) das Passwort über das Web-UI geändert hat (First-Login-Flow).
Das ist ein SPA — per curl nicht machbar. Dieses Skript:

  1. Loggt sich mit admin/admin ein
  2. Führt den erzwungenen Passwort-Wechsel durch (→ SONAR_ADMIN_PASSWORD)
  3. Erzeugt einen Scanner-Token über die Browser-Session
  4. Schreibt den Token nach state/sonar-token.txt (0600, wird von sonar-scan.sh genutzt)

Nur beim ersten Setup nötig. Danach reicht `scripts/sonar-scan.sh`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

FABRIKANT_DIR = Path(__file__).resolve().parent.parent
HOST_URL = os.environ.get("SONAR_HOST_URL", "http://127.0.0.1:9000")
ADMIN_USER = os.environ.get("SONAR_ADMIN_USER", "admin")
OLD_PASS = os.environ.get("SONAR_OLD_PASSWORD", "admin")
NEW_PASS = os.environ.get("SONAR_ADMIN_PASSWORD", "fabrikant-admin")
TOKEN_FILE = FABRIKANT_DIR / "state" / "sonar-token.txt"


def _fill_login(page):
    page.fill('input[name="login"]', ADMIN_USER)
    page.fill('input[name="password"]', OLD_PASS)
    page.click('button[type="submit"]')


def _complete_password_change(page) -> bool:
    """First-Login: erzwungenes Passwort-Wechsel-Formular ausfüllen. True, wenn es da war."""
    try:
        page.wait_for_selector('input[name="passwordConfirmation"]', timeout=8000)
    except Exception:
        return False  # kein Passwort-Wechsel nötig
    page.fill('input[name="password"]', NEW_PASS)
    page.fill('input[name="passwordConfirmation"]', NEW_PASS)
    page.click('button[type="submit"]')
    return True


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(HOST_URL, wait_until="networkidle")

        _fill_login(page)

        if _complete_password_change(page):
            # Kein Passwort-Wert im Log (Secret-Leak) — nur Bestätigung.
            print("[sonar-init] Admin-Passwort geändert (First-Login)")
            # Nach dem Wechsel ggf. erneut einloggen
            page.goto(HOST_URL, wait_until="networkidle")
            if page.is_visible('input[name="login"]'):
                page.fill('input[name="login"]', ADMIN_USER)
                page.fill('input[name="password"]', NEW_PASS)
                page.click('button[type="submit"]')

        # Auf das Dashboard warten (Login-Formular verschwunden)
        page.wait_for_timeout(3000)

        # Token über die Browser-Session erzeugen (nutzt das Session-Cookie)
        resp = page.evaluate(
            """async () => {
                const r = await fetch('/api/user_tokens/generate?name=fabrikant-scanner', {method: 'POST'});
                return {status: r.status, body: await r.text()};
            }"""
        )
        if resp["status"] != 200:
            print(f"[sonar-init] FEHLER: Token-Generierung HTTP {resp['status']}: {resp['body'][:200]}")
            browser.close()
            return 1
        token = json.loads(resp["body"])["token"]
        browser.close()

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token)
    os.chmod(TOKEN_FILE, 0o600)  # Token ist ein Secret → nur Owner lesbar
    print(f"[sonar-init] Token erzeugt → {TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
