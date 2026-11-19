"""Token-Zähler mit Cache-Aufschlüsselung: Formen (Poll/Usage), Summen, Log-Zeile, Agent-Usage."""

import pytest

from fabrik.backends.opencode import opencode_tokens
from fabrik.steps.agent import _agent_usage
from fabrik.tokens import TokenCounts, as_count, compact


@pytest.mark.parametrize(("value", "expected"), [(7, 7), (0, 0), (-3, 0), (True, 0), ("5", 0), (2.5, 0), (None, 0)])
def test_as_count_accepts_only_non_negative_ints(value, expected):
    assert as_count(value) == expected


@pytest.mark.parametrize(
    ("n", "text"), [(0, "0"), (999, "999"), (1_000, "1.0k"), (12_345, "12.3k"), (1_299_893, "1.30M")]
)
def test_compact(n, text):
    assert compact(n) == text


def test_add_sums_every_field():
    assert TokenCounts(10, 2, 6, 1) + TokenCounts(5, 1, 4, 0) == TokenCounts(15, 3, 10, 1)


def test_fresh_input_excludes_cache_reads_and_never_goes_negative():
    assert TokenCounts(input=100, cache_read=80, cache_write=15).fresh_input == 20
    assert TokenCounts(input=5, cache_read=9).fresh_input == 0  # inkonsistente Backend-Zahlen


def test_poll_form_round_trips_and_tolerates_garbage():
    t = TokenCounts(100, 5, 60, 10)
    assert TokenCounts.from_poll(t.as_poll()) == t
    assert TokenCounts.from_poll({"input": 3, "output": 1}) == TokenCounts(3, 1)  # Alt-Form ohne Cache
    assert TokenCounts.from_poll(None) == TokenCounts()
    assert TokenCounts.from_poll({"input": "x", "cache_read": -1}) == TokenCounts()


def test_usage_form_round_trips():
    t = TokenCounts(100, 5, 60, 10)
    assert t.usage_fields() == {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "total_tokens": 105,
        "cache_read_tokens": 60,
        "cache_write_tokens": 10,
    }
    assert TokenCounts.from_usage(t.usage_fields()) == t
    assert TokenCounts.from_usage("kaputt") == TokenCounts()


def test_summary_shows_cached_share_only_when_reported():
    assert TokenCounts(1_299_893, 10_147, 1_280_000).summary() == "1.30M in (1.28M cached) / 10.1k out tokens"
    assert TokenCounts(12, 3).summary() == "12 in / 3 out tokens"


def test_opencode_tokens_adds_cache_to_input_and_reasoning_to_output():
    raw = {"input": 100, "output": 5, "reasoning": 3, "cache": {"read": 10, "write": 2}}
    assert opencode_tokens(raw) == TokenCounts(input=112, output=8, cache_read=10, cache_write=2)
    assert opencode_tokens({"input": 4, "output": 1}) == TokenCounts(4, 1)
    assert opencode_tokens(None) == TokenCounts()


def test_agent_usage_carries_cache_split_of_earlier_attempts():
    """Ein verworfener Versuch kostet auch Cache-Tokens — sie gehören in die Summe."""
    first = {"tokens": TokenCounts(1000, 10, 800, 100).as_poll(), "cost": 0.5}
    carry = _agent_usage(first, "claude_code", "sonnet")
    last = {"tokens": TokenCounts(500, 5, 400, 50).as_poll(), "cost": 0.25}
    usage = _agent_usage(last, "claude_code", "sonnet", carry)
    assert usage == {
        "model": "sonnet",
        "provider": "claude_code",
        "prompt_tokens": 1500,
        "completion_tokens": 15,
        "total_tokens": 1515,
        "cache_read_tokens": 1200,
        "cache_write_tokens": 150,
        "billing": "unknown",
        "cost_usd": 0.75,
    }
