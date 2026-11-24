"""Injection detectors behind one interface (ADR 0060 stage B).

A detector turns text into :class:`~fabrikant.fetch.scan.ScanFinding` objects. Two ship:

* :class:`RegexDetector` — the built-in structural/phrase rules (zero dependencies, default).
* :class:`LLMGuardDetector` — an established model-based classifier (LLM Guard's
  ``PromptInjection`` scanner), optional extra ``fabrikant[injection-llm-guard]``.

Detectors are *signals*. A finding may route a source to quarantine or review; a
``clean`` verdict never grants a capability (see docs/threat-model.md). Hence the
runner fails closed: a crashing or truncated detector yields a finding, never silence.
"""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from .scan import SUSPICIOUS, ScanFinding, scan

DETECTORS_ENV = "FABRIKANT_FETCH_DETECTORS"  # comma-separated detector names
DEFAULT_DETECTORS = ("regex",)
RULE_DETECTOR_ERROR = "detector_error"
RULE_SCAN_INCOMPLETE = "scan_incomplete"
RULE_CLASSIFIER = "injection_classifier"

# Classifier inputs are token-limited (~512 tokens for the usual DeBERTa models ≈ 2000 chars).
DEFAULT_CHUNK_CHARS = 2000
DEFAULT_MAX_CHUNKS = 200  # bound latency on huge pages; the remainder is reported, not ignored
DEFAULT_THRESHOLD = 0.8
_EXCERPT_CHARS = 120
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


class DetectorUnavailable(RuntimeError):
    """A configured detector cannot run (missing optional dependency)."""


class Detector(Protocol):
    """Anything that can scan text for injection signals."""

    name: str
    #: True if the detector also wants the raw markup (hidden CSS, tags); False = cleaned text only.
    scans_raw: bool

    def ensure_available(self) -> None: ...

    def scan(self, text: str) -> list[ScanFinding]: ...


class RegexDetector:
    """Built-in rules from :mod:`fabrikant.fetch.scan` (cheap, deterministic, zero-dependency)."""

    name = "regex"
    scans_raw = True

    def ensure_available(self) -> None:
        return None

    def scan(self, text: str) -> list[ScanFinding]:
        return scan(text)


def chunk_text(text: str, size: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Split into chunks of at most ``size`` chars, preferring paragraph boundaries."""
    chunks: list[str] = []
    current = ""
    for para in _PARAGRAPH_SPLIT.split(text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:  # a single oversized paragraph: hard split
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:size])
            para = para[size:]
        if current and len(current) + len(para) + 2 > size:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


class LLMGuardDetector:
    """Model-based injection classifier via LLM Guard's ``PromptInjection`` scanner.

    Findings are ``suspicious`` by default: classifiers false-positive on security
    articles (which the research fetcher reads on purpose), so a hit asks for review
    instead of auto-quarantining. Pass ``severity=QUARANTINE`` to be stricter.
    ``scanner`` can inject any object with ``scan(text) -> (sanitized, is_valid, score)``.
    """

    name = "llm-guard"
    scans_raw = False

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        severity: str = SUSPICIOUS,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
        scanner: Any = None,
    ) -> None:
        self.threshold = threshold
        self.severity = severity
        self.chunk_chars = chunk_chars
        self.max_chunks = max_chunks
        self._scanner = scanner

    def ensure_available(self) -> None:
        if self._scanner is None and importlib.util.find_spec("llm_guard") is None:
            raise DetectorUnavailable(
                "detector 'llm-guard' needs the optional extra: pip install 'fabrikant[injection-llm-guard]'"
            )

    def _load(self) -> Any:
        if self._scanner is None:
            self.ensure_available()
            from llm_guard.input_scanners import PromptInjection  # type: ignore[import-not-found]
            from llm_guard.input_scanners.prompt_injection import MatchType  # type: ignore[import-not-found]

            # Chunks are already model-sized, so FULL scores each chunk as a whole.
            self._scanner = PromptInjection(threshold=self.threshold, match_type=MatchType.FULL)
        return self._scanner

    def scan(self, text: str) -> list[ScanFinding]:
        scanner = self._load()
        chunks = chunk_text(text, self.chunk_chars)
        findings: list[ScanFinding] = []
        for chunk in chunks[: self.max_chunks]:
            _sanitized, is_valid, score = scanner.scan(chunk)
            if not is_valid:
                excerpt = " ".join(chunk.split())[:_EXCERPT_CHARS]
                findings.append(ScanFinding(RULE_CLASSIFIER, self.severity, f"score={score:.2f}: {excerpt}…"))
        if len(chunks) > self.max_chunks:
            skipped = len(chunks) - self.max_chunks
            findings.append(
                ScanFinding(
                    RULE_SCAN_INCOMPLETE, SUSPICIOUS, f"{self.name}: {skipped} of {len(chunks)} chunks not scanned"
                )
            )
        return findings


_REGISTRY: dict[str, type] = {RegexDetector.name: RegexDetector, LLMGuardDetector.name: LLMGuardDetector}


def default_detector_names() -> tuple[str, ...]:
    """Detector names from ``FABRIKANT_FETCH_DETECTORS`` (comma-separated), else the default."""
    raw = os.environ.get(DETECTORS_ENV, "")
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    return names or DEFAULT_DETECTORS


def build_detectors(names: Iterable[str] | None = None) -> list[Detector]:
    """Instantiate detectors by name; unknown names and missing extras raise at configuration time."""
    built: list[Detector] = []
    for name in names or default_detector_names():
        cls = _REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"unknown detector {name!r} (known: {', '.join(sorted(_REGISTRY))})")
        det = cls()
        det.ensure_available()
        built.append(det)
    return built


def run_detectors(detectors: Sequence[Detector], text: str, *, raw: bool = False) -> list[ScanFinding]:
    """Run detectors over one text view (``raw`` = original markup, else cleaned text).

    Fails closed: a detector that raises contributes a ``suspicious`` finding so the
    source can never be reported ``clean`` just because a detector broke.
    """
    findings: list[ScanFinding] = []
    for det in detectors:
        if raw and not det.scans_raw:
            continue
        try:
            findings.extend(det.scan(text))
        except Exception as e:
            findings.append(ScanFinding(RULE_DETECTOR_ERROR, SUSPICIOUS, f"{det.name}: {type(e).__name__}: {e}"[:300]))
    return findings
