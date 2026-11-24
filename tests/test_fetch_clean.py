"""Tests für Stufe A: deterministische Bereinigung (ADR 0060 §2a)."""

from __future__ import annotations

from fabrikant.fetch.clean import clean_html, normalize_text


def test_normalize_strips_invisible_chars():
    assert normalize_text("a\u200bb\u200ec\u200fd\ufeffe") == "abcde"


def test_normalize_strips_c0_control_chars():
    assert normalize_text("a\x00b\x07c\x1fd") == "abcd"


def test_normalize_collapses_whitespace():
    assert normalize_text("a   b\t\tc\n\n\n\nd") == "a b c\n\nd"


def test_clean_html_extracts_visible_text():
    html = "<html><body><h1>Title</h1><p>Hello <b>world</b></p></body></html>"
    assert "Title" in clean_html(html)
    assert "Hello world" in clean_html(html)


def test_clean_html_drops_script_and_style():
    html = "<html><script>var x='evil()';</script><style>.a{color:red}</style><p>ok</p></html>"
    out = clean_html(html)
    assert "evil" not in out
    assert "color:red" not in out
    assert "ok" in out


def test_clean_html_drops_hidden_display_none():
    html = '<p>visible</p><span style="display:none">hidden-secret</span>'
    out = clean_html(html)
    assert "visible" in out
    assert "hidden-secret" not in out


def test_clean_html_drops_hidden_attribute():
    html = "<p>shown</p><div hidden>not-shown</div>"
    out = clean_html(html)
    assert "shown" in out
    assert "not-shown" not in out


def test_clean_html_strips_comments():
    html = "<p>text</p><!-- secret comment -->"
    assert "secret comment" not in clean_html(html)


def test_clean_html_strips_data_uris():
    html = '<a href="data:text/html;base64,PHNjcmlwdD4">x</a><p>fine</p>'
    out = clean_html(html)
    assert "data:text/html" not in out
    assert "fine" in out


def test_clean_html_malformed_does_not_crash():
    out = clean_html("<p>unclosed <b>bold and <div>stuff")
    assert "bold" in out and "stuff" in out
