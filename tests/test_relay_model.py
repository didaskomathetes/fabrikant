"""Relay-Domänenmodell: Brief, Settings, Handoff, Autorisierung, evaluate, Status-Kommentar."""

from datetime import UTC, datetime, timedelta

import pytest
from helpers import not_none

from fabrikant.relay import status
from fabrikant.relay.model import (
    DEFAULT_MAX_SESSIONS,
    Action,
    Brief,
    Handoff,
    HandoffError,
    Labels,
    RunOutcome,
    Settings,
    Status,
    StatusState,
    Task,
    evaluate,
    failure_streak,
    normalize_login,
    parse_time,
)
from fabrikant.retry import RetryPolicy
from fabrikant.runmodel import FailureKind

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
BODY = """Intro text.

## Goal
Import historical records into the target dataset.

## Session
Use profile `staging-writer`. Batches of 200.

## Verify
- dataset item count == filtered source pairs
```verify
echo 412/2300
```

## Audit access
Read-only profile `staging-readonly`.

## Settings
max_sessions: 12
session_minutes: 30
scope: `src/import/**`, docs/x.md
"""
LABELS = Labels()
ALLOWED = frozenset({"alice"})


def make_task(body: str = BODY, labels=(LABELS.task,), **kw) -> Task:
    base = dict(
        number=7,
        title="QA import",
        body=body,
        labels=frozenset(labels),
        author="alice",
        created_at=NOW - timedelta(days=2),
        label_actor="alice",
        label_at=NOW - timedelta(days=1),
    )
    return Task(**{**base, **kw})


def decide(task, handoff=None, state=None, **kw):
    return evaluate(task, handoff, state or StatusState(), allowed=ALLOWED, labels=LABELS, now=NOW, **kw)


# ── Brief / Settings ─────────────────────────────────────────────────────────


def test_brief_sections_verify_command_and_settings():
    brief = Brief("t", BODY)
    assert brief.missing_sections() == []
    assert brief.verify_command == "echo 412/2300"
    assert brief.settings == Settings(max_sessions=12, session_minutes=30, scope=("src/import/**", "docs/x.md"))
    assert "staging-readonly" in brief.sections["audit access"]


def test_brief_missing_sections_and_defaults():
    brief = Brief("t", "## Goal\nx\n")
    assert brief.missing_sections() == ["session", "verify"]
    assert brief.verify_command is None
    assert brief.settings == Settings()


def test_verify_hash_ignores_trailing_whitespace_and_crlf_but_not_content():
    a = Brief("t", BODY).verify_sha256
    assert Brief("t", BODY.replace("\n", "  \r\n")).verify_sha256 == a
    assert Brief("t", BODY.replace("== filtered", "<= filtered")).verify_sha256 != a
    # Settings gehören nicht zum Pin: max_sessions erhöhen entblockt ohne Re-Pin.
    assert Brief("t", BODY.replace("max_sessions: 12", "max_sessions: 40")).verify_sha256 == a


@pytest.mark.parametrize("raw", ["max_sessions: zero", "max_sessions: -3", "max_sessions: 0"])
def test_settings_invalid_numbers_fall_back_to_default(raw):
    assert Settings.parse(raw).max_sessions == DEFAULT_MAX_SESSIONS


# ── Handoff ──────────────────────────────────────────────────────────────────


def test_handoff_roundtrip():
    h = Handoff.new(7, "QA import", "abc")
    h.session, h.progress, h.not_before, h.last_session = 3, "412/2300", NOW, NOW
    again = Handoff.parse(h.render())
    assert again == h
    assert again.not_before == NOW and again.status is Status.ACTIVE


def test_handoff_parse_accepts_yaml_timestamps_and_z_suffix():
    text = (
        "---\nissue: 7\nstatus: blocked\nnot_before: 2026-10-22T08:00:00Z\nlast_session: 2026-10-21 10:00:00\n---\nbody"
    )
    h = Handoff.parse(text)
    assert h.status is Status.BLOCKED
    assert h.not_before == datetime(2026, 10, 22, 8, tzinfo=UTC)
    assert h.last_session == datetime(2026, 10, 21, 10, tzinfo=UTC)
    assert h.body == "body"


