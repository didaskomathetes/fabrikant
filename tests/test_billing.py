"""Abrechnungsart: Erkennung aus der Auth je Backend, Spec-Override, Validierung, Kosten-Aufteilung."""

import pytest

from fabrikant import billing, costs
from fabrikant.backends.claude_code import ClaudeCodeBackend
from fabrikant.backends.codex import CodexBackend
from fabrikant.backends.copilot import CopilotBackend
from fabrikant.billing import Billing
from fabrikant.failures import classify_agent_output
from fabrikant.runmodel import FailureKind, Run, RunStatus, Step, StepStatus, Usage
from fabrikant.steps.agent import _detected_billing

_AUTH = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CODEX_AUTH_JSON_B64")


@pytest.fixture
def auth(monkeypatch):
    """Auth-Variablen leeren; ``auth(NAME, …)`` setzt einzelne."""
    for name in _AUTH:
        monkeypatch.delenv(name, raising=False)

    def set_(*names):
        for name in names:
            monkeypatch.setenv(name, "x")

    return set_


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ((), Billing.UNKNOWN),
        (("CLAUDE_CODE_OAUTH_TOKEN",), Billing.SUBSCRIPTION),
        (("ANTHROPIC_API_KEY",), Billing.API),
        (("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"), Billing.API),  # Key gewinnt im -p-Modus
    ],
)
def test_claude_code_billing_follows_passed_auth(auth, names, expected):
    auth(*names)
    assert ClaudeCodeBackend("", "sonnet").billing() == expected


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ((), Billing.UNKNOWN),  # CLI liest ~/.codex/auth.json — Art offen
        (("CODEX_AUTH_JSON_B64",), Billing.SUBSCRIPTION),
        (("OPENAI_API_KEY",), Billing.API),
        (("OPENAI_API_KEY", "CODEX_AUTH_JSON_B64"), Billing.UNKNOWN),
    ],
)
def test_codex_billing_follows_passed_auth(auth, names, expected):
    auth(*names)
    assert CodexBackend().billing() == expected


def test_copilot_is_always_subscription():
    assert CopilotBackend().billing() == Billing.SUBSCRIPTION


def test_backend_without_detection_is_unknown():
    assert _detected_billing(object()) == Billing.UNKNOWN  # type: ignore[arg-type]


def test_step_override_wins_over_detection():
    assert billing.for_step({"billing": "api"}, Billing.SUBSCRIPTION) == Billing.API
    assert billing.for_step({}, Billing.SUBSCRIPTION) == Billing.SUBSCRIPTION
    assert billing.for_step({"billing": "bogus"}) == Billing.UNKNOWN


def test_validate_rejects_unknown_values():
    assert billing.validate({"billing": "subscription"}, "s") == []
    assert billing.validate({}, "s") == []
    assert "billing muss" in billing.validate({"billing": "flat"}, "s")[0]


def test_costs_split_subscription_share():
    steps = (
        Step(name="a", type="agent", status=StepStatus.PASS, usage=Usage(cost_usd=1.0, billing="subscription")),
        Step(name="b", type="agent", status=StepStatus.PASS, usage=Usage(cost_usd=0.25, billing="api")),
    )
    d = costs.run_cost(Run(id="r", project="p", loop="l", status=RunStatus.PASS, steps=steps)).as_dict()
    assert (d["cost_usd"], d["subscription_cost_usd"]) == (1.25, 1.0)


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your usage limit. Upgrade to Pro or try later.",
        '{"rate_limit_reached_type":"workspace_member_usage_limit_reached"}',
        "workspace_owner_credits_depleted",
    ],
)
def test_codex_limits_are_environment_failures(text):
    assert classify_agent_output(text).kind == FailureKind.ENVIRONMENT
