"""Tests: Factory-KPIs (`kpis`, Issue #127) — Cycle Time, Judge-Veto, Defect Escape, Prometheus, Store-Backends."""

import json

import pytest

from fabrik import config, kpis, metrics
from fabrik.runmodel import FailureKind, ReviewResult, Run, RunStatus, Step, StepStatus, VerifyCheck
from fabrik.runstore import STORE


def _step(review=None, verify=()):
    return Step(name="s", type="agent", status=StepStatus.PASS, review=review, verify=tuple(verify))


def _run(rid, status=RunStatus.PASS, started="", finished="", steps=(), failure_kind=None, project="p", loop="l"):
    return Run(
        id=rid,
        project=project,
        loop=loop,
        status=status,
        started=started,
        finished=finished,
        steps=tuple(steps),
        failure_kind=failure_kind,
    )


def _fail(rid, started, kind=FailureKind.OUTCOME):
    return _run(rid, RunStatus.FAIL, started, started, failure_kind=kind)


def test_cycle_time_only_for_passed_runs_with_valid_times():
    ok = _run("a", started="2026-01-01T10:00:00+00:00", finished="2026-01-01T10:01:30+00:00")
    assert kpis.cycle_time_s(ok) == 90
    assert kpis.cycle_time_s(_run("b", RunStatus.FAIL, "2026-01-01T10:00:00", "2026-01-01T10:01:00")) is None
    assert kpis.cycle_time_s(_run("c", started="2026-01-01T10:00:00")) is None
    assert kpis.cycle_time_s(_run("d", started="kaputt", finished="2026-01-01T10:00:00")) is None
    assert kpis.cycle_time_s(_run("e", started="2026-01-01T11:00:00", finished="2026-01-01T10:00:00")) is None


def test_cycle_time_stats_even_and_odd():
    runs = [
        _run(str(i), started="2026-01-01T10:00:00", finished=f"2026-01-01T10:00:{s:02d}")
        for i, s in enumerate([10, 20, 51])
    ]
    ct = kpis.loop_kpis(runs).as_dict()["cycle_time_s"]
    assert ct == {"count": 3, "avg": 27.0, "median": 20.0, "max": 51.0}
    ct = kpis.loop_kpis(runs[:2]).as_dict()["cycle_time_s"]
    assert ct["median"] == 15.0


def test_empty_loop_has_no_rates():
    d = kpis.loop_kpis([]).as_dict()
    assert d["runs"] == 0 and d["judge_veto_rate"] is None and d["defect_escape_rate"] is None
    assert d["cycle_time_s"] == {"count": 0, "avg": None, "median": None, "max": None}


def test_judge_veto_rate():
    veto = _step(review=ReviewResult(verdict="changes_requested"))
    approved = _step(review=ReviewResult(verdict="approved"))
    runs = [_run("1", steps=[veto, approved]), _run("2", steps=[approved]), _run("3", steps=[_step()])]
    t = kpis.loop_kpis(runs)
    assert (t.reviewed_runs, t.vetoed_runs, t.judge_veto_rate) == (2, 1, 0.5)


def test_defect_caught_vs_escaped():
    red = _step(verify=[VerifyCheck(name="t", ok=False)])
    runs = [
        _run("1", RunStatus.FAIL, "2026-01-01T10:00:00", steps=[red]),  # gefangen (verify rot)
        _run("2", started="2026-01-02T10:00:00"),  # akzeptiert
        _fail("3", "2026-01-03T10:00:00"),  # Outcome-Fehler nach Accept → entkommen
        _fail("4", "2026-01-04T10:00:00", FailureKind.TRANSIENT),  # kein Defekt
    ]
    t = kpis.loop_kpis(reversed(runs))  # Eingabereihenfolge egal
    assert (t.defects_caught, t.defects_escaped, t.defect_escape_rate) == (1, 1, 0.5)


def test_failure_before_any_accept_is_caught_not_escaped():
    t = kpis.loop_kpis([_fail("1", "2026-01-01T10:00:00"), _run("2", started="2026-01-02T10:00:00")])
    assert (t.defects_caught, t.defects_escaped) == (1, 0)


def test_report_groups_by_project_and_loop():
    out = kpis.kpi_report([_run("1", project="a", loop="x"), _run("2", project="a", loop="y"), _run("3", project="b")])
    assert out["projects"]["a"]["runs"] == 2 and sorted(out["projects"]["a"]["loops"]) == ["x", "y"]
    assert out["total"]["runs"] == 3
    assert kpis.kpi_report([]) == {"projects": {}, "total": kpis.KpiTotals().as_dict()}


def test_render_prometheus_skips_undefined_values():
    text = kpis.render_prometheus(
        [_run("1", started="2026-01-01T10:00:00", finished="2026-01-01T10:00:05", loop='a"b')]
    )
    assert "# TYPE fabrik_kpi_cycle_time_avg_seconds gauge" in text
    assert 'fabrik_kpi_cycle_time_avg_seconds{project="p",loop="a\\"b"} 5.0' in text
    assert 'fabrik_kpi_defects_caught{project="p",loop="a\\"b"} 0' in text
    assert "fabrik_kpi_judge_veto_rate{" not in text  # kein Review → kein Sample
    assert kpis.render_prometheus([]).count("# HELP") == len(kpis._PROM_GAUGES)


def test_counter_render_unchanged_by_shared_helper():
    c = metrics.Counter("x_total", "h\\elp", ["a"])
    c.inc(2, a="v")
    assert c.render() == '# HELP x_total h\\\\elp\n# TYPE x_total counter\nx_total{a="v"} 2.0'