@pytest.mark.parametrize(
    "text",
    [
        "no front matter",
        "---\n[1, 2]\n---\n",
        "---\nstatus: active\n---\n",  # issue fehlt
        "---\nissue: 7\nstatus: weird\n---\n",
        "---\nissue: 7\nnot_before: tomorrow\n---\n",
        "---\nissue: 7\n  bad: [yaml\n---\n",
    ],
)
def test_handoff_parse_rejects_broken_files(text):
    with pytest.raises(HandoffError):
        Handoff.parse(text)


def test_adopt_harness_fields_resets_only_harness_owned_fields():
    before = Handoff.new(7, "t", "pin")
    before.session = 2
    after = Handoff.parse(before.render())
    after.session, after.verify_sha256, after.progress, after.status = 99, "forged", "10/20", Status.DONE
    assert after.adopt_harness_fields(before) == ["session", "verify_sha256"]
    assert (after.session, after.verify_sha256, after.progress, after.status) == (2, "pin", "10/20", Status.DONE)


def test_append_log_inserts_into_log_section_before_later_sections():
    h = Handoff(issue=7, body="## State\nx\n\n## Log\n- s1\n\n## Notes\nn\n")
    h.append_log("s2")
    assert h.body == "## State\nx\n\n## Log\n- s1\n- s2\n\n## Notes\nn\n"
    h2 = Handoff(issue=7, body="## State\nx\n")
    h2.append_log("s1")
    assert h2.body.endswith("## Log\n- s1\n")
    assert h2.section("log") == "- s1"


def test_normalize_login_and_parse_time():
    assert normalize_login("app/My-Builder-App") == normalize_login("my-builder-app[bot]")
    assert normalize_login(None) == ""
    assert parse_time("") is None
    assert parse_time(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)


# ── Autorisierung ────────────────────────────────────────────────────────────


def test_authorized_when_label_and_body_from_allowed_login():
    assert make_task().authorization_error(ALLOWED, LABELS.task) is None


def test_removed_label_revokes_even_with_allowed_label_history():
    # Label von einem allowed-Login gesetzt, danach entfernt: ein von Hand gestarteter Run darf nicht laufen.
    err = make_task(labels=()).authorization_error(ALLOWED, LABELS.task)
    assert err and "fehlt" in err


def test_label_from_other_login_is_rejected():
    err = not_none(make_task(label_actor="mallory").authorization_error(ALLOWED, LABELS.task))
    assert "mallory" in err


def test_body_edit_by_other_login_needs_relabel():
    edited = make_task(body_editor="mallory", body_edited_at=NOW - timedelta(hours=1))
    assert "mallory" in not_none(edited.authorization_error(ALLOWED, LABELS.task))
    relabeled = make_task(
        body_editor="mallory", body_edited_at=NOW - timedelta(hours=2), label_at=NOW - timedelta(hours=1)
    )
    assert relabeled.authorization_error(ALLOWED, LABELS.task) is None


def test_foreign_author_without_edit_is_approved_by_later_label():
    assert make_task(author="colleague").authorization_error(ALLOWED, LABELS.task) is None


# ── evaluate ─────────────────────────────────────────────────────────────────


def handoff(**kw) -> Handoff:
    h = Handoff.new(7, "t", Brief("t", BODY).verify_sha256)
    for k, v in kw.items():
        setattr(h, k, v)
    return h


def test_unauthorized_task_is_skipped_with_notification():
    d = decide(make_task(label_actor="mallory"))
    assert (d.action, d.notify) == (Action.SKIP, True)


def test_incomplete_brief_blocks():
    d = decide(make_task(body="## Goal\nx"))
    assert d.action is Action.BLOCK and "Session" in d.reason and "Verify" in d.reason


def test_blocked_label_skips_and_done_label_triggers_audit_once():
    assert decide(make_task(labels=(LABELS.task, LABELS.blocked))).action is Action.SKIP
    assert decide(make_task(labels=(LABELS.task, LABELS.done))).action is Action.AUDIT
    assert decide(make_task(labels=(LABELS.task, LABELS.done, LABELS.audited))).action is Action.SKIP
    assert decide(make_task(labels=(LABELS.task, LABELS.done)), audit=False).action is Action.SKIP


