"""runmigrate (#278): Run-JSON + sessionlog.db → SQL-Store — Probelauf ohne Schreiben, Idempotenz,
Report über nicht abbildbare Records, Session-Log-Übernahme, Archivierung, show/export, CLI."""

import json
import sqlite3

import pytest
from sqlalchemy import text

from fabrikant import cli, config, runs, sessionlog
from fabrikant.runmigrate import migrate_runs
from fabrikant.runstore_sql import store_for_url

FINDING = {"severity": "high", "text": "t", "suggestion": "fix it", "file": "x.py"}


def _rec(run_id, **kw):
    return {"id": run_id, "loop": "l", "status": "pass", "started": "2026-01-01T00:00:00+00:00", "steps": [], **kw}


def _save(runs_dir, project, rec):
    d = runs_dir / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rec['id']}.json").write_text(json.dumps(rec))


@pytest.fixture
def history(runs_dir, tmp_path, monkeypatch):
    """Zwei gültige Runs (einer mit Review + Session-Log), ein defekter, ein nicht abbildbarer, eine Dublette."""
    review = {"verdict": "changes_requested", "pr": 3, "round": 1, "findings": [FINDING]}
    _save(runs_dir, "p", _rec("a", steps=[{"name": "r", "type": "pr_review", "status": "pass", "review": review}]))
    _save(runs_dir, "p", _rec("b", status="needs_approval", approval={"reason": "step"}, vars={"n": 1}))
    (runs_dir / "p" / "broken.json").write_text("{")
    _save(runs_dir, "p", _rec("weird", surprise=True))
    _save(runs_dir, "q", _rec("a"))  # gleiche Id in anderem Projekt
    db = tmp_path / "sessionlog.db"
    monkeypatch.setenv("FABRIKANT_SESSIONLOG", str(db))
    log = sessionlog.SessionLog("a", "p", "l")
    log.start()
    log.event("step_done", step_name="r", usage={"prompt_tokens": 2, "completion_tokens": 1})
    log.checkpoint(0, "r", "pass")
    log.artifact("r", "report", "/tmp/r.md")
    log.finish("pass")
    sessionlog.SessionLog("ghost", "testproj", "l").start()  # Session ohne Run-Record
    sessionlog._conn.close()
    monkeypatch.setattr(sessionlog, "_conn", None)
    return db


def _url(tmp_path):
    return f"sqlite:///{tmp_path / 'target.db'}"


def _step_core(steps):
    """Core fields of the steps (LLM foreign fields like the review finding `file` are dropped by the SQL store)."""
    return [{k: s.get(k) for k in ("name", "type", "status")} for s in steps]


def test_dry_run_reports_without_writing(history, runs_dir, tmp_path):
    before = history.read_bytes()
    report = migrate_runs(runs_dir, history, _url(tmp_path), dry_run=True)
    assert not (tmp_path / "target.db").exists() and history.read_bytes() == before
    assert (report.runs_found, report.runs_imported) == (5, 2)
    reasons = {(i.run_id, i.reason.split(":")[0]) for i in report.issues}
    assert ("broken", "corrupt run record broken.json") in reasons
    assert ("weird", "run") in reasons  # unknown fields
    assert ("a", "Id schon in Projekt 'p'") in reasons
    assert report.sessions_imported == 1 and report.events_imported == 3 and report.checkpoints_imported == 1
    assert report.orphan_sessions == {"testproj": 1}
    assert not report.ok and "Dry run" in report.lines()[0]


def test_import_writes_runs_and_session_log_and_is_idempotent(history, runs_dir, tmp_path):
    first = migrate_runs(runs_dir, history, _url(tmp_path))
    store = store_for_url(_url(tmp_path))
    assert store.get_record("b")["vars"] == {"n": 1}
    finding = store.get_record("a")["steps"][0]["review"]["findings"][0]
    assert finding == {"severity": "high", "text": "t", "suggestion": "fix it"}  # LLM foreign field `file` is dropped
    with store.engine.connect() as conn:
        counts = [
            conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            for t in ("runs", "run_events", "run_checkpoints", "run_artifacts")
        ]
    assert counts == [2, 3, 1, 1] and first.runs_imported == 2

    second = migrate_runs(runs_dir, history, _url(tmp_path))
    assert (second.runs_imported, second.runs_existing, second.sessions_imported, second.sessions_existing) == (
        0,
        2,
        0,
        1,
    )
    with store.engine.connect() as conn:
        assert [
            conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            for t in ("runs", "run_events", "run_checkpoints", "run_artifacts")
        ] == counts


