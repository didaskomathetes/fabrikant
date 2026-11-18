"""Factory-KPIs aus dem Run-Store (Issue #127, framework-v1-draft.md §5).

Rein lesend und store-neutral: arbeitet auf typisierten :class:`~fabrik.runmodel.Run`-Objekten.

* **Cycle Time**: ``finished - started`` erfolgreicher Runs (Spec-Start → Accept/Merge).
* **Judge-Veto-Rate**: Anteil der Runs mit Review-Step, in denen ein Review ``changes_requested``
  lieferte (= ausgelöste Nacharbeit).
* **Defect Escape**: Der Store kennt keinen Post-Merge-Befund. Proxy: ein fachlich gescheiterter Run
  (``fail``/``outcome``) desselben Projekts+Loops *nach* einem früheren erfolgreichen Run gilt als
  *entkommen* (Defekt tauchte nach der Annahme wieder auf). *Gefangen* = Run mit Defekt-Signal
  (roter ``verify``-Check, ``changes_requested``, Outcome-Fehler), der nicht entkommen ist.
  ``defect_escape_rate = escaped / (caught + escaped)``.

Dazu Betriebszahlen je Gruppe: erfolgreiche/gescheiterte Runs (``fail``/``error``), Laufzeit aller
beendeten Runs (Summe/Mittel/Max) und Token-/Kosten-Summen aus :mod:`fabrik.costs`.

Replayability (§5) ist nicht Teil dieses Moduls.
"""

from __future__ import annotations

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
    """JSON-Projektion von :class:`KpiTotals`; Raten/Zeiten sind ``None``, wenn nicht berechenbar."""

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
    """Dauer eines erfolgreichen Runs in Sekunden; None bei anderem Status, fehlenden/ungültigen/negativen Zeiten."""
    return runtime_s(run) if run.status is RunStatus.PASS else None


def runtime_s(run: Run) -> float | None:
    """Laufzeit eines beendeten Runs in Sekunden (jeder Status); None bei fehlenden/ungültigen/negativen Zeiten."""
    if not run.started or not run.finished:
        return None
    try:
        seconds = (to_utc(run.finished) - to_utc(run.started)).total_seconds()
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def is_failed(run: Run) -> bool:
    """True bei Run-Status ``fail`` oder ``error`` (technisch oder fachlich gescheitert)."""
    return run.status in FAILED_RUN_STATUSES


def is_vetoed(run: Run) -> bool:
    """True, wenn ein Review-Step des Runs ``changes_requested`` lieferte."""
    return any(s.review is not None and s.review.verdict == VERDICT_CHANGES_REQUESTED for s in run.steps)


def is_outcome_failure(run: Run) -> bool:
    """True bei Run-Status ``fail`` mit Fehlerklasse ``outcome`` (echtes negatives Ergebnis)."""
    return run.status is RunStatus.FAIL and run.failure_kind is FailureKind.OUTCOME


def has_defect_signal(run: Run) -> bool:
    """True bei rotem ``verify``-Check, Review-Veto oder Outcome-Fehler."""
    return is_vetoed(run) or any(not c.ok for s in run.steps for c in s.verify) or is_outcome_failure(run)


@dataclass(slots=True)
class KpiTotals:
    """Rohzähler der KPIs (summierbar über Loops/Projekte)."""

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
        """Addiert eine andere Summe."""
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
        """Vetoed / reviewed Runs; None ohne Review."""
        return _rate(self.vetoed_runs, self.reviewed_runs)

    @property
    def defect_escape_rate(self) -> float | None:
        """Escaped / (caught + escaped); None ohne Defekte."""
        return _rate(self.defects_escaped, self.defects_caught + self.defects_escaped)

    @property
    def cycle_time_avg_s(self) -> float | None:
        """Mittlere Cycle Time in Sekunden; None ohne erfolgreiche Runs."""
        return round(sum(self.cycle_times) / len(self.cycle_times), SECONDS_DIGITS) if self.cycle_times else None

    @property
    def cycle_time_median_s(self) -> float | None:
        """Median der Cycle Time in Sekunden; None ohne erfolgreiche Runs."""
        times = sorted(self.cycle_times)
        if not times:
            return None
        mid = len(times) // 2
        median = times[mid] if len(times) % 2 else (times[mid - 1] + times[mid]) / 2
        return round(median, SECONDS_DIGITS)

    @property
    def runtime_total_s(self) -> float:
        """Summe der Laufzeiten aller beendeten Runs in Sekunden."""
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
    """KPIs der Runs *eines* Loops (Reihenfolge der Eingabe egal; Escape braucht die Chronologie)."""
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
    """KPIs je (Projekt, Loop), nach Schlüssel sortiert."""
    grouped: dict[tuple[str, str], list[Run]] = {}
    for run in runs:
        grouped.setdefault((run.project, run.loop), []).append(run)
    return {key: loop_kpis(rs) for key, rs in sorted(grouped.items())}


def kpi_report(runs: Iterable[Run]) -> dict:
    """KPIs je Projekt (mit Loop-Aufschlüsselung) und insgesamt."""
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
    """``GET /api/kpis``: Factory-KPIs im Zeitraum. ValueError bei ungültigem Datum."""
    return kpi_report(runs_in_range(project, date_from, date_to))


# Prometheus-Gauges je (project, loop): (Name, HELP, Wert); None-Werte werden nicht exportiert.
_PROM_GAUGES: tuple[tuple[str, str, Callable[[KpiTotals], float | None]], ...] = (
    ("fabrik_kpi_cycle_time_avg_seconds", "Mean cycle time of passed runs", lambda t: t.cycle_time_avg_s),
    ("fabrik_kpi_judge_veto_rate", "Share of reviewed runs with changes_requested", lambda t: t.judge_veto_rate),
    ("fabrik_kpi_defects_caught", "Runs with a defect signal that did not resurface", lambda t: t.defects_caught),
    ("fabrik_kpi_defects_escaped", "Outcome failures after an earlier accepted run", lambda t: t.defects_escaped),
    ("fabrik_kpi_defect_escape_rate", "escaped / (caught + escaped)", lambda t: t.defect_escape_rate),
    ("fabrik_kpi_runs_failed", "Runs with status fail or error", lambda t: t.failed),
    ("fabrik_kpi_runtime_seconds_total", "Summed runtime of finished runs", lambda t: t.runtime_total_s),
    ("fabrik_kpi_tokens_total", "Tokens used by agent/LLM steps", lambda t: t.cost.total_tokens),
    ("fabrik_kpi_cost_usd_total", "Known cost of agent/LLM steps in USD", lambda t: round(t.cost.cost_usd, 6)),
)


def render_prometheus(runs: Iterable[Run]) -> str:
    """KPIs je (project, loop) als Prometheus-Gauges (Textformat, ohne abschließende Leerzeile)."""
    totals = _by_loop(runs)
    blocks = []
    for name, help_text, value in _PROM_GAUGES:
        samples = [(key, v) for key, t in totals.items() if (v := value(t)) is not None]
        blocks.append(metrics.render_samples(name, help_text, "gauge", ["project", "loop"], samples))
    return "\n".join(blocks)


def prometheus_text() -> str:
    """Prometheus-Text der KPIs über alle gespeicherten Runs (für ``GET /api/metrics``)."""
    return render_prometheus(runs_in_range(None, "", ""))
