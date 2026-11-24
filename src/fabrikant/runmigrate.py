"""One-off import of the run JSON and the session log into the SQL run store (ADR 0034 no. 8, #278).

Source: the JSON records under ``runs/`` (:meth:`fabrikant.runstore.RunStore.raw_records`) and the
file ``sessionlog.db`` (read only). Target: the SQL store under the DB URL.

- Idempotent: runs that already exist in the target are skipped; session-log entries are only
  taken over for runs that have no events in the target yet.
- Never silent: every record that cannot be mapped (broken JSON, schema/constraint violation, duplicate
  id) and every discrepancy between JSON and stored run (compared typed) is listed with
  run id and reason in the :class:`MigrationReport`. Sessions without a run record are counted.
- ``dry_run``: the same import into an in-memory DB (full constraint check); the target is only
  read, and only if it already exists.
- Archiving: after a real import the imported run JSON is moved to ``archive_dir``
  (layout like ``runs/``) — never deleted; broken/deviating records stay.

Run logs remain files.
"""

from __future__ import annotations

import copy
import shutil
import sqlite3
from collections import Counter
from dataclasses import dataclass, field, fields
from pathlib import Path

from sqlalchemy import insert, inspect, select
from sqlalchemy.engine import make_url

from .i18n import t
from .runschema import run_artifacts, run_checkpoints, run_events, runs
from .runstore import RecordError, RunStore, RunStoreError, parse_run
from .runstore_sql import SqlRunStore, make_engine, migrate, store_for_url

IN_MEMORY_URL = "sqlite://"
SAMPLE_LIMIT = 10  # so viele Beispiel-Ids je Sammelposten im Text-Report

_EVENT_COLS = ("seq", "ts", "kind", "step_name", "step_type", "status", "data")
_CHECKPOINT_COLS = ("step_idx", "step_name", "status", "data")
_ARTIFACT_COLS = ("step_name", "kind", "path", "created_at")


@dataclass(frozen=True)
class MigrationIssue:
    """A record that was not (or only deviatingly) imported."""

    run_id: str
    source: str
    reason: str