def test_first_session_and_due_session():
    assert decide(make_task()).action is Action.SESSION
    assert decide(make_task(), handoff(session=3)).action is Action.SESSION


def test_not_before_in_future_skips_active_task_only():
    later = NOW + timedelta(hours=1)
    assert decide(make_task(), handoff(not_before=later)).action is Action.SKIP
    # Human removed the blocked label: continue even if the agent had set not_before.
    resumed = decide(make_task(), handoff(not_before=later, status=Status.BLOCKED))
    assert resumed.action is Action.SESSION


def test_max_sessions_blocks():
    d = decide(make_task(), handoff(session=12))
    assert d.action is Action.BLOCK and "max_sessions (12)" in d.reason


def test_changed_verify_blocks_once_then_repins_after_unblock():
    h = handoff(verify_sha256="old")
    first = decide(make_task(), h)
    assert first.action is Action.BLOCK and first.verify_pending == Brief("t", BODY).verify_sha256
    second = decide(make_task(), h, StatusState(verify_pending=first.verify_pending))
    assert (second.action, second.repin) == (Action.SESSION, True)


# ── Status-Kommentar ─────────────────────────────────────────────────────────


def test_status_comment_roundtrips_state_and_shows_block_reason():
    h = handoff(session=3, progress="412/2300", last_session=NOW)
    h.block("checkpoint: approve first batch")
    state = StatusState(verify_pending="x", audit="pass", last_commit="abc1234", note="n", failures_seen="r1")
    body = status.render("o/r", "fabrikant/relay/7", ".fabrikant/relay/7/HANDOFF.md", h, state, 12)
    assert body.startswith(status.STATUS_MARKER)
    assert "3 / 12" in body and "approve first batch" in body and "abc1234" in body
    assert "https://github.com/o/r/blob/fabrikant/relay/7/.fabrikant/relay/7/HANDOFF.md" in body
    assert status.parse_state(body) == state


def test_status_comment_without_handoff_and_broken_state():
    body = status.render("o/r", "b", "p", None, StatusState(), 30)
    assert "No session yet" in body
    assert status.parse_state("no state") == StatusState()
    assert status.parse_state("<!-- fabrikant-relay-state {broken} -->") == StatusState()
    assert StatusState.from_dict("nope") == StatusState()


# ── Fehlerbremse (Retry-Policy ADR 0036, Fehlerklassen ADR 0035) ────────────

POLICY = RetryPolicy(max=2, backoff=(timedelta(minutes=5), timedelta(minutes=30)))


def outcome(
    n: int, published=False, kind="transient", finished: datetime | None = NOW - timedelta(hours=1), retry_after=None
):
    return RunOutcome(
        f"20261021T{n:02d}0000Z-relay-session", published, FailureKind(kind) if kind else None, finished, retry_after
    )


def test_failure_streak_stops_at_published_and_acknowledged():
    recent = [outcome(1, published=True), outcome(3), outcome(2)]
    assert [o.run_id for o in failure_streak(recent)] == [outcome(3).run_id, outcome(2).run_id]
    assert failure_streak(recent, acknowledged=outcome(3).run_id) == []
    assert failure_streak([]) == []


@pytest.mark.parametrize("kind", ["", "unknown", "environment", "outcome"])
def test_unknown_or_non_retryable_failure_blocks_immediately(kind):
    d = decide(make_task(), handoff(), recent=[outcome(1, kind=kind)], retry=POLICY)
    assert d.action is Action.BLOCK and f"`{kind or 'unknown'}`" in d.reason
    assert d.failures_seen == outcome(1).run_id


def test_without_policy_every_failure_blocks():
    assert decide(make_task(), handoff(), recent=[outcome(1)]).action is Action.BLOCK


def test_retry_waits_for_backoff_then_runs():
    failed_at = NOW - timedelta(minutes=3)
    waiting = decide(make_task(), handoff(), recent=[outcome(1, finished=failed_at)], retry=POLICY)
    assert waiting.action is Action.SKIP and "Retry 1/2" in waiting.reason and "12:02:00Z" in waiting.reason
    later = NOW - timedelta(minutes=6)
    assert decide(make_task(), handoff(), recent=[outcome(1, finished=later)], retry=POLICY).action is Action.SESSION