def test_clean_history_is_ok_and_typed_identical(runs_dir, tmp_path):
    _save(
        runs_dir,
        "p",
        _rec("a", status="fail", failure_kind="outcome", steps=[{"name": "s", "type": "shell", "status": "skipped"}]),
    )
    report = migrate_runs(runs_dir, tmp_path / "missing.db", _url(tmp_path))
    assert report.ok and report.runs_imported == 1 and report.sessions_imported == 0


def test_cli_migrate_runs(history, runs_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(config.DB_URL_ENV, _url(tmp_path))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["migrate-runs", "--dry-run"])
    out = capsys.readouterr().out
    assert exit_info.value.code == 1  # Probleme gefunden
    assert "Runs: 5 gefunden, 2 importiert" in out and "✖ broken" in out and "testproj (1)" in out


def test_existing_target_is_read_in_dry_run(runs_dir, tmp_path):
    _save(runs_dir, "p", _rec("a"))
    migrate_runs(runs_dir, tmp_path / "none.db", _url(tmp_path))
    _save(runs_dir, "p", _rec("c"))
    report = migrate_runs(runs_dir, tmp_path / "none.db", _url(tmp_path), dry_run=True)
    assert (report.runs_existing, report.runs_imported) == (1, 1)
    assert sqlite3.connect(tmp_path / "target.db").execute("SELECT count(*) FROM runs").fetchone() == (1,)


def test_legacy_import_runs_refuses_on_sql_backend(monkeypatch, capsys):
    monkeypatch.setenv(config.RUN_STORE_ENV, config.RUN_STORE_SQL)
    with pytest.raises(SystemExit):
        cli.main(["import-runs"])
    assert "migrate-runs" in capsys.readouterr().out


def test_archive_moves_only_imported_files(history, runs_dir, tmp_path):
    archive = tmp_path / "archive"
    report = migrate_runs(runs_dir, history, _url(tmp_path), archive_dir=archive)
    # importiert (a, b) → archiviert; defekt (broken), nicht abbildbar (weird), Dublette (q/a) bleiben
    assert report.runs_archived == 2 and report.archive_dir == str(archive)
    assert (archive / "p" / "a.json").is_file() and (archive / "p" / "b.json").is_file()
    assert not (runs_dir / "p" / "a.json").exists() and not (runs_dir / "p" / "b.json").exists()
    assert (runs_dir / "p" / "broken.json").is_file()
    assert (runs_dir / "p" / "weird.json").is_file()
    assert (runs_dir / "q" / "a.json").is_file()
    assert any("Archiv" in line for line in report.lines())


def test_deviating_record_stays_in_runs_not_archived(runs_dir, tmp_path):
    """Ein Record, der importiert aber vom gespeicherten Stand abweicht, bleibt in runs/ (nicht archiviert)."""
    # Review-Finding mit nicht-string `severity`: parse_run → "", SQL-Store → str(5) → Mismatch after import
    finding = {"severity": 5, "text": "t", "suggestion": "fix it"}
    _save(
        runs_dir,
        "p",
        _rec(
            "dev",
            steps=[
                {
                    "name": "r",
                    "type": "pr_review",
                    "status": "pass",
                    "review": {"verdict": "changes_requested", "findings": [finding]},
                }
            ],
        ),
    )
    archive = tmp_path / "archive"
    report = migrate_runs(runs_dir, tmp_path / "none.db", _url(tmp_path), archive_dir=archive)
    assert report.runs_imported == 1
    assert report.runs_archived == 0
    assert (runs_dir / "p" / "dev.json").is_file()  # bleibt in runs/ liegen
    assert not (archive / "p" / "dev.json").exists()
    assert any(i.run_id == "dev" and "Mismatch after import" in i.reason for i in report.issues)
    assert not report.ok


def test_dry_run_does_not_archive(history, runs_dir, tmp_path):
    archive = tmp_path / "archive"
    report = migrate_runs(runs_dir, history, _url(tmp_path), dry_run=True, archive_dir=archive)
    assert not archive.exists()
    assert report.runs_archived == 0 and not report.archive_dir
    assert (runs_dir / "p" / "a.json").is_file()


def test_second_run_after_archive_is_noop(history, runs_dir, tmp_path):
    archive = tmp_path / "archive"
    migrate_runs(runs_dir, history, _url(tmp_path), archive_dir=archive)
    store = store_for_url(_url(tmp_path))
    tables = ("runs", "run_events", "run_checkpoints", "run_artifacts")
    with store.engine.connect() as conn:
        counts = [conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in tables]
    second = migrate_runs(runs_dir, history, _url(tmp_path), archive_dir=archive)
    # q/a ist keine Dublette mehr (p/a archiviert) und "a" steht im Ziel → wird archiviert;
    # broken/weird bleiben liegen
    assert (second.runs_found, second.runs_imported, second.runs_existing, second.runs_archived) == (3, 0, 1, 1)
    assert (archive / "q" / "a.json").is_file() and not (runs_dir / "q" / "a.json").exists()
    assert (runs_dir / "p" / "broken.json").is_file() and (runs_dir / "p" / "weird.json").is_file()
    with store.engine.connect() as conn:
        assert [conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in tables] == counts


