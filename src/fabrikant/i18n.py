"""Backend messages (run log, chat, CLI, API errors, approval context) in de/en.

Language: ``FABRIKANT_LANG`` (``de`` | ``en``, default ``de`` = previous behavior). The web UI has
its own catalogs (``web/i18n``, ADR 0049); this covers text the server produces itself, including
text persisted in run records. Missing keys fall back to ``de``, then to the key.
"""

from __future__ import annotations

import os

from .messages import de, en

DEFAULT_LANG = "de"
SUPPORTED_LANGS = ("de", "en")

_CATALOGS: dict[str, dict[str, str]] = {"de": de.MESSAGES, "en": en.MESSAGES}


def current_lang() -> str:
    """Active language from ``FABRIKANT_LANG`` (invalid/empty → default); read at call time."""
    lang = os.environ.get("FABRIKANT_LANG", "").strip().lower()
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def t(key: str, /, **vars: object) -> str:
    """Text for ``key`` in the active language; ``{placeholders}`` are filled from ``vars``."""
    text = _CATALOGS[current_lang()].get(key) or _CATALOGS[DEFAULT_LANG].get(key) or key
    return text.format(**vars) if vars else text
