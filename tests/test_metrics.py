"""KPI-Metriken (src/fabrikant/metrics.py): Prometheus-Textformat + Label-Escaping.

Review PR 141: Label-Werte mit Backslash, Anführungszeichen oder
Zeilenumbruch durften ungültige Samples erzeugen. Diese Tests pinnen das
Escaping (Exposition-Format-Regeln) und die Grund-Render-Semantik.
"""

import math
import threading

import pytest

from fabrikant import metrics
from fabrikant.metrics import Counter, _escape_label_value


@pytest.fixture
def clean_registry():
    """Isoliert die globale Counter-Registry (Snapshot + Restore)."""
    saved = list(metrics._counters)
    metrics._counters.clear()
    yield metrics
    metrics._counters[:] = saved


def test_escape_label_value_backslash_quote_newline():
    # Reihenfolge: erst Backslash, dann Anführungszeichen, dann Zeilenumbruch.
    assert _escape_label_value("a\\b") == "a\\\\b"
    assert _escape_label_value('a"b') == 'a\\"b'
    assert _escape_label_value("a\nb") == "a\\nb"
    # Kombination: Backslash vor Anführungszeichen wird korrekt doppelt.
    assert _escape_label_value('a\\"b') == 'a\\\\\\"b'
    assert _escape_label_value("") == ""
    assert _escape_label_value("plain") == "plain"


def test_render_escapes_label_values():
    c = Counter("m_total", "help", ["tier", "reason"])
    c.inc(tier="t\\1", reason='r"2')
    c.inc(tier="t3", reason="r\n4")
    out = c.render()
    # Ungeschützte Sonderzeichen dürfen nicht mehr im Sample stehen.
    assert 'tier="t\\\\1"' in out
    assert 'reason="r\\"2"' in out
    assert 'reason="r\\n4"' in out
    # Jedes Sample ist eine eigene Zeile (kein rohes Zeilenumbruch im Wert).
    sample_lines = [ln for ln in out.splitlines() if ln.startswith("m_total{")]
    assert len(sample_lines) == 2
    for ln in sample_lines:
        assert "\n" not in ln


def test_render_plain_values_unchanged():
    c = Counter("m_total", "help", ["tier"])
    c.inc(tier="fast")
    out = c.render()
    assert 'm_total{tier="fast"} 1.0' in out
    assert "# TYPE m_total counter" in out


def test_inc_rejects_negative_amount():
    """Prometheus-Counter sind monoton: negative Werte werden abgelehnt."""
    c = Counter("m_total", "help", ["tier"])
    c.inc(tier="fast")
    with pytest.raises(ValueError):
        c.inc(-1, tier="fast")
    # Ablehnung verändert den Wert nicht.
    assert c.value(tier="fast") == 1.0


def test_inc_rejects_nan_and_inf():
    """NaN/inf würden ungültige Samples exportieren — werden abgelehnt."""
    c = Counter("m_total", "help", ["tier"])
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            c.inc(bad, tier="fast")
    assert c.value(tier="fast") == 0.0


def test_multiple_label_combinations():
    """Jede Label-Kombination ist eine eigene Zeitreihe."""
    c = Counter("m_total", "help", ["tier", "reason"])
    c.inc(tier="fast", reason="timeout")
    c.inc(tier="fast", reason="timeout")
    c.inc(tier="slow", reason="crash")
    assert c.value(tier="fast", reason="timeout") == 2.0
    assert c.value(tier="slow", reason="crash") == 1.0
    # Unbelegte Kombination bleibt 0.0.
    assert c.value(tier="fast", reason="crash") == 0.0


def test_missing_labels_treated_as_empty():
    """Fehlende Labels werden zu leeren Strings (stabile Keys)."""
    c = Counter("m_total", "help", ["tier", "reason"])
    c.inc(tier="fast")  # `reason` fehlt → ""
    assert c.value(tier="fast", reason="") == 1.0
    assert c.value(tier="fast") == 1.0


