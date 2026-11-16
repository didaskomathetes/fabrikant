"""Tests: Run-Historie (`runsearch`) — Filter, Freitext, Datumsgrenzen, Cursor-Paginierung, API, Performance.

Die Vertragstests laufen gegen beide Run-Store-Backends (Fixture `idx`); Tests zur JSON-Ablage
(Verzeichnis = Projekt, gleiche Id in zwei Projekten) nur gegen JSON (`json_idx`)."""

import base64
import json
import statistics
import time
import urllib.error
import urllib.request

import pytest
from helpers import not_none

from fabrik import config, runsearch
from fabrik.runstore import STORE


@pytest.fixture
def idx(store_backend, runs_dir):
    """Run-Verzeichnis; das aktive Backend (JSON | SQL) parametrisiert `store_backend`."""
    return runs_dir


@pytest.fixture
def json_idx(runs_dir):
    return runs_dir


def _rec(i, project="p", loop="lint", status="pass", day=1, **kw):
    return {
        "id": f"2026010{day}T{i:06d}Z-{loop}",
        "project": project,
        "loop": loop,
        "trigger": kw.pop("trigger", "manual"),
        "status": status,
        "started": f"2026-01-0{day}T00:{i // 60:02d}:{i % 60:02d}+00:00",
        **kw,
    }


def _save(runs_dir, rec):
    """Record ablegen wie das aktive Backend: JSON als Datei (inkl. Altbestand), SQL über den Store
    (ohne Übergangsprüfung — Testdaten setzen Status direkt)."""
    if config.run_store_backend() == config.RUN_STORE_SQL:
        STORE.backend()._modify(rec["project"], rec["id"], lambda _current: dict(rec))
        return
    d = (runs_dir or config.RUNS_DIR) / rec["project"]
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rec['id']}.json").write_text(json.dumps(rec))


def _save_all(recs):
    for rec in recs:
        _save(None, rec)


def _ids(res):
    return [r["id"] for r in res["runs"]]


def test_written_run_record_is_found(idx):
    rec = _rec(1, note="Hallo Welt")
    _save(idx, rec)
    assert _ids(runsearch.search_runs("p")) == [rec["id"]]


def test_search_row_carries_the_run_verdict(idx):
    step = {"name": "audit-publish", "type": "shell", "status": "pass", "verdict": "fail"}
    _save(idx, _rec(1, status="pass", steps=[step]))
    _save(idx, _rec(2, status="pass"))
    rows = {r["id"]: r for r in runsearch.search_runs("p")["runs"]}
    assert sorted(r["verdict"] for r in rows.values()) == ["", "fail"]


def test_updated_run_record_is_reflected_in_search(idx):
    rec = _rec(1, status="running")
    _save(idx, rec)
    _save(idx, {**rec, "status": "needs_approval"})
    res = runsearch.search_runs("p")
    assert [r["status"] for r in res["runs"]] == ["needs_approval"]
    assert _ids(runsearch.search_runs("p", q="running")) == []
    assert _ids(runsearch.search_runs("p", q="needs_approval")) == [rec["id"]]


def test_filters_status_loop_project(idx):
    _save(idx, _rec(1, status="fail"))
    _save(idx, _rec(2, status="pass", loop="test"))
    _save(idx, _rec(3, status="needs_approval", loop="test"))
    _save(idx, _rec(4, project="other"))
    assert len(runsearch.search_runs("p")["runs"]) == 3
    assert len(runsearch.search_runs()["runs"]) == 4
    assert [r["status"] for r in runsearch.search_runs("p", status="fail,needs_approval")["runs"]] == [
        "needs_approval",
        "fail",
    ]
    assert len(runsearch.search_runs("p", loop="test")["runs"]) == 2
    assert len(runsearch.search_runs("p", loop="test", status="pass")["runs"]) == 1


def test_text_search_covers_note_trigger_step_summaries(idx):
    _save(idx, _rec(1, note="Hotfix für Login"))
    _save(idx, _rec(2, trigger="cron"))
    rec3 = _rec(
        3,
        steps=[{"name": "check", "type": "shell", "status": "fail", "summary": "boom: ConnectionRefusedError im Step"}],
    )
    _save(idx, rec3)
    assert _ids(runsearch.search_runs("p", q="login")) == [_rec(1)["id"]]
    assert _ids(runsearch.search_runs("p", q="cron")) == [_rec(2)["id"]]
    assert _ids(runsearch.search_runs("p", q="ConnectionRefused")) == [rec3["id"]]  # Teilstring
    assert _ids(runsearch.search_runs("p", q="boom Step")) == [rec3["id"]]  # AND
    assert _ids(runsearch.search_runs("p", q="refused")) == [rec3["id"]]  # case-insensitiv
    assert runsearch.search_runs("p", q="nichtvorhanden")["runs"] == []