def test_max_counts_additional_attempts():
    old = NOW - timedelta(hours=1)
    two = [outcome(2, finished=old), outcome(1, finished=old)]  # erster Run + 1 Retry gescheitert
    assert decide(make_task(), handoff(), recent=two, retry=POLICY).action is Action.SESSION  # Retry 2/2
    three = [outcome(3, finished=old), *two]
    d = decide(make_task(), handoff(), recent=three, retry=POLICY)
    assert d.action is Action.BLOCK and "erschöpft (2 Retries" in d.reason
    # Nach dem Entblocken (failures_seen quittiert) wird neu gezählt.
    again = decide(make_task(), handoff(), StatusState(failures_seen=d.failures_seen), recent=three, retry=POLICY)
    assert again.action is Action.SESSION


def test_policy_on_restricts_retried_kinds():
    only_interrupted = RetryPolicy(max=2, on=frozenset({FailureKind.INTERRUPTED}))
    assert decide(make_task(), handoff(), recent=[outcome(1)], retry=only_interrupted).action is Action.BLOCK


def test_retry_waits_for_reported_retry_after():
    failed_at = NOW - timedelta(minutes=10)  # Backoff (5m) wäre vorbei, das Rate-Limit noch nicht
    reset = NOW + timedelta(minutes=20)
    d = decide(make_task(), handoff(), recent=[outcome(1, finished=failed_at, retry_after=reset)], retry=POLICY)
    assert d.action is Action.SKIP and "12:20:00Z" in d.reason


def test_failure_without_end_time_blocks_instead_of_waiting_forever():
    d = decide(make_task(), handoff(), recent=[outcome(1, finished=None)], retry=POLICY)
    assert d.action is Action.BLOCK


def test_failure_guard_also_holds_back_audits():
    d = decide(make_task(labels=(LABELS.task, LABELS.done)), handoff(), recent=[outcome(1, kind="environment")])
    assert d.action is Action.BLOCK


# ── Abschnitte / Status-Kommentar mit Ergebnis ─────────────────────────────


def test_set_section_replaces_inserts_before_log_and_removes():
    h = Handoff(issue=7, body="## State\nx\n\n## Log\n- s1\n")
    h.set_section("Result", "done well")
    assert h.body == "## State\nx\n\n## Result\ndone well\n\n## Log\n- s1\n"
    h.set_section("Result", "better")
    assert h.section("result") == "better" and h.body.count("## Result") == 1
    h.set_section("State", "y")
    assert h.body.startswith("## State\ny\n\n## Result")
    h.set_section("Result", "")
    assert "Result" not in h.body and h.section("log") == "- s1"
    h.set_section("Missing", "")  # entfernen, was fehlt: No-Op
    no_log = Handoff(issue=7, body="## State\nx\n")
    no_log.set_section("Result", "r")
    assert no_log.body.endswith("## Result\nr\n")
    tail = Handoff(issue=7, body="## Log\n- a\n\n## Result\nold\n")
    tail.set_section("Result", "new")
    assert tail.body == "## Log\n- a\n\n## Result\nnew\n"


def test_status_comment_shows_result_and_audit_sections():
    h = handoff(session=2, status=Status.DONE)
    h.set_section("Result", "44 findings, report at `.fabrikant/relay/7/report.md`.")
    h.set_section("Audit", "**pass** · details: `AUDIT.md`\n\nAll invariants hold.")
    body = status.render("o/r", "fabrikant/relay/7", ".fabrikant/relay/7/HANDOFF.md", h, StatusState(audit="pass"), 8)
    assert "#### Result\n44 findings" in body and "#### Audit\n**pass**" in body
    assert "pass · [AUDIT.md](https://github.com/o/r/blob/fabrikant/relay/7/.fabrikant/relay/7/AUDIT.md)" in body
    long = handoff(status=Status.DONE)
    long.set_section("Result", "x" * (status.SECTION_MAX_CHARS + 50))
    rendered = status.render("o/r", "b", "p/HANDOFF.md", long, StatusState(), 8)
    assert "x" * status.SECTION_MAX_CHARS + " …" in rendered
