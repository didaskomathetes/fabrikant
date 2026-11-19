"""Abrechnungsart eines LLM-/Agent-Steps: zählt ``cost_usd`` wirklich als Rechnung oder nur als Schätzung?

- ``api``: pro Token abgerechnet (API-Key) — ``cost_usd`` ist echtes Geld.
- ``subscription``: Abo-Login (Claude/ChatGPT-Plan, Copilot) — verbraucht Nutzungslimits/Credits;
  ``cost_usd`` ist nur das API-Listenpreis-Äquivalent.
- ``unknown``: nicht ableitbar (z. B. opencode: hängt vom Provider ab — lokal, API-Key oder Abo).

Die Backends leiten die Art aus der Auth ab, die fabrik der CLI übergibt. Ein Step kann sie mit
``billing:`` im Projekt-Spec festlegen; das ist bei opencode der einzige Weg.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

STEP_KEY = "billing"


class Billing(StrEnum):
    """Abrechnungsart einer Usage."""

    API = "api"
    SUBSCRIPTION = "subscription"
    UNKNOWN = "unknown"


def from_auth(
    env: Mapping[str, str], *, api: tuple[str, ...], subscription: tuple[str, ...], api_wins: bool = False
) -> Billing:
    """Art aus den übergebenen Auth-Variablen: genau eine Sorte gesetzt → diese, sonst ``unknown``.

    ``api_wins``: die CLI nimmt bei beiden den API-Key (Claude Code im ``-p``-Modus)."""
    has_api = any(env.get(n) for n in api)
    has_sub = any(env.get(n) for n in subscription)
    if has_api and api_wins:
        return Billing.API
    if has_api == has_sub:
        return Billing.UNKNOWN
    return Billing.API if has_api else Billing.SUBSCRIPTION


def for_step(step: Mapping, detected: Billing = Billing.UNKNOWN) -> Billing:
    """``billing:`` aus dem Step hat Vorrang vor der Erkennung des Backends."""
    value = step.get(STEP_KEY)
    return Billing(value) if value in Billing._value2member_map_ else detected


def validate(step: Mapping, where: str) -> list[str]:
    """Spec-Prüfung von ``billing:`` (optional)."""
    value = step.get(STEP_KEY)
    if value is None or value in Billing._value2member_map_:
        return []
    return [f"{where}: billing muss einer von {[b.value for b in Billing]} sein (ist {value!r})"]
