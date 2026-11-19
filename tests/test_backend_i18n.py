"""Backend-i18n: Kataloge de/en haben dieselben Keys; Fallbacks."""

from fabrik import i18n


def test_catalogs_have_identical_keys():
    assert i18n._CATALOGS["de"].keys() == i18n._CATALOGS["en"].keys()


def test_placeholders_match():
    import string

    def fields(s):
        return {f for _, f, _, _ in string.Formatter().parse(s) if f}

    for key, de in i18n._CATALOGS["de"].items():
        assert fields(de) == fields(i18n._CATALOGS["en"][key]), key


def test_lang_selection_and_fallback(monkeypatch):
    monkeypatch.delenv("FABRIK_LANG", raising=False)
    assert i18n.current_lang() == "de"
    monkeypatch.setenv("FABRIK_LANG", "EN")
    assert i18n.t("approval.why") == "why"
    monkeypatch.setenv("FABRIK_LANG", "xx")
    assert i18n.current_lang() == "de"
    assert i18n.t("no.such.key") == "no.such.key"
    assert i18n.t("approval.tier_what", tier=2).startswith("Tier 2")


def test_all_used_keys_exist_and_no_unused_keys():
    import re
    from pathlib import Path

    src = Path(i18n.__file__).parent
    used = set()
    for f in src.rglob("*.py"):
        if "messages" in f.parts:
            continue
        used |= set(re.findall(r"\b_?t\(\s*[\"']([a-z0-9_.]+)[\"']", f.read_text(encoding="utf-8")))
        used |= set(re.findall(r"\bi18n\.t\(\s*[\"']([a-z0-9_.]+)[\"']", f.read_text(encoding="utf-8")))
    used |= {"approval.condition", "approval.why", "approval.what"}  # built dynamically in approval.py
    catalog = set(i18n._CATALOGS["de"])
    assert used - catalog == set(), "keys used but not in the catalog"
    assert catalog - used == set(), "unused catalog keys"
