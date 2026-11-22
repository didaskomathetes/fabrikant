"""Tests für Zugriffsprofile + einmalige Grants (ADR 0060, Stufen 3–4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fabrik.fetch.profiles import (
    AccessProfile,
    FetchProfile,
    Grant,
    GrantStore,
    fetcher_config_for,
    is_readonly_credential,
    is_write_credential,
    parse_access_spec,
    profile_names_for_loop,
    resolve_profiles,
    validate_access_spec,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

SPEC = {
    "access": {
        "research-web": {
            "fetch": {
                "approved_domains": ["arxiv.org", "docs.python.org"],
                "limits": {"fetches_per_session": 40, "queries_per_session": 15},
            }
        },
        "langfuse-ro": {
            "egress": ["langfuse.example.internal"],
            "env": {"LANGFUSE_SECRET_KEY": "langfuse-ro", "LANGFUSE_RO": "langfuse-ro"},
        },
        "dangerous-write": {
            "egress": ["ci.example.internal"],
            "env": {"CI_DEPLOY_TOKEN": "ci-write", "CI_RO_KEY": "ci-ro"},
        },
    },
    "loops": {
        "develop": {"access": ["langfuse-ro"]},
        "conflicted": {"access": ["research-web", "dangerous-write"]},
    },
    "relay": {"access": ["research-web"]},
}


# --- FetchProfile --------------------------------------------------------------
def test_fetch_profile_from_dict():
    fp = FetchProfile.from_dict({"approved_domains": ["a.org"], "limits": {"fetches_per_session": 10}})
    assert fp is not None
    assert fp.approved_domains == ("a.org",)
    assert fp.fetches_per_session == 10
    assert fp.queries_per_session == 15  # Default
    assert fp.has_classifier is False


def test_fetch_profile_defaults_and_bad():
    assert FetchProfile.from_dict(None) is None
    assert FetchProfile.from_dict("x") is None
    fp = FetchProfile.from_dict({})
    assert fp is not None
    assert fp.approved_domains == ()
    assert fp.fetches_per_session == 40
    # bool statt int → Default
    assert FetchProfile.from_dict({"limits": {"fetches_per_session": True}}).fetches_per_session == 40


def test_fetch_profile_classifier_flag():
    assert FetchProfile.from_dict({"classifier": {"connection": "x"}}).has_classifier is True
    assert FetchProfile.from_dict({}).has_classifier is False


# --- AccessProfile -------------------------------------------------------------
def test_access_profile_full():
    ap = AccessProfile.from_dict(SPEC["access"]["langfuse-ro"] | {"name": "langfuse-ro"})
    assert ap is not None
    assert ap.name == "langfuse-ro"
    assert ap.egress == ("langfuse.example.internal",)
    assert ap.env == {"LANGFUSE_SECRET_KEY": "langfuse-ro", "LANGFUSE_RO": "langfuse-ro"}
    assert ap.fetch is None
    assert ap.to_dict()["egress"] == ["langfuse.example.internal"]


def test_access_profile_requires_name():
    assert AccessProfile.from_dict({"egress": ["x"]}) is None  # kein name
    assert AccessProfile.from_dict(None) is None
    assert AccessProfile.from_dict({"name": "  "}) is None


def test_carries_write_credentials():
    ro = AccessProfile.from_dict({"name": "ro", "env": {"LANGFUSE_RO": "c"}})
    wr = AccessProfile.from_dict({"name": "wr", "env": {"DEPLOY_TOKEN": "c"}})
    assert ro is not None and ro.carries_write_credentials() is False
    assert wr is not None and wr.carries_write_credentials() is True


# --- Credential-Heuristik ------------------------------------------------------
def test_credential_heuristics():
    assert is_readonly_credential("LANGFUSE_RO")
    assert is_readonly_credential("CI_RO_KEY")
    assert not is_readonly_credential("LANGFUSE_SECRET_KEY")
    # Secret + nicht-RO → write
    assert is_write_credential("DEPLOY_TOKEN")
    assert is_write_credential("CI_DEPLOY_TOKEN")
    # Secret + RO → kein write
    assert not is_write_credential("LANGFUSE_RO")
    # kein Secret → kein write
    assert not is_write_credential("LOG_LEVEL")


# --- Spec-Parsing & Resolution -------------------------------------------------
def test_parse_access_spec():
    profs = parse_access_spec(SPEC)
    assert set(profs) == {"research-web", "langfuse-ro", "dangerous-write"}
    assert profs["research-web"].fetch is not None
    assert profs["langfuse-ro"].egress == ("langfuse.example.internal",)
    assert parse_access_spec(None) == {}
    assert parse_access_spec({"access": "nope"}) == {}
    assert parse_access_spec({}) == {}


def test_profile_names_for_loop():
    assert profile_names_for_loop(SPEC, "develop") == ["langfuse-ro"]
    assert profile_names_for_loop(SPEC, "relay") == ["research-web"]  # Sonderfall
    assert profile_names_for_loop(SPEC, "ghost") == []
    assert profile_names_for_loop(None, "develop") == []


def test_resolve_profiles_skips_undefined():
    spec = {"access": {"a": {"fetch": {"approved_domains": ["x"]}}}, "loops": {"L": {"access": ["a", "ghost"]}}}
    got = resolve_profiles(spec, "L")
    assert [p.name for p in got] == ["a"]  # ghost wird weggelassen


# --- Validierung (egress + fetch mit Write-Creds) ------------------------------
def test_validate_no_conflict_on_ro_creds():
    # develop: nur langfuse-ro (RO) → kein fetch + kein write → sauber
    spec = {"access": SPEC["access"], "loops": {"develop": {"access": ["langfuse-ro"]}}}
    assert validate_access_spec(spec) == []


def test_validate_conflict_on_write_creds():
    # conflicted: research-web (fetch) + dangerous-write (egress + write) → Konflikt
    problems = validate_access_spec(SPEC)
    assert len(problems) == 1
    assert "conflicted" in problems[0]
    assert "fetch-Profil" in problems[0]


def test_validate_ignores_ro_egress_with_fetch():
    # fetch + egress, aber egress nur RO-Creds → KEIN Konflikt
    spec = {
        "access": {
            "web": {"fetch": {"approved_domains": ["a"]}},
            "ro": {"egress": ["x"], "env": {"RO_KEY": "c"}},
        },
        "loops": {"L": {"access": ["web", "ro"]}},
    }
    assert validate_access_spec(spec) == []


# --- Fetcher-Anbindung ---------------------------------------------------------
def test_fetcher_config_union_and_min():
    spec = {
        "access": {
            "a": {"fetch": {"approved_domains": ["x.org"], "limits": {"fetches_per_session": 20}}},
            "b": {"fetch": {"approved_domains": ["y.org", "x.org"], "limits": {"queries_per_session": 5}}},
        },
        "loops": {"L": {"access": ["a", "b"]}},
    }
    cfg = fetcher_config_for(resolve_profiles(spec, "L"))
    assert cfg["approved_domains"] == ["x.org", "y.org"]  # Union
    assert cfg["fetches_per_session"] == 20  # min(20, 40)
    assert cfg["queries_per_session"] == 5  # min(15, 5)


def test_fetcher_config_no_fetch_profile():
    assert fetcher_config_for([AccessProfile(name="x", egress=("y",))])["approved_domains"] == []
    assert fetcher_config_for([])["approved_domains"] == []


# --- Grant ---------------------------------------------------------------------
def test_grant_time_expiry():
    g = Grant(profile_name="p", item="PR #1", granted_at=NOW, expires_at=NOW + timedelta(hours=24))
    assert g.is_active(NOW + timedelta(hours=1))
    assert not g.is_active(NOW + timedelta(hours=25))


def test_grant_session_expiry():
    g = Grant(profile_name="p", item="RT #2", granted_at=NOW, max_sessions=2)
    assert g.is_active(NOW)
    g.record_use()
    g.record_use()
    assert not g.is_active(NOW)  # 2/2 verbraucht


def test_grant_roundtrip():
    g = Grant(
        profile_name="p",
        item="PR #1",
        granted_at=NOW,
        expires_at=NOW + timedelta(hours=24),
        max_sessions=3,
        used_sessions=1,
    )
    back = Grant.from_dict(g.to_dict())
    assert back is not None
    assert back.profile_name == "p" and back.max_sessions == 3 and back.used_sessions == 1
    assert back.expires_at == g.expires_at
    assert Grant.from_dict({"profile_name": "p"}) is None  # kein item
    assert Grant.from_dict({"item": "x"}) is None  # kein profile_name
    assert Grant.from_dict("x") is None


# --- GrantStore ----------------------------------------------------------------
def test_grant_store_lifecycle():
    store = GrantStore()
    assert store.active_profile_names(NOW) == set()
    store.grant("research-web", "PR #123", now=NOW, ttl_hours=24)
    assert store.active_profile_names(NOW) == {"research-web"}
    # develop hat research-web NICHT statisch, aber via Grant:
    eff = [p.name for p in store.effective_profiles(SPEC, "develop", NOW)]
    assert eff == ["langfuse-ro", "research-web"]
    # Ablauf:
    assert "research-web" not in store.active_profile_names(NOW + timedelta(hours=25))


def test_grant_store_record_usage():
    store = GrantStore()
    store.grant("p", "PR #1", now=NOW, max_sessions=1)
    assert store.record_usage("p", "PR #1", NOW) is True
    # max_sessions=1 → nach 1 Nutzung inaktiv
    assert store.record_usage("p", "PR #1", NOW) is False  # kein aktiver Grant mehr
    # falsches Item → False
    store2 = GrantStore()
    store2.grant("p", "PR #9", now=NOW, max_sessions=5)
    assert store2.record_usage("p", "PR #1", NOW) is False


def test_grant_store_roundtrip():
    store = GrantStore()
    store.grant("research-web", "PR #123", now=NOW, ttl_hours=24, max_sessions=3)
    back = GrantStore.from_dict(store.to_dict())
    assert back is not None
    assert len(back.grants) == 1
    assert back.grants[0].profile_name == "research-web"
    assert back.grants[0].max_sessions == 3
    assert GrantStore.from_dict(None) is None
    assert GrantStore.from_dict({}) is None  # kein grants
