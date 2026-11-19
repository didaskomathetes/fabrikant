"""SQL-Run-Store (ADR 0034, #277): Schema/Migration, SQLite-Pragmas, DB-Constraints (je ein Test),
strikte Record-Abbildung, Session-Log im Store, Backend-Auswahl.

Die backend-neutrale Store-Suite (Operationen, Vorbedingungen, Round-Trip) liegt in test_runstore.py."""

import json

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import insert, inspect, text
from sqlalchemy.exc import IntegrityError

from fabrik import config, runs, sessionlog
from fabrik.runschema import (
    approval_permissions,
    approvals,
    metadata,
    review_findings,
    review_results,
    run_artifacts,
    run_checkpoints,
    run_events,
    run_vars,
    steps,
    verify_checks,
)
from fabrik.runschema import runs as runs_table
from fabrik.runstore import STORE, PreconditionError, RunStore, RunStoreError
from fabrik.runstore_sql import InvalidRecordError, SqlRunStore, make_engine, migrate, store_for_url


@pytest.fixture
def sql(monkeypatch, runs_dir) -> SqlRunStore:
    monkeypatch.setenv(config.RUN_STORE_ENV, config.RUN_STORE_SQL)
    store = STORE.backend()
    assert isinstance(store, SqlRunStore)
    return store


def _rec(**kw):
    return {"id": "r1", "loop": "l", "status": "running", "started": "2026-01-01T00:00:00+00:00", **kw}


# ---------------------------------------------------------------- Backend-Auswahl, Engine, Migration
def test_default_backend_is_sql_and_db_url_lives_in_state_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(config.RUN_STORE_ENV)
    assert config.run_store_backend() == config.RUN_STORE_SQL
    monkeypatch.setenv(config.RUN_STORE_ENV, config.RUN_STORE_JSON)
    assert isinstance(STORE.backend(), RunStore)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    assert config.db_url() == f"sqlite:///{tmp_path / 'state' / config.DB_FILENAME}"
    monkeypatch.setenv(config.DB_URL_ENV, "sqlite:///x.db")
    assert config.db_url() == "sqlite:///x.db"


def test_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv(config.RUN_STORE_ENV, "mongo")
    with pytest.raises(ValueError, match="FABRIK_RUN_STORE"):
        config.run_store_backend()


def test_sql_backend_uses_default_sqlite_file_in_state_dir(sql):
    assert sql.engine.url.database == str(config.STATE_DIR / config.DB_FILENAME)
    assert (config.STATE_DIR / config.DB_FILENAME).is_file()
    assert STORE.backend() is sql  # einmal je URL angelegt


def test_sqlite_pragmas(sql):
    with sql.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == config.SQLITE_BUSY_TIMEOUT_MS
        # lower() ist Unicode-fähig (Python), nicht nur ASCII wie SQLite-builtin
        assert conn.exec_driver_sql("SELECT lower('ÄÖÜ')").scalar() == "äöü"


