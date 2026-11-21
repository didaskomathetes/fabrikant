"""Gehärteter Research-Fetcher (ADR 0060, Stufen 1–2).

Externe Web-Daten sind untrusted (Leitprinzip 7). Der Fetcher holt Seiten,
bereinigt sie deterministisch (Stufe A), scannt sie deterministisch auf
Prompt-Injection (Stufe B, ohne LLM) und liefert ein Ergebnis mit Status
``clean`` / ``suspicious`` / ``quarantined`` plus Herkunftsprotokoll.

Nur Auto-Freigaben (Herkunft + ``approved_domains``) werden geholt; alles
andere wird nicht geholt (Stufe 2: gebündelte menschliche Freigabe).
"""

from __future__ import annotations

from .clean import clean_html, normalize_text
from .fetcher import Fetcher, FetchResult
from .scan import ScanFinding, scan

__all__ = [
    "FetchResult",
    "Fetcher",
    "ScanFinding",
    "clean_html",
    "normalize_text",
    "scan",
]
