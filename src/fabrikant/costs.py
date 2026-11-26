"""Cost rollup from the runs' ``step_usage`` data (issue #126).

Read-only and store-neutral: works on typed :class:`~fabrikant.runmodel.Run` objects.
A step without ``usage`` (e.g. shell) does not count; a usage without ``cost_usd`` (backend provides
no costs) contributes tokens and increases ``unpriced_steps`` — the sum is then a lower bound.
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
    """JSON projection of :class:`CostTotals`."""

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
    """Sum of token/cost values. ``cost_usd`` counts only known costs.

    ``cache_read_tokens``/``cache_write_tokens`` are included in ``prompt_tokens``.
    ``subscription_cost_usd`` is the part of ``cost_usd`` from subscription steps — only the API list-price
    equivalent, not an invoice (``fabrikant.billing``)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    subscription_cost_usd: float = 0.0
    unpriced_steps: int = 0

    def add(self, usage: Usage) -> None:
        """Adds a step usage; ``total_tokens`` falls back to prompt+completion if 0."""
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
        """Add another total."""
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
    """Costs of a run: total, per step and per backend (``usage.provider``)."""

    totals: CostTotals = field(default_factory=CostTotals)
    steps: list[dict] = field(default_factory=list)
    by_backend: dict[str, CostTotals] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """JSON form for ``GET /api/run/<id>``."""
        return {
            **self.totals.as_dict(),
            "steps": self.steps,
            "by_backend": {b: t.as_dict() for b, t in sorted(self.by_backend.items())},
        }


def run_cost(run: Run) -> RunCost:
    """Cost rollup of a run; a run without usage steps yields zero sums."""
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
    """Costs per project (with run count and backend breakdown) and in total."""
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
    """Runs of a project (or all) in the period (``from``/``to`` as in the run search).


    ValueError on an invalid date."""
    lower, upper, upper_exclusive = date_bounds(date_from, date_to)
    query = RunQuery(project=project, lower=lower, upper=upper, upper_exclusive=upper_exclusive)
    return (r for r in STORE.load_all(project) if query.matches(r))


def costs_report(project: str | None = None, date_from: str = "", date_to: str = "") -> dict:
    """``GET /api/kpis/costs``: costs per project in the period. ValueError on an invalid date."""

    return kpi_costs(runs_in_range(project, date_from, date_to))
