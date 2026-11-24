"""runs: claim_run, Vorrang-Warter, runs_dir_for, list_runs, last_run_per_loop, parse_runs, select_pending_approvals, get_run, get_run_log, _valid_run_id."""

from __future__ import annotations

import json
from typing import Any

import pytest

from fabrikant import runs


def _write(runs_dir, project, rid, status="pass", loop="l", log=None):
    d = runs_dir / project
    d.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {
        "id": rid,
        "project": project,
        "loop": loop,
        "status": status,
        "started": "2024-01-01T00:00:00Z",
    }
    (d / f"{rid}.json").write_text(json.dumps(rec))
    if log is not None:
        (d / f"{rid}.log").write_text(log)
    return rec


# ---------------------------------------------------------------- _valid_run_id
def test_valid_run_id():
    assert runs._valid_run_id("20240101T000000Z-abc-l") is True
    assert runs._valid_run_id("") is False
    assert runs._valid_run_id("a/b") is False
    assert runs._valid_run_id("a\\b") is False
    assert runs._valid_run_id("..") is False


# ---------------------------------------------------------------- runs_dir_for


def test_claim_run_yields_to_priority_waiter(runs_dir):
    """Wartende Freigabe hat Vorrang: ein neuer Claim bekommt den FREIEN Slot nicht (ADR 0015)."""
    runs.mark_priority_waiter("acq_pending", +1)
    try:
        assert runs.has_priority_waiter("acq_pending")
        assert runs.claim_run("acq_pending", "l") is None
        other = runs.claim_run("acq_other", "l")
        assert other is not None, "andere Projekte unbeeinflusst"
        other.release()
    finally:
        runs.mark_priority_waiter("acq_pending", -1)
    claim = runs.claim_run("acq_pending", "l")
    assert claim is not None
    claim.release()


def test_mark_priority_waiter_counts_and_cleans_up():
    runs.mark_priority_waiter("cnt", +1)
    runs.mark_priority_waiter("cnt", +1)
    runs.mark_priority_waiter("cnt", -1)
    assert runs.has_priority_waiter("cnt")
    runs.mark_priority_waiter("cnt", -1)
    assert not runs.has_priority_waiter("cnt")
    runs.mark_priority_waiter("cnt", -1)  # Unterlauf → kein negativer Zähler
    assert not runs.has_priority_waiter("cnt")


def test_runs_dir_for(runs_dir):
    d = runs.runs_dir_for("p")
    assert d.is_dir()
    assert d == runs_dir / "p"


# ---------------------------------------------------------------- list_runs
def test_list_runs_all(runs_dir):
    _write(runs_dir, "p", "r1")
    _write(runs_dir, "q", "r2")
    out = runs.list_runs()
    assert {r["id"] for r in out} == {"r1", "r2"}


def test_list_runs_filter_project(runs_dir):
    _write(runs_dir, "p", "r1")
    _write(runs_dir, "q", "r2")
    out = runs.list_runs("p")
    assert [r["id"] for r in out] == ["r1"]


def test_list_runs_limit(runs_dir):
    for i in range(5):
        _write(runs_dir, "p", f"r{i}")
    out = runs.list_runs("p", limit=3)
    assert len(out) == 3


def test_list_runs_skips_corrupt(runs_dir):
    _write(runs_dir, "p", "r1")
    d = runs_dir / "p"
    (d / "bad.json").write_text("{not json")
    out = runs.list_runs("p")
    assert [r["id"] for r in out] == ["r1"]


@pytest.mark.parametrize("payload", ["[]", "null", '"str"', "42"])
def test_list_runs_skips_non_object_json(runs_dir, payload):
    _write(runs_dir, "p", "r1")
    (runs_dir / "p" / "bad.json").write_text(payload)
    assert [r["id"] for r in runs.list_runs("p")] == ["r1"]
    assert runs.pending_approvals("p") == []


def test_last_run_per_loop_sees_runs_beyond_recent_window(runs_dir):
    """Ein seltener Loop bleibt sichtbar, auch wenn viele neuere Runs anderer
    Loops folgen (früher: nur die 20 neuesten → "nie gelaufen")."""
    old = _write(runs_dir, "p", "r-old", status="fail", loop="rare")
    old["finished"] = "2024-01-01T00:01:00Z"
    (runs_dir / "p" / "r-old.json").write_text(json.dumps(old))
    for i in range(30):
        rec = _write(runs_dir, "p", f"r-{i:02d}", loop="busy")
        rec["started"] = f"2024-02-01T00:00:{i:02d}Z"
        (runs_dir / "p" / f"r-{i:02d}.json").write_text(json.dumps(rec))
    last = runs.last_run_per_loop(runs.load_runs("p"))
    assert last["rare"] == {
        "status": "fail",
        "finished": "2024-01-01T00:01:00Z",
        "id": "r-old",
        "failure_kind": "unknown",  # Altbestand ohne Feld (ADR 0035)
    }
    assert last["busy"]["id"] == "r-29"