def _write(runs_dir, rid, project, started, finished, status="pass", failure_kind=None):
    rec = {"id": rid, "project": project, "loop": "l", "status": status, "started": started, "finished": finished}
    if failure_kind:
        rec["failure_kind"] = failure_kind
    if config.run_store_backend() == config.RUN_STORE_SQL:
        STORE.backend()._modify(project, rid, lambda _current: dict(rec))
        return
    d = runs_dir / project
    d.mkdir(exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps(rec))


@pytest.fixture
def seeded(store_backend, runs_dir):
    _write(runs_dir, "20260101T000000Z-l", "p", "2026-01-01T10:00:00+00:00", "2026-01-01T10:01:00+00:00")
    _write(runs_dir, "20260102T000000Z-l", "p", "2026-01-02T10:00:00+00:00", "2026-01-02T10:03:00+00:00")
    _write(runs_dir, "20260103T000000Z-l", "p", "2026-01-03T10:00:00+00:00", "", "fail", "outcome")
    _write(runs_dir, "20260102T000001Z-l", "q", "2026-01-02T11:00:00+00:00", "2026-01-02T11:00:10+00:00")
    return runs_dir


def test_kpis_report_from_store(seeded):
    out = kpis.kpis_report()
    assert out["total"]["runs"] == 4 and out["total"]["defects_escaped"] == 1
    assert out["projects"]["p"]["cycle_time_s"]["avg"] == 120.0
    assert kpis.kpis_report("q")["total"]["runs"] == 1
    assert kpis.kpis_report(date_from="2026-01-02", date_to="2026-01-02")["total"]["runs"] == 2


def test_kpis_report_rejects_bad_date(seeded):
    with pytest.raises(ValueError):
        kpis.kpis_report(date_from="gestern")


def test_prometheus_text_from_store(seeded):
    kpis.reset_prometheus_cache()
    text = kpis.prometheus_text()
    assert 'fabrik_kpi_defects_escaped{project="p",loop="l"} 1' in text
    assert 'fabrik_kpi_cycle_time_avg_seconds{project="q",loop="l"} 10.0' in text


def _with_usage(run, cost, tokens=10):
    from dataclasses import replace

    from fabrik.runmodel import Usage

    step = Step(
        name="a",
        type="agent",
        status=StepStatus.PASS,
        usage=Usage(provider="b", prompt_tokens=tokens, completion_tokens=0, total_tokens=tokens, cost_usd=cost),
    )
    return replace(run, steps=(step,))


def test_runtime_covers_any_status_but_needs_both_timestamps():
    fail = _run("a", RunStatus.FAIL, "2026-01-01T10:00:00", "2026-01-01T10:00:30")
    assert kpis.runtime_s(fail) == 30
    assert kpis.runtime_s(_run("b", started="2026-01-01T10:00:00")) is None
    assert kpis.runtime_s(_run("c", started="kaputt", finished="2026-01-01T10:00:00")) is None
    assert kpis.runtime_s(_run("d", started="2026-01-01T11:00:00", finished="2026-01-01T10:00:00")) is None


def test_failed_counts_fail_and_error_only():
    statuses = [RunStatus.PASS, RunStatus.FAIL, RunStatus.ERROR, RunStatus.REJECTED, RunStatus.RUNNING]
    t = kpis.loop_kpis([_run(str(i), s, started=f"2026-01-0{i + 1}T10:00:00") for i, s in enumerate(statuses)])
    assert (t.runs, t.passed, t.failed) == (5, 1, 2)


def test_runtime_cost_and_tokens_aggregate_and_merge():
    a = _with_usage(_run("1", started="2026-01-01T10:00:00", finished="2026-01-01T10:01:00"), 0.5, 100)
    b = _with_usage(_run("2", RunStatus.FAIL, "2026-01-02T10:00:00", "2026-01-02T10:03:00"), None, 50)
    out = kpis.kpi_report([a, b, _run("3", project="q")])
    p = out["projects"]["p"]
    assert p["runtime_s"] == {"count": 2, "total": 240, "avg": 120, "max": 180}
    assert p["cost"]["total_tokens"] == 150 and p["cost"]["cost_usd"] == 0.5 and p["cost"]["unpriced_steps"] == 1
    assert out["total"]["cost"]["total_tokens"] == 150 and out["total"]["failed"] == 1
    assert out["projects"]["q"]["runtime_s"]["avg"] is None  # nie beendet
    assert kpis.loop_kpis([]).as_dict()["cost"]["total_tokens"] == 0


def test_prometheus_exports_ops_gauges():
    run = _with_usage(_run("1", started="2026-01-01T10:00:00", finished="2026-01-01T10:01:00"), 0.25, 7)
    text = kpis.render_prometheus([run])
    assert 'fabrik_kpi_tokens_total{project="p",loop="l"} 7' in text
    assert 'fabrik_kpi_cost_usd_total{project="p",loop="l"} 0.25' in text
    assert 'fabrik_kpi_runtime_seconds_total{project="p",loop="l"} 60' in text
    assert 'fabrik_kpi_runs_failed{project="p",loop="l"} 0' in text


def test_prometheus_text_is_cached_within_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(kpis, "runs_in_range", lambda *a: calls.append(a) or [])
    kpis.reset_prometheus_cache()
    kpis.prometheus_text()
    kpis.prometheus_text()
    assert len(calls) == 1
    monkeypatch.setattr(kpis, "PROMETHEUS_CACHE_TTL_S", 0.0)
    kpis.prometheus_text()
    assert len(calls) == 2
    kpis.reset_prometheus_cache()
