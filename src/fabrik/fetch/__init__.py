"""Gehärteter Research-Fetcher (ADR 0060, Stufen 1–2).

Externe Web-Daten sind untrusted (Leitprinzip 7). Der Fetcher holt Seiten,
bereinigt sie deterministisch (Stufe A), scannt sie deterministisch auf
Prompt-Injection (Stufe B, ohne LLM) und liefert ein Ergebnis mit Status
``clean`` / ``suspicious`` / ``quarantined`` plus Herkunftsprotokoll.

Auto-Freigaben (Herkunft + ``approved_domains``) werden geholt; offene URLs
werden gebündelt zu EINEM ``access``-Freigabe-Request (Stufe 2, ADR 0059 §5a).
"""

from __future__ import annotations

from .approval import ACCESS_TIMEOUT_HOURS, AccessRequest, PendingURL, bundle_access
from .clean import clean_html, normalize_text
from .fetcher import Fetcher, FetchResult, results_to_pending
from .scan import QUARANTINE, SUSPICIOUS, ScanFinding, rule_by_id, rules, scan, verdict

__all__ = [
    "ACCESS_TIMEOUT_HOURS",
    "AccessRequest",
    "PendingURL",
    "bundle_access",
    "clean_html",
    "normalize_text",
    "QUARANTINE",
    "SUSPICIOUS",
    "Fetcher",
    "FetchResult",
    "results_to_pending",
    "ScanFinding",
    "rules",
    "rule_by_id",
    "scan",
    "verdict",
]
