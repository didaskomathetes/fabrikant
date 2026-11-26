"""Hardened research fetcher (ADR 0060, stages 1–2).

External web data is untrusted (guiding principle 7). The fetcher fetches pages,
cleans them deterministically (stage A), scans them deterministically for
prompt injection (stage B, no LLM) and returns a result with status
``clean`` / ``suspicious`` / ``quarantined`` plus a provenance ledger.

Auto-approvals (provenance + ``approved_domains``) are fetched; open URLs
are bundled into ONE ``access`` approval request (stage 2, ADR 0059 §5a).
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