def test_archive_collision_keeps_source(history, runs_dir, tmp_path):
    archive = tmp_path / "archive"
    (archive / "p").mkdir(parents=True)
    (archive / "p" / "a.json").write_text("besteht schon")
    report = migrate_runs(runs_dir, history, _url(tmp_path), archive_dir=archive)
    assert report.runs_archived == 1  # nur b
    assert (runs_dir / "p" / "a.json").is_file()  # Kollision → Quelle bleibt liegen
    assert (archive / "p" / "a.json").read_text() == "besteht schon"
    assert any(i.run_id == "a" and "Archive collision" in i.reason for i in report.issues)
    assert not report.ok


def test_session_log_dump_file_backend(history):
    dump = sessionlog.session_log_dump("a")
    assert len(dump["events"]) == 3 and len(dump["checkpoints"]) == 1 and len(dump["artifacts"]) == 1
    assert dump["events"][0]["kind"] == "run_start"
    assert dump["checkpoints"][0]["step_name"] == "r"
    assert dump["artifacts"][0]["path"] == "/tmp/r.md"


def test_session_log_dump_missing_file_creates_nothing(monkeypatch, tmp_path):
    p = tmp_path / "nope" / "sessionlog.db"
    monkeypatch.setenv("FABRIKANT_SESSIONLOG", str(p))
    monkeypatch.setattr(sessionlog, "_conn", None)
    assert sessionlog.session_log_dump("a") == {"events": [], "checkpoints": [], "artifacts": []}
    assert not p.exists()


def test_session_log_dump_sql_backend(history, runs_dir, tmp_path, monkeypatch):
    migrate_runs(runs_dir, history, _url(tmp_path))
    monkeypatch.setenv(config.RUN_STORE_ENV, config.RUN_STORE_SQL)
    monkeypatch.setenv(config.DB_URL_ENV, _url(tmp_path))
    dump = sessionlog.session_log_dump("a")
    assert len(dump["events"]) == 3 and len(dump["checkpoints"]) == 1 and len(dump["artifacts"]) == 1
    assert dump["events"][0]["kind"] == "run_start"


def test_cli_runs_show_and_export(history, runs_dir, tmp_path, monkeypatch, capsys):
    migrate_runs(runs_dir, history, _url(tmp_path))
    monkeypatch.setenv(config.RUN_STORE_ENV, config.RUN_STORE_SQL)
    monkeypatch.setenv(config.DB_URL_ENV, _url(tmp_path))
    cli.main(["runs", "show", "a", "--project", "p"])
    out = capsys.readouterr().out
    assert (
        "a  p/l  pass" in out
        and "✔ r [pr_review]" in out
        and "Session log: 3 events, 1 checkpoints, 1 artifacts" in out
    )
    cli.main(["runs", "export", "a", "--project", "p"])
    rec = json.loads(capsys.readouterr().out)
    assert rec["id"] == "a" and rec["project"] == "p"
    assert len(rec["session_log"]["events"]) == 3 and len(rec["session_log"]["artifacts"]) == 1


def test_cli_runs_show_not_found(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(config.RUN_STORE_ENV, config.RUN_STORE_SQL)
    monkeypatch.setenv(config.DB_URL_ENV, _url(tmp_path))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["runs", "show", "missing"])
    assert exit_info.value.code == 1
    assert "✖ Run missing nicht gefunden" in capsys.readouterr().out


def test_read_paths_after_switch_match_source(history, runs_dir, tmp_path, monkeypatch):
    """Spot-Check: nach Import + Umschaltung auf SQL liefern die Lesepfade (CLI/UI/API) dieselben Runs."""
    migrate_runs(runs_dir, history, _url(tmp_path))
    monkeypatch.setenv(config.RUN_STORE_ENV, config.RUN_STORE_SQL)
    monkeypatch.setenv(config.DB_URL_ENV, _url(tmp_path))
    listed = {r["id"]: r for r in runs.list_runs()}
    assert set(listed) == {"a", "b"}
    for rid in ("a", "b"):
        src = json.loads((runs_dir / "p" / f"{rid}.json").read_text())
        got = runs.get_run(rid)
        assert got["id"] == src["id"] and got["project"] == "p" and got["loop"] == src["loop"]
        assert got["status"] == src["status"] and got["started"] == src["started"]
        assert _step_core(got["steps"]) == _step_core(src["steps"])