def test_concurrent_inc():
    """Parallele inc()s dürfen keine Inkremente verlieren (Lock pro Counter)."""
    c = Counter("m_total", "help", ["tier"])
    per_thread, threads_n = 1000, 8

    def worker():
        for _ in range(per_thread):
            c.inc(tier="fast")

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.value(tier="fast") == per_thread * threads_n


def test_register_returns_counter_and_renders(clean_registry):
    c = Counter("m_total", "help", ["tier"])
    assert clean_registry.register(c) is c
    c.inc(tier="fast")
    out = clean_registry.render()
    assert 'm_total{tier="fast"} 1.0' in out
    assert "# TYPE m_total counter" in out


def test_render_empty_registry(clean_registry):
    """Ohne Counter liefert render() nur den trennenden Zeilenumbruch."""
    assert clean_registry.render() == "\n"


def test_register_duplicate_name_rejected(clean_registry):
    """Doppelte Registrierung desselben Namens erzeugt doppelte Samples — abgelehnt."""
    c = Counter("m_total", "help", ["tier"])
    clean_registry.register(c)
    with pytest.raises(ValueError):
        clean_registry.register(Counter("m_total", "help", ["tier"]))
    # Der erste Counter bleibt der einzige im Export.
    c.inc(tier="fast")
    out = clean_registry.render()
    assert out.count('m_total{tier="fast"} 1.0') == 1


def test_counter_rejects_invalid_names():
    """Ungültige Metriknamen würden den Text-Export ungültig machen — abgelehnt."""
    for bad in ("", "123abc", "abc-def", "abc def", "abc.def", "äbc"):
        with pytest.raises(ValueError):
            Counter(bad, "help", ["tier"])


def test_counter_accepts_valid_names():
    """Gültige Prometheus-Namen (inkl. ':' und '_') werden akzeptiert."""
    for good in ("m_total", "fabrikant_reaped_runs_total", "job:metric:name", "_leading", "A1_b2"):
        Counter(good, "help", ["tier"])  # darf nicht werfen


def test_counter_rejects_help_with_newline():
    """HELP mit Zeilenumbruch bricht das Exposition-Format — abgelehnt."""
    with pytest.raises(ValueError):
        Counter("m_total", "zeile1\nzeile2", ["tier"])
    # Einzeiliger HELP ist erlaubt.
    Counter("m_total", "zeile1 zeile2", ["tier"])


def test_counter_rejects_name_with_trailing_newline():
    """`$` matcht vor abschließendem Newline — der Randfall muss abgelehnt werden."""
    with pytest.raises(ValueError):
        Counter("valid\n", "help", ["tier"])
    with pytest.raises(ValueError):
        Counter("valid\n\n", "help", ["tier"])


def test_counter_rejects_invalid_label_names():
    """Ungültige Label-Namen erzeugen ungültige Samples — abgelehnt."""
    for bad in ("", "1abc", "a-b", "a b", "a.b", "a:b", "äbc"):
        with pytest.raises(ValueError):
            Counter("m_total", "help", [bad])


def test_counter_rejects_duplicate_labels():
    """Doppelte Label-Namen erzeugen mehrdeutige Samples — abgelehnt."""
    with pytest.raises(ValueError):
        Counter("m_total", "help", ["tier", "tier"])
    # Gleiche Namen in anderer Reihenfolge sind ebenfalls Duplikate.
    with pytest.raises(ValueError):
        Counter("m_total", "help", ["tier", "reason", "tier"])


def test_render_escapes_help_backslash():
    """HELP-Backslash wird gemäß Expositionsformat escaped (\\ → \\\\)."""
    c = Counter("m_total", "help with \\ backslash", ["tier"])
    out = c.render()
    assert "# HELP m_total help with \\\\ backslash" in out
    # Kein roher Backslash im HELP.
    assert "# HELP m_total help with \\ backslash" not in out