def test_last_run_per_loop_skips_records_without_loop(runs_dir):
    rec = _write(runs_dir, "p", "r-noloop")
    del rec["loop"]
    (runs_dir / "p" / "r-noloop.json").write_text(json.dumps(rec))
    assert runs.last_run_per_loop(runs.load_runs("p")) == {}


def test_last_run_per_loop_ignores_records_without_valid_id(runs_dir):
    """Ein neueres, aber id-loses Record verdrängt den letzten gültigen Run nicht."""
    _write(runs_dir, "p", "r-ok", loop="lint")
    (runs_dir / "p" / "broken.json").write_text(
        json.dumps({"loop": "lint", "status": "pass", "started": "2099-01-01T00:00:00Z"})
    )
    (runs_dir / "p" / "badid.json").write_text(
        json.dumps({"id": 7, "loop": "lint", "status": "pass", "started": "2099-01-01T00:00:00Z"})
    )
    assert runs.last_run_per_loop(runs.load_runs("p"))["lint"]["id"] == "r-ok"


def test_last_run_per_loop_picks_newest_regardless_of_order():
    recs = [
        {"id": "a", "loop": "l", "status": "pass", "started": "2024-01-02"},
        {"id": "b", "loop": "l", "status": "fail", "started": "2024-01-03"},
        {"id": "c", "loop": "l", "status": "pass", "started": "2024-01-01"},
        {"id": "d", "loop": "l", "status": "pass", "started": None},
    ]
    assert runs.last_run_per_loop(runs.parse_runs(recs, "p"))["l"]["id"] == "b"


def test_parse_runs_skips_unloadable_records():
    recs = [{"id": "a", "status": "pass"}, {"id": "", "status": "pass"}, {"id": "b", "status": 5}]
    assert [r.id for r in runs.parse_runs(recs, "p")] == ["a"]


def test_select_pending_approvals_filters_and_sorts():
    recs = [
        {"id": "a", "status": "needs_approval", "started": "2024-01-01"},
        {"id": "b", "status": "pass", "started": "2024-01-05"},
        {"id": "c", "status": "needs_approval", "started": "2024-01-03"},
        {"id": "", "status": "needs_approval", "started": "2024-01-09"},
    ]
    assert [r["id"] for r in runs.select_pending_approvals(recs)] == ["c", "a"]


def test_list_runs_sorted_by_started(runs_dir):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "old.json").write_text(json.dumps({"id": "old", "started": "2020-01-01"}))
    (d / "new.json").write_text(json.dumps({"id": "new", "started": "2024-01-01"}))
    out = runs.list_runs("p")
    assert [r["id"] for r in out] == ["new", "old"]


# ---------------------------------------------------------------- pending_approvals
def test_pending_approvals_filters_needs_approval(runs_dir):
    _write(runs_dir, "p", "r1", status="pass")
    _write(runs_dir, "p", "r2", status="needs_approval")
    _write(runs_dir, "p", "r3", status="running")
    out = runs.pending_approvals("p")
    assert [r["id"] for r in out] == ["r2"]


def test_pending_approvals_all_projects(runs_dir):
    _write(runs_dir, "p", "r1", status="needs_approval")
    _write(runs_dir, "q", "r2", status="needs_approval")
    _write(runs_dir, "q", "r3", status="pass")
    out = runs.pending_approvals()
    assert {r["id"] for r in out} == {"r1", "r2"}


def test_pending_approvals_sorted_newest_first(runs_dir):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "old.json").write_text(json.dumps({"id": "old", "status": "needs_approval", "started": "2020-01-01"}))
    (d / "new.json").write_text(json.dumps({"id": "new", "status": "needs_approval", "started": "2024-01-01"}))
    out = runs.pending_approvals("p")
    assert [r["id"] for r in out] == ["new", "old"]


def test_pending_approvals_skips_corrupt_and_strips_steps_output(runs_dir):
    _write(runs_dir, "p", "r1", status="needs_approval")
    d = runs_dir / "p"
    (d / "bad.json").write_text("{not json")
    (d / "r2.json").write_text(json.dumps({"id": "r2", "status": "needs_approval", "steps_output": "x" * 100}))
    out = runs.pending_approvals("p")
    assert {r["id"] for r in out} == {"r1", "r2"}
    assert all("steps_output" not in r for r in out)


def test_pending_approvals_tolerates_non_string_started(runs_dir):
    _write(runs_dir, "p", "r1", status="needs_approval")
    (runs_dir / "p" / "r2.json").write_text(json.dumps({"id": "r2", "status": "needs_approval", "started": 42}))
    assert {r["id"] for r in runs.pending_approvals("p")} == {"r1", "r2"}
    assert {r["id"] for r in runs.list_runs("p")} == {"r1", "r2"}


