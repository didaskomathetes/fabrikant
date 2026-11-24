"""Browser-Test (Playwright/Chromium) für das Einstellungs-Menü „Verbindungen“ (ADR 0052)."""

from __future__ import annotations

import pytest

from fabrikant import config, connections

pytestmark = pytest.mark.browser

SPEC_YAML = """\
name: uitest
root: __ROOT__
repo:
  branch: main
loops:
  lint:
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: "check", cmd: "echo lint"}
"""


@pytest.fixture
def fabrikant_url(tmp_path, monkeypatch, live_server):
    home = tmp_path / "fabrikant_home"
    (home / "projects").mkdir(parents=True)
    (home / "runs" / "uitest").mkdir(parents=True)
    (home / "projects" / "uitest.fabrikant.yaml").write_text(SPEC_YAML.replace("__ROOT__", str(home)))
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    return live_server()


@pytest.fixture
def page(fabrikant_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 800})
        pg.add_init_script("Object.defineProperty(navigator, 'language', {value: 'en-US'});")
        pg.goto(fabrikant_url)
        pg.wait_for_selector("#projects .project")
        yield pg
        browser.close()


def test_add_edit_delete_connection_via_settings(page):
    page.click("#settingstoggle")
    page.wait_for_selector("#settingspanel:not(.hidden)")
    page.wait_for_selector("text=no connections yet")  # Liste wird asynchron geladen

    page.click("#connadd")
    page.fill("#cf-name", "oc")
    page.select_option("#cf-driver", "opencode")
    page.fill("#cf-endpoint", "http://127.0.0.1:4096")
    page.fill("#cf-model", "llama/q")
    page.fill("#cf-secret", "pw-secret-123")
    page.check("#cf-default")
    page.click("#connform button[type=submit]")
    page.wait_for_selector("#settingsbody td:text-is('oc')")
    row = page.inner_text("#settingsbody tbody tr")
    assert "opencode" in row and "default" in row and "🔑" in row
    assert "pw-secret-123" not in page.content()
    assert connections.get_connection("oc", connections.ConnectionKind.AGENT).secret == "pw-secret-123"

    page.click("[data-conn-edit=oc]")
    assert page.get_attribute("#cf-secret", "placeholder").startswith("set")
    page.fill("#cf-model", "llama/other")
    page.click("#connform button[type=submit]")
    page.wait_for_selector("#settingsbody td:text-is('http://127.0.0.1:4096 · llama/other')")
    assert connections.get_connection("oc", connections.ConnectionKind.AGENT).secret == "pw-secret-123"

    page.once("dialog", lambda d: d.accept())
    page.click("[data-conn-del=oc]")
    page.wait_for_selector("text=no connections yet")


def test_invalid_connection_shows_server_error(page):
    page.click("#settingstoggle")
    page.click("#connadd")
    page.fill("#cf-name", "bad name")
    page.click("#connform button[type=submit]")
    page.wait_for_function("document.querySelector('#cf-error').textContent.length > 0")
    assert "Name" in page.inner_text("#cf-error")


def test_escape_closes_settings(page):
    page.click("#settingstoggle")
    page.keyboard.press("Escape")
    assert "hidden" in page.get_attribute("#settingspanel", "class")


def test_short_secret_shows_masking_hint(page):
    """Secrets unter der Masking-Grenze (secrets.MIN_SECRET_LEN) bekommen beim Tippen einen Hinweis."""
    page.click("#settingstoggle")
    page.wait_for_selector("#settingspanel:not(.hidden)")
    page.wait_for_selector("text=no connections yet")
    page.click("#connadd")
    page.fill("#cf-secret", "short")
    assert "not masked in logs" in page.inner_text("#cf-secret-hint")
    page.fill("#cf-secret", "long-enough-secret")
    assert page.inner_text("#cf-secret-hint") == ""
