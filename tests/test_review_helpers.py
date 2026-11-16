"""Gemeinsame Helfer der Review-Steps ``review`` und ``pr_review`` (Issue #218)."""

from __future__ import annotations

import json

import pytest

from fabrik.runmodel import FailureKind
from fabrik.steps import review
from fabrik.steps.review import _parse_verdict


@pytest.mark.parametrize(
    ("step", "env", "expected"),
    [
        ({"backend": " codex "}, "claude_code", "codex"),
        ({}, "claude_code", "claude_code"),
        ({}, None, review.DEFAULT_REVIEW_BACKEND),
        ({"backend": ""}, "", review.DEFAULT_REVIEW_BACKEND),
    ],
)
def test_resolve_backend(monkeypatch, step, env, expected):
    if env is None:
        monkeypatch.delenv("FABRIK_AGENT_BACKEND", raising=False)
    else:
        monkeypatch.setenv("FABRIK_AGENT_BACKEND", env)
    assert review._resolve_backend(step) == expected


def test_cold_critic_requires_model():
    err = review._check_cold_critic("", {}, "pr_review")
    assert err is not None
    assert err["summary"] == "pr_review: model fehlt (Pflicht)"
    assert err["failure_kind"] == FailureKind.ENVIRONMENT.value


def test_cold_critic_rejects_builder_model():
    err = review._check_cold_critic("m1", {"last_agent_model": " m1 "}, "review-Step")
    assert err is not None
    assert err["summary"].startswith("review-Step: model muss ≠ Builder-Modell sein (m1)")


@pytest.mark.parametrize("ctx", [{}, {"last_agent_model": "m2"}, {"last_agent_model": None}])
def test_cold_critic_accepts_different_or_unknown_builder(ctx):
    assert review._check_cold_critic("m1", ctx, "review-Step") is None


def test_finding_label_tolerates_missing_fields():
    assert review._finding_label({}) == "[?] "
    assert review._finding_label({"severity": "high", "text": "x"}) == "[high] x"


def test_summarize_findings_empty():
    assert review._summarize_findings([]) == "ohne Findings"


def test_summarize_findings_caps_at_limit():
    findings = [{"severity": "low", "text": str(i)} for i in range(review.SUMMARY_FINDINGS + 2)]
    out = review._summarize_findings(findings)
    assert out.count("[low]") == review.SUMMARY_FINDINGS
    assert out.endswith(f"[low] {review.SUMMARY_FINDINGS - 1}")


def test_review_field_with_extras():
    verdict = {"verdict": "approved", "findings": [], "summary": 3}
    assert review._review_field(verdict, "m", pr=7, round=2) == {
        "verdict": "approved",
        "model": "m",
        "findings": [],
        "acceptance": [],
        "summary": "3",
        "pr": 7,
        "round": 2,
    }


def test_review_field_without_summary():
    assert review._review_field({"verdict": "changes_requested", "findings": []}, "m")["summary"] == ""


def test_review_field_carries_acceptance():
    """ADR 0013: AC-Verifikation (auch `unverifiable`) landet im Run-Record."""
    verdict = {
        "verdict": "approved",
        "findings": [],
        "summary": "ok",
        "acceptance": [{"criterion": "AC", "status": "unverifiable", "evidence": "nur im Betrieb"}],
    }
    assert review._review_field(verdict, "m")["acceptance"] == [
        {"criterion": "AC", "status": "unverifiable", "evidence": "nur im Betrieb"}
    ]


# ---------------------------------------------------------------- acceptance (ADR 0013 Stufe 2)
def test_validate_acceptance_missing_is_empty():
    assert review._validate_acceptance(None) == []


def test_validate_acceptance_valid():
    raw = [
        {"criterion": "Tests grün", "status": "met", "evidence": "pytest"},
        {"criterion": "Runtime-Verhalten", "status": "unverifiable"},  # evidence optional
    ]
    assert review._validate_acceptance(raw) == [
        {"criterion": "Tests grün", "status": "met", "evidence": "pytest"},
        {"criterion": "Runtime-Verhalten", "status": "unverifiable", "evidence": ""},
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "met",  # kein List
        ["string"],  # Eintrag kein Dict
        [{"status": "met"}],  # Kriterium fehlt
        [{"criterion": "  ", "status": "met"}],  # leeres Kriterium
        [{"criterion": "x"}],  # Status fehlt
        [{"criterion": "x", "status": "maybe"}],  # unbekannter Status
        [{"criterion": "x", "status": "MET"}],  # Status ist case-sensitiv
    ],
)
def test_validate_acceptance_invalid_rejected(raw):
    """Ungültige Struktur wird abgelehnt (None), nicht still ignoriert."""
    assert review._validate_acceptance(raw) is None


def test_parse_verdict_acceptance_valid():
    d = _parse_verdict(
        '{"verdict": "approved", "findings": [], "summary": "ok", '
        '"acceptance": [{"criterion": "c", "status": "met", "evidence": "e"}]}'
    )
    assert d["acceptance"] == [{"criterion": "c", "status": "met", "evidence": "e"}]


def test_parse_verdict_acceptance_missing_defaults_empty():
    d = _parse_verdict('{"verdict": "approved", "findings": [], "summary": "ok"}')
    assert d["acceptance"] == []


@pytest.mark.parametrize(
    "acceptance",
    [
        "met",
        [{"status": "met"}],
        [{"criterion": "c", "status": "maybe", "evidence": "e"}],
    ],
)
def test_parse_verdict_rejects_invalid_acceptance(acceptance):
    """Ungültiges acceptance-JSON → kein Verdict (Step failt, kein Silent-Pass)."""
    text = json.dumps({"verdict": "approved", "findings": [], "summary": "ok", "acceptance": acceptance})
    assert _parse_verdict(text) is None