def test_load_records_propagates_unexpected_io_errors(runs_dir, monkeypatch):
    _write(runs_dir, "p", "r1", status="needs_approval")

    def boom(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(runs.Path, "read_text", boom)
    with pytest.raises(PermissionError):
        runs.pending_approvals("p")


def test_pending_approvals_empty(runs_dir):
    _write(runs_dir, "p", "r1", status="pass")
    assert runs.pending_approvals("p") == []
    assert runs.pending_approvals("keiner") == []


# ---------------------------------------------------------------- get_run
def test_get_run_by_project(runs_dir):
    _write(runs_dir, "p", "r1")
    assert runs.get_run("r1", "p")["id"] == "r1"
    assert runs.get_run("r1")["id"] == "r1"  # ohne Projekt → Suche


def test_get_run_missing(runs_dir):
    assert runs.get_run("nope") is None


def test_get_run_invalid_id(runs_dir):
    assert runs.get_run("../etc") is None


# ---------------------------------------------------------------- get_run_log
def test_get_run_log(runs_dir):
    _write(runs_dir, "p", "r1", log="line1\nline2")
    assert runs.get_run_log("r1", "p") == "line1\nline2"
    assert runs.get_run_log("r1") == "line1\nline2"


def test_get_run_log_missing(runs_dir):
    _write(runs_dir, "p", "r1")  # kein .log
    assert runs.get_run_log("r1", "p") is None
    assert runs.get_run_log("nope") is None


def test_get_run_log_invalid_id(runs_dir):
    assert runs.get_run_log("..") is None


# ---------------------------------------------------------------- List/Get ignorieren .tmp-Rückstände
def test_list_get_ignore_stray_tmp(runs_dir):
    """Eine verwaiste .tmp (Crash-Rückstand) stört List/Get nicht."""
    _write(runs_dir, "p", "r1")
    (runs_dir / "p" / "r1.json.tmp").write_text("{partial")
    assert [r["id"] for r in runs.list_runs("p")] == ["r1"]
    assert runs.get_run("r1", "p")["status"] == "pass"


def test_claim_run_registered_waiter_is_consumed(runs_dir):
    """registered=True: der synchron eingetragene Vorrang-Warter wird nach dem Claim abgebaut."""
    runs.mark_priority_waiter("reg", +1)
    claim = runs.claim_run("reg", "l", blocking=True, registered=True)
    assert claim is not None
    assert not runs.has_priority_waiter("reg")
    claim.release()


def test_pending_approvals_skips_records_without_valid_id(runs_dir):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "noid.json").write_text(json.dumps({"status": "needs_approval", "started": "2024-01-01"}))
    (d / "badid.json").write_text(json.dumps({"id": 5, "status": "needs_approval"}))
    (d / "ok.json").write_text(json.dumps({"id": "ok", "status": "needs_approval"}))
    assert [r["id"] for r in runs.pending_approvals("p")] == ["ok"]


# ---------------------------------------------------------------- Projekt-Traversal
def test_valid_project():
    assert runs._valid_project("p") is True
    for bad in ("", ".", "..", "../x", "a/b", "a\\b", "/etc"):
        assert runs._valid_project(bad) is False


def test_project_traversal_rejected(runs_dir):
    _write(runs_dir, "p", "r1", log="x")
    outside = runs_dir.parent / "secret"
    outside.mkdir(exist_ok=True)
    (outside / "r1.json").write_text(json.dumps({"id": "r1"}))
    (outside / "r1.log").write_text("geheim")
    for bad in ("../secret", str(outside), "..", "a/b"):
        assert runs.get_run("r1", bad) is None
        assert runs.get_run_log("r1", bad) is None
        assert runs.list_runs(bad) == []


@pytest.mark.parametrize("bad_loop", [["a"], {"a": 1}, 7, "", None])
def test_last_run_per_loop_skips_non_string_loop(runs_dir, bad_loop):
    """Beschädigte `loop`-Werte (unhashbar/numerisch/leer) kippen die Projektion nicht."""
    rec = _write(runs_dir, "p", "r-bad")
    rec["loop"] = bad_loop
    (runs_dir / "p" / "r-bad.json").write_text(json.dumps(rec))
    _write(runs_dir, "p", "r-ok", loop="ok")
    assert list(runs.last_run_per_loop(runs.load_runs("p"))) == ["ok"]


def test_last_run_per_loop_skips_unloadable_and_normalizes_fields(runs_dir):
    """Unbekannter Status → Record nicht ladbar (kein "null"-Badge); beschädigtes `finished` → ""."""
    bad = _write(runs_dir, "p", "r-bad", loop="l")
    bad.update(status=5, started="2099-01-01T00:00:00Z")
    (runs_dir / "p" / "r-bad.json").write_text(json.dumps(bad))
    ok = _write(runs_dir, "p", "r-ok", loop="l")
    ok["finished"] = ["x"]
    (runs_dir / "p" / "r-ok.json").write_text(json.dumps(ok))
    assert runs.last_run_per_loop(runs.load_runs("p"))["l"] == {
        "status": "pass",
        "finished": "",
        "id": "r-ok",
        "failure_kind": "",
    }