def test_log_text_is_not_searched(idx):
    rec = _rec(1)
    _save(idx, rec)
    (idx / "p").mkdir(exist_ok=True)
    (idx / "p" / f"{rec['id']}.log").write_text("nur-im-log\n")
    assert runsearch.search_runs("p", q="nur-im-log")["runs"] == []


def test_search_query_syntax_is_neutralised(idx):
    _save(idx, _rec(1))
    assert runsearch.search_runs("p", q='" OR * NEAR(')["runs"] == []
    assert len(runsearch.search_runs("p", q="   ")["runs"]) == 1  # nur Whitespace = kein Filter


def test_date_range(idx):
    for day in (1, 2, 3):
        _save(idx, _rec(day, day=day))
    assert len(runsearch.search_runs("p", date_from="2026-01-02")["runs"]) == 2
    assert len(runsearch.search_runs("p", date_to="2026-01-02")["runs"]) == 2  # inklusive
    assert len(runsearch.search_runs("p", date_from="2026-01-02", date_to="2026-01-02")["runs"]) == 1
    assert len(runsearch.search_runs("p", date_to="2026-01-02T00:00:00")["runs"]) == 1


def test_date_filter_respects_offsets(idx):
    # 23:30+02:00 = 21:30 UTC, also vor 22:00 UTC (lexikografisch käme es danach)
    _save(idx, _rec(1, started="2026-01-01T23:30:00+02:00"))
    assert len(runsearch.search_runs("p", date_to="2026-01-01T22:00:00+00:00")["runs"]) == 1
    assert runsearch.search_runs("p", date_from="2026-01-01T22:00:00+00:00")["runs"] == []
    assert len(runsearch.search_runs("p", date_from="2026-01-01T21:00:00+00:00")["runs"]) == 1
    assert runsearch.search_runs("p", date_to="2026-01-01T23:00:00+03:00")["runs"] == []  # = 20:00 UTC


def test_pure_date_bounds_use_utc_consistently(idx):
    # 2026-01-01T23:30-05:00 = 2026-01-02T04:30 UTC → liegt am 2. Januar, nicht am 1.
    _save(idx, _rec(1, started="2026-01-01T23:30:00-05:00"))
    assert runsearch.search_runs("p", date_from="2026-01-02", date_to="2026-01-01")["runs"] == []
    assert len(runsearch.search_runs("p", date_from="2026-01-02", date_to="2026-01-02")["runs"]) == 1
    assert runsearch.search_runs("p", date_to="2026-01-01")["runs"] == []


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()


@pytest.mark.parametrize(
    "kw",
    [
        {"date_from": "gestern"},
        {"date_to": "2026-13"},
        {"cursor": "%%%"},
        {"date_from": "2026-01-01T99:99"},
        {"date_from": "2026-01-01T25:00:00"},
        {"cursor": _b64([1, 2, 3])},
        {"cursor": _b64([None, None, None])},
        {"cursor": _b64(["a", "b"])},
        {"cursor": _b64({"a": "b"})},
    ],
)
def test_invalid_params_raise(idx, kw):
    with pytest.raises(ValueError):
        runsearch.search_runs("p", **kw)


