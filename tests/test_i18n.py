"""i18n-Kataloge (ADR 0049, Issue #370): de/en bleiben vollständig und konsistent.

Statische Prüfungen der Web-Dateien (kein Browser):
- de.json und en.json haben exakt dieselben Keys
- jeder in web/app.js verwendete t("…")-Key existiert in beiden Katalogen
- jedes data-i18n*-Attribut in web/index.html existiert in beiden Katalogen
- keine hartkodierten Locale-Strings (de-DE) in web/
"""

import json
import re

import pytest

from fabrikant import config

WEB_DIR = config.WEB_DIR
I18N_DIR = WEB_DIR / "i18n"
LANGS = ("de", "en")


def _catalog(lang: str) -> dict[str, str]:
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def _app_js() -> str:
    return (WEB_DIR / "app.js").read_text(encoding="utf-8")


def _index_html() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def test_catalogs_exist_and_are_valid_json():
    for lang in LANGS:
        path = I18N_DIR / f"{lang}.json"
        assert path.is_file(), f"{path} fehlt"
        data = _catalog(lang)
        assert data, f"{path} ist leer"
        assert all(isinstance(v, str) for v in data.values()), f"{path}: alle Werte müssen Strings sein"


def test_catalogs_have_identical_keys():
    de, en = _catalog("de"), _catalog("en")
    assert set(de) == set(en), (
        f"Key-Abweichung: nur in de: {sorted(set(de) - set(en))}, nur in en: {sorted(set(en) - set(de))}"
    )


def test_no_hardcoded_locale_in_web_dir():
    """Kein hartkodiertes de-DE (oder anderes BCP-47-Locale) in den Web-Dateien."""
    for path in WEB_DIR.rglob("*"):
        if path.suffix not in (".js", ".html", ".css"):
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bde-DE\b", text), f"{path}: hartkodiertes Locale 'de-DE'"


def _t_keys_in_app_js() -> set[str]:
    """Alle statischen t("key")-Aufrufe in app.js.

    Erfasst drei Formen des ersten Arguments:
    - String-Literal: t("key", …) bzw. tOr("key", fallback)
    - Template-Literal: t(`prefix${…}`) → statischer Präfix (Präfix-Matching)
    - Ternär: t(cond ? "a" : "b", …) → beide Zweige
    Dynamische Aufrufe (t(el.dataset.i18n)) liefern keinen statischen Key."""
    src = _app_js()
    keys: set[str] = set()
    for m in re.finditer(r"(?<![\w.])t(?:Or)?\(\s*\"([^\"]+)\"", src):
        keys.add(m.group(1))
    for m in re.finditer(r"(?<![\w.])t(?:Or)?\(\s*`([^`$]+)\$\{", src):
        keys.add(m.group(1) + "*")  # Präfix-Placeholder
    for m in re.finditer(r"(?<![\w.])t(?:Or)?\([^()]*\?\s*\"([^\"]+)\"\s*:\s*\"([^\"]+)\"", src):
        keys.add(m.group(1))
        keys.add(m.group(2))
    return keys


def test_all_t_keys_exist_in_both_catalogs():
    de, en = _catalog("de"), _catalog("en")
    missing = []
    for key in sorted(_t_keys_in_app_js()):
        if key.endswith("*"):
            prefix = key[:-1]
            if not any(k.startswith(prefix) for k in de):
                missing.append(f"{key} (Präfix, kein Key in de)")
            if not any(k.startswith(prefix) for k in en):
                missing.append(f"{key} (Präfix, kein Key in en)")
        else:
            if key not in de:
                missing.append(f"{key} (fehlend in de)")
            if key not in en:
                missing.append(f"{key} (fehlend in en)")
    assert not missing, "t()-Keys ohne Katalog-Eintrag: " + ", ".join(missing)


def _data_i18n_keys_in_index_html() -> set[str]:
    src = _index_html()
    keys: set[str] = set()
    for attr in ("data-i18n", "data-i18n-ph", "data-i18n-title"):
        keys.update(re.findall(rf'{attr}="([^"]+)"', src))
    return keys


def test_all_data_i18n_keys_exist_in_both_catalogs():
    de, en = _catalog("de"), _catalog("en")
    missing = [k for k in sorted(_data_i18n_keys_in_index_html()) if k not in de or k not in en]
    assert not missing, "data-i18n-Keys ohne Katalog-Eintrag: " + ", ".join(missing)


def test_no_unused_catalog_keys():
    """Jeder Katalog-Key wird in app.js (t()) oder index.html (data-i18n*) verwendet."""
    de = _catalog("de")
    used = _t_keys_in_app_js() | _data_i18n_keys_in_index_html()
    unused = []
    for key in sorted(de):
        if key in used:
            continue
        if any(u.endswith("*") and key.startswith(u[:-1]) for u in used):
            continue
        unused.append(key)
    assert not unused, "Unbenutzte Katalog-Keys: " + ", ".join(unused)


def test_langtoggle_button_exists_in_index_html():
    assert 'id="langtoggle"' in _index_html(), "Sprach-Umschalter fehlt im Header"


def test_i18n_catalogs_are_served_by_server(live_server):
    """Der Server liefert die Kataloge unter /i18n/{lang}.json (WEB_DIR-Static)."""
    import urllib.request

    url = live_server()
    for lang in LANGS:
        with urllib.request.urlopen(f"{url}/i18n/{lang}.json") as r:
            assert r.status == 200
            served = json.loads(r.read().decode("utf-8"))
        assert served == _catalog(lang), f"/i18n/{lang}.json weicht von der Datei ab"


@pytest.mark.parametrize("lang", LANGS)
def test_catalog_placeholders_match(lang):
    """{Platzhalter} in de- und en-Werten eines Keys müssen identisch sein."""
    de, en = _catalog("de"), _catalog("en")
    for key in de:
        de_ph = set(re.findall(r"\{(\w+)\}", de[key]))
        en_ph = set(re.findall(r"\{(\w+)\}", en[key]))
        assert de_ph == en_ph, f"{key}: Platzhalter de={sorted(de_ph)} ≠ en={sorted(en_ph)}"


def test_document_language_matches_the_static_fallback_text():
    """Before (or without) JavaScript, assistive technology reads the static labels in the language of
    `<html lang>`: it must name the language those labels are written in (the runtime switch sets it later)."""
    import html

    src = _index_html()
    lang = re.search(r'<html lang="([^"]+)"', src)
    assert lang is not None
    catalog = _catalog(lang.group(1))
    pairs = re.findall(r'data-i18n="([^"]+)"[^>]*>([^<]+)<', src)
    assert pairs
    assert [k for k, text in pairs if catalog.get(k) != html.unescape(text).strip()] == []
