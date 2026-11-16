"""Retry-Ketten (ADR 0036): Store, Zustandsautomat, Policy, Trigger-Schlüssel,
Cron-Validierung, Ketten-Updates, Versuche starten, UI-Aktionen.

Isoliert über ``ChainStore(path=tmp)``; ``config.STATE_DIR`` ist pro Test
bereits isoliert (conftest).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from helpers import http_error, not_none

from fabrik import config, pause, retry_chains, specs
from fabrik.retry import RetryPolicy
from fabrik.retry_chains import (
    CHAIN_TRANSITIONS,
    BackfillSummary,
    ChainAction,
    ChainState,
    ChainStore,
    RetryBlocked,
    RetryChain,
    RunEnd,
    _attempt_vars,
    _decide_outcome,
    _gh_get,
    _trigger_for_key,
    backfill_chains,
    cron_min_interval,
    dismiss_chain,
    on_run_finished,
    on_run_finished_safe,
    policy_for,
    record_run_end,
    release_stale_claims,
    retry_now,
    start_due_attempts,
    trigger_key,
    validate_retry_cron,
)
from fabrik.runmodel import FailureKind, InvalidTransition, check_transition

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
FINISHED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
CRON_KEY = "cron:* * * * *"
ISSUE_KEY = "issue:owner/repo#42"
PR_KEY = "pr:owner/repo#7"


@pytest.fixture(autouse=True)
def _clear_cron_cache():
    """Cron-Intervall-Cache pro Test leeren (modul-global)."""
    retry_chains._cron_interval_cache.clear()
    yield
    retry_chains._cron_interval_cache.clear()


@pytest.fixture
def store(tmp_path):
    return ChainStore(path=tmp_path / "retry-chains.json")


@pytest.fixture
def fake_now(monkeypatch):
    """``config.utcnow`` auf den festen ``NOW`` legen (Due-Checks deterministisch)."""
    monkeypatch.setattr(config, "utcnow", lambda: NOW)
    return NOW


def _write_chain(store, project="p", loop="l", key=CRON_KEY, state=ChainState.PENDING, **fields):
    """Kette mit voller Feld-Kontrolle in den Store schreiben (merge, nicht Replace)."""
    chain = RetryChain(
        project=project,
        loop=loop,
        trigger_key=key,
        state=state,
        next_attempt_at=fields.pop("next_attempt_at", ""),
        last_run_id=fields.pop("last_run_id", ""),
        claimed_run_id=fields.pop("claimed_run_id", ""),
        created=fields.pop("created", NOW.isoformat()),
        updated=fields.pop("updated", NOW.isoformat()),
    )
    chains = store._load()
    chains[chain.key] = asdict(chain)
    store._save(chains)
    return store.get(project, loop, key)


def _spec(github="owner/repo", trigger_type="on_issue", loop_retry=None, project_retry=None):
    """Minimal-Spec für Policy-/Trigger-Tests."""
    loop = {"triggers": [{"type": trigger_type, "repo": github}]}
    if loop_retry is not None:
        loop["retry"] = loop_retry
    spec = {"name": "p", "github": github, "loops": {"l": loop}}
    if project_retry is not None:
        spec["retry"] = project_retry
    return spec


# ---------------------------------------------------------------- Zustandsautomat
def test_chain_transitions_table():
    assert CHAIN_TRANSITIONS[ChainState.PENDING] == frozenset(
        {ChainState.EXHAUSTED, ChainState.RESOLVED, ChainState.DISMISSED}
    )
    assert CHAIN_TRANSITIONS[ChainState.EXHAUSTED] == frozenset(
        {ChainState.PENDING, ChainState.RESOLVED, ChainState.DISMISSED}
    )
    assert CHAIN_TRANSITIONS[ChainState.RESOLVED] == frozenset()
    assert CHAIN_TRANSITIONS[ChainState.DISMISSED] == frozenset()


@pytest.mark.parametrize(
    ("cur", "target"),
    [
        (ChainState.PENDING, ChainState.EXHAUSTED),
        (ChainState.PENDING, ChainState.RESOLVED),
        (ChainState.PENDING, ChainState.DISMISSED),
        (ChainState.EXHAUSTED, ChainState.PENDING),
        (ChainState.EXHAUSTED, ChainState.RESOLVED),
        (ChainState.EXHAUSTED, ChainState.DISMISSED),
    ],
)
def test_check_transition_allowed(cur, target):
    assert check_transition(CHAIN_TRANSITIONS, cur, target) is target


@pytest.mark.parametrize(
    ("cur", "target"),
    [
        (ChainState.PENDING, ChainState.PENDING),
        (ChainState.EXHAUSTED, ChainState.EXHAUSTED),
        (ChainState.RESOLVED, ChainState.PENDING),
        (ChainState.RESOLVED, ChainState.EXHAUSTED),
        (ChainState.RESOLVED, ChainState.DISMISSED),
        (ChainState.DISMISSED, ChainState.PENDING),
        (ChainState.DISMISSED, ChainState.EXHAUSTED),
        (ChainState.DISMISSED, ChainState.RESOLVED),
    ],
)
def test_check_transition_forbidden(cur, target):
    with pytest.raises(InvalidTransition):
        check_transition(CHAIN_TRANSITIONS, cur, target)


# ---------------------------------------------------------------- ChainStore.create
def test_create_pending(store):
    chain = store.create("p", "l", CRON_KEY, next_attempt_at=NOW, last_run_id="r1", now=NOW)
    assert chain.state is ChainState.PENDING
    assert chain.next_attempt_at == NOW.isoformat()
    assert chain.last_run_id == "r1"
    assert chain.claimed_run_id == ""
    assert store.get("p", "l", CRON_KEY) is not None


def test_create_blocked_by_pending(store):
    store.create("p", "l", CRON_KEY, next_attempt_at=NOW, last_run_id="r1", now=NOW)
    with pytest.raises(ValueError, match="bereits pending"):
        store.create("p", "l", CRON_KEY, next_attempt_at=NOW, last_run_id="r2", now=NOW)


@pytest.mark.parametrize("state", [ChainState.EXHAUSTED, ChainState.RESOLVED, ChainState.DISMISSED])
def test_create_allowed_after_terminal(store, state):
    _write_chain(store, state=state)
    chain = store.create("p", "l", CRON_KEY, next_attempt_at=NOW, last_run_id="r2", now=NOW)
    assert chain.state is ChainState.PENDING
    assert chain.last_run_id == "r2"


def test_create_exhausted_for_backfill(store):
    """Bestandsabgleich: direkt ``exhausted`` anlegen (ohne fälligen Versuch)."""
    chain = store.create(
        "p", "l", ISSUE_KEY, next_attempt_at=None, last_run_id="r9", now=NOW, state=ChainState.EXHAUSTED
    )
    assert (chain.state, chain.next_attempt_at, chain.last_run_id) == (ChainState.EXHAUSTED, "", "r9")
    assert store.get("p", "l", ISSUE_KEY) is not None


@pytest.mark.parametrize(
    ("state", "next_at"),
    [(ChainState.PENDING, None), (ChainState.EXHAUSTED, NOW)],
)
def test_create_rejects_inconsistent_state(store, state, next_at):
    with pytest.raises(ValueError, match="next_attempt_at"):
        store.create("p", "l", CRON_KEY, next_attempt_at=next_at, last_run_id="r1", now=NOW, state=state)
    assert store.get("p", "l", CRON_KEY) is None


# ---------------------------------------------------------------- ChainStore.claim
def test_claim_success_when_due(store):
    _write_chain(store, next_attempt_at=(NOW - timedelta(minutes=1)).isoformat())
    assert store.claim("p", "l", CRON_KEY, "run1", NOW) is True
    assert store.get("p", "l", CRON_KEY).claimed_run_id == "run1"


def test_claim_fails_when_not_due(store):
    _write_chain(store, next_attempt_at=(NOW + timedelta(minutes=5)).isoformat())
    assert store.claim("p", "l", CRON_KEY, "run1", NOW) is False
    assert store.get("p", "l", CRON_KEY).claimed_run_id == ""


def test_claim_fails_when_claimed(store):
    _write_chain(store, next_attempt_at=NOW.isoformat(), claimed_run_id="other")
    assert store.claim("p", "l", CRON_KEY, "run1", NOW) is False


def test_claim_fails_when_not_pending(store):
    _write_chain(store, state=ChainState.EXHAUSTED, next_attempt_at=NOW.isoformat())
    assert store.claim("p", "l", CRON_KEY, "run1", NOW) is False


def test_claim_fails_when_missing(store):
    assert store.claim("p", "l", CRON_KEY, "run1", NOW) is False


def test_claim_force_from_exhausted(store):
    _write_chain(store, state=ChainState.EXHAUSTED)
    assert store.claim("p", "l", CRON_KEY, "run1", NOW, force=True) is True
    chain = store.get("p", "l", CRON_KEY)
    # In derselben Transaktion wieder pending — der Claim bleibt beim neuen Run.
    assert (chain.state, chain.claimed_run_id, chain.next_attempt_at) == (ChainState.PENDING, "run1", "")


def test_claim_force_rejects_pending(store):
    _write_chain(store, next_attempt_at=NOW.isoformat())
    assert store.claim("p", "l", CRON_KEY, "run1", NOW, force=True) is False


def test_claim_force_rejects_claimed_exhausted(store):
    _write_chain(store, state=ChainState.EXHAUSTED, claimed_run_id="other")
    assert store.claim("p", "l", CRON_KEY, "run1", NOW, force=True) is False


# ---------------------------------------------------------------- ChainStore.transition
def test_transition_sets_next_attempt_at(store):
    _write_chain(store, next_attempt_at="")
    new_at = NOW + timedelta(hours=1)
    updated = store.transition("p", "l", CRON_KEY, to=ChainState.EXHAUSTED, now=NOW, next_attempt_at=new_at)
    assert updated.next_attempt_at == new_at.isoformat()


def test_transition_none_deletes_next_attempt_at(store):
    _write_chain(store, next_attempt_at=NOW.isoformat())
    updated = store.transition("p", "l", CRON_KEY, to=ChainState.EXHAUSTED, now=NOW, next_attempt_at=None)
    assert updated.next_attempt_at == ""


def test_transition_unset_keeps_next_attempt_at(store):
    _write_chain(store, next_attempt_at=NOW.isoformat())
    updated = store.transition("p", "l", CRON_KEY, to=ChainState.EXHAUSTED, now=NOW)
    assert updated.next_attempt_at == NOW.isoformat()


def test_transition_updates_last_run_id(store):
    _write_chain(store, last_run_id="old")
    updated = store.transition("p", "l", CRON_KEY, to=ChainState.EXHAUSTED, now=NOW, last_run_id="new")
    assert updated.last_run_id == "new"


def test_transition_keeps_last_run_id_when_none(store):
    _write_chain(store, last_run_id="old")
    updated = store.transition("p", "l", CRON_KEY, to=ChainState.EXHAUSTED, now=NOW)
    assert updated.last_run_id == "old"


def test_transition_releases_claim(store):
    _write_chain(store, claimed_run_id="run1")
    updated = store.transition("p", "l", CRON_KEY, to=ChainState.EXHAUSTED, now=NOW)
    assert updated.claimed_run_id == ""


def test_transition_missing_returns_none(store):
    assert store.transition("p", "l", CRON_KEY, to=ChainState.EXHAUSTED, now=NOW) is None


def test_transition_forbidden_raises(store):
    _write_chain(store, state=ChainState.RESOLVED)
    with pytest.raises(InvalidTransition):
        store.transition("p", "l", CRON_KEY, to=ChainState.PENDING, now=NOW)


def test_transition_to_none_keeps_state(store):
    _write_chain(store, claimed_run_id="run1", next_attempt_at=NOW.isoformat())
    updated = store.transition("p", "l", CRON_KEY, to=None, now=NOW)
    assert (updated.state, updated.claimed_run_id) == (ChainState.PENDING, "")


def test_transition_expect_claim_mismatch_raises(store):
    _write_chain(store, claimed_run_id="other")
    with pytest.raises(retry_chains.ClaimConflict):
        store.transition("p", "l", CRON_KEY, to=ChainState.EXHAUSTED, now=NOW, expect_claim="")
    assert store.get("p", "l", CRON_KEY).claimed_run_id == "other"


# ---------------------------------------------------------------- ChainStore.release_claim / set_state
def test_release_claim_by_owner(store):
    _write_chain(store, claimed_run_id="run1", last_run_id="r1")
    updated = store.release_claim("p", "l", CRON_KEY, "run1", NOW, to=ChainState.EXHAUSTED, next_attempt_at=None)
    assert updated.state is ChainState.EXHAUSTED
    assert updated.next_attempt_at == ""
    assert updated.last_run_id == "r1"
    assert updated.claimed_run_id == ""


def test_release_claim_of_other_run_is_noop(store):
    """Compare-and-Release: fremder Claim bleibt unangetastet."""
    _write_chain(store, claimed_run_id="other", next_attempt_at=NOW.isoformat())
    assert store.release_claim("p", "l", CRON_KEY, "run1", NOW) is None
    assert store.get("p", "l", CRON_KEY).claimed_run_id == "other"


def test_release_claim_forbidden_transition_is_noop(store):
    _write_chain(store, state=ChainState.RESOLVED, claimed_run_id="run1")
    assert store.release_claim("p", "l", CRON_KEY, "run1", NOW, to=ChainState.PENDING) is None


def test_set_state_dismissed_clears_next_attempt_at(store):
    _write_chain(store, next_attempt_at=NOW.isoformat())
    updated = store.set_state("p", "l", CRON_KEY, ChainState.DISMISSED, NOW)
    assert updated.state is ChainState.DISMISSED
    assert updated.next_attempt_at == ""


def test_set_state_resolved_keeps_next_attempt_at(store):
    _write_chain(store, next_attempt_at=NOW.isoformat())
    updated = store.set_state("p", "l", CRON_KEY, ChainState.RESOLVED, NOW)
    assert updated.state is ChainState.RESOLVED
    assert updated.next_attempt_at == NOW.isoformat()


# ---------------------------------------------------------------- ChainStore.due
def test_due_returns_pending_due_unclaimed(store):
    _write_chain(store, next_attempt_at=(NOW - timedelta(minutes=1)).isoformat())
    due = store.due(NOW)
    assert len(due) == 1
    assert due[0].state is ChainState.PENDING


def test_due_excludes_not_due(store):
    _write_chain(store, next_attempt_at=(NOW + timedelta(minutes=5)).isoformat())
    assert store.due(NOW) == []


def test_due_excludes_claimed(store):
    _write_chain(store, next_attempt_at=NOW.isoformat(), claimed_run_id="run1")
    assert store.due(NOW) == []


def test_due_excludes_non_pending(store):
    _write_chain(store, state=ChainState.EXHAUSTED, next_attempt_at=NOW.isoformat())
    assert store.due(NOW) == []


# ---------------------------------------------------------------- ChainStore.all / get / robustness
def test_all_returns_all_chains(store):
    _write_chain(store, key=CRON_KEY)
    _write_chain(store, key=ISSUE_KEY)
    assert len(store.all()) == 2


def test_all_skips_corrupt_entries(store, tmp_path):
    _write_chain(store, key=CRON_KEY)
    p = store.root
    data = json.loads(p.read_text())
    data["bad::entry::x"] = "not-a-dict"
    data["bad2::entry::y"] = {"state": "bogus"}
    p.write_text(json.dumps(data))
    assert len(store.all()) == 1


def test_get_missing_returns_none(store):
    assert store.get("p", "l", "nope") is None


def test_load_corrupt_file_raises(store):
    store.root.write_text("{nicht json")
    with pytest.raises(retry_chains.ChainStoreCorrupt):
        store._load()


def test_corrupt_store_refuses_mutation(store):
    """Ein abgeschnittener Store darf beim nächsten Schreiben nicht alle Ketten löschen."""
    store.root.write_text('{"p::l::cron:x": {"state": "pending"')
    with pytest.raises(retry_chains.ChainStoreCorrupt):
        store.create("p", "l", CRON_KEY, next_attempt_at=NOW, last_run_id="r0", now=NOW)
    assert store.root.read_text() == '{"p::l::cron:x": {"state": "pending"'


def test_load_non_object_raises(store):
    store.root.write_text("[]")
    with pytest.raises(retry_chains.ChainStoreCorrupt):
        store._load()


# ---------------------------------------------------------------- policy_for
def test_policy_for_none_when_no_retry():
    assert policy_for(_spec(), "l") is None


def test_policy_for_project_level():
    spec = _spec(project_retry={"max": 2, "backoff": ["1m"]})
    policy = policy_for(spec, "l")
    assert policy is not None
    assert policy.max == 2
    assert policy.backoff == (timedelta(minutes=1),)


def test_policy_for_loop_overrides_project():
    spec = _spec(
        project_retry={"max": 5, "backoff": ["10m"]},
        loop_retry={"max": 2, "backoff": ["1m"]},
    )
    policy = policy_for(spec, "l")
    assert policy is not None
    assert policy.max == 2
    assert policy.backoff == (timedelta(minutes=1),)


def test_policy_for_loop_max_zero_disables():
    spec = _spec(project_retry={"max": 5}, loop_retry={"max": 0})
    assert policy_for(spec, "l") is None


def test_policy_for_tolerant_parse_of_bad_block():
    # Defekte Blöcke sind Spec-Fehler (specs.py validiert); hier tolerant:
    # kein Raise, sondern Policy mit Defaults (Fehler landen in `errs`, die
    # `policy_for` verwirft).
    spec = _spec(loop_retry={"max": "viel"})
    policy = policy_for(spec, "l")
    assert policy is not None
    assert policy.max == 3  # Default


# ---------------------------------------------------------------- trigger_key
def test_trigger_key_cron():
    spec = _spec(trigger_type="cron")
    spec["loops"]["l"]["triggers"] = [{"type": "cron", "cron": "*/5 * * * *"}]
    assert trigger_key(spec, "l", "cron") == "cron:*/5 * * * *"


def test_trigger_key_cron_without_expr_is_none():
    spec = _spec(trigger_type="cron")
    spec["loops"]["l"]["triggers"] = [{"type": "cron"}]
    assert trigger_key(spec, "l", "cron") is None


def test_trigger_key_issue():
    assert trigger_key(_spec(), "l", "on_issue:#42") == "issue:owner/repo#42"


def test_trigger_key_pr_excludes_head_sha():
    assert trigger_key(_spec(trigger_type="on_pr"), "l", "on_pr:#7@abc123") == "pr:owner/repo#7"


def test_trigger_key_no_repo_is_none():
    spec = _spec(github="")
    assert trigger_key(spec, "l", "on_issue:#42") is None


def test_trigger_key_manual_is_none():
    assert trigger_key(_spec(), "l", "manual") is None


def test_trigger_key_uses_repo_of_matching_trigger_type():
    """on_issue in Repo A, on_pr in Repo B: jeder Schlüssel trägt das Repo seines Triggers."""
    spec = {
        "name": "p",
        "github": "owner/repo",
        "loops": {"l": {"triggers": [{"type": "on_issue", "repo": "org/a"}, {"type": "on_pr", "repo": "org/b"}]}},
    }
    assert trigger_key(spec, "l", "on_issue:#1") == "issue:org/a#1"
    assert trigger_key(spec, "l", "on_pr:#7@sha") == "pr:org/b#7"


def test_key_for_record_prefers_record_field():
    rec = {"trigger": "cron", "trigger_key": "cron:0 * * * *"}
    assert retry_chains.key_for_record(_spec(trigger_type="cron"), "l", rec) == "cron:0 * * * *"


def test_key_for_record_legacy_recomputes():
    assert retry_chains.key_for_record(_spec(), "l", {"trigger": "on_issue:#42"}) == ISSUE_KEY
    assert retry_chains.key_for_record(None, "l", {"trigger": "on_issue:#42"}) == ""


def test_cron_min_interval_zero_step_is_invalid():
    assert cron_min_interval("*/0 * * * *") is None


def test_trigger_key_repo_falls_back_to_github():
    spec = _spec(github="owner/repo")
    spec["loops"]["l"]["triggers"] = [{"type": "on_issue"}]  # kein repo-Feld
    assert trigger_key(spec, "l", "on_issue:#9") == "issue:owner/repo#9"


# ---------------------------------------------------------------- _trigger_for_key
def test_trigger_for_key_issue():
    assert _trigger_for_key(ISSUE_KEY) == "on_issue:#42"


def test_trigger_for_key_pr():
    assert _trigger_for_key(PR_KEY) == "on_pr:#7"


def test_trigger_for_key_cron():
    assert _trigger_for_key(CRON_KEY) == "cron"


def test_trigger_for_key_pr_uses_head_sha():
    """PR-Versuche tragen den aktuellen Head-SHA (ADR 0023) — Reconcile-Kompatibilität."""
    assert _trigger_for_key(PR_KEY, {"pr_head_sha": "abc123"}) == "on_pr:#7@abc123"


def test_trigger_for_key_pr_without_sha():
    assert _trigger_for_key(PR_KEY) == "on_pr:#7"
    assert _trigger_for_key(PR_KEY, {}) == "on_pr:#7"


# ---------------------------------------------------------------- cron_min_interval
def test_cron_min_interval_every_minute():
    assert cron_min_interval("* * * * *") == timedelta(minutes=1)


def test_cron_min_interval_every_5_minutes():
    assert cron_min_interval("*/5 * * * *") == timedelta(minutes=5)


def test_cron_min_interval_every_hour():
    assert cron_min_interval("0 * * * *") == timedelta(hours=1)


def test_cron_min_interval_invalid_is_none():
    assert cron_min_interval("not a cron") is None


def test_cron_min_interval_uses_shortest_gap_not_first():
    """01:00→23:00 sind 22 h, aber 23:00→01:00 nur 2 h."""
    assert cron_min_interval("0 1,23 * * *") == timedelta(hours=2)


def test_cron_min_interval_shortest_gap_across_months():
    # 1. und 28. jedes Monats: kürzester Abstand 28.2.2028 → 1.3.2028 (Schaltjahr) = 2 Tage.
    assert cron_min_interval("0 0 1,28 * *") == timedelta(days=2)


def test_cron_min_interval_weekly():
    assert cron_min_interval("30 6 * * 1") == timedelta(days=7)


def test_cron_min_interval_single_tick_is_none():
    assert cron_min_interval("0 0 29 2 *") is None  # nur der 29.2. im Fenster


def test_cron_min_interval_invalid_field_is_none():
    assert cron_min_interval("x * * * *") is None


def test_cron_min_interval_cached():
    first = cron_min_interval("* * * * *")
    retry_chains._cron_interval_cache.clear()
    # Nach Cache-Leeren wird neu berechnet — gleiches Ergebnis.
    assert cron_min_interval("* * * * *") == first


# ---------------------------------------------------------------- validate_retry_cron
def _cron_ldef(expr="*/5 * * * *"):
    return {"triggers": [{"type": "cron", "cron": expr}]}


def test_validate_retry_cron_no_policy_no_errors():
    errs, warns = validate_retry_cron(_spec(), "l", _cron_ldef(), None)
    assert errs == [] and warns == []


def test_validate_retry_cron_backoff_ge_interval_is_error():
    policy = RetryPolicy(max=3, backoff=(timedelta(minutes=10),), on=frozenset({FailureKind.TRANSIENT}))
    errs, _ = validate_retry_cron(_spec(), "l", _cron_ldef("*/5 * * * *"), policy)
    assert any("backoff-Wert" in e for e in errs)


def test_validate_retry_cron_backoff_below_interval_no_error():
    policy = RetryPolicy(max=3, backoff=(timedelta(minutes=1),), on=frozenset({FailureKind.TRANSIENT}))
    errs, _ = validate_retry_cron(_spec(), "l", _cron_ldef("*/5 * * * *"), policy)
    assert errs == []


def test_validate_retry_cron_ignores_backoff_beyond_max():
    """max: 1 nutzt nur den ersten Backoff-Wert; 2h (nie benutzt) ist kein Fehler."""
    policy = RetryPolicy(
        max=1, backoff=(timedelta(minutes=5), timedelta(hours=2)), on=frozenset({FailureKind.TRANSIENT})
    )
    errs, _ = validate_retry_cron(_spec(), "l", _cron_ldef("0 * * * *"), policy)
    assert errs == []


def test_validate_retry_cron_total_exceeds_interval_is_warning():
    policy = RetryPolicy(max=3, backoff=(timedelta(minutes=2),) * 3, on=frozenset({FailureKind.TRANSIENT}))
    _, warns = validate_retry_cron(_spec(), "l", _cron_ldef("*/5 * * * *"), policy)
    assert any("länger als Cron-Intervall" in w for w in warns)


def test_validate_retry_cron_total_within_interval_no_warning():
    policy = RetryPolicy(max=3, backoff=(timedelta(minutes=1),) * 3, on=frozenset({FailureKind.TRANSIENT}))
    _, warns = validate_retry_cron(_spec(), "l", _cron_ldef("*/5 * * * *"), policy)
    assert warns == []


def test_validate_retry_cron_ignores_non_cron_triggers():
    policy = RetryPolicy(max=3, backoff=(timedelta(minutes=10),), on=frozenset({FailureKind.TRANSIENT}))
    errs, warns = validate_retry_cron(_spec(), "l", {"triggers": [{"type": "on_issue"}]}, policy)
    assert errs == [] and warns == []


# ---------------------------------------------------------------- _decide_outcome
def _policy(max=3):
    return RetryPolicy(
        max=max, backoff=(timedelta(minutes=5),), on=frozenset({FailureKind.TRANSIENT, FailureKind.INTERRUPTED})
    )


def _chain(state, next_at="", claimed="", last="r0"):
    return RetryChain(
        project="p",
        loop="l",
        trigger_key=CRON_KEY,
        state=state,
        next_attempt_at=next_at,
        claimed_run_id=claimed,
        last_run_id=last,
    )


def _end(status="fail", kind=FailureKind.TRANSIENT, attempt=0, run_id="r1", retry_of="", retry_after=None):
    return RunEnd(run_id, status, kind, attempt, retry_of, FINISHED, retry_after)


def _attempt_run(status="fail", kind=FailureKind.TRANSIENT, attempt=1):
    """Versuch r1, der den Claim einer pending-Kette hält."""
    return _chain(ChainState.PENDING, claimed="r1"), _end(status, kind, attempt, retry_of="r0")


def test_decide_pass_resolves_pending():
    d = _decide_outcome(_chain(ChainState.PENDING), _policy(), _end("pass", attempt=1))
    assert (d.action, d.target) == (ChainAction.TRANSITION, ChainState.RESOLVED)
    assert d.next_attempt_at is retry_chains._UNSET


def test_decide_pass_resolves_even_while_other_attempt_claims():
    """ADR 0036: ein pass desselben Schlüssels (auch manuell) löst die Kette."""
    d = _decide_outcome(_chain(ChainState.PENDING, claimed="other"), _policy(), _end("pass"))
    assert (d.action, d.target) == (ChainAction.TRANSITION, ChainState.RESOLVED)


def test_decide_pass_no_chain_is_none():
    d = _decide_outcome(None, _policy(), _end("pass"))
    assert d.action is ChainAction.NONE and d.in_chain is False


def test_decide_rejected_attempt_dismisses():
    chain, run = _attempt_run("rejected")
    d = _decide_outcome(chain, _policy(), run)
    assert (d.action, d.target) == (ChainAction.TRANSITION, ChainState.DISMISSED)
    assert d.next_attempt_at is None


def test_decide_rejected_foreign_run_is_none():
    d = _decide_outcome(_chain(ChainState.PENDING), _policy(), _end("rejected"))
    assert d.action is ChainAction.NONE and d.in_chain is False


def test_decide_rejected_no_chain_is_none():
    d = _decide_outcome(None, _policy(), _end("rejected"))
    assert d.action is ChainAction.NONE


def test_decide_needs_approval_keeps_claim():
    chain, run = _attempt_run("needs_approval")
    d = _decide_outcome(chain, _policy(), run)
    assert d.action is ChainAction.NONE and d.in_chain is True


def test_decide_attempt_fail_below_max_stays_pending():
    chain, run = _attempt_run(attempt=1)
    d = _decide_outcome(chain, _policy(max=3), run)
    assert (d.action, d.target) == (ChainAction.TRANSITION, ChainState.PENDING)
    assert d.next_attempt_at == FINISHED + timedelta(minutes=5)
    assert d.last_run_id == "r1"
    assert d.attempt == 2


def test_decide_attempt_fail_at_max_exhausts():
    chain, run = _attempt_run(attempt=3)
    d = _decide_outcome(chain, _policy(max=3), run)
    assert (d.action, d.target) == (ChainAction.TRANSITION, ChainState.EXHAUSTED)
    assert d.next_attempt_at is None
    assert d.last_run_id == "r1"


def test_decide_attempt_fail_out_of_on_exhausts():
    chain, run = _attempt_run(kind=FailureKind.OUTCOME)
    d = _decide_outcome(chain, _policy(), run)
    assert (d.action, d.target) == (ChainAction.TRANSITION, ChainState.EXHAUSTED)


def test_decide_attempt_fail_without_policy_exhausts():
    """Retry abgeschaltet, während die Kette lief: Kette endet, Claim wird frei."""
    chain, run = _attempt_run()
    d = _decide_outcome(chain, None, run)
    assert (d.action, d.target) == (ChainAction.TRANSITION, ChainState.EXHAUSTED)


def test_decide_fail_no_policy_no_chain_is_none():
    d = _decide_outcome(None, None, _end())
    assert d.action is ChainAction.NONE and d.in_chain is False


def test_decide_fail_no_chain_creates():
    d = _decide_outcome(None, _policy(max=3), _end())
    assert (d.action, d.target) == (ChainAction.CREATE, ChainState.PENDING)
    assert d.next_attempt_at == FINISHED + timedelta(minutes=5)
    assert d.last_run_id == "r1"
    assert d.attempt == 1


def test_decide_terminal_reopen_creates_fresh_chain():
    for state in (ChainState.RESOLVED, ChainState.DISMISSED):
        d = _decide_outcome(_chain(state), _policy(max=3), _end())
        assert (d.action, d.target) == (ChainAction.CREATE, ChainState.PENDING)


def test_decide_late_attempt_of_dismissed_chain_plans_nothing():
    """Verwerfen während eines laufenden Versuchs: der Versuch plant nichts mehr."""
    d = _decide_outcome(_chain(ChainState.DISMISSED, last="r0"), _policy(), _end(attempt=1, retry_of="r0"))
    assert d.action is ChainAction.NONE
    assert d.in_chain is True  # Zustand dismissed → kein Alert


def test_decide_foreign_fail_while_attempt_claims_is_none():
    """Ein dritter Run (manuell/Webhook) darf den Claim eines laufenden Versuchs nicht freigeben."""
    d = _decide_outcome(_chain(ChainState.PENDING, claimed="attempt"), _policy(), _end(run_id="manual"))
    assert d.action is ChainAction.NONE and d.in_chain is False


def test_decide_foreign_fail_during_backoff_keeps_budget():
    d = _decide_outcome(_chain(ChainState.PENDING, next_at=NOW.isoformat()), _policy(), _end(run_id="manual"))
    assert d.action is ChainAction.NONE and d.in_chain is False


def test_decide_foreign_fail_reopens_exhausted_chain():
    d = _decide_outcome(_chain(ChainState.EXHAUSTED), _policy(), _end(run_id="tick"))
    assert (d.action, d.target, d.attempt) == (ChainAction.TRANSITION, ChainState.PENDING, 1)


def test_decide_foreign_non_retryable_fail_on_exhausted_is_not_in_chain():
    """Sonst würde ein unabhängiger Fehlschlag als run_exhausted gemeldet."""
    d = _decide_outcome(_chain(ChainState.EXHAUSTED), _policy(), _end(kind=FailureKind.OUTCOME, run_id="tick"))
    assert d.action is ChainAction.NONE and d.in_chain is False


def test_decide_retry_after_shifts_next_attempt():
    retry_after = FINISHED + timedelta(hours=1)
    d = _decide_outcome(None, _policy(max=3), _end(retry_after=retry_after))
    assert d.next_attempt_at == retry_after  # retry_after > backoff


def test_decide_retry_after_capped_by_max_wait():
    policy = RetryPolicy(
        max=3, backoff=(timedelta(minutes=5),), on=frozenset({FailureKind.TRANSIENT}), max_wait=timedelta(hours=1)
    )
    d = _decide_outcome(None, policy, _end(retry_after=FINISHED + timedelta(hours=5)))
    assert d.next_attempt_at == FINISHED + timedelta(hours=1)  # auf max_wait gekappt


def test_run_end_from_record_clamps_attempt():
    run = RunEnd.from_record({"id": "r", "status": "fail", "attempt": -3}, NOW)
    assert run.attempt == 0 and run.finished == NOW


# ---------------------------------------------------------------- on_run_finished
def test_on_run_finished_no_key_returns_not_in_chain():
    spec = _spec()
    record = {
        "id": "r1",
        "project": "p",
        "loop": "l",
        "trigger": "manual",
        "status": "fail",
        "failure_kind": "transient",
    }
    assert on_run_finished(spec, "p", "l", record) == {"in_chain": False}


def test_on_run_finished_creates_chain_on_first_failure(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    spec = _spec(loop_retry={"max": 3, "backoff": ["5m"]})
    record = {
        "id": "r1",
        "project": "p",
        "loop": "l",
        "trigger": "on_issue:#42",
        "status": "fail",
        "failure_kind": "transient",
        "attempt": 0,
        "finished": FINISHED.isoformat(),
    }
    info = on_run_finished(spec, "p", "l", record)
    assert info["in_chain"] is True
    assert info["state"] == "pending"
    assert info["attempt"] == 1
    chain = store.get("p", "l", ISSUE_KEY)
    assert chain is not None
    assert chain.state is ChainState.PENDING
    assert chain.last_run_id == "r1"


def test_on_run_finished_uses_record_trigger_key(store, monkeypatch):
    """Spec-Änderung während des Runs (Cron-Ausdruck): die Kette wird über den
    Record-Schlüssel gebucht, nicht über den neuen Spec-Schlüssel."""
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    spec = _spec(trigger_type="cron", loop_retry={"max": 3})
    spec["loops"]["l"]["triggers"] = [{"type": "cron", "cron": "*/10 * * * *"}]  # geändert
    record = {
        "id": "r1",
        "project": "p",
        "loop": "l",
        "trigger": "cron",
        "trigger_key": "cron:*/5 * * * *",
        "status": "fail",
        "failure_kind": "transient",
        "attempt": 0,
        "finished": FINISHED.isoformat(),
    }
    info = on_run_finished(spec, "p", "l", record)
    assert info["in_chain"] is True
    assert store.get("p", "l", "cron:*/5 * * * *") is not None
    assert store.get("p", "l", "cron:*/10 * * * *") is None


def test_on_run_finished_safe_swallows_store_errors(store, monkeypatch):
    """Volles Dateisystem: die Ketten-Buchhaltung darf nicht propagieren
    (sonst würde das Alert des Runs übersprungen)."""
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)

    def boom(chains):
        raise OSError("no space left on device")

    monkeypatch.setattr(store, "_save", boom)
    spec = _spec(loop_retry={"max": 3})
    assert on_run_finished_safe(spec, "p", "l", _fail_record()) == {"in_chain": False}


def test_on_run_finished_corrupt_store_is_reported(store, monkeypatch, capsys):
    """Defekter Ketten-Store ist kein Konflikt: on_run_finished propagiert, _safe meldet ihn."""
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    store.root.write_text("{nicht json")
    spec = _spec(loop_retry={"max": 3})
    with pytest.raises(retry_chains.ChainStoreCorrupt):
        retry_chains.on_run_finished(spec, "p", "l", _fail_record())
    assert on_run_finished_safe(spec, "p", "l", _fail_record()) == {"in_chain": False}
    assert "nicht gebucht" in capsys.readouterr().out


def test_record_run_end_uses_given_projects(store, monkeypatch):
    """Aufrufer mit vielen Runs (Reaper) reichen die einmal geladenen Specs durch."""
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    monkeypatch.setattr(retry_chains, "load_projects", lambda: pytest.fail("Specs erneut geladen"))
    info = retry_chains.record_run_end("p", _fail_record(), {"p": _spec(loop_retry={"max": 3})})
    assert info["in_chain"] is True


def test_on_run_finished_exhausts_at_max(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    spec = _spec(loop_retry={"max": 1, "backoff": ["5m"]})
    _write_chain(store, key=ISSUE_KEY, state=ChainState.PENDING, last_run_id="r0", claimed_run_id="r1")
    record = {
        "id": "r1",
        "project": "p",
        "loop": "l",
        "trigger": "on_issue:#42",
        "status": "fail",
        "failure_kind": "transient",
        "attempt": 1,
        "finished": FINISHED.isoformat(),
    }
    info = on_run_finished(spec, "p", "l", record)
    assert info["state"] == "exhausted"
    assert store.get("p", "l", ISSUE_KEY).state is ChainState.EXHAUSTED


def test_on_run_finished_resolves_on_pass(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    spec = _spec(loop_retry={"max": 3})
    _write_chain(store, key=ISSUE_KEY, state=ChainState.PENDING)
    record = {"id": "r1", "project": "p", "loop": "l", "trigger": "on_issue:#42", "status": "pass", "attempt": 1}
    info = on_run_finished(spec, "p", "l", record)
    assert info["state"] == "resolved"
    assert store.get("p", "l", ISSUE_KEY).state is ChainState.RESOLVED


def test_on_run_finished_coerces_bad_attempt_to_zero(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    spec = _spec(loop_retry={"max": 3, "backoff": ["5m"]})
    record = {
        "id": "r1",
        "project": "p",
        "loop": "l",
        "trigger": "on_issue:#42",
        "status": "fail",
        "failure_kind": "transient",
        "attempt": "bogus",
        "finished": FINISHED.isoformat(),
    }
    info = on_run_finished(spec, "p", "l", record)
    assert info["in_chain"] is True
    assert info["attempt"] == 1  # attempt 0 → +1


def test_on_run_finished_conflict_retry_recovers(store, monkeypatch):
    """Paralleler Zustandswechsel: erster Transition-Versuch schlägt fehl,
    zweiter gelingt (neu entscheiden)."""
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    spec = _spec(loop_retry={"max": 3, "backoff": ["5m"]})
    _write_chain(store, key=ISSUE_KEY, state=ChainState.EXHAUSTED, last_run_id="r0")
    record = {
        "id": "r1",
        "project": "p",
        "loop": "l",
        "trigger": "on_issue:#42",
        "status": "fail",
        "failure_kind": "transient",
        "attempt": 0,
        "finished": FINISHED.isoformat(),
    }
    real_transition = store.transition

    def flaky_transition(*a, **kw):
        flaky_transition.calls = getattr(flaky_transition, "calls", 0) + 1
        if flaky_transition.calls == 1:
            # Simuliere: Kette wurde parallel auf pending gesetzt → EXHAUSTED→PENDING
            # wäre erlaubt, aber wir zwingen einen Konflikt, indem wir den Zustand
            # zwischen Lesen und Schreiben drehen.
            _write_chain(store, key=ISSUE_KEY, state=ChainState.PENDING, last_run_id="r0")
            raise InvalidTransition("simulierter Konflikt")
        return real_transition(*a, **kw)

    monkeypatch.setattr(store, "transition", flaky_transition)
    info = on_run_finished(spec, "p", "l", record)
    # Neu entschieden aus pending: der Run hält keinen Claim → fremder Run,
    # die Kette plant unverändert weiter (Budget nicht zurückgesetzt).
    assert flaky_transition.calls == 1
    assert info == {"in_chain": False}
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.last_run_id) == (ChainState.PENDING, "r0")


# ---------------------------------------------------------------- Tri-State GitHub-Fetch
@pytest.fixture
def gh(monkeypatch):
    """GitHub-API stubben; liefert die aufgerufenen URLs."""
    calls = []
    state = {"response": {}, "token": "tok"}

    def api(method, url, tok):
        calls.append(url)
        resp = state["response"]
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr("fabrik.gitops._gh_token_safe", lambda role="dev": state["token"])
    monkeypatch.setattr("fabrik.gitops._gh_api", api)
    state["calls"] = calls
    return state


def test_gh_get_issue_found(gh):
    gh["response"] = {"number": 42, "title": "t", "body": "b", "state": "open"}
    data, api_error = _gh_get("owner/repo", "issue", "42")
    assert api_error is False
    assert data["title"] == "t"
    assert gh["calls"] == ["https://api.github.com/repos/owner/repo/issues/42"]


def test_gh_get_pr_uses_pulls_endpoint(gh):
    gh["response"] = {"number": 7, "state": "open", "head": {"ref": "b", "sha": "s"}}
    data, api_error = _gh_get("org/other", "pr", "7")
    assert data["number"] == 7 and api_error is False
    assert gh["calls"] == ["https://api.github.com/repos/org/other/pulls/7"]


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_gh_get_closed_is_gone(gh, kind):
    """Geschlossene Issues und gemergte/geschlossene PRs liefern 200 — gelten aber als weg."""
    gh["response"] = {"number": 1, "state": "closed", "merged": kind == "pr"}
    assert _gh_get("owner/repo", kind, "1") == (None, False)


def test_gh_get_closed_pr_accepted_when_loop_fires_on_closed(gh):
    """on_pr mit ``states: [closed]``: ein geschlossener PR ist der Trigger, nicht sein Ende."""
    gh["response"] = {"number": 7, "state": "closed"}
    data, api_error = _gh_get("owner/repo", "pr", "7", accept_closed=True)
    assert (data["number"], api_error) == (7, False)


def test_attempt_vars_passes_accept_closed_only_for_prs(monkeypatch):
    seen = []
    monkeypatch.setattr(
        retry_chains, "_gh_get", lambda r, k, n, accept_closed=False: seen.append(accept_closed) or (None, False)
    )
    _attempt_vars(PR_KEY, accept_closed=True)
    _attempt_vars(ISSUE_KEY, accept_closed=True)
    assert seen == [True, False]


@pytest.mark.parametrize(
    ("states", "expected"), [(None, False), (["open"], False), (["closed"], True), (["open", "closed"], True)]
)
def test_pr_accepts_closed(states, expected):
    spec = _spec(trigger_type="on_pr")
    if states is not None:
        spec["loops"]["l"]["triggers"][0]["states"] = states
    assert retry_chains.pr_accepts_closed(spec, "l") is expected


def test_gh_get_404_is_gone(gh):
    gh["response"] = http_error(404)
    assert _gh_get("owner/repo", "issue", "42") == (None, False)


def test_gh_get_api_error_is_transient(gh):
    gh["response"] = http_error(500)
    assert _gh_get("owner/repo", "issue", "42") == (None, True)


def test_gh_get_network_error_is_transient(gh):
    gh["response"] = OSError("boom")
    assert _gh_get("owner/repo", "pr", "42") == (None, True)


def test_gh_get_no_token_is_transient(gh):
    gh["token"] = ""
    assert _gh_get("owner/repo", "issue", "42") == (None, True)
    assert gh["calls"] == []


def test_gh_get_malformed_response_is_gone(gh):
    gh["response"] = ["not", "a", "dict"]
    assert _gh_get("owner/repo", "issue", "42") == (None, False)


def test_attempt_vars_issue_uses_repo_from_key(monkeypatch):
    """Das Repo kommt aus dem Ketten-Schlüssel (Trigger-repo), nicht aus spec.github."""
    seen = []

    def fake_get(repo, kind, num, accept_closed=False):
        seen.append((repo, kind, num))
        return {"number": 42, "title": "t", "body": "b"}, False

    monkeypatch.setattr(retry_chains, "_gh_get", fake_get)
    vars, api_error = _attempt_vars("issue:org/other#42")
    assert api_error is False
    assert vars == {"issue_number": 42, "issue_title": "t", "issue_body": "b"}
    assert seen == [("org/other", "issue", "42")]


def test_attempt_vars_issue_gone(monkeypatch):
    monkeypatch.setattr(retry_chains, "_gh_get", lambda r, k, n, **kw: (None, False))
    assert _attempt_vars(ISSUE_KEY) == (None, False)


def test_attempt_vars_issue_api_error(monkeypatch):
    monkeypatch.setattr(retry_chains, "_gh_get", lambda r, k, n, **kw: (None, True))
    assert _attempt_vars(ISSUE_KEY) == (None, True)


def test_attempt_vars_pr_found(monkeypatch):
    monkeypatch.setattr(
        retry_chains,
        "_gh_get",
        lambda r, k, n, **kw: ({"number": 7, "title": "t", "html_url": "u", "head": {"ref": "b", "sha": "s"}}, False),
    )
    vars, api_error = _attempt_vars(PR_KEY)
    assert api_error is False
    assert vars["pr_number"] == 7
    assert vars["pr_head_sha"] == "s"


def test_attempt_vars_cron_is_empty():
    assert _attempt_vars(CRON_KEY) == ({}, False)


# ---------------------------------------------------------------- on_run_finished: Claim-Besitz
def _fail_record(run_id="r1", attempt=0, retry_of="", status="fail", kind="transient"):
    return {
        "id": run_id,
        "project": "p",
        "loop": "l",
        "trigger": "on_issue:#42",
        "status": status,
        "failure_kind": kind,
        "attempt": attempt,
        "retry_of": retry_of,
        "finished": FINISHED.isoformat(),
    }


def test_on_run_finished_foreign_run_keeps_attempt_claim(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, key=ISSUE_KEY, claimed_run_id="attempt", last_run_id="r0", next_attempt_at=NOW.isoformat())
    info = on_run_finished(_spec(loop_retry={"max": 3}), "p", "l", _fail_record("manual"))
    assert info == {"in_chain": False}  # klassisches run_failed
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.claimed_run_id, chain.last_run_id) == ("attempt", "r0")


def test_on_run_finished_releases_claim_when_retry_disabled(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, key=ISSUE_KEY, claimed_run_id="r1", last_run_id="r0")
    info = on_run_finished(_spec(), "p", "l", _fail_record(attempt=1, retry_of="r0"))
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.claimed_run_id) == (ChainState.EXHAUSTED, "")
    assert info["state"] == "exhausted"


def test_on_run_finished_rejected_attempt_dismisses(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, key=ISSUE_KEY, claimed_run_id="r1", last_run_id="r0")
    on_run_finished(_spec(loop_retry={"max": 3}), "p", "l", _fail_record(attempt=1, retry_of="r0", status="rejected"))
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.claimed_run_id) == (ChainState.DISMISSED, "")


def test_on_run_finished_dismissed_mid_attempt_plans_nothing(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, key=ISSUE_KEY, claimed_run_id="r1", last_run_id="r0")
    dismiss_chain("p", "l", ISSUE_KEY)
    info = on_run_finished(_spec(loop_retry={"max": 3}), "p", "l", _fail_record(attempt=1, retry_of="r0"))
    assert info["state"] == "dismissed"
    assert [c.state for c in store.all()] == [ChainState.DISMISSED]


def test_on_run_finished_needs_approval_keeps_claim(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, key=ISSUE_KEY, claimed_run_id="r1", last_run_id="r0")
    on_run_finished(_spec(loop_retry={"max": 3}), "p", "l", _fail_record(status="needs_approval", retry_of="r0"))
    assert store.get("p", "l", ISSUE_KEY).claimed_run_id == "r1"


def test_record_run_end_loads_spec(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    monkeypatch.setattr(retry_chains, "load_projects", lambda: {"p": _spec(loop_retry={"max": 3})})
    info = record_run_end("p", _fail_record())
    assert info["state"] == "pending"


def test_record_run_end_unknown_project(monkeypatch):
    monkeypatch.setattr(retry_chains, "load_projects", lambda: {})
    assert record_run_end("p", _fail_record()) == {"in_chain": False}


# ---------------------------------------------------------------- UI-Aktionen
def test_dismiss_chain_from_pending(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, next_attempt_at=NOW.isoformat())
    updated = dismiss_chain("p", "l", CRON_KEY, actor="user:x")
    assert updated is not None
    assert updated.state is ChainState.DISMISSED
    assert updated.next_attempt_at == ""


def test_dismiss_chain_from_exhausted(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, state=ChainState.EXHAUSTED)
    updated = dismiss_chain("p", "l", CRON_KEY)
    assert updated is not None
    assert updated.state is ChainState.DISMISSED


def test_dismiss_chain_missing_returns_none(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    assert dismiss_chain("p", "l", CRON_KEY) is None


def test_dismiss_chain_terminal_returns_none(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, state=ChainState.RESOLVED)
    assert dismiss_chain("p", "l", CRON_KEY) is None


@pytest.fixture
def manual_env(store, monkeypatch, fake_now, runs_dir):
    """retry_now mit abgefangenem Vorrang-Worker: ``spawned`` sammelt die
    Aufrufe, ``run(claim)`` führt den Versuch aus."""
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    monkeypatch.setattr(retry_chains, "load_projects", lambda: {"p": _spec(loop_retry={"max": 3})})
    spawned = []
    monkeypatch.setattr(
        "fabrik.engine.spawn_priority_worker",
        lambda project, loop, variables, *, work, **kw: spawned.append((variables, work)),
    )
    runs = []
    monkeypatch.setattr("fabrik.engine.run_loop", lambda *a, **k: runs.append(k) or {})
    return {"store": store, "spawned": spawned, "runs": runs, "run": lambda: spawned[0][1](_FakeClaim())}


def test_retry_now_from_exhausted_keeps_claim(manual_env):
    store = manual_env["store"]
    _write_chain(store, state=ChainState.EXHAUSTED, last_run_id="r0")
    run_id = retry_now("p", "l", CRON_KEY, actor="x")
    assert run_id is not None
    # Kette ist wieder pending (volles Budget); der neue Run hält den Claim.
    chain = store.get("p", "l", CRON_KEY)
    assert (chain.state, chain.next_attempt_at, chain.claimed_run_id) == (ChainState.PENDING, "", run_id)
    assert len(manual_env["spawned"]) == 1


def test_retry_now_queues_with_priority_claim_on_trigger(manual_env):
    """Kein nicht-blockierender Claim: der Vorrang-Worker wartet auf den Slot
    (ADR 0015) — mit dem Issue-Bezug aus dem Schlüssel für die Dedup."""
    _write_chain(manual_env["store"], key=ISSUE_KEY, state=ChainState.EXHAUSTED, last_run_id="r0")
    retry_now("p", "l", ISSUE_KEY)
    assert manual_env["spawned"][0][0] == {"issue_number": 42}


def test_retry_now_runs_attempt_zero(manual_env):
    _write_chain(manual_env["store"], state=ChainState.EXHAUSTED, last_run_id="r0")
    run_id = retry_now("p", "l", CRON_KEY)
    manual_env["run"]()
    run = manual_env["runs"][0]
    assert run["run_id"] == run_id
    assert (run["attempt"], run["retry_of"], run["trigger"], run["chain_key"]) == (0, "r0", "cron", CRON_KEY)


def test_retry_now_passes_actor_to_run(manual_env):
    """Der Auslöser landet als Name in ``started_by`` (Audit, ADR 0030)."""
    _write_chain(manual_env["store"], state=ChainState.EXHAUSTED, last_run_id="r0")
    retry_now("p", "l", CRON_KEY, actor="alice")
    manual_env["run"]()
    assert manual_env["runs"][0]["actor"] == "alice"


def test_retry_now_cannot_start_shelves_chain_and_records_error(manual_env, monkeypatch):
    """Start scheitert (z. B. Loop gelöscht): Kette wieder exhausted, und die
    gemeldete Run-Id zeigt auf einen error-Record mit dem Grund."""
    from fabrik.failures import SetupError
    from fabrik.runs import get_run

    def boom(*a, **k):
        raise SetupError("Loop unbekannt: l")

    monkeypatch.setattr("fabrik.engine.run_loop", boom)
    store = manual_env["store"]
    _write_chain(store, state=ChainState.EXHAUSTED, last_run_id="r0")
    run_id = not_none(retry_now("p", "l", CRON_KEY, actor="alice"))
    manual_env["run"]()
    chain = store.get("p", "l", CRON_KEY)
    assert (chain.state, chain.claimed_run_id, chain.last_run_id) == (ChainState.EXHAUSTED, "", "r0")
    rec = get_run(run_id, "p")
    assert rec["status"] == "error" and "Loop unbekannt" in rec["error"]
    assert (rec["started_by"], rec["retry_of"], rec["trigger_key"]) == ("alice", "r0", CRON_KEY)


def test_retry_now_trigger_gone_dismisses_and_records_error(manual_env, monkeypatch):
    from fabrik.runs import get_run

    monkeypatch.setattr(retry_chains, "_attempt_vars", lambda key, **kw: (None, False))
    _write_chain(manual_env["store"], key=ISSUE_KEY, state=ChainState.EXHAUSTED, last_run_id="r0")
    run_id = not_none(retry_now("p", "l", ISSUE_KEY))
    manual_env["run"]()
    assert manual_env["store"].get("p", "l", ISSUE_KEY).state is ChainState.DISMISSED
    assert get_run(run_id, "p")["status"] == "error"


def test_retry_now_worker_start_failure(manual_env, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr("fabrik.engine.spawn_priority_worker", fail)
    _write_chain(manual_env["store"], state=ChainState.EXHAUSTED, last_run_id="r0")
    with pytest.raises(RetryBlocked):
        retry_now("p", "l", CRON_KEY)
    assert manual_env["store"].get("p", "l", CRON_KEY).state is ChainState.EXHAUSTED


def test_retry_now_disabled_loop_raises(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    monkeypatch.setattr(pause, "manual_block_reason", lambda p, loop: "deaktiviert")
    _write_chain(store, state=ChainState.EXHAUSTED)
    with pytest.raises(RetryBlocked):
        retry_now("p", "l", CRON_KEY)
    assert store.get("p", "l", CRON_KEY).state is ChainState.EXHAUSTED


def test_retry_now_rejects_pending(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, state=ChainState.PENDING)
    assert retry_now("p", "l", CRON_KEY) is None


def test_retry_now_missing_returns_none(store, monkeypatch):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    assert retry_now("p", "l", CRON_KEY) is None


# ---------------------------------------------------------------- release_stale_claims
def _write_run(runs_dir, rec):
    (runs_dir / "p").mkdir(parents=True, exist_ok=True)
    (runs_dir / "p" / f"{rec['id']}.json").write_text(json.dumps({"project": "p", "loop": "l", **rec}))


def test_release_stale_claims_frees_inactive(store, monkeypatch, runs_dir, fake_now):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, claimed_run_id="r1", last_run_id="r0", next_attempt_at=NOW.isoformat())
    # Run r1 existiert nicht → nicht aktiv → Claim wird freigegeben.
    freed = release_stale_claims()
    assert freed == 1
    chain = store.get("p", "l", CRON_KEY)
    assert (chain.claimed_run_id, chain.state, chain.next_attempt_at) == ("", ChainState.PENDING, NOW.isoformat())


def test_release_stale_claims_forced_claim_goes_exhausted(store, monkeypatch, runs_dir, fake_now):
    """jetzt erneut + Neustart, bevor der Versuch einen Record schreibt: die
    Kette darf nicht pending ohne Fälligkeitszeit hängen bleiben (Cron-Loop
    tot) — zurück nach exhausted."""
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, claimed_run_id="r1", last_run_id="r0", next_attempt_at="")
    freed = release_stale_claims()
    assert freed == 1
    chain = store.get("p", "l", CRON_KEY)
    assert (chain.state, chain.claimed_run_id, chain.next_attempt_at) == (ChainState.EXHAUSTED, "", "")


def test_release_stale_claims_records_finished_run(store, monkeypatch, runs_dir, fake_now):
    """Run beendet, Kette aber nie aktualisiert (z. B. Absturz): Ende nachtragen."""
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    monkeypatch.setattr(retry_chains, "load_projects", lambda: {"p": _spec(loop_retry={"max": 3})})
    _write_chain(store, key=ISSUE_KEY, claimed_run_id="r1", last_run_id="r0")
    _write_run(runs_dir, {"id": "r1", "status": "rejected", "trigger": "on_issue:#42", "retry_of": "r0"})
    assert release_stale_claims() == 1
    assert store.get("p", "l", ISSUE_KEY).state is ChainState.DISMISSED


def test_release_stale_claims_keeps_running(store, monkeypatch, runs_dir, fake_now):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, claimed_run_id="r1", last_run_id="r0")
    _write_run(runs_dir, {"id": "r1", "status": "running"})
    freed = release_stale_claims()
    assert freed == 0
    assert store.get("p", "l", CRON_KEY).claimed_run_id == "r1"


def test_release_stale_claims_keeps_needs_approval(store, monkeypatch, runs_dir, fake_now):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, claimed_run_id="r1", last_run_id="r0")
    _write_run(runs_dir, {"id": "r1", "status": "needs_approval"})
    assert release_stale_claims() == 0


def test_release_stale_claims_ignores_unclaimed(store, monkeypatch, fake_now):
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    _write_chain(store, claimed_run_id="")
    assert release_stale_claims() == 0


def test_reaper_interrupted_run_opens_chain(store, monkeypatch, runs_dir, fake_now):
    """Neustart während Run 0: der Reaper eröffnet die Kette (on: [interrupted])."""
    from fabrik import runs

    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    monkeypatch.setattr(retry_chains, "load_projects", lambda: {"p": _spec(loop_retry={"max": 3})})
    monkeypatch.setattr(specs, "load_projects", lambda: {"p": _spec(loop_retry={"max": 3})})  # Reaper lädt einmal
    _write_run(runs_dir, {"id": "r1", "status": "running", "trigger": "on_issue:#42"})
    assert runs.reap_orphaned_runs() == ["r1"]
    chain = store.get("p", "l", ISSUE_KEY)
    assert chain.state is ChainState.PENDING and chain.last_run_id == "r1"


def test_reaper_interrupted_attempt_consumes_budget(store, monkeypatch, runs_dir, fake_now):
    """Neustart während Versuch n: Versuch verbraucht, nächster nach Backoff, last_run_id rückt vor."""
    from fabrik import runs

    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    monkeypatch.setattr(retry_chains, "load_projects", lambda: {"p": _spec(loop_retry={"max": 3})})
    monkeypatch.setattr(specs, "load_projects", lambda: {"p": _spec(loop_retry={"max": 3})})  # Reaper lädt einmal
    _write_chain(store, key=ISSUE_KEY, claimed_run_id="r2", last_run_id="r1", next_attempt_at=NOW.isoformat())
    _write_run(runs_dir, {"id": "r2", "status": "running", "trigger": "on_issue:#42", "attempt": 1, "retry_of": "r1"})
    runs.reap_orphaned_runs()
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.claimed_run_id, chain.last_run_id) == (ChainState.PENDING, "", "r2")
    assert chain.next_attempt_at > NOW.isoformat()


def test_reaper_loads_specs_once(store, monkeypatch, runs_dir, fake_now):
    """Mehrere verwaiste Runs: die Specs werden einmal geladen, nicht je Run."""
    from fabrik import runs

    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    monkeypatch.setattr(retry_chains, "load_projects", lambda: {"p": _spec(loop_retry={"max": 3})})
    loads = []
    monkeypatch.setattr(specs, "load_projects", lambda: loads.append(1) or {"p": _spec(loop_retry={"max": 3})})
    for i in range(3):
        _write_run(runs_dir, {"id": f"r{i}", "status": "running", "trigger": f"on_issue:#{40 + i}"})
    assert sorted(runs.reap_orphaned_runs()) == ["r0", "r1", "r2"]
    assert loads == [1]


# ---------------------------------------------------------------- start_due_attempts
@pytest.fixture
def due_env(store, monkeypatch, fake_now):
    """Store, Pool und Projekt-Spec für start_due_attempts."""
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    submitted = []
    monkeypatch.setattr("fabrik.engine._get_run_pool", lambda: _FakePool(submitted))
    monkeypatch.setattr(retry_chains, "get_run", lambda rid, proj: None)
    projects = {"p": _spec(loop_retry={"max": 3})}
    monkeypatch.setattr(retry_chains, "load_projects", lambda: projects)
    return {"store": store, "submitted": submitted, "projects": projects}


_PAST = (NOW - timedelta(minutes=1)).isoformat()


def test_start_due_attempts_starts_cron_attempt(due_env):
    store = due_env["store"]
    _write_chain(store, next_attempt_at=_PAST)
    started = start_due_attempts()
    assert len(started) == 1
    assert len(due_env["submitted"]) == 1
    assert store.get("p", "l", CRON_KEY).claimed_run_id == started[0]


def test_start_due_attempts_skips_paused_loop(due_env, monkeypatch):
    store = due_env["store"]
    monkeypatch.setattr(pause, "trigger_blocked", lambda p, loop: True)
    _write_chain(store, next_attempt_at=_PAST)
    assert start_due_attempts() == []
    assert due_env["submitted"] == []
    assert store.get("p", "l", CRON_KEY).claimed_run_id == ""


def test_attempt_dismisses_gone_trigger(due_env, monkeypatch):
    store = due_env["store"]
    monkeypatch.setattr(retry_chains, "_attempt_vars", lambda key, **kw: (None, False))
    _write_chain(store, key=ISSUE_KEY, next_attempt_at=_PAST)
    start_due_attempts()
    due_env["submitted"][0]()
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.claimed_run_id) == (ChainState.DISMISSED, "")


def test_start_due_attempts_makes_no_github_calls(due_env, monkeypatch):
    """Der Scheduler-Tick (2 s) blockiert nicht auf GitHub — der Abruf läuft im Worker."""
    fetches = []
    monkeypatch.setattr(retry_chains, "_attempt_vars", lambda key, **kw: fetches.append(key) or ({}, False))
    _write_chain(due_env["store"], key=ISSUE_KEY, next_attempt_at=_PAST)
    assert len(start_due_attempts()) == 1
    assert fetches == []


def test_start_due_attempts_uses_given_projects(due_env, monkeypatch):
    def no_load():
        raise AssertionError("load_projects darf nicht erneut laufen")

    monkeypatch.setattr(retry_chains, "load_projects", no_load)
    _write_chain(due_env["store"], next_attempt_at=_PAST)
    assert len(start_due_attempts(due_env["projects"])) == 1


def test_start_due_attempts_passes_accept_closed(due_env):
    spec = _spec(trigger_type="on_pr", loop_retry={"max": 3})
    spec["loops"]["l"]["triggers"][0]["states"] = ["closed"]
    due_env["projects"]["p"] = spec
    _write_chain(due_env["store"], key=PR_KEY, next_attempt_at=_PAST)
    start_due_attempts()
    assert due_env["submitted"][0].__self__.accept_closed is True


def test_attempt_keeps_pending_on_api_error(due_env, monkeypatch):
    """Transienter API-Fehler: Kette bleibt pending, aber um ATTEMPT_START_DEFER
    verschoben — kein Claim-Sturm (GitHub-GET) alle 2 s."""
    store = due_env["store"]
    monkeypatch.setattr(retry_chains, "_attempt_vars", lambda key, **kw: (None, True))
    _write_chain(store, key=ISSUE_KEY, next_attempt_at=_PAST)
    start_due_attempts()
    due_env["submitted"][0]()
    chain = store.get("p", "l", ISSUE_KEY)
    assert chain.state is ChainState.PENDING
    assert chain.next_attempt_at == (NOW + retry_chains.ATTEMPT_START_DEFER).isoformat()
    assert start_due_attempts() == []  # nächster Tick: nicht fällig


def test_start_due_attempts_exhausts_when_retry_disabled(due_env):
    store = due_env["store"]
    due_env["projects"]["p"] = _spec()  # retry-Block entfernt
    _write_chain(store, next_attempt_at=_PAST)
    assert start_due_attempts() == []
    chain = store.get("p", "l", CRON_KEY)
    assert (chain.state, chain.next_attempt_at) == (ChainState.EXHAUSTED, "")


def test_start_due_attempts_dismisses_when_loop_gone(due_env):
    store = due_env["store"]
    spec = _spec(loop_retry={"max": 3})
    spec["loops"] = {"other": spec["loops"]["l"]}  # Loop "l" entfernt
    due_env["projects"]["p"] = spec
    _write_chain(store, next_attempt_at=_PAST)
    assert start_due_attempts() == []
    assert store.get("p", "l", CRON_KEY).state is ChainState.DISMISSED


def test_start_due_attempts_keeps_pending_when_project_missing(due_env):
    """Fehlendes Projekt (Spec z. B. gerade defekt): Kette bleibt pending,
    kein Dismiss — der nächste Tick probiert es erneut."""
    store = due_env["store"]
    due_env["projects"].clear()
    _write_chain(store, next_attempt_at=_PAST)
    assert start_due_attempts() == []
    assert store.get("p", "l", CRON_KEY).state is ChainState.PENDING


def test_start_due_attempts_keeps_pending_when_spec_broken(due_env):
    store = due_env["store"]
    due_env["projects"]["p"] = {"name": "p", "_error": "defekt"}
    _write_chain(store, next_attempt_at=_PAST)
    assert start_due_attempts() == []
    assert store.get("p", "l", CRON_KEY).state is ChainState.PENDING


def test_start_due_attempts_derives_attempt_from_last_run(due_env, monkeypatch):
    store = due_env["store"]
    runs = []
    monkeypatch.setattr(retry_chains, "get_run", lambda rid, proj: {"attempt": 2})
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: _FakeClaim())
    monkeypatch.setattr("fabrik.engine.run_loop", lambda *a, **k: runs.append(k) or {})
    _write_chain(store, next_attempt_at=_PAST, last_run_id="r2")
    started = start_due_attempts()
    due_env["submitted"][0]()
    assert runs[0]["run_id"] == started[0]
    assert (runs[0]["attempt"], runs[0]["retry_of"]) == (3, "r2")


def test_start_due_attempts_fetches_github_once(due_env, monkeypatch):
    store = due_env["store"]
    fetches, runs = [], []
    monkeypatch.setattr(
        retry_chains, "_attempt_vars", lambda key, **kw: fetches.append(key) or ({"issue_number": 42}, False)
    )
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: _FakeClaim())
    monkeypatch.setattr("fabrik.engine.run_loop", lambda *a, **k: runs.append(k) or {})
    _write_chain(store, key=ISSUE_KEY, next_attempt_at=_PAST)
    start_due_attempts()
    due_env["submitted"][0]()
    assert fetches == [ISSUE_KEY]
    assert runs[0]["initial_vars"] == {"issue_number": 42}
    assert runs[0]["trigger"] == "on_issue:#42"


def test_start_due_attempts_pr_trigger_has_head_sha(due_env, monkeypatch):
    """PR-Retry auf dem neuen Head: Trigger trägt den SHA (ADR 0023) — sonst
    würde das Reconcile einen zweiten Run auslösen."""
    store = due_env["store"]
    runs = []
    monkeypatch.setattr(
        retry_chains, "_attempt_vars", lambda key, **kw: ({"pr_number": 7, "pr_head_sha": "abc123"}, False)
    )
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: _FakeClaim())
    monkeypatch.setattr("fabrik.engine.run_loop", lambda *a, **k: runs.append(k) or {})
    _write_chain(store, key=PR_KEY, next_attempt_at=_PAST)
    start_due_attempts()
    due_env["submitted"][0]()
    assert runs[0]["trigger"] == "on_pr:#7@abc123"


def test_attempt_without_slot_is_deferred(due_env, monkeypatch):
    """Kein Slot: Claim frei, aber next_attempt_at verschoben — kein Claim-Sturm alle 2 s."""
    store = due_env["store"]
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: None)
    _write_chain(store, next_attempt_at=_PAST)
    start_due_attempts()
    due_env["submitted"][0]()
    chain = store.get("p", "l", CRON_KEY)
    assert chain.claimed_run_id == ""
    assert chain.next_attempt_at == (NOW + retry_chains.ATTEMPT_START_DEFER).isoformat()
    assert start_due_attempts() == []  # nächster Tick: nicht fällig


def test_attempt_paused_after_queueing_is_deferred(due_env, monkeypatch):
    store = due_env["store"]
    claims = []
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: claims.append(1))
    _write_chain(store, next_attempt_at=_PAST)
    start_due_attempts()
    monkeypatch.setattr(pause, "effective_state", lambda p, loop: "paused")
    due_env["submitted"][0]()
    assert claims == []
    chain = store.get("p", "l", CRON_KEY)
    assert chain.claimed_run_id == "" and chain.state is ChainState.PENDING


def test_attempt_run_loop_error_releases_claim(due_env, monkeypatch):
    store = due_env["store"]
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: _FakeClaim())

    def boom(*a, **k):
        raise RuntimeError("setup")

    monkeypatch.setattr("fabrik.engine.run_loop", boom)
    _write_chain(store, next_attempt_at=_PAST)
    start_due_attempts()
    due_env["submitted"][0]()
    assert store.get("p", "l", CRON_KEY).claimed_run_id == ""


def test_attempt_crash_after_record_consumes_budget(due_env, monkeypatch, runs_dir):
    """run_loop scheitert nach dem Anlegen des Records (z. B. _persist):
    Record wird error, der Versuch zählt — sonst liefe ein dauerhafter
    Absturz endlos jede Minute."""
    from fabrik.runs import get_run

    store = due_env["store"]
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: _FakeClaim())
    monkeypatch.setattr(retry_chains, "get_run", get_run)

    def crash(project, loop, *, run_id, **k):
        _write_run(runs_dir, {"id": run_id, "status": "running", "trigger": "cron", "trigger_key": CRON_KEY})
        raise OSError("disk full")

    monkeypatch.setattr("fabrik.engine.run_loop", crash)
    _write_chain(store, next_attempt_at=_PAST, last_run_id="r0")
    run_id = start_due_attempts()[0]
    due_env["submitted"][0]()
    rec = get_run(run_id, "p")
    assert (rec["status"], rec["error"]) == ("error", "disk full")
    chain = store.get("p", "l", CRON_KEY)
    assert (chain.claimed_run_id, chain.last_run_id) == ("", run_id)  # Versuch verbucht


def test_attempt_record_carries_chain_key(due_env, monkeypatch):
    """Der Versuch meldet sich bei der Kette, deren Claim er hält — auch wenn
    sich der Cron-Ausdruck seitdem geändert hat."""
    store = due_env["store"]
    runs = []
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: _FakeClaim())
    monkeypatch.setattr("fabrik.engine.run_loop", lambda *a, **k: runs.append(k) or {})
    _write_chain(store, key="cron:0 * * * *", next_attempt_at=_PAST)
    start_due_attempts()
    due_env["submitted"][0]()
    assert runs[0]["chain_key"] == "cron:0 * * * *"


def test_attempt_stops_when_claim_cleared(due_env, monkeypatch):
    """Verwerfen zwischen Claim und Start: der Versuch darf nicht starten
    (die Kette ist bereits beendet)."""
    store = due_env["store"]
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: _FakeClaim())
    runs = []
    monkeypatch.setattr("fabrik.engine.run_loop", lambda *a, **k: runs.append(k) or {})
    _write_chain(store, next_attempt_at=_PAST)
    start_due_attempts()
    store.set_state("p", "l", CRON_KEY, ChainState.DISMISSED, NOW)
    due_env["submitted"][0]()
    assert runs == []
    assert store.get("p", "l", CRON_KEY).state is ChainState.DISMISSED


def test_start_due_attempts_skips_not_due(due_env):
    _write_chain(due_env["store"], next_attempt_at=(NOW + timedelta(minutes=5)).isoformat())
    assert start_due_attempts() == []
    assert due_env["submitted"] == []


def test_due_attempts_survive_restart(due_env, runs_dir):
    """Neustart (analog `requeue_queued_runs`): Ketten, die vor dem Neustart fällig
    wurden, startet der erste Tick — überfällige sofort, Versuchszahl aus dem letzten
    Run abgeleitet (ADR 0036, Entscheidung 5)."""
    from fabrik import runs

    store = due_env["store"]
    _write_run(
        runs_dir,
        {
            "id": "r1",
            "status": "fail",
            "trigger": "on_issue:#42",
            "attempt": 0,
            "failure_kind": "interrupted",
            "finished": _PAST,
        },
    )
    _write_chain(store, key=ISSUE_KEY, next_attempt_at=_PAST, last_run_id="r1")
    # Neustart: verwaiste Runs reppen (keine), veraltete Claims freigeben (keine),
    # erster Scheduler-Tick startet fällige Versuche.
    assert runs.reap_orphaned_runs() == []
    started = start_due_attempts(due_env["projects"])
    assert len(started) == 1
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.claimed_run_id, chain.last_run_id) == (ChainState.PENDING, started[0], "r1")


# ---------------------------------------------------------------- Bestandsabgleich
def _mk_backfill(runs_dir, store, spec=None):
    """Runs schreiben + `backfill_chains` gegen die feste Zeit `NOW` aufrufen."""
    spec = spec or _spec(loop_retry={"max": 3})

    def run(run_id, num, kind=None, status="fail", started="2026-01-01T00:00:00+00:00", finished=None, **extra):
        rec = {"id": run_id, "trigger": f"on_issue:#{num}", "status": status, "started": started, **extra}
        if kind:
            rec["failure_kind"] = kind
        if finished:
            rec["finished"] = finished
        _write_run(runs_dir, rec)

    def backfill():
        return backfill_chains(projects={"p": spec}, store=store, now=NOW)

    return run, backfill


@pytest.mark.parametrize("kind", ["interrupted", "transient"])
def test_backfill_retryable_last_run_is_pending(runs_dir, store, kind):
    run, backfill = _mk_backfill(runs_dir, store)
    run("r1", 42, kind=kind, finished="2026-01-01T00:10:00+00:00")
    assert backfill() == BackfillSummary(pending=1)
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.last_run_id) == (ChainState.PENDING, "r1")
    # Run ist lange zu Ende → 5m-Backoff abgelaufen → ab sofort fällig.
    assert chain.next_attempt_at == NOW.isoformat()


def test_backfill_respects_backoff_for_recent_run(runs_dir, store):
    run, backfill = _mk_backfill(runs_dir, store)
    run(
        "r1",
        42,
        kind="transient",
        started=(NOW - timedelta(minutes=6)).isoformat(),
        finished=(NOW - timedelta(minutes=1)).isoformat(),
    )
    backfill()
    chain = store.get("p", "l", ISSUE_KEY)
    assert chain.next_attempt_at == (NOW + timedelta(minutes=4)).isoformat()


@pytest.mark.parametrize(
    ("on", "kind", "state"),
    [
        (["interrupted"], "interrupted", ChainState.PENDING),
        (["interrupted"], "transient", ChainState.EXHAUSTED),
        (["transient"], "transient", ChainState.PENDING),
        (["transient"], "interrupted", ChainState.EXHAUSTED),
    ],
)
def test_backfill_checks_kind_against_policy_on(runs_dir, store, on, kind, state):
    """Klasse des letzten Runs wird gegen `on` der Policy des Loops geprüft,
    nicht gegen die globalen Retry-Klassen — ein `on`-Subset wird im Abgleich
    nicht aufgeweitet."""
    run, backfill = _mk_backfill(runs_dir, store, spec=_spec(loop_retry={"max": 3, "on": on}))
    run("r1", 42, kind=kind, finished="2026-01-01T00:10:00+00:00")
    summary = backfill()
    assert (summary.pending, summary.exhausted, summary.skipped) == (
        (1, 0, 0) if state is ChainState.PENDING else (0, 1, 0)
    )
    assert store.get("p", "l", ISSUE_KEY).state is state


def test_backfill_caps_retry_after_at_max_wait(runs_dir, store):
    """Ein gemeldetes `retry_after` des letzten Runs bestimmt die Fälligkeit,
    auf `max_wait` gekappt (gleiche Semantik wie im laufenden Retry)."""
    run, backfill = _mk_backfill(runs_dir, store, spec=_spec(loop_retry={"max": 3, "max_wait": "2h"}))
    finished = NOW - timedelta(hours=1)
    run("r1", 42, kind="transient", finished=finished.isoformat(), retry_after=(NOW + timedelta(hours=5)).isoformat())
    backfill()
    chain = store.get("p", "l", ISSUE_KEY)
    # retry_after = finished + 5h, max_wait = 2h → fällig um finished + 2h.
    assert chain.next_attempt_at == (NOW + timedelta(hours=1)).isoformat()


@pytest.mark.parametrize("kind", ["environment", "outcome", "unknown"])
def test_backfill_non_retryable_last_run_is_exhausted(runs_dir, store, kind):
    run, backfill = _mk_backfill(runs_dir, store)
    run("r1", 42, kind=kind)
    assert backfill() == BackfillSummary(exhausted=1)
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.next_attempt_at, chain.last_run_id) == (ChainState.EXHAUSTED, "", "r1")


def test_backfill_run_without_kind_is_exhausted(runs_dir, store):
    """Altbestand ohne `failure_kind` → nie automatisch wiederholt, abgelegt."""
    run, backfill = _mk_backfill(runs_dir, store)
    run("r1", 42)
    backfill()
    assert store.get("p", "l", ISSUE_KEY).state is ChainState.EXHAUSTED


def test_backfill_uses_kind_of_last_run(runs_dir, store):
    """Klasse des letzten (neuesten) Runs bestimmt den Kette-Zustand."""
    run, backfill = _mk_backfill(runs_dir, store)
    run("r1", 42, kind="interrupted", started="2026-01-01T00:00:00+00:00")
    run("r2", 42, kind="environment", started="2026-01-01T01:00:00+00:00")
    assert backfill() == BackfillSummary(exhausted=1)
    assert store.get("p", "l", ISSUE_KEY).last_run_id == "r2"


@pytest.mark.parametrize("status", ["pass", "rejected", "queued"])
def test_backfill_skips_group_with_non_failed_run(runs_dir, store, status):
    run, backfill = _mk_backfill(runs_dir, store)
    run("r1", 42, kind="transient", started="2026-01-01T00:00:00+00:00")
    run("r2", 42, status=status, started="2026-01-01T01:00:00+00:00")
    assert backfill() == BackfillSummary(skipped=1)
    assert store.get("p", "l", ISSUE_KEY) is None


def test_backfill_skips_existing_chain(runs_dir, store):
    run, backfill = _mk_backfill(runs_dir, store)
    _write_chain(store, key=ISSUE_KEY, state=ChainState.EXHAUSTED, last_run_id="x")
    run("r1", 42, kind="transient")
    assert backfill() == BackfillSummary(skipped=1)


def test_backfill_is_idempotent(runs_dir, store):
    run, backfill = _mk_backfill(runs_dir, store)
    run("r1", 42, kind="transient")
    run("r2", 7, kind="outcome")
    assert backfill() == BackfillSummary(pending=1, exhausted=1)
    assert backfill() == BackfillSummary(skipped=2)


def test_backfill_skips_loop_without_retry(runs_dir, store):
    run, backfill = _mk_backfill(runs_dir, store, spec=_spec())
    run("r1", 42, kind="transient")
    assert backfill() == BackfillSummary()
    assert store.all() == []


def test_backfill_skips_trigger_without_key(runs_dir, store):
    """Trigger ohne kanonischen Schlüssel (manual) → keine Kette, nicht einmal übersprungen."""
    run, backfill = _mk_backfill(runs_dir, store)
    _write_run(
        runs_dir,
        {
            "id": "r1",
            "trigger": "manual",
            "status": "fail",
            "failure_kind": "transient",
            "started": "2026-01-01T00:00:00+00:00",
        },
    )
    assert backfill() == BackfillSummary()
    assert store.all() == []


def test_backfill_skips_error_spec(runs_dir, store):
    _write_run(
        runs_dir,
        {
            "id": "r1",
            "loop": "l",
            "trigger": "on_issue:#42",
            "status": "fail",
            "failure_kind": "transient",
            "started": "2026-01-01T00:00:00+00:00",
        },
    )
    assert backfill_chains(projects={"p": {"_error": "kaputt"}}, store=store, now=NOW) == BackfillSummary()
    assert store.all() == []


def test_backfill_pending_chain_starts_with_scheduler(due_env, runs_dir):
    """End-to-End: Abgleich legt fällige Kette an, der erste Tick startet den Versuch."""
    store = due_env["store"]
    _write_run(
        runs_dir,
        {
            "id": "r1",
            "status": "fail",
            "trigger": "on_issue:#42",
            "failure_kind": "interrupted",
            "started": "2026-01-01T00:00:00+00:00",
            "finished": "2026-01-01T00:10:00+00:00",
        },
    )
    assert backfill_chains(projects=due_env["projects"], store=store, now=NOW) == BackfillSummary(pending=1)
    assert len(start_due_attempts(due_env["projects"])) == 1
    assert store.get("p", "l", ISSUE_KEY).is_claimed


def test_backfill_multiple_old_runs_get_full_budget(due_env, runs_dir, monkeypatch):
    """Mehrere gescheiterte Altruns (``attempt`` 0 bzw. fehlend) zählen nicht gegen
    das Budget: eine Kette mit dem letzten Run als ``last_run_id``, und der erste
    Versuch des ersten Ticks ist Versuch 1 — volles Budget von ``max``."""
    from fabrik.runs import get_run

    store = due_env["store"]
    started = []
    monkeypatch.setattr(retry_chains, "get_run", get_run)  # Altruns wirklich lesen
    monkeypatch.setattr(retry_chains, "_attempt_vars", lambda key, **kw: ({"issue_number": 42}, False))
    monkeypatch.setattr("fabrik.runs.claim_run", lambda *a, **k: _FakeClaim())
    monkeypatch.setattr("fabrik.engine.run_loop", lambda *a, **k: started.append(k) or {})
    _write_run(
        runs_dir,
        {
            "id": "r1",
            "status": "fail",
            "trigger": "on_issue:#42",
            "failure_kind": "interrupted",
            "attempt": 0,
            "started": "2026-01-01T00:00:00+00:00",
            "finished": "2026-01-01T00:10:00+00:00",
        },
    )
    _write_run(
        runs_dir,
        {
            "id": "r2",
            "status": "fail",
            "trigger": "on_issue:#42",
            "failure_kind": "transient",
            "started": "2026-01-01T01:00:00+00:00",
            "finished": "2026-01-01T01:10:00+00:00",
        },
    )
    assert backfill_chains(projects=due_env["projects"], store=store, now=NOW) == BackfillSummary(pending=1)
    chain = store.get("p", "l", ISSUE_KEY)
    assert chain.last_run_id == "r2"
    assert len(start_due_attempts(due_env["projects"])) == 1
    due_env["submitted"][0]()
    assert (started[0]["attempt"], started[0]["retry_of"]) == (1, "r2")


def test_backfill_pending_chain_for_gone_trigger_dismisses_on_first_tick(due_env, runs_dir, monkeypatch):
    """Der Trigger existiert nicht mehr (Issue zu, PR gemergt): der Abgleich legt
    trotzdem eine `pending`-Kette an (er fragt kein GitHub ab), aber der Versuch
    des ersten Ticks legt sie als `dismissed` ab."""
    store = due_env["store"]
    monkeypatch.setattr(retry_chains, "_attempt_vars", lambda key, **kw: (None, False))  # Issue geschlossen
    _write_run(
        runs_dir,
        {
            "id": "r1",
            "status": "fail",
            "trigger": "on_issue:#42",
            "failure_kind": "interrupted",
            "started": "2026-01-01T00:00:00+00:00",
            "finished": "2026-01-01T00:10:00+00:00",
        },
    )
    assert backfill_chains(projects=due_env["projects"], store=store, now=NOW) == BackfillSummary(pending=1)
    assert len(start_due_attempts(due_env["projects"])) == 1
    due_env["submitted"][0]()  # Worker fragt GitHub ab
    chain = store.get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.claimed_run_id) == (ChainState.DISMISSED, "")


def test_backfill_defaults_load_from_environment(monkeypatch, runs_dir):
    """Ohne Argumente: Specs, Store (``state/retry-chains.json``) und Zeit aus der Umgebung."""
    _write_run(
        runs_dir,
        {
            "id": "r1",
            "status": "fail",
            "trigger": "on_issue:#42",
            "failure_kind": "transient",
            "started": "2026-01-01T00:00:00+00:00",
            "finished": "2026-01-01T00:10:00+00:00",
        },
    )
    monkeypatch.setattr(retry_chains, "load_projects", lambda: {"p": _spec(loop_retry={"max": 3})})
    monkeypatch.setattr(config, "utcnow", lambda: NOW)
    assert backfill_chains() == BackfillSummary(pending=1)
    chain = ChainStore().get("p", "l", ISSUE_KEY)
    assert (chain.state, chain.last_run_id) == (ChainState.PENDING, "r1")


def test_backfill_ignores_runs_of_loops_without_retry(runs_dir, store):
    """Runs eines Loops ohne Retry-Policy werden ignoriert (nicht einmal übersprungen)."""
    spec = _spec(loop_retry={"max": 3})
    spec["loops"]["other"] = {"triggers": [{"type": "on_issue", "repo": "owner/repo"}]}
    _write_run(
        runs_dir,
        {
            "id": "r1",
            "loop": "other",
            "trigger": "on_issue:#42",
            "status": "fail",
            "failure_kind": "transient",
            "started": "2026-01-01T00:00:00+00:00",
        },
    )
    _write_run(
        runs_dir,
        {
            "id": "r2",
            "trigger": "on_issue:#43",
            "status": "fail",
            "failure_kind": "transient",
            "started": "2026-01-01T00:00:00+00:00",
        },
    )
    assert backfill_chains(projects={"p": spec}, store=store, now=NOW) == BackfillSummary(pending=1)
    assert store.get("p", "l", "issue:owner/repo#42") is None  # falscher Loop
    assert store.get("p", "l", "issue:owner/repo#43") is not None


# ---------------------------------------------------------------- Hilfsklassen
class _FakePool:
    def __init__(self, submitted):
        self._submitted = submitted

    def submit(self, fn):
        self._submitted.append(fn)
        return None


class _FakeClaim:
    scope = "scope"

    def release(self):
        pass


# ---------------------------------------------------------------- Engine: Ketten-Schlüssel im Record
def test_init_record_prefers_chain_key(runs_dir, projects_dir):
    """Ein Versuch trägt den Schlüssel seiner Kette, nicht den aus der geänderten Spec."""
    from fabrik import engine

    spec = {
        "_path": str(projects_dir / "none.yaml"),
        "loops": {"l": {"triggers": [{"type": "cron", "cron": "30 * * * *"}]}},
    }
    rec, _, _ = engine._init_record(spec, "p", {}, "l", "cron", "", None, "r1", runs_dir, chain_key="cron:0 * * * *")
    assert rec["trigger_key"] == "cron:0 * * * *"
    rec, _, _ = engine._init_record(spec, "p", {}, "l", "cron", "", None, "r2", runs_dir)
    assert rec["trigger_key"] == "cron:30 * * * *"