def test_cursor_pagination_covers_all_runs_once(idx):
    recs = [_rec(i, day=1 + i % 3) for i in range(25)]
    _save_all(recs)
    seen, cursor = [], ""
    while True:
        res = runsearch.search_runs("p", cursor=cursor, limit=7)
        seen += _ids(res)
        cursor = res["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 25
    expected = [r["id"] for r in sorted(recs, key=lambda r: (r["started"], r["id"]), reverse=True)]
    assert seen == expected


def test_same_started_ties_are_stable(idx):
    recs = [{**_rec(i), "started": "2026-01-01T00:00:00+00:00", "id": f"r{i}"} for i in range(5)]
    _save_all(recs)
    first = runsearch.search_runs("p", limit=2)
    second = runsearch.search_runs("p", limit=2, cursor=not_none(first["next_cursor"]))
    third = runsearch.search_runs("p", limit=2, cursor=not_none(second["next_cursor"]))
    assert _ids(first) + _ids(second) + _ids(third) == ["r4", "r3", "r2", "r1", "r0"]
    assert third["next_cursor"] is None


def test_limit_is_clamped(idx):
    _save_all([_rec(i) for i in range(3)])
    assert len(runsearch.search_runs("p", limit=0)["runs"]) == 1
    assert len(runsearch.search_runs("p", limit=10_000)["runs"]) == 3


def test_directory_wins_over_record_project(json_idx):
    idx = json_idx
    rec = _rec(1, project="project-b")
    d = idx / "project-a"
    d.mkdir()
    (d / f"{rec['id']}.json").write_text(json.dumps(rec))
    assert _ids(runsearch.search_runs("project-a")) == [rec["id"]]
    assert _ids(runsearch.search_runs("project-b")) == []


def test_project_inferred_from_directory(json_idx):
    idx = json_idx
    d = idx / "dirproj"
    d.mkdir()
    rec = _rec(1)
    del rec["project"]
    (d / f"{rec['id']}.json").write_text(json.dumps(rec))
    assert _ids(runsearch.search_runs("dirproj")) == [rec["id"]]


def test_unknown_status_is_excluded_from_search(json_idx):
    """Record mit unbekanntem Status ist nicht im Modell darstellbar → aus der Suche
    ausgeschlossen (nicht nur Status-Filter-Verfehlung), nur über load_report sichtbar
    (ADR 0034, tolerant laden). Nur JSON: der SQL-Store erzwingt den Status-Constraint."""
    good = _rec(1, status="pass")
    bad = _rec(2, status="weird_status")
    _save(json_idx, good)
    _save(json_idx, bad)
    assert _ids(runsearch.search_runs("p")) == [good["id"]]
    assert _ids(runsearch.search_runs("p", status="weird_status")) == []
    loaded, report = STORE.load_report("p")
    assert [r.id for r in loaded] == [good["id"]]
    assert len(report.failures) == 1 and "unbekannter Status" in report.failures[0].reason


def test_same_run_id_in_two_projects_is_kept_separate(json_idx):
    idx = json_idx
    a, b = _rec(1, project="a", note="alpha"), _rec(1, project="b", note="beta")
    assert a["id"] == b["id"]
    _save(idx, a)
    _save(idx, b)
    _save(idx, {**a, "status": "fail"})  # Update betrifft nur Projekt a
    assert [(r["project"], r["status"]) for r in runsearch.search_runs()["runs"]] == [("b", "pass"), ("a", "fail")]
    assert _ids(runsearch.search_runs(q="beta")) == [b["id"]]
    first = runsearch.search_runs(limit=1)
    second = runsearch.search_runs(limit=1, cursor=not_none(first["next_cursor"]))
    assert [r["project"] for r in first["runs"] + second["runs"]] == ["b", "a"]


# ---------------------------------------------------------------- API
@pytest.fixture
def api(runs_dir, live_server):
    base = live_server()

    def get(path):
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    return get


def test_api_search_mode_and_pagination(idx, api):
    for i in range(5):
        _save(idx, _rec(i, status="needs_approval" if i == 0 else "pass"))
    code, body = api("/api/runs?project=p&q=&limit=2")
    assert code == 200 and len(body["runs"]) == 2 and body["next_cursor"]
    code, body2 = api(f"/api/runs?project=p&limit=10&cursor={body['next_cursor']}")
    assert len(body2["runs"]) == 3 and body2["next_cursor"] is None
    code, body = api("/api/runs?project=p&status=needs_approval")
    assert [r["status"] for r in body["runs"]] == ["needs_approval"]
    code, body = api("/api/runs?project=p&from=2026-01-01&to=2026-01-01&loop=lint&q=manual")
    assert len(body["runs"]) == 5


def test_api_legacy_list_without_search_params(idx, api):
    _save(idx, _rec(1))
    code, body = api("/api/runs?project=p&limit=5")
    assert code == 200 and isinstance(body, list) and len(body) == 1


def test_api_legacy_bad_limit_400(api):
    code, body = api("/api/runs?project=p&limit=abc")
    assert code == 400 and "error" in body


@pytest.mark.parametrize("qs", ["limit=abc", "from=bogus", "cursor=%25%25", "to=2026-99"])
def test_api_bad_params_400(api, qs):
    code, body = api(f"/api/runs?project=p&{qs}")
    assert code == 400 and "error" in body


# ---------------------------------------------------------------- Performance
# Datei-basierte Suche liest alle Records (O(n)): geprüft wird die heutige Größenordnung
# (~2 300 Runs). Das 10k-Ziel gilt für die Suche auf den Run-Tabellen (ADR 0034, #277).
PERF_RUNS_JSON = 3_000
PERF_RUNS_SQL = 10_000
PERF_BUDGET_S = 0.2
PERF_STEPS_PER_RUN = 3
# Median über mehrere Läufe: ein einzelner Scheduler-/IO-Ausreißer des Runners ist kein Regressionssignal.
PERF_REPEATS = 5
_PERF_QUERIES = (
    {},
    {"q": "deploy"},
    {"q": "routine", "status": "pass", "loop": "lint", "date_from": "2026-01-02", "date_to": "2026-01-05"},
    {"status": "needs_approval"},
    {"q": "summary-7"},  # trifft nur Step-Summaries
    {"status": "fail", "failure_kind": "transient"},
)


def _perf_records(n):
    loops = ["lint", "test", "develop", "review"]
    return [
        {
            **_rec(i % 3600, loop=loops[i % 4], status=("needs_approval" if i % 500 == 0 else "pass"), day=1 + i % 9),
            "id": f"run-{i:05d}",
            "note": f"Notiz {i} {'deploy' if i % 97 == 0 else 'routine'}",
            "steps": [
                {"name": f"s{j}", "type": "shell", "status": "pass", "summary": f"summary-{(i * 3 + j) % 1000}"}
                for j in range(PERF_STEPS_PER_RUN)
            ],
            **({"status": "fail", "failure_kind": "transient"} if i % 250 == 1 else {}),
        }
        for i in range(n)
    ]


def _timed_search(kw) -> float:
    t = time.perf_counter()
    res = runsearch.search_runs("p", **kw)
    dt = time.perf_counter() - t
    assert res["runs"], kw
    return dt


def _assert_queries_within_budget():
    """Budget gilt für den laufenden Server (warm): der erste Scan nach dem Start parst alle
    JSON-Records (O(n), einmalig) und wird daher vorab ungemessen ausgeführt."""
    runsearch.search_runs("p")
    for kw in _PERF_QUERIES:
        dt = statistics.median(_timed_search(kw) for _ in range(PERF_REPEATS))
        assert dt < PERF_BUDGET_S, (kw, dt)


@pytest.mark.perf
def test_search_under_200ms_with_3k_runs(json_idx):
    _save_all(_perf_records(PERF_RUNS_JSON))
    _assert_queries_within_budget()


@pytest.mark.perf
def test_sql_search_under_200ms_with_10k_runs(runs_dir, monkeypatch):
    """Indiziertes SELECT auf den Run-Tabellen: < 200 ms bei 10 000 Runs (ADR 0034, #277)."""
    from fabrik.runstore_sql import encode_record

    monkeypatch.setenv(config.RUN_STORE_ENV, config.RUN_STORE_SQL)
    store = STORE.backend()
    with store.write_tx() as conn:  # Bulk-Seed in einer Transaktion
        for rec in _perf_records(PERF_RUNS_SQL):
            store._save(conn, encode_record(rec, "p", rec["id"]), exists=False)
    assert len(store.load_records("p")) == PERF_RUNS_SQL
    _assert_queries_within_budget()


def test_api_run_resolves_by_project_for_duplicate_ids(json_idx, api):
    idx = json_idx
    for proj in ("p1", "p2"):
        d = idx / proj
        d.mkdir()
        (d / "r1.json").write_text(json.dumps({**_rec(1), "id": "r1", "project": proj}))
    for proj in ("p1", "p2"):
        code, body = api(f"/api/run/r1?project={proj}")
        assert code == 200 and body["project"] == proj
    code, _ = api("/api/run/r1?project=nope")
    assert code == 200  # unbekanntes Projekt → Fallback auf Suche über alle Projekte


def test_cursor_roundtrip():
    cur = runsearch.encode_cursor("2026-01-02T03:04:05+00:00", "proj", "run-1")
    assert runsearch.decode_cursor(cur) == ("2026-01-02T03:04:05+00:00", "proj", "run-1")
