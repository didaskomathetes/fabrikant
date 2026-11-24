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
from .detectors import Detector, DetectorUnavailable, LLMGuardDetector, RegexDetector, build_detectors
from .fetcher import Fetcher, FetchResult, results_to_pending
from .profiles import (
    AccessProfile,
    FetchProfile,
    Grant,
    GrantStore,
    fetcher_config_for,
    parse_access_spec,
    profile_names_for_loop,
    resolve_profiles,
    validate_access_spec,
)
from .scan import QUARANTINE, SUSPICIOUS, ScanFinding, rule_by_id, rules, scan, verdict

__all__ = [
    "ACCESS_TIMEOUT_HOURS",
    "AccessProfile",
    "AccessRequest",
    "FetchProfile",
    "PendingURL",
    "Grant",
    "GrantStore",
    "bundle_access",
    "clean_html",
    "normalize_text",
    "QUARANTINE",
    "SUSPICIOUS",
    "Detector",
    "DetectorUnavailable",
    "LLMGuardDetector",
    "RegexDetector",
    "build_detectors",
    "Fetcher",
    "FetchResult",
    "results_to_pending",
    "ScanFinding",
    "fetcher_config_for",
    "parse_access_spec",
    "profile_names_for_loop",
    "resolve_profiles",
    "validate_access_spec",
    "rules",
    "rule_by_id",
    "scan",
    "verdict",
]
