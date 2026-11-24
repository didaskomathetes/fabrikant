"""Web-UI enthält keine Inline-Event-Handler (Issue #268, ADR 0055).

Inline-Handler (``onclick="…"``) sind ein XSS-Vektor und umgehen CSP. Alle
Handler sind per ``addEventListener``/Delegation gebunden; die Werte stehen in
``data-*``-Attributen. Dieser Test scannt ``web/`` (ohne ``vendor/``) und
scheitert, sobald wieder ein ``on*``-Attribut mit String-Wert eingebaut wird.
"""

import re

from fabrikant import config

# on*-Attribut mit String-Wert: Zeilenanfang oder Leerzeichen vor ``on``, dann
# Buchstaben, dann ``=`` und Anführungszeichen. Trefft keine Wörter wie
# ``content=`` (kein Leerzeichen/Zeilenanfang vor dem ``on``).
_INLINE_HANDLER = re.compile(r"(?:^|\s)on[a-z]{2,}=[\"']")


def _web_files() -> list:
    """Alle JS/HTML-Dateien in ``web/`` außer ``vendor/`` (drittes Code, nicht unser Stil)."""
    files = []
    for pattern in ("*.js", "*.html"):
        for f in config.WEB_DIR.rglob(pattern):
            if "vendor" in f.parts:
                continue
            files.append(f)
    return files


def test_no_inline_event_handlers_in_web():
    offenders = []
    for f in _web_files():
        src = f.read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            if _INLINE_HANDLER.search(line):
                offenders.append(f"{f.name}:{i}: {line.strip()}")
    assert not offenders, "Inline-Event-Handler gefunden (Issue #268):\n" + "\n".join(offenders)
