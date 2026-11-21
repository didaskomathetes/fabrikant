"""Stufe A: deterministische Bereinigung (ADR 0060 §2a) — keine LLM.

Nur Text-Transformation, keine Interpretation. Ziel: aus rohem HTML/Text
einen sauberen, injektionsarmen Klartext machen, bevor er den Agenten
erreicht. Alle Regeln sind deterministisch und unit-testbar.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Zero-Width + BiDi-Override-Zeichen: unsichtbar, aber sie können Text
# umordnen/verbergen (Right-to-Left-Override). Immer entfernen.
_INVISIBLE_RE = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff]"
)
# C0-Steuerzeichen (außer \t\n\r): tauchen in normalisiertem Text nicht vor.
_C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# HTML-Kommentare.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# data:-URIs (JS-Payloads in href/src).
_DATA_URI_RE = re.compile(r"data:[^\"'\s>]+", re.IGNORECASE)
# Mehrere Leerzeichen/Zeilen zusammenfassen.
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """Extrahiert sichtbaren Text und wirft script/style/noscript/hidden weg."""

    _DROP = {"script", "style", "noscript", "template", "iframe", "object", "embed"}
    _BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._DROP:
            self._drop_depth += 1
            return
        attrdict = dict(attrs)
        hidden = "hidden" in attrdict
        style = (attrdict.get("style") or "").lower()
        if hidden or "display:none" in style.replace(" ", ""):
            self._drop_depth += 1
        if tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._DROP and self._drop_depth > 0:
            self._drop_depth -= 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._drop_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def normalize_text(text: str) -> str:
    """Zeichen-Ebene: unsichtbare/Steuerzeichen raus, Whitespace normalisieren."""
    text = _INVISIBLE_RE.sub("", text)
    text = _C0_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def clean_html(html: str) -> str:
    """HTML → bereinigter Klartext (Stufe A): Kommentar/data-URI raus, sichtbarer
    Text extrahieren (script/style/hidden weg), dann Zeichen-Ebene normalisieren."""
    html = _COMMENT_RE.sub(" ", html)
    html = _DATA_URI_RE.sub(" ", html)
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        raw = extractor.text()
    except Exception:
        # Unsauberes HTML darf den Run nicht killen: Rohtext bereinigen statt crashen.
        raw = re.sub(r"<[^>]+>", " ", html)
    return normalize_text(raw)
