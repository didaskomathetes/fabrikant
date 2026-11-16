"""Retry-Policy (`retry: {max, backoff, on, max_wait}`): Parsing, Semantik von max/backoff/on/max_wait."""

from datetime import UTC, datetime, timedelta

import pytest

from fabrik.retry import DEFAULT_BACKOFF, DEFAULT_MAX_WAIT, RetryPolicy, format_duration, parse_duration
from fabrik.runmodel import FailureKind


@pytest.mark.parametrize(("raw", "seconds"), [("45s", 45), ("5m", 300), ("2h", 7200), ("1d", 86400), (" 3m ", 180)])
def test_parse_duration(raw, seconds):
    assert parse_duration(raw) == timedelta(seconds=seconds)


@pytest.mark.parametrize("raw", ["0m", "5", "5x", "-1m", "", None, 5])
def test_parse_duration_rejects(raw):
    with pytest.raises(ValueError):
        parse_duration(raw)


@pytest.mark.parametrize(("seconds", "text"), [(86400, "1d"), (7200, "2h"), (300, "5m"), (45, "45s")])
def test_format_duration(seconds, text):
    assert format_duration(timedelta(seconds=seconds)) == text


def parse(raw):
    errs: list[str] = []
    return RetryPolicy.parse(raw, "retry", errs), errs


def test_defaults_and_full_block():
    assert parse(None) == (None, [])
    policy, errs = parse({})
    assert errs == [] and policy == RetryPolicy()
    policy, errs = parse({"max": 2, "backoff": ["1m", "1h"], "on": ["interrupted"]})
    assert errs == [] and policy.max == 2 and policy.on == frozenset({"interrupted"})


def test_max_zero_means_no_retry():
    assert parse({"max": 0}) == (None, [])


def test_delay_uses_last_backoff_for_later_attempts():
    policy = RetryPolicy(max=5)
    assert [policy.delay(i) for i in (1, 2, 3, 4, 5)] == [*DEFAULT_BACKOFF, DEFAULT_BACKOFF[-1], DEFAULT_BACKOFF[-1]]


def test_allows_only_configured_kinds_and_never_unknown():
    policy = RetryPolicy()
    assert policy.allows(FailureKind.TRANSIENT) and policy.allows(FailureKind.INTERRUPTED)
    assert not policy.allows(FailureKind.UNKNOWN) and not policy.allows(None)
    assert not policy.allows(FailureKind.ENVIRONMENT)


def test_max_wait_parsed_and_defaulted():
    assert parse({"max_wait": "6h"})[0].max_wait == timedelta(hours=6)
    assert parse({})[0].max_wait == DEFAULT_MAX_WAIT


END = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        (None, END + timedelta(minutes=5)),  # nur Backoff
        (END + timedelta(minutes=1), END + timedelta(minutes=5)),  # Backoff ist später
        (END + timedelta(hours=1), END + timedelta(hours=1)),  # Limit-Reset ist später
        (END + timedelta(days=3), END + timedelta(hours=2)),  # auf max_wait gekappt
    ],
)
def test_next_attempt_at_honors_retry_after_capped_by_max_wait(retry_after, expected):
    policy = RetryPolicy(max_wait=timedelta(hours=2))
    assert policy.next_attempt_at(1, END, retry_after) == expected


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ([1], "Mapping"),
        ({"max": 3, "tries": 1}, "Mapping"),
        ({"max": -1}, "retry.max"),
        ({"max": True}, "retry.max"),
        ({"backoff": []}, "retry.backoff"),
        ({"backoff": "5m"}, "retry.backoff"),
        ({"backoff": ["5m", "soon"]}, "retry.backoff"),
        ({"on": ["unknown"]}, "retry.on"),
        ({"on": ["environment"]}, "retry.on"),
        ({"on": []}, "retry.on"),
        ({"max_wait": "soon"}, "retry.max_wait"),
    ],
)
def test_invalid_policies_report_errors(raw, fragment):
    _, errs = parse(raw)
    assert any(fragment in e for e in errs)


def test_yaml_on_key_parsed_as_true_is_accepted():
    import yaml

    raw = yaml.safe_load("max: 1\non: [interrupted]\n")
    assert True in raw  # YAML 1.1: `on` → True
    policy, errs = parse(raw)
    assert errs == [] and policy.on == frozenset({"interrupted"})
