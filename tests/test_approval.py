"""Tests: ApprovalRequest (typisierter record['approval'])."""

from __future__ import annotations

import pytest

from fabrikant.approval import ApprovalRequest


def test_to_dict_omits_empty_fields():
    assert ApprovalRequest(reason="step", step="s", what="x").to_dict() == {"reason": "step", "step": "s", "what": "x"}


def test_to_dict_keeps_tier_and_permissions():
    d = ApprovalRequest(reason="tier", tier=2, permissions=["edit * → allow"]).to_dict()
    assert d == {"reason": "tier", "tier": 2, "permissions": ["edit * → allow"]}


def test_from_dict_roundtrip_and_unknown_keys():
    d = {"reason": "step", "why": "w", "what": "x", "issue": "http://i", "legacy": 1}
    req = ApprovalRequest.from_dict(d)
    assert req.why == "w"
    assert req.to_dict() == {k: v for k, v in d.items() if k != "legacy"}


def test_from_dict_tolerates_non_dict_and_missing_reason():
    assert ApprovalRequest.from_dict(None) == ApprovalRequest(reason="")
    assert ApprovalRequest.from_dict("kaputt").lines() == []
    assert ApprovalRequest.from_dict({"what": "x", "tier": None}).tier is None


def test_merged_overrides_and_keeps_reason_when_absent():
    base = ApprovalRequest(reason="gate", step="hg", what="Kritizitäts-Check", issue="http://i")
    merged = base.merged({"reason": "criticality", "what": "Kritische Änderung: ADR"})
    assert (merged.reason, merged.what, merged.step, merged.issue) == (
        "criticality",
        "Kritische Änderung: ADR",
        "hg",
        "http://i",
    )
    assert base.merged({"what": "y"}).reason == "gate"
    assert base.merged(None) == base


def test_lines_order():
    req = ApprovalRequest(
        reason="step",
        condition="label = x",
        why="w",
        what="was",
        permissions=["p"],
        issue="http://i",
        tier=3,
    )
    assert req.lines() == ["bedingung: label = x", "warum: w", "was: was", "rechte: p", "issue: http://i", "tier: 3"]


@pytest.mark.parametrize(
    ("raw", "expected_perms"),
    [({"permissions": 123}, []), ({"permissions": "abc"}, []), ({"permissions": ["a", 1, None]}, ["a"])],
)
def test_from_dict_sanitizes_permissions(raw, expected_perms):
    req = ApprovalRequest.from_dict({"reason": "step", **raw})
    assert req.permissions == expected_perms
    assert all(isinstance(line, str) for line in req.lines())


def test_from_dict_drops_wrongly_typed_fields():
    req = ApprovalRequest.from_dict({"reason": 5, "why": ["x"], "tier": "2", "issue": "u"})
    assert (req.reason, req.why, req.tier, req.issue) == ("", "", None, "u")
    assert ApprovalRequest.from_dict({"tier": True}).tier is None
    assert ApprovalRequest.from_dict({"tier": 2}).tier == 2


def test_lines_pr_with_and_without_title():
    assert ApprovalRequest(reason="step", pr="http://p", pr_title="T").lines() == ["pr: T — http://p"]
    assert ApprovalRequest(reason="step", pr="http://p").lines() == ["pr: http://p"]


def test_from_dict_roundtrips_pr_fields():
    d = {"reason": "step", "pr": "http://p", "pr_title": "T"}
    assert ApprovalRequest.from_dict(d).to_dict() == d


def test_lines_follow_fabrikant_lang(monkeypatch):
    req = ApprovalRequest(reason="step", condition="c", why="w", what="x", permissions=["p"])
    monkeypatch.setenv("FABRIKANT_LANG", "en")
    assert req.lines() == ["condition: c", "why: w", "what: x", "permissions: p"]
