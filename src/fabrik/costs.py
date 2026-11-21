"""Kosten-Rollup aus den ``step_usage``-Daten der Runs (Issue #126).

Rein lesend und store-neutral: arbeitet auf typisierten :class:`~fabrik.runmodel.Run`-Objekten.
Ein Step ohne ``usage`` (z. B. Shell) zählt nicht; ein Usage ohne ``cost_usd`` (Backend liefert
keine Kosten) trägt Tokens bei und erhöht ``unpriced_steps`` — die Summe ist dann eine Untergrenze.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TypedDict

from .billing import Billing
from .runmodel import Run, Usage
from .runquery import RunQuery, date_bounds
from .runstore import STORE

UNKNOWN_BACKEND = "unknown"


class CostSummary(TypedDict):
    """JSON-Projektion von :class:`CostTotals`."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    subscription_cost_usd: float
    unpriced_steps: int


@dataclass(slots=True)
class CostTotals:
    """Summe von Token-/Kostenwerten. ``cost_usd`` zählt nur bekannte Kosten.

    ``cache_read_tokens``/``cache_write_tokens`` sind in ``prompt_tokens`` enthalten.
    ``subscription_cost_usd`` ist der Teil von ``cost_usd`` aus Abo-Steps — nur API-Listenpreis-Äquivalent,
    keine Rechnung (``fabrik.billing``)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    subscription_cost_usd: float = 0.0
    unpriced_steps: int = 0

    def add(self, usage: Usage) -> None:
        """Addiert ein Step-Usage; ``total_tokens`` fällt auf prompt+completion zurück, wenn 0."""
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens or usage.prompt_tokens + usage.completion_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        if usage.cost_usd is None:
            self.unpriced_steps += 1
        else:
            self.cost_usd += usage.cost_usd
            if usage.billing == Billing.SUBSCRIPTION:
                self.subscription_cost_usd += usage.cost_usd

    def merge(self, other: CostTotals) -> None:
        """Addiert eine andere Summe."""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cost_usd += other.cost_usd
        self.subscription_cost_usd += other.subscription_cost_usd
        self.unpriced_steps += other.unpriced_steps

    def as_dict(self) -> CostSummary:
        """JSON-Form (Kosten auf 6 Nachkommastellen gerundet)."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "subscription_cost_usd": round(self.subscription_cost_usd, 6),
            "unpriced_steps": self.unpriced_steps,
        }


@dataclass(slots=True)
class RunCost:
    """Kosten eines Runs: Gesamtsumme, je Step und je Backend (``usage.provider``)."""

    totals: CostTotals = field(default_factory=CostTotals)
    steps: list[dict] = field(default_factory=list)
    by_backend: dict[str, CostTotals] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """JSON-Form für ``GET /api/run/<id>``."""
        return {
            **self.totals.as_dict(),
            "steps": self.steps,
            "by_backend": {b: t.as_dict() for b, t in sorted(self.by_backend.items())},
        }


def run_cost(run: Run) -> RunCost:
    """Kosten-Rollup eines Runs; ein Run ohne Usage-Steps ergibt Nullsummen."""
    out = RunCost()
    for step in run.steps:
        usage = step.usage
        if usage is None:
            continue
        backend = usage.provider or UNKNOWN_BACKEND
        single = CostTotals()
        single.add(usage)
        out.totals.merge(single)
        out.by_backend.setdefault(backend, CostTotals()).merge(single)
        out.steps.append({"step": step.name, "backend": backend, "model": usage.model, **single.as_dict()})
    return out


def kpi_costs(runs: Iterable[Run]) -> dict:
    """Kosten je Projekt (mit Run-Zahl und Backend-Aufschlüsselung) und insgesamt."""
    projects: dict[str, dict] = {}
    total = CostTotals()
    for run in runs:
        rc = run_cost(run)
        entry = projects.setdefault(run.project, {"runs": 0, "totals": CostTotals(), "by_backend": {}})
        entry["runs"] += 1
        entry["totals"].merge(rc.totals)
        for backend, t in rc.by_backend.items():
            entry["by_backend"].setdefault(backend, CostTotals()).merge(t)
        total.merge(rc.totals)
    return {
        "projects": {
            name: {
                "runs": e["runs"],
                **e["totals"].as_dict(),
                "by_backend": {b: t.as_dict() for b, t in sorted(e["by_backend"].items())},
            }
            for name, e in sorted(projects.items())
        },
        "total": total.as_dict(),
    }


def runs_in_range(project: str | None, date_from: str, date_to: str) -> Iterable[Run]:
    """Runs eines Projekts (oder aller) im Zeitraum (``from``/``to`` wie in der Run-Suche).

    ValueError bei ungültigem Datum."""
    lower, upper, upper_exclusive = date_bounds(date_from, date_to)
    query = RunQuery(project=project, lower=lower, upper=upper, upper_exclusive=upper_exclusive)
    return (r for r in STORE.load_all(project) if query.matches(r))


def costs_report(project: str | None = None, date_from: str = "", date_to: str = "") -> dict:
    """``GET /api/kpis/costs``: Kosten je Projekt im Zeitraum. ValueError bei ungültigem Datum."""
    return kpi_costs(runs_in_range(project, date_from, date_to))
