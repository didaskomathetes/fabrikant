"""Factory KPIs from the run store (issue #127, framework-v1-draft.md §5).

Read-only and store-neutral: works on typed :class:`~fabrikant.runmodel.Run` objects.

* **Cycle time**: ``finished - started`` of successful runs (spec start → accept/merge).
* **Judge veto rate**: share of runs with a review step in which a review returned ``changes_requested``
  (= triggered rework).
* **Defect escape**: the store has no post-merge finding. Proxy: a functionally failed run
  (``fail``/``outcome``) of the same project+loop *after* an earlier successful run counts as
  *escaped* (the defect resurfaced after acceptance). *Caught* = run with a defect signal
  (red ``verify`` check, ``changes_requested``, outcome failure) that did not escape.
  ``defect_escape_rate = escaped / (caught + escaped)``.

Plus operational figures per group: successful/failed runs (``fail``/``error``), runtime of all
finished runs (sum/mean/max) and token/cost sums from :mod:`fabrikant.costs`.

Replayability (§5) is not part of this module.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TypedDict

from . import metrics
from .costs import CostSummary, CostTotals, run_cost, runs_in_range
from .runmodel import FAILED_RUN_STATUSES, FailureKind, Run, RunStatus
from .runquery import to_utc, utc_sort_text

VERDICT_CHANGES_REQUESTED = "changes_requested"
RATE_DIGITS = 4
SECONDS_DIGITS = 1


class KpiSummary(TypedDict):
    """JSON projection of :class:`KpiTotals`; rates/times are ``None`` when not computable."""

    runs: int
    passed: int
    failed: int
    runtime_s: dict[str, float | int | None]
    cost: CostSummary
    cycle_time_s: dict[str, float | int | None]
    reviewed_runs: int
    vetoed_runs: int
    judge_veto_rate: float | None
    defects_caught: int
    defects_escaped: int
    defect_escape_rate: float | None


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, RATE_DIGITS) if whole else None


def cycle_time_s(run: Run) -> float | None:
    """Duration of a successful run in seconds; None for other status, missing/invalid/negative times."""
    return runtime_s(run) if run.status is RunStatus.PASS else None


def runtime_s(run: Run) -> float | None:
    """Runtime of a finished run in seconds (any status); None for missing/invalid/negative times."""
    if not run.started or not run.finished:
        return None
    try:
        seconds = (to_utc(run.finished) - to_utc(run.started)).total_seconds()
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def is_failed(run: Run) -> bool:
    """True for run status ``fail`` or ``error`` (failed technically or functionally)."""
    return run.status in FAILED_RUN_STATUSES


def is_vetoed(run: Run) -> bool:
    """True if a review step of the run returned ``changes_requested``."""
    return any(s.review is not None and s.review.verdict == VERDICT_CHANGES_REQUESTED for s in run.steps)


def is_outcome_failure(run: Run) -> bool:
    """True for run status ``fail`` with error class ``outcome`` (a genuine negative result)."""
    return run.status is RunStatus.FAIL and run.failure_kind is FailureKind.OUTCOME


def has_defect_signal(run: Run) -> bool:
    """True for a red ``verify`` check, review veto or outcome failure."""
    return is_vetoed(run) or any(not c.ok for s in run.steps for c in s.verify) or is_outcome_failure(run)


@dataclass(slots=True)
class KpiTotals:
    """Raw counters of the KPIs (summable across loops/projects)."""

    runs: int = 0
    passed: int = 0
    failed: int = 0
    runtimes: list[float] = field(default_factory=list)
    cost: CostTotals = field(default_factory=CostTotals)
    cycle_times: list[float] = field(default_factory=list)
    reviewed_runs: int = 0
    vetoed_runs: int = 0
    defects_caught: int = 0
    defects_escaped: int = 0

    def merge(self, other: KpiTotals) -> None:
        """Add another total."""
        self.runs += other.runs
        self.passed += other.passed
        self.failed += other.failed
        self.runtimes += other.runtimes
        self.cost.merge(other.cost)
        self.cycle_times += other.cycle_times
        self.reviewed_runs += other.reviewed_runs
        self.vetoed_runs += other.vetoed_runs
        self.defects_caught += other.defects_caught
        self.defects_escaped += other.defects_escaped

    @property
    def judge_veto_rate(self) -> float | None:
        """Vetoed / reviewed runs; None without review."""
        return _rate(self.vetoed_runs, self.reviewed_runs)

    @property
    def defect_escape_rate(self) -> float | None:
        """Escaped / (caught + escaped); None without defects."""
        return _rate(self.defects_escaped, self.defects_caught + self.defects_escaped)

    @property
    def cycle_time_avg_s(self) -> float | None:
        """Mean cycle time in seconds; None without successful runs."""
        return round(sum(self.cycle_times) / len(self.cycle_times), SECONDS_DIGITS) if self.cycle_times else None

    @property
    def cycle_time_median_s(self) -> float | None:
        """Median cycle time in seconds; None without successful runs."""
        times = sorted(self.cycle_times)
        if not times:
            return None
        mid = len(times) // 2
        median = times[mid] if len(times) % 2 else (times[mid - 1] + times[mid]) / 2
        return round(median, SECONDS_DIGITS)

    @property
    def runtime_total_s(self) -> float:
        """Sum of the durations of all finished runs in seconds."""
        return round(sum(self.runtimes), SECONDS_DIGITS)

    def as_dict(self) -> KpiSummary:
        """JSON-Form."""
        return {
            "runs": self.runs,
            "passed": self.passed,
            "failed": self.failed,
            "runtime_s": {
                "count": len(self.runtimes),
                "total": self.runtime_total_s,
                "avg": round(sum(self.runtimes) / len(self.runtimes), SECONDS_DIGITS) if self.runtimes else None,
                "max": round(max(self.runtimes), SECONDS_DIGITS) if self.runtimes else None,
            },
            "cost": self.cost.as_dict(),
            "cycle_time_s": {
                "count": len(self.cycle_times),
                "avg": self.cycle_time_avg_s,
                "median": self.cycle_time_median_s,
                "max": round(max(self.cycle_times), SECONDS_DIGITS) if self.cycle_times else None,
            },
            "reviewed_runs": self.reviewed_runs,
            "vetoed_runs": self.vetoed_runs,
            "judge_veto_rate": self.judge_veto_rate,
            "defects_caught": self.defects_caught,
            "defects_escaped": self.defects_escaped,
            "defect_escape_rate": self.defect_escape_rate,
        }


def _chronological(run: Run) -> tuple[str, str]:
    return utc_sort_text(run.started) or "", run.id


def loop_kpis(runs: Iterable[Run]) -> KpiTotals:
    """KPIs of the runs of *one* loop (input order does not matter; escape needs the chronology)."""
    out = KpiTotals()
    accepted_before = False
    for run in sorted(runs, key=_chronological):
        out.runs += 1
        cycle = cycle_time_s(run)
        if run.status is RunStatus.PASS:
            out.passed += 1
        if cycle is not None:
            out.cycle_times.append(cycle)
        out.failed += is_failed(run)
        if (runtime := runtime_s(run)) is not None:
            out.runtimes.append(runtime)
        out.cost.merge(run_cost(run).totals)
        if any(s.review is not None for s in run.steps):
            out.reviewed_runs += 1
            out.vetoed_runs += is_vetoed(run)
        if accepted_before and is_outcome_failure(run):
            out.defects_escaped += 1
        elif has_defect_signal(run):
            out.defects_caught += 1
        accepted_before = accepted_before or run.status is RunStatus.PASS
    return out


def _by_loop(runs: Iterable[Run]) -> dict[tuple[str, str], KpiTotals]:
    """KPIs per (project, loop), sorted by key."""
    grouped: dict[tuple[str, str], list[Run]] = {}
    for run in runs:
        grouped.setdefault((run.project, run.loop), []).append(run)
    return {key: loop_kpis(rs) for key, rs in sorted(grouped.items())}


def kpi_report(runs: Iterable[Run]) -> dict:
    """KPIs per project (with loop breakdown) and overall."""
    per_project: dict[str, dict[str, KpiTotals]] = {}
    for (project, loop), totals in _by_loop(runs).items():
        per_project.setdefault(project, {})[loop] = totals
    projects: dict[str, dict] = {}
    total = KpiTotals()
    for name, loops in per_project.items():
        merged = KpiTotals()
        for t in loops.values():
            merged.merge(t)
        total.merge(merged)
        projects[name] = {**merged.as_dict(), "loops": {loop: t.as_dict() for loop, t in loops.items()}}
    return {"projects": projects, "total": total.as_dict()}


def kpis_report(project: str | None = None, date_from: str = "", date_to: str = "") -> dict:
    """``GET /api/kpis``: factory KPIs in the period. ValueError for an invalid date."""
    return kpi_report(runs_in_range(project, date_from, date_to))


# Prometheus gauges per (project, loop): (name, HELP, value); None values are not exported.
_PROM_GAUGES: tuple[tuple[str, str, Callable[[KpiTotals], float | None]], ...] = (
    ("fabrikant_kpi_cycle_time_avg_seconds", "Mean cycle time of passed runs", lambda t: t.cycle_time_avg_s),
    ("fabrikant_kpi_judge_veto_rate", "Share of reviewed runs with changes_requested", lambda t: t.judge_veto_rate),
    ("fabrikant_kpi_defects_caught", "Runs with a defect signal that did not resurface", lambda t: t.defects_caught),
    ("fabrikant_kpi_defects_escaped", "Outcome failures after an earlier accepted run", lambda t: t.defects_escaped),
    ("fabrikant_kpi_defect_escape_rate", "escaped / (caught + escaped)", lambda t: t.defect_escape_rate),
    ("fabrikant_kpi_runs_failed", "Runs with status fail or error", lambda t: t.failed),
    ("fabrikant_kpi_runtime_seconds_total", "Summed runtime of finished runs", lambda t: t.runtime_total_s),
    ("fabrikant_kpi_tokens_total", "Tokens used by agent/LLM steps", lambda t: t.cost.total_tokens),
    ("fabrikant_kpi_cost_usd_total", "Known cost of agent/LLM steps in USD", lambda t: round(t.cost.cost_usd, 6)),
)


def render_prometheus(runs: Iterable[Run]) -> str:
    """KPIs per (project, loop) as Prometheus gauges (text format, without trailing blank line)."""
    totals = _by_loop(runs)
    blocks = []
    for name, help_text, value in _PROM_GAUGES:
        samples = [(key, v) for key, t in totals.items() if (v := value(t)) is not None]
        blocks.append(metrics.render_samples(name, help_text, "gauge", ["project", "loop"], samples))
    return "\n".join(blocks)


# Scrapes would otherwise load the entire run store on every request; a short TTL cache caps the load.
PROMETHEUS_CACHE_TTL_S = 15.0
_prom_cache: tuple[float, str] | None = None
_prom_lock = threading.Lock()


def reset_prometheus_cache() -> None:
    """Discard the cached Prometheus text (tests, admin hooks)."""
    global _prom_cache
    with _prom_lock:
        _prom_cache = None


def prometheus_text() -> str:
    """Prometheus text of the KPIs over all stored runs (for ``GET /api/metrics``).

    Cached for ``PROMETHEUS_CACHE_TTL_S`` seconds — the computation reads the entire run store."""
    global _prom_cache
    with _prom_lock:
        now = time.monotonic()
        if _prom_cache is None or now - _prom_cache[0] >= PROMETHEUS_CACHE_TTL_S:
            _prom_cache = (now, render_prometheus(runs_in_range(None, "", "")))
        return _prom_cache[1]