@dataclass
class MigrationReport:
    """Ergebnis eines (Probe-)Imports."""

    dry_run: bool
    target: str
    runs_found: int = 0
    runs_imported: int = 0
    runs_existing: int = 0
    runs_archived: int = 0
    archive_dir: str = ""
    sessions_imported: int = 0
    events_imported: int = 0
    checkpoints_imported: int = 0
    artifacts_imported: int = 0
    sessions_existing: int = 0
    orphan_sessions: Counter[str] = field(default_factory=Counter)  # Projekt → Anzahl Sessions ohne Run
    issues: list[MigrationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if every found run was imported or already existed, without discrepancy."""
        return not self.issues

    def lines(self) -> list[str]:
        """Lesbarer Report (CLI)."""
        mode = "Probelauf (nichts geschrieben)" if self.dry_run else "Import"
        out = [
            f"{mode} → {self.target}",
            t(
                "runmigrate.runs_found_imported_already_present",
                v1=self.runs_found,
                v2=self.runs_imported,
                v3=self.runs_existing,
            ),
            f"Session-Log: {self.sessions_imported} Sessions ({self.events_imported} Events, "
            f"{self.checkpoints_imported} Checkpoints, {self.artifacts_imported} Artefakte) importiert, "
            f"{self.sessions_existing} schon vorhanden",
        ]
        if self.archive_dir:
            out.append(f"Archiv: {self.runs_archived} Run-JSON nach {self.archive_dir} verschoben")
        if self.orphan_sessions:
            total = sum(self.orphan_sessions.values())
            top = ", ".join(f"{p} ({n})" for p, n in self.orphan_sessions.most_common(SAMPLE_LIMIT))
            out.append(t("runmigrate.sessions_without_run_record_not", total=total, top=top))
        out.append(f"Probleme: {len(self.issues)}")
        out += [f"  ✖ {i.run_id} [{i.source}]: {i.reason}" for i in self.issues]
        return out


def _run_diff(before, after) -> list[str]:
    return [f.name for f in fields(before) if getattr(before, f.name) != getattr(after, f.name)]


def _target_state(url: str) -> tuple[set[str], set[str]]:
    """(run ids, run ids with events) of an existing target, read only; if it is missing → empty."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and not (parsed.database and Path(parsed.database).is_file()):
        return set(), set()
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            if "runs" not in inspect(conn).get_table_names():
                return set(), set()
            ids = set(conn.execute(select(runs.c.id)).scalars())
            with_events = set(conn.execute(select(run_events.c.run_id).distinct()).scalars())
        return ids, with_events
    finally:
        engine.dispose()


def _import_runs(
    source: RunStore, store: SqlRunStore, existing: set[str], report: MigrationReport
) -> tuple[set[str], set[Path]]:
    """JSON records → store; returns (ids of all runs in the target, paths of the archivable JSON files).

    Only what is in the target and was imported without discrepancy (or already
    existed) is archivable. Broken/deviating records and duplicates stay (exact paths,
    not just ids — a duplicate shares the id with an archivable record)."""
    present = set(existing)
    archivable: set[Path] = set()
    seen: dict[str, str] = {}
    for path, raw in source.raw_records():
        report.runs_found += 1
        run_id, project = path.stem, path.parent.name
        where = f"{project}/{path.name}"
        if isinstance(raw, RunStoreError):
            report.issues.append(MigrationIssue(run_id, where, str(raw)))
            continue
        if run_id in seen:
            report.issues.append(MigrationIssue(run_id, where, t("runmigrate.id_already_in_project", v1=seen[run_id])))
            continue
        seen[run_id] = project
        if run_id in existing:
            report.runs_existing += 1
            archivable.add(path)
            continue
        try:
            expected = parse_run(raw, project)
        except RecordError as e:
            report.issues.append(MigrationIssue(run_id, where, t("runmigrate.cannot_be_mapped_to_the", e=e)))
            continue
        try:
            store.create(project, run_id, copy.deepcopy(raw))
        except RunStoreError as e:
            report.issues.append(MigrationIssue(run_id, where, str(e)))
            continue
        report.runs_imported += 1
        present.add(run_id)
        stored = store.get(run_id, project)
        diff = _run_diff(expected, stored) if stored is not None else [t("runmigrate.unreadable")]
        if diff:
            report.issues.append(MigrationIssue(run_id, where, f"Abweichung nach Import: {', '.join(diff)}"))
        else:
            archivable.add(path)
    return present, archivable


def archive_runs(runs_dir: Path, archive_dir: Path, archivable: set[Path]) -> tuple[int, list[MigrationIssue]]:
    """Move archivable run JSON to ``archive_dir`` (layout like ``runs/``); never delete.

    Collision at the target (file already there) → source stays + issue.
    Returns (number of moved files, issues)."""
    moved = 0
    issues: list[MigrationIssue] = []
    for path in sorted(archivable):
        dest = archive_dir / path.parent.name / path.name
        try:
            if dest.exists():
                issues.append(
                    MigrationIssue(
                        path.stem, f"{path.parent.name}/{path.name}", "Archiv-Kollision: Ziel existiert schon"
                    )
                )
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            moved += 1
        except OSError as e:
            issues.append(
                MigrationIssue(path.stem, f"{path.parent.name}/{path.name}", f"Archivierung fehlgeschlagen: {e}")
            )
    return moved, issues


def _rows(conn: sqlite3.Connection, sql: str, session_id: str, cols: tuple[str, ...]) -> list[dict]:
    return [dict(zip(cols, row, strict=True)) for row in conn.execute(sql, (session_id,))]


def _import_session_log(
    sessionlog_path: Path, store: SqlRunStore, present: set[str], with_events: set[str], report: MigrationReport
) -> None:
    """Events/checkpoints/artifacts from ``sessionlog.db`` for runs in the target (without own events)."""
    if not sessionlog_path.is_file():
        return
    src = sqlite3.connect(f"file:{sessionlog_path}?mode=ro", uri=True)
    try:
        for session_id, project in src.execute("SELECT session_id, project FROM sessions ORDER BY session_id"):
            if session_id not in present:
                report.orphan_sessions[project or "?"] += 1
                continue
            if session_id in with_events:
                report.sessions_existing += 1
                continue
            events = _rows(
                src,
                f"SELECT {', '.join(_EVENT_COLS)} FROM events WHERE session_id=? ORDER BY id",  # nosec B608 # konstante Spalten, Werte als ?-Parameter
                session_id,
                _EVENT_COLS,
            )
            checkpoints = _rows(
                src,
                f"SELECT {', '.join(_CHECKPOINT_COLS)} FROM checkpoints WHERE session_id=?",  # nosec B608 # konstante Spalten, Werte als ?-Parameter
                session_id,
                _CHECKPOINT_COLS,
            )
            artifacts = _rows(
                src,
                f"SELECT {', '.join(_ARTIFACT_COLS)} FROM artifacts WHERE session_id=? ORDER BY id",  # nosec B608 # konstante Spalten, Werte als ?-Parameter
                session_id,
                _ARTIFACT_COLS,
            )
            try:
                with store.write_tx() as conn:
                    for table, rows in (
                        (run_events, events),
                        (run_checkpoints, checkpoints),
                        (run_artifacts, artifacts),
                    ):
                        if rows:
                            conn.execute(insert(table), [{"run_id": session_id, **r} for r in rows])
            except RunStoreError as e:
                report.issues.append(MigrationIssue(session_id, "sessionlog.db", str(e)))
                continue
            report.sessions_imported += 1
            report.events_imported += len(events)
            report.checkpoints_imported += len(checkpoints)
            report.artifacts_imported += len(artifacts)
    finally:
        src.close()


def migrate_runs(
    runs_dir: Path,
    sessionlog_path: Path,
    db_url: str,
    *,
    dry_run: bool = False,
    archive_dir: Path | None = None,
) -> MigrationReport:
    """Import run JSON + session log to ``db_url`` (or dry run into an in-memory DB).

    ``archive_dir``: afterwards the imported run JSON is moved there (never deleted) —
    only in the real import, not in the dry run."""
    report = MigrationReport(dry_run=dry_run, target=make_url(db_url).render_as_string())
    existing, with_events = _target_state(db_url)
    if dry_run:
        engine = make_engine(IN_MEMORY_URL)
        migrate(engine)
        store = SqlRunStore(engine)
    else:
        store = store_for_url(db_url)
    try:
        present, archivable = _import_runs(RunStore(runs_dir), store, existing, report)
        _import_session_log(sessionlog_path, store, present, with_events, report)
    finally:
        if dry_run:
            store.engine.dispose()
    if not dry_run and archive_dir is not None:
        report.archive_dir = str(archive_dir)
        report.runs_archived, archive_issues = archive_runs(runs_dir, archive_dir, archivable)
        report.issues.extend(archive_issues)
    return report
