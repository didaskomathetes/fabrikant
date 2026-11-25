"""Stage A: deterministic cleaning (ADR 0060 §2a) — no LLM.

Text transformation only, no interpretation. Goal: turn raw HTML/text into clean,
low-injection plain text before it reaches the agent.
All rules are deterministic and unit-testable.
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

# Zero-width + BiDi-override characters: invisible, but they can reorder/hide text
# (right-to-left override). Always remove.
_INVISIBLE_RE = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff]"
)
# C0 control characters (except \t\n\r): do not occur in normalized text.
_C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# HTML-Kommentare.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# data:-URIs (JS-Payloads in href/src).
_DATA_URI_RE = re.compile(r"data:[^\"'\s>]+", re.IGNORECASE)
# Mehrere Leerzeichen/Zeilen zusammenfassen.
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """Extract visible text, dropping script/style/noscript/hidden subtrees.

    Open elements are tracked on a stack so that a hidden element only hides its
    own subtree (until its end tag) instead of everything after it."""

    _DROP = {"script", "style", "noscript", "template", "iframe", "object", "embed"}
    _BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    # Elements without end tag: never pushed on the stack.
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._stack: list[tuple[str, bool]] = []  # (tag, hides its subtree)
        self._drop_depth = 0

    @staticmethod
    def _is_hidden(attrs) -> bool:
        attrdict = dict(attrs)
        style = (attrdict.get("style") or "").lower().replace(" ", "")
        return "hidden" in attrdict or "display:none" in style

    def handle_starttag(self, tag, attrs):
        if tag in self._VOID:
            if tag in self._BREAK and self._drop_depth == 0:
                self._chunks.append("\n")
            return
        hides = tag in self._DROP or self._is_hidden(attrs)
        self._stack.append((tag, hides))
        if hides:
            self._drop_depth += 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_startendtag(self, tag, attrs):
        # <x/> never opens a subtree.
        if tag in self._BREAK and self._drop_depth == 0:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        # Pop up to the matching open element; stray end tags are ignored.
        if not any(t == tag for t, _ in self._stack):
            return
        while self._stack:
            t, hides = self._stack.pop()
            if hides:
                self._drop_depth -= 1
            if t == tag:
                break
        if tag in self._BREAK and self._drop_depth == 0:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._drop_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def normalize_text(text: str) -> str:
    """Character level: NFKC (fullwidth/compat forms), strip invisible/control chars, normalise whitespace."""
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_RE.sub("", text)
    text = _C0_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def clean_html(html: str) -> str:
    """HTML → bereinigter Klartext (Stufe A): Kommentar/data-URI raus, sichtbarer
    Text extrahieren (script/style/hidden weg), dann Zeichen-Ebene normalisieren."""
    html = _COMMENT_RE.sub("", html)  # joined like a browser renders it (no split-word evasion)
    html = _DATA_URI_RE.sub(" ", html)
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        raw = extractor.text()
    except Exception:
        # Dirty HTML must not kill the run: clean the raw text instead of crashing.
        raw = re.sub(r"<[^>]+>", " ", html)
    return normalize_text(raw)
