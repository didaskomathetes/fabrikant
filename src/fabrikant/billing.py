"""Billing kind of an LLM/agent step: does ``cost_usd`` count as a real bill or only as an estimate?

- ``api``: billed per token (API key) — ``cost_usd`` is real money.
- ``subscription``: subscription login (Claude/ChatGPT plan, Copilot) — consumes usage limits/credits;
  ``cost_usd`` is only the API list-price equivalent.
- ``unknown``: not derivable (e.g. opencode: depends on the provider — local, API key or subscription).

The backends derive the kind from the auth that fabrikant passes to the CLI. A step can set it with
``billing:`` in the project spec; for opencode that is the only way.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

STEP_KEY = "billing"


class Billing(StrEnum):
    """Billing type of a usage."""

    API = "api"
    SUBSCRIPTION = "subscription"
    UNKNOWN = "unknown"


def from_auth(
    env: Mapping[str, str], *, api: tuple[str, ...], subscription: tuple[str, ...], api_wins: bool = False
) -> Billing:
    """Kind from the passed auth variables: exactly one kind set → that one, else ``unknown``.

    ``api_wins``: the CLI takes the API key when both are set (Claude Code in ``-p`` mode)."""
    has_api = any(env.get(n) for n in api)
    has_sub = any(env.get(n) for n in subscription)
    if has_api and api_wins:
        return Billing.API
    if has_api == has_sub:
        return Billing.UNKNOWN
    return Billing.API if has_api else Billing.SUBSCRIPTION


def for_step(step: Mapping, detected: Billing = Billing.UNKNOWN) -> Billing:
    """``billing:`` from the step takes precedence over the backend's detection."""
    value = step.get(STEP_KEY)
    return Billing(value) if value in Billing._value2member_map_ else detected


def validate(step: Mapping, where: str) -> list[str]:
    """Spec check of ``billing:`` (optional)."""
    value = step.get(STEP_KEY)
    if value is None or value in Billing._value2member_map_:
        return []
    return [f"{where}: billing must be one of {[b.value for b in Billing]} (is {value!r})"]
