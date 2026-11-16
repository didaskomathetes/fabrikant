"""Browser-Tests (Playwright/Chromium) für die UI-Sprache (ADR 0049, Issue #370).

Lädt die UI mit deutscher bzw. englischer Browser-Sprache und prüft:
- <html lang> folgt der aufgelösten Sprache (localStorage > Browser > "en")
- Beispiel-Texte sind in der jeweiligen Sprache
- Der Umschalter im Header wechselt die Sprache und persistiert sie in
  localStorage; die gespeicherte Wahl schlägt die Browser-Sprache.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from fabrik import config

pytestmark = pytest.mark.browser

SPEC_YAML = """\
name: uitest
description: "i18n-Testprojekt"
root: __ROOT__
repo:
  branch: main
loops:
  lint:
    description: "Lint-Loop"
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: "check", cmd: "echo lint"}
"""


@pytest.fixture
def fabrik_home(tmp_path, monkeypatch):
    """Isolierte FABRIK_HOME mit Test-Spec (toucht echte Daten nicht)."""
    home = tmp_path / "fabrik_home"
    (home / "projects").mkdir(parents=True)
    (home / "runs" / "uitest").mkdir(parents=True)
    (home / "projects" / "uitest.fabrik.yaml").write_text(SPEC_YAML.replace("__ROOT__", str(home)))
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    return home


@pytest.fixture
def fabrik_url(fabrik_home, live_server):
    """fabrik-HTTP-Server auf einem freien Port (ohne Scheduler)."""
    return live_server()


@pytest.fixture
def i18n_page(fabrik_url):
    """Factory: Chromium-Seite mit vorgegebener Browser-Sprache (navigator.language)."""
    from playwright.sync_api import sync_playwright

    @contextmanager
    def open(browser_lang: str):
        with sync_playwright() as p:
            browser = p.chromium.launch()
            pg = browser.new_page(viewport={"width": 1280, "height": 560})
            pg.add_init_script(f"Object.defineProperty(navigator, 'language', {{value: '{browser_lang}'}});")
            pg.goto(fabrik_url)
            pg.wait_for_selector("#projects .project")
            yield pg
            browser.close()

    yield open


def test_german_browser_gets_german_ui(i18n_page):
    """Browser-Sprache de → <html lang="de"> + deutsche Beispiel-Texte."""
    with i18n_page("de-DE") as pg:
        pg.wait_for_function("document.documentElement.lang === 'de'")
        assert pg.get_attribute("html", "lang") == "de"
        assert pg.inner_text("#placeholder") == "Projekt wählen"
        assert pg.inner_text("#langtoggle") == "DE"
        assert pg.inner_text("#chattoggle") == "✉ Chat"
        assert pg.inner_text("#chatpanel .chatbar [data-i18n='chat.title']") == "fabrik-copilot"


def test_english_browser_gets_english_ui(i18n_page):
    """Browser-Sprache en → <html lang="en"> + englische Beispiel-Texte."""
    with i18n_page("en-US") as pg:
        pg.wait_for_function("document.documentElement.lang === 'en'")
        assert pg.get_attribute("html", "lang") == "en"
        assert pg.inner_text("#placeholder") == "Select a project"
        assert pg.inner_text("#langtoggle") == "EN"


def test_unknown_browser_language_falls_back_to_english(i18n_page):
    """Unbekannte Browser-Sprache (z. B. fr) → Fallback 'en'."""
    with i18n_page("fr-FR") as pg:
        pg.wait_for_function("document.documentElement.lang === 'en'")
        assert pg.inner_text("#placeholder") == "Select a project"


def test_toggle_switches_language_and_persists(i18n_page):
    """Umschalter im Header: de → en, persistiert in localStorage, überlebt Reload."""
    with i18n_page("de-DE") as pg:
        pg.wait_for_function("document.documentElement.lang === 'de'")
        pg.click("#langtoggle")
        pg.wait_for_function("document.documentElement.lang === 'en'")
        assert pg.inner_text("#placeholder") == "Select a project"
        assert pg.inner_text("#langtoggle") == "EN"
        assert pg.evaluate("localStorage.getItem('fabrik.lang')") == "en"
        # Zweiter Klick zurück nach de.
        pg.click("#langtoggle")
        pg.wait_for_function("document.documentElement.lang === 'de'")
        assert pg.inner_text("#placeholder") == "Projekt wählen"
        # Reload: die gespeicherte Wahl (de) schlägt die Browser-Sprache.
        pg.reload()
        pg.wait_for_function("document.documentElement.lang === 'de'")
        assert pg.inner_text("#placeholder") == "Projekt wählen"


def test_stored_choice_wins_over_browser_language(i18n_page):
    """Gespeicherte Sprachwahl (localStorage) schlägt die Browser-Sprache."""
    with i18n_page("de-DE") as pg:
        pg.wait_for_function("document.documentElement.lang === 'de'")
        pg.evaluate("localStorage.setItem('fabrik.lang', 'en')")
        pg.reload()
        pg.wait_for_function("document.documentElement.lang === 'en'")
        assert pg.inner_text("#placeholder") == "Select a project"


def test_detail_view_renders_in_active_language(i18n_page):
    """Dynamische Views (Detail-View) folgen der aktiven Sprache."""
    with i18n_page("en-US") as pg:
        pg.wait_for_function("document.documentElement.lang === 'en'")
        pg.click("#projects .project")
        pg.wait_for_selector("#detail .loop")
        # Detail-View-Buttons in Englisch.
        assert pg.locator("#detail button:has-text('graph')").count() >= 1
        assert pg.locator("#detail button:has-text('spec')").count() >= 1
        # Sprachwechsel → dieselbe View in Deutsch, Buttons übersetzt.
        pg.click("#langtoggle")
        pg.wait_for_function("document.documentElement.lang === 'de'")
        assert pg.locator("#detail .loop").count() >= 1
        # Toggle-Button zeigt im Listen-Modus "Graph"; Run-Button ist übersetzt.
        assert pg.locator("#detail button:has-text('Graph')").count() >= 1
        assert pg.locator("#detail button:has-text('starten')").count() >= 1