def test_upgrade_head_on_empty_db_matches_schema(tmp_path):
    """Alembic `upgrade head` auf leerer DB erzeugt genau das Schema aus fabrik.runschema."""
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    migrate(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
        tables = set(inspect(conn).get_table_names())
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    engine.dispose()
    assert diff == []
    assert tables == set(metadata.tables) | {"alembic_version"}
    assert version == "0004"


def test_migrate_is_idempotent(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    migrate(engine)
    migrate(engine)
    engine.dispose()


def test_unusable_db_url_raises_store_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises((RunStoreError, OSError)):
        store_for_url(f"sqlite:///{blocker / 'sub' / 'db.sqlite'}")


# ---------------------------------------------------------------- DB-Constraints (je Constraint ein Test)
def _seed_run(conn, run_id="r1", status="running"):
    conn.execute(insert(runs_table).values(id=run_id, project="p", loop="l", status=status))


def _seed_step(conn, status="pass"):
    _seed_run(conn)
    conn.execute(insert(steps).values(run_id="r1", idx=0, name="a", type="shell", status=status))


CONSTRAINT_CASES = {
    "run_status": lambda c: c.execute(insert(runs_table).values(id="r", project="p", loop="l", status="weird")),
    "run_id_empty": lambda c: c.execute(insert(runs_table).values(id="", project="p", loop="l", status="pass")),
    "run_project_empty": lambda c: c.execute(insert(runs_table).values(id="r", project="", loop="l", status="pass")),
    "run_loop_required": lambda c: c.execute(insert(runs_table).values(id="r", project="p", status="pass")),
    "run_status_required": lambda c: c.execute(insert(runs_table).values(id="r", project="p", loop="l")),
    "run_failure_kind_value": lambda c: c.execute(
        insert(runs_table).values(id="r", project="p", loop="l", status="fail", failure_kind="bogus")
    ),
    "run_failure_kind_only_failed": lambda c: c.execute(
        insert(runs_table).values(id="r", project="p", loop="l", status="pass", failure_kind="outcome")
    ),
    "run_attempt_non_negative": lambda c: c.execute(
        insert(runs_table).values(id="r", project="p", loop="l", status="pass", attempt=-1)
    ),
    "run_id_unique": lambda c: (_seed_run(c), _seed_run(c)),
    "step_fk_run": lambda c: c.execute(insert(steps).values(run_id="nope", idx=0, name="a", type="t", status="pass")),
    "step_status": lambda c: _seed_step(c, status="weird"),
    "step_name_required": lambda c: (
        _seed_run(c),
        c.execute(insert(steps).values(run_id="r1", idx=0, type="t", status="pass")),
    ),
    "step_idx_non_negative": lambda c: (
        _seed_run(c),
        c.execute(insert(steps).values(run_id="r1", idx=-1, name="a", type="t", status="pass")),
    ),
    "step_failure_kind_only_failed": lambda c: (
        _seed_run(c),
        c.execute(insert(steps).values(run_id="r1", idx=0, name="a", type="t", status="pass", failure_kind="unknown")),
    ),
    "review_fk_step": lambda c: (
        _seed_run(c),
        c.execute(insert(review_results).values(run_id="r1", step_idx=5, verdict="approved")),
    ),
    "review_verdict_required": lambda c: (
        _seed_step(c),
        c.execute(insert(review_results).values(run_id="r1", step_idx=0)),
    ),
    "review_round_positive": lambda c: (
        _seed_step(c),
        c.execute(insert(review_results).values(run_id="r1", step_idx=0, verdict="approved", round=0)),
    ),
    "finding_fk_review": lambda c: (
        _seed_step(c),
        c.execute(insert(review_findings).values(run_id="r1", step_idx=0, seq=0, text="x")),
    ),
    "verify_name_required": lambda c: (
        _seed_step(c),
        c.execute(insert(verify_checks).values(run_id="r1", step_idx=0, seq=0, ok=True)),
    ),
    "approval_status": lambda c: (_seed_run(c), c.execute(insert(approvals).values(run_id="r1", status="maybe"))),
    "approval_reason": lambda c: (
        _seed_run(c),
        c.execute(insert(approvals).values(run_id="r1", status="pending", reason="bogus")),
    ),
    "approval_fk_run": lambda c: c.execute(insert(approvals).values(run_id="nope", status="pending")),
    "permission_fk_approval": lambda c: (
        _seed_run(c),
        c.execute(insert(approval_permissions).values(run_id="r1", seq=0, permission="x")),
    ),
    "var_scope": lambda c: (
        _seed_run(c),
        c.execute(insert(run_vars).values(run_id="r1", scope="x", name="a", type="str", value="v")),
    ),
    "var_type": lambda c: (
        _seed_run(c),
        c.execute(insert(run_vars).values(run_id="r1", scope="vars", name="a", type="list", value="v")),
    ),
    "var_fk_run": lambda c: c.execute(
        insert(run_vars).values(run_id="nope", scope="vars", name="a", type="str", value="")
    ),
    "event_fk_run": lambda c: c.execute(insert(run_events).values(run_id="nope", seq=1, ts="t", kind="k")),
    "event_kind_not_empty": lambda c: (
        _seed_run(c),
        c.execute(insert(run_events).values(run_id="r1", seq=1, ts="t", kind="")),
    ),
    "artifact_fk_run": lambda c: c.execute(
        insert(run_artifacts).values(run_id="nope", kind="report", path="p", created_at="t")
    ),
    "checkpoint_fk_run": lambda c: c.execute(
        insert(run_checkpoints).values(run_id="nope", step_idx=0, step_name="a", status="pass")
    ),
    "checkpoint_once_per_step": lambda c: (
        _seed_run(c),
        c.execute(insert(run_checkpoints).values(run_id="r1", step_idx=0, step_name="a", status="pass")),
        c.execute(insert(run_checkpoints).values(run_id="r1", step_idx=0, step_name="a", status="pass")),
    ),
}


@pytest.mark.parametrize("case", sorted(CONSTRAINT_CASES))
def test_db_rejects_invalid_state(sql, case):
    with pytest.raises(IntegrityError), sql.engine.begin() as conn:
        CONSTRAINT_CASES[case](conn)
    with sql.engine.connect() as conn:  # Transaktion zurückgerollt
        assert conn.execute(text("SELECT count(*) FROM runs")).scalar() == 0


def test_deleting_a_run_cascades_to_children(sql):
    sql.create("p", "r1", _rec(steps=[{"name": "a", "type": "shell", "status": "pass", "usage": {"model": "m"}}]))
    with sql.engine.begin() as conn:
        conn.execute(runs_table.delete())
        assert conn.execute(text("SELECT count(*) FROM steps")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM step_usage")).scalar() == 0


# ---------------------------------------------------------------- Strikte Record-Abbildung
@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (_rec(surprise=1), "unbekannte Felder surprise"),
        (_rec(note=5), "run.note: text erwartet"),
        (_rec(resumed="yes"), "run.resumed: bool erwartet"),
        (_rec(attempt=True), "run.attempt: int erwartet"),
        (_rec(vars={"a": [1]}), "vars.a: skalarer Wert"),
        (_rec(id="other"), "Record-id"),
        (_rec(steps=["kaputt"]), r"steps\[0\]: Objekt erwartet"),
        (_rec(steps=[{"name": "a", "type": "t", "status": "pass", "extra": 1}]), "unbekannte Felder extra"),
        (_rec(steps=[{"name": "a", "type": "t", "status": "pass", "usage": {"tokens": 1}}]), "usage: unbekannte"),
        (_rec(steps=[{"name": "a", "type": "t", "status": "pass", "review": {"verdict": "x", "pr": "abc"}}]), "pr"),
        (_rec(approval={"reason": "step", "permissions": [1]}), "permissions"),
        (_rec(status="weird"), "Status|CHECK"),
        (_rec(steps=[{"name": "a", "status": "pass"}]), "NOT NULL"),  # type fehlt
        (_rec(status="pass", failure_kind="outcome"), "CHECK"),  # failure_kind nur bei fail/error
    ],
)
def test_invalid_records_are_rejected_and_nothing_is_written(sql, record, reason):
    with pytest.raises(InvalidRecordError, match=reason):
        sql.create("p", "r1", record)
    assert sql.get_record("r1") is None


def test_invalid_update_leaves_stored_record_unchanged(sql):
    sql.create("p", "r1", _rec(note="keep"))
    with pytest.raises(InvalidRecordError):
        sql.write("p", "r1", _rec(note="new", bogus=True))
    assert sql.get_record("r1")["note"] == "keep"


def test_run_id_belongs_to_one_project(sql):
    sql.create("p", "r1", _rec())
    with pytest.raises(PreconditionError, match="gehört zu Projekt 'p'"):
        sql.write("q", "r1", _rec(), allow_missing=True)


def test_steps_output_and_foreign_finding_fields_are_dropped(sql):
    review = {"verdict": "approved", "pr": "7", "findings": [{"severity": "low", "text": "t", "file": "x.py"}, "raw"]}
    sql.create(
        "p", "r1", _rec(steps_output={"a": "b"}, steps=[{"name": "r", "type": "r", "status": "pass", "review": review}])
    )
    stored = sql.get_record("r1")
    assert "steps_output" not in stored
    assert stored["steps"][0]["review"] == {
        "verdict": "approved",
        "pr": 7,
        "findings": [{"severity": "low", "text": "t"}, {"text": "raw"}],
    }


def test_approval_status_is_stored_as_real_state(sql):
    sql.create("p", "r1", _rec(status="needs_approval", waiting_step="tier-approval", approval={"reason": "tier"}))
    sql.mark_approval_queued("p", "r1", sql.get_record("r1"), actor="ann")
    sql.reject("p", "r1", sql.get_record("r1"), actor="bob")
    with sql.engine.connect() as conn:
        rows = conn.execute(text("SELECT status, reason FROM approvals")).all()
    assert rows == [("rejected", "tier")]


def test_queued_approval_state(sql):
    sql.create("p", "r1", _rec(status="needs_approval", approval={"reason": "step"}))
    sql.mark_approval_queued("p", "r1", sql.get_record("r1"), actor="ann")
    with sql.engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM approvals")).scalar() == "queued"
    assert sql.get("r1").approval.status.value == "queued"  # type: ignore[union-attr]


# ---------------------------------------------------------------- Lesewege außerhalb des Stores (runs, scheduler)
def test_runs_readers_work_on_sql_backend(sql):
    sql.create("p", "a", _rec(id="a", status="needs_approval", trigger="on_issue:#1", vars={"issue_number": "1"}))
    sql.create("p", "b", _rec(id="b", status="pass", started="2026-01-02T00:00:00+00:00"))
    assert runs.get_run("a", "p")["status"] == "needs_approval"
    assert runs.get_run("a", "nope")["id"] == "a"  # Fallback über alle Projekte
    assert [r["id"] for r in runs.list_runs("p")] == ["b", "a"]
    assert [r["id"] for r in runs.pending_approvals()] == ["a"]
    assert runs.loop_run_triggers("p", "l") == {"on_issue:#1", ""}
    assert runs.paused_dedupe_keys("p", "l") == {"issue_number=1"}
    assert runs.find_run("b").status.value == "pass"  # type: ignore[union-attr]


def test_reap_orphaned_runs_on_sql_backend(sql, monkeypatch):
    from fabrik import retry_chains

    monkeypatch.setattr(retry_chains, "record_run_end", lambda *a, **k: None)
    monkeypatch.setattr(retry_chains, "release_stale_claims", lambda: None)
    monkeypatch.setattr("fabrik.specs.load_projects", lambda: {})
    sql.create("p", "a", _rec(id="a"))
    sql.create("p", "b", _rec(id="b", status="pass"))
    assert runs.reap_orphaned_runs() == ["a"]
    assert sql.get_record("a")["status"] == "error"


def test_stale_check_on_sql_backend(sql, monkeypatch):
    from fabrik import scheduler

    monkeypatch.setattr(scheduler.retry_chains, "on_run_finished_safe", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "_notify", lambda *a, **k: [])
    monkeypatch.setattr(scheduler, "load_projects", lambda: {})
    sql.create("p", "old", _rec(id="old", started="2020-01-01T00:00:00+00:00"))
    sql.create("p", "new", _rec(id="new", started=config.utcnow().isoformat()))
    scheduler.Scheduler.__new__(scheduler.Scheduler)._check_stale_runs()
    assert sql.get_record("old")["status"] == "error" and sql.get_record("new")["status"] == "running"


# ---------------------------------------------------------------- Session-Log im Store (ADR 0002)
def test_session_log_lives_in_the_store(sql):
    sql.create("p", "r1", _rec())
    log = sessionlog.SessionLog("r1", "p", "l", "manual")
    log.start()
    log.event(
        "step_done",
        step_name="a",
        step_type="llm",
        status="pass",
        usage={"prompt_tokens": 3, "completion_tokens": 4, "cost_usd": 0.5},
    )
    log.event("tool_call", step_name="a", tool={"name": "bash"})
    log.event("message", step_name="a", text="hallo")
    log.artifact("a", "report", "/tmp/x")
    log.checkpoint(0, "a", "pass")
    log.checkpoint(0, "a", "pass")  # Duplikat: append-only, still verschluckt (best-effort)
    log.set_status("needs_approval")  # Session-Status = Run-Status: nichts zu tun
    log.finish("pass")
    assert sessionlog.session_totals("r1") == {"tokens_in": 3, "tokens_out": 4, "cost_usd": 0.5}
    entries = sessionlog.transcript("r1")
    assert [(e.role, e.text or e.tool) for e in entries] == [("tool", {"name": "bash"}), ("assistant", "hallo")]
    assert sessionlog.transcript("r1", after=entries[0].id)[0].text == "hallo"
    with sql.engine.connect() as conn:
        kinds = [k for (k,) in conn.execute(text("SELECT kind FROM run_events ORDER BY seq"))]
        assert conn.execute(text("SELECT count(*) FROM run_checkpoints")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM run_artifacts")).scalar() == 1
    assert kinds == ["run_start", "step_done", "tool_call", "message", "run_end"]
    assert not sessionlog.db_path().exists()  # keine separate sessionlog.db


def test_session_log_resume_continues_sequence(sql):
    sql.create("p", "r1", _rec())
    sessionlog.SessionLog("r1", "p", "l").start()
    resumed = sessionlog.SessionLog("r1", "p", "l")
    resumed.start()
    with sql.engine.connect() as conn:
        assert [s for (s,) in conn.execute(text("SELECT seq FROM run_events ORDER BY id"))] == [1, 2]


def test_session_log_redacts_and_never_raises(sql, monkeypatch):
    monkeypatch.setattr(sessionlog, "_redact", lambda s: s.replace("geheim", "***"))
    sql.create("p", "r1", _rec())
    log = sessionlog.SessionLog("r1", "p", "l")
    log.event("message", text="geheim")
    sessionlog.SessionLog("unknown-run", "p", "l").event("message", text="x")  # FK verletzt → verschluckt
    sessionlog.finish_orphaned_run("r1", "2026-01-01")  # entfällt im SQL-Store
    with sql.engine.connect() as conn:
        assert json.loads(conn.execute(text("SELECT data FROM run_events")).scalar())["text"] == "***"


# ---------------------------------------------------------------- Engine Ende-zu-Ende auf dem SQL-Store
def _spec(projects_dir, name: str, steps_yaml: str) -> str:
    (projects_dir / f"{name}.fabrik.yaml").write_text(
        f"name: {name}\nroot: /tmp\nloops:\n  t:\n    steps:\n{steps_yaml}"
    )
    return name


def test_engine_pause_approve_resume_on_sql_store(sql, projects_dir):
    """run_loop → needs_approval → Approve → Resume: alles über den SQL-Store, keine Run-JSON."""
    from fabrik import engine

    name = _spec(
        projects_dir,
        "sqlflow",
        "      - {name: ok, type: shell, cmd: 'echo hi'}\n"
        "      - {name: gated, type: shell, cmd: 'echo {issue_number}', approval: true}\n",
    )
    rec = engine.run_loop(name, "t", trigger="on_issue:#42", initial_vars={"issue_number": "42"})
    assert rec["status"] == "needs_approval"
    stored = sql.get_record(rec["id"], name)
    assert stored["vars"]["issue_number"] == "42" and stored["approval"]["step"] == "gated"
    assert sql.get(rec["id"]).approval.status.value == "pending"  # type: ignore[union-attr]

    sql.approve(name, rec["id"], stored, actor="ann")
    done = engine.run_loop(name, "t", resume_id=rec["id"])
    assert done["status"] == "pass"
    final = sql.get_record(rec["id"], name)
    assert final["status"] == "pass" and final["resumed"] is True and final["approved_by"] == "ann"
    assert [(s["name"], s["status"]) for s in final["steps"]] == [("ok", "pass"), ("gated", "pass")]
    assert sql.get(rec["id"]).approval.status.value == "approved"  # type: ignore[union-attr]
    assert not list(config.RUNS_DIR.glob("*/*.json"))  # keine Run-JSON
    with sql.engine.connect() as conn:
        kinds = [k for (k,) in conn.execute(text("SELECT kind FROM run_events WHERE run_id = :r"), {"r": rec["id"]})]
    assert kinds.count("run_start") == 2 and kinds[-1] == "run_end"


def test_engine_failed_run_is_searchable_on_sql_store(sql, projects_dir):
    from fabrik import engine, runsearch

    name = _spec(projects_dir, "sqlfail", "      - {name: s1, type: shell, cmd: 'exit 127'}\n")
    rec = engine.run_loop(name, "t")
    assert rec["status"] == "fail"
    assert sql.get_record(rec["id"])["failure_kind"] == "environment"
    hits = runsearch.search_runs(name, status="fail", failure_kind="environment")["runs"]
    assert [h["id"] for h in hits] == [rec["id"]]


# ---------------------------------------------------------------- Kein Run-JSON außerhalb des Stores (ADR 0034, #276/#277)
RUN_JSON_PATTERNS = (r'glob\("\*\.json"\)', r'glob\("\*/\*\.json"\)', r"\{run_id\}\.json")


def test_no_module_outside_the_store_reads_run_json():
    import re
    from pathlib import Path

    src = Path(config.__file__).resolve().parent
    offenders = [
        f"{path.relative_to(src)}: {pattern}"
        for path in src.rglob("*.py")
        if path.name != "runstore.py"
        for pattern in RUN_JSON_PATTERNS
        if re.search(pattern, path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_step_verdict_round_trips_and_migration_keeps_child_rows(sql, tmp_path):
    step = {"name": "audit-publish", "type": "shell", "status": "pass", "verdict": "fail"}
    sql.create("p", "v", _rec(id="v", status="pass", steps=[step]))
    stored = runs.get_run("v", "p")
    assert stored["steps"][0]["verdict"] == "fail"
    assert runs.find_run("v").verdict == "fail"  # type: ignore[union-attr]

    # 0002 → 0003 auf einer DB mit Step + Kind-Zeile: ADD COLUMN darf nichts kaskadierend löschen.
    engine = make_engine(f"sqlite:///{tmp_path / 'up.db'}")
    migrate(engine, "0002")
    with engine.begin() as conn:
        _seed_step(conn)
        conn.execute(text("INSERT INTO step_usage (run_id, step_idx, model) VALUES ('r1', 0, 'm')"))
    migrate(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM step_usage").scalar() == 1
        assert conn.exec_driver_sql("SELECT verdict FROM steps").scalar() is None
    engine.dispose()


def test_escalation_approval_round_trips_and_migration_keeps_permissions(sql, tmp_path):
    """ADR 0059: reason=escalation + kind im SQL-Store; 0003 → 0004 verliert keine Freigabe-Rechte."""
    approval = {"reason": "escalation", "kind": "stalled", "why": "2 Runden ohne Änderung"}
    sql.create("p", "e", _rec(id="e", status="needs_approval", approval=approval))
    assert runs.get_run("e", "p")["approval"] == approval

    engine = make_engine(f"sqlite:///{tmp_path / 'up4.db'}")
    migrate(engine, "0003")
    with engine.begin() as conn:
        _seed_step(conn)
        conn.execute(text("INSERT INTO approvals (run_id, status, reason) VALUES ('r1', 'pending', 'gate')"))
        conn.execute(text("INSERT INTO approval_permissions (run_id, seq, permission) VALUES ('r1', 0, 'push')"))
    migrate(engine)
    with engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM approval_permissions").scalar() == 1
        conn.execute(text("UPDATE approvals SET reason = 'escalation', kind = 'blocked' WHERE run_id = 'r1'"))
    engine.dispose()
