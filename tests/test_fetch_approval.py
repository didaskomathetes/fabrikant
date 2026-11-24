"""Tests für die gebündelte `access`-Freigabe (ADR 0060 §3, ADR 0059 §5a)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fabrikant.fetch import (
    ACCESS_TIMEOUT_HOURS,
    AccessRequest,
    PendingURL,
    bundle_access,
    results_to_pending,
)
from fabrikant.fetch.fetcher import Fetcher

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _url(u: str, domain: str = "", found_in: str = "") -> PendingURL:
    return PendingURL(url=u, domain=domain or u.split("/")[2], found_in=found_in, scan="", why="test")


def _req(urls, **kw) -> AccessRequest:
    return AccessRequest(item="relay-1", urls=[_url(u) for u in urls], created_at=NOW, **kw)


# --- PendingURL ----------------------------------------------------------------
def test_pending_url_roundtrip():
    p = _url("https://a.example/x", "a.example", "search")
    d = p.to_dict()
    assert d == {"url": "https://a.example/x", "domain": "a.example", "found_in": "search", "scan": "", "why": "test"}
    back = PendingURL.from_dict(d)
    assert back == p


def test_pending_url_rejects_bad():
    assert PendingURL.from_dict(None) is None
    assert PendingURL.from_dict({"domain": "x"}) is None  # kein url
    assert PendingURL.from_dict("not-a-dict") is None


# --- pending / expiry ----------------------------------------------------------
def test_pending_lists_undecided():
    r = _req(["https://a.example/1", "https://b.example/2"])
    r.grant("https://a.example/1")
    assert [u.url for u in r.pending()] == ["https://b.example/2"]


def test_expiry():
    r = _req(["https://a.example/1"])
    assert not r.is_expired(NOW + timedelta(hours=1))
    assert r.is_expired(NOW + timedelta(hours=ACCESS_TIMEOUT_HOURS + 1))
    assert r.status == "open"
    r.check_expiry(NOW + timedelta(hours=ACCESS_TIMEOUT_HOURS + 1))
    assert r.status == "expired"
    # innerhalb des Zeitfensters: bleibt open
    r2 = _req(["https://a.example/1"])
    r2.check_expiry(NOW + timedelta(hours=1))
    assert r2.status == "open"


# --- grant / reject ------------------------------------------------------------
def test_grant_reject_per_url():
    r = _req(["https://a.example/1", "https://b.example/2", "https://c.example/3"])
    assert r.grant("https://a.example/1") == ["https://a.example/1"]
    assert r.reject("https://b.example/2") == ["https://b.example/2"]
    assert r.granted_urls() == ["https://a.example/1"]
    assert r.rejected_urls() == ["https://b.example/2"]
    assert [u.url for u in r.pending()] == ["https://c.example/3"]
    assert r.status == "open"


def test_grant_all():
    r = _req(["https://a.example/1", "https://b.example/2"])
    assert sorted(r.grant("all")) == ["https://a.example/1", "https://b.example/2"]
    assert r.status == "resolved"  # alles entschieden


def test_no_double_decision():
    r = _req(["https://a.example/1"])
    r.grant("https://a.example/1")
    # bereits freigegeben → Reject ändert nichts
    assert r.reject("https://a.example/1") == []
    assert r.decided["https://a.example/1"] == "granted"


def test_unknown_url_ignored():
    r = _req(["https://a.example/1"])
    assert r.grant("https://zzz.example/nope") == []  # nicht Teil des Requests
    assert r.granted_urls() == []


# --- Domain-Statistik / Suggest domain -----------------------------------------
def test_domain_stats_counts_decisions():
    r = _req(["https://a.example/1", "https://a.example/2", "https://b.example/3"])
    r.grant(["https://a.example/1", "https://a.example/2"])
    r.reject("https://b.example/3")
    assert r.domain_stats() == {"a.example": 2, "b.example": 1}


def test_suggest_domain_threshold():
    # 1 Grant pro Domain → unter Schwelle → None
    r1 = _req(["https://a.example/1"])
    r1.grant("https://a.example/1")
    assert r1.suggest_domain() is None
    # 2 Grants auf derselben Domain → Vorschlag
    r2 = _req(["https://a.example/1", "https://a.example/2"])
    r2.grant("all")
    assert r2.suggest_domain() == "a.example"


# --- Anhängen ------------------------------------------------------------------
def test_append_dedups():
    r = _req(["https://a.example/1"])
    added = r.append([_url("https://a.example/1"), _url("https://b.example/2")])
    assert [u.url for u in added] == ["https://b.example/2"]
    assert len(r.urls) == 2


# --- ADR-0059-Kopplung ---------------------------------------------------------
def test_to_approval_request():
    r = _req(["https://a.example/1", "https://a.example/2", "https://b.example/3"])
    r.grant("https://a.example/1")
    ar = r.to_approval_request()
    assert ar.reason == "escalation"
    assert ar.kind == "access"
    assert ar.step == "fetch"
    assert "2 URL" in ar.why  # 2 noch offen
    assert "a.example" in ar.what and "b.example" in ar.what


# --- Persistenz ----------------------------------------------------------------
def test_request_roundtrip():
    r = _req(["https://a.example/1", "https://b.example/2"])
    r.grant("https://a.example/1")
    d = r.to_dict()
    back = AccessRequest.from_dict(d)
    assert back is not None
    assert back.item == "relay-1"
    assert len(back.urls) == 2
    assert back.decided["https://a.example/1"] == "granted"
    assert back.status == "open"
    assert back.timeout_hours == ACCESS_TIMEOUT_HOURS


def test_request_from_dict_rejects_bad():
    assert AccessRequest.from_dict(None) is None
    assert AccessRequest.from_dict({}) is None  # kein item
    assert AccessRequest.from_dict({"item": "x", "decided": "not-a-dict"}) is not None


# --- bundle_access / results_to_pending ----------------------------------------
def test_bundle_access_dedups_and_empty():
    assert bundle_access("i", []) is None
    req = bundle_access("i", [_url("https://a.example/1"), _url("https://a.example/1")], now=NOW)
    assert req is not None
    assert len(req.urls) == 1
    assert req.created_at == NOW


def test_bundle_access_accepts_dicts():
    req = bundle_access("i", [{"url": "https://a.example/1", "domain": "a.example", "found_in": "search"}], now=NOW)
    assert req is not None
    assert req.urls[0].found_in == "search"


def test_results_to_pending_only_not_fetched():
    f = Fetcher(approved_domains={"arxiv.org"})
    res = [
        f.fetch("https://bad.example/1", found_in="search"),
        f.fetch("https://bad2.example/2", found_in="src-9"),
    ]
    pending = results_to_pending(res)
    assert len(pending) == 2
    assert pending[0].domain == "bad.example"
    assert pending[0].found_in == "search"
    # ein clean-Result würde gefiltert — hier simuliert:
    from fabrikant.fetch.fetcher import FetchResult

    mixed = res + [FetchResult(url="https://ok.example", status="clean", domain="ok.example")]
    assert len(results_to_pending(mixed)) == 2
