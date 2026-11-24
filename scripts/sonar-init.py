"""One-off SonarQube initialization via Playwright (first login + token).

A fresh SonarQube Community instance locks the API until the default admin
(admin/admin) has changed the password via the web UI (first-login flow).
That is an SPA — not doable with curl. This script:

  1. Logs in with admin/admin
  2. Performs the forced password change (→ SONAR_ADMIN_PASSWORD)
  3. Creates a scanner token via the browser session
  4. Writes the token to state/sonar-token.txt (0600, used by sonar-scan.sh)

Only needed for the first setup. Afterwards `scripts/sonar-scan.sh` is enough.
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
    """First login: fill in the forced password change form. True if it was present."""
    try:
        page.wait_for_selector('input[name="passwordConfirmation"]', timeout=8000)
    except Exception:
        return False  # no password change needed
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
            # No password value in the log (secret leak) — confirmation only.
            print("[sonar-init] Admin password changed (first login)")
            # Log in again after the change if necessary
            page.goto(HOST_URL, wait_until="networkidle")
            if page.is_visible('input[name="login"]'):
                page.fill('input[name="login"]', ADMIN_USER)
                page.fill('input[name="password"]', NEW_PASS)
                page.click('button[type="submit"]')

        # Wait for the dashboard (login form gone)
        page.wait_for_timeout(3000)

        # Create the token via the browser session (uses the session cookie)
        resp = page.evaluate(
            """async () => {
                const r = await fetch('/api/user_tokens/generate?name=fabrikant-scanner', {method: 'POST'});
                return {status: r.status, body: await r.text()};
            }"""
        )
        if resp["status"] != 200:
            print(f"[sonar-init] ERROR: token generation HTTP {resp['status']}: {resp['body'][:200]}")
            browser.close()
            return 1
        token = json.loads(resp["body"])["token"]
        browser.close()

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token)
    os.chmod(TOKEN_FILE, 0o600)  # token is a secret → owner-readable only
    print(f"[sonar-init] Token erzeugt → {TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
