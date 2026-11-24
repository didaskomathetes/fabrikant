"""Tests: Kosten-Rollup (`costs`, Issue #126) — Aggregation, leere Runs, fehlende Usage, Zeitraum, Store-Backends."""

import json

import pytest

from fabrikant import config, costs, runsearch
from fabrikant.runmodel import Run, RunStatus, Step, StepStatus, Usage
from fabrikant.runstore import STORE


def _step(name, usage=None):
    return Step(name=name, type="agent", status=StepStatus.PASS, usage=usage)


def _run(steps, project="p", rid="r1"):
    return Run(id=rid, project=project, loop="l", status=RunStatus.PASS, steps=tuple(steps))


def test_run_without_usage_is_zero():
    rc = costs.run_cost(_run([_step("sh")]))
    assert rc.totals.as_dict() == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.0,
        "subscription_cost_usd": 0.0,
        "unpriced_steps": 0,
    }
    assert rc.steps == [] and rc.by_backend == {}


def test_run_without_steps_is_zero():
    assert costs.run_cost(_run([])).as_dict()["total_tokens"] == 0


def test_sums_per_step_and_backend():
    u1 = Usage(
        model="m1", provider="claude_code", prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.25
    )
    u2 = Usage(model="m2", provider="opencode", prompt_tokens=4, completion_tokens=1, cost_usd=0.0)
    u3 = Usage(model="m1", provider="claude_code", prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.5)
    d = costs.run_cost(_run([_step("a", u1), _step("b", u2), _step("c", u3), _step("sh")])).as_dict()
    assert d["total_tokens"] == 15 + 5 + 2  # u2: total_tokens fehlt → prompt+completion
    assert d["cost_usd"] == 0.75
    assert [s["step"] for s in d["steps"]] == ["a", "b", "c"]
    assert d["by_backend"]["claude_code"]["cost_usd"] == 0.75
    assert d["by_backend"]["opencode"]["total_tokens"] == 5


def test_unknown_cost_counts_as_unpriced_and_missing_provider_as_unknown():
    d = costs.run_cost(_run([_step("a", Usage(prompt_tokens=3, completion_tokens=2))])).as_dict()
    assert d["cost_usd"] == 0.0 and d["unpriced_steps"] == 1 and d["total_tokens"] == 5
    assert list(d["by_backend"]) == [costs.UNKNOWN_BACKEND]


def test_kpi_costs_groups_by_project():
    u = Usage(provider="b", prompt_tokens=1, completion_tokens=1, cost_usd=1.0)
    out = costs.kpi_costs([_run([_step("a", u)], "p1", "r1"), _run([_step("a", u)], "p1", "r2"), _run([], "p2", "r3")])
    assert out["projects"]["p1"]["runs"] == 2 and out["projects"]["p1"]["cost_usd"] == 2.0
    assert out["projects"]["p2"]["runs"] == 1 and out["projects"]["p2"]["total_tokens"] == 0
    assert out["total"]["cost_usd"] == 2.0
    assert costs.kpi_costs([]) == {"projects": {}, "total": costs.CostTotals().as_dict()}


def _write(runs_dir, rid, project, started, cost):
    usage = {"provider": "b", "prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5, "cost_usd": cost}
    rec = {
        "id": rid,
        "project": project,
        "loop": "l",
        "status": "pass",
        "started": started,
        "steps": [{"name": "a", "type": "agent", "status": "pass", "usage": usage}],
    }
    if config.run_store_backend() == config.RUN_STORE_SQL:
        STORE.backend()._modify(project, rid, lambda _current: dict(rec))
        return
    d = runs_dir / project
    d.mkdir(exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps(rec))


@pytest.fixture
def seeded(store_backend, runs_dir):
    _write(runs_dir, "20260101T000000Z-l", "p", "2026-01-01T10:00:00+00:00", 1.0)
    _write(runs_dir, "20260102T000000Z-l", "p", "2026-01-02T10:00:00+00:00", 2.0)
    _write(runs_dir, "20260102T000001Z-l", "q", "2026-01-02T11:00:00+00:00", 4.0)
    return runs_dir


def test_costs_report_filters_project_and_range(seeded):
    assert costs.costs_report()["total"]["cost_usd"] == 7.0
    assert costs.costs_report("p")["total"]["cost_usd"] == 3.0
    day2 = costs.costs_report(date_from="2026-01-02", date_to="2026-01-02")
    assert day2["total"]["cost_usd"] == 6.0 and sorted(day2["projects"]) == ["p", "q"]


def test_costs_report_rejects_bad_date(seeded):
    with pytest.raises(ValueError):
        costs.costs_report(date_from="gestern")


def test_search_rows_carry_cost(seeded):
    rows = runsearch.search_runs("p")["runs"]
    assert {r["cost"]["cost_usd"] for r in rows} == {1.0, 2.0}


def test_totals_sum_cache_tokens_per_step_and_backend():
    u1 = Usage(
        provider="claude_code",
        prompt_tokens=100,
        completion_tokens=5,
        cache_read_tokens=80,
        cache_write_tokens=10,
        cost_usd=0.1,
    )
    u2 = Usage(provider="claude_code", prompt_tokens=50, completion_tokens=5, cache_read_tokens=40, cost_usd=0.1)
    d = costs.run_cost(_run([_step("a", u1), _step("b", u2)])).as_dict()
    assert (d["cache_read_tokens"], d["cache_write_tokens"]) == (120, 10)
    assert d["by_backend"]["claude_code"]["cache_read_tokens"] == 120
    assert d["steps"][0]["cache_read_tokens"] == 80
