"""Durable session log (final schema, ADR 0002; ADR 0034 no. 7).

Append-only event store: events, artifacts, checkpoints per run (session = run).

- JSON run store (default until #278): own SQLite file ``sessionlog.db`` with
  table ``sessions`` as a projection of the run record.
- SQL run store (``FABRIKANT_RUN_STORE=sql``): the log lives in the same store
  (:class:`fabrikant.runstore_sql.SqlSessionLog`); status and times of the session
  are those of the run, ``set_status``/``finish_orphaned_run`` are dropped.

Best effort: log errors fell no run (ADR 0002, invariant 5).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import config
from .i18n import t
from .runstore import STORE, RunStore
from .secrets import _redact

if TYPE_CHECKING:
    from .runstore_sql import SqlSessionLog

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    project      TEXT NOT NULL,
    loop         TEXT NOT NULL,
    trigger      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    spec_hash    TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'native'
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    seq         INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    step_name   TEXT NOT NULL DEFAULT '',
    step_type   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    step_name   TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    step_idx    INTEGER NOT NULL,
    step_name   TEXT NOT NULL,
    status      TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, step_idx)
);
"""

_conn: sqlite3.Connection | None = None
_conn_guard = threading.Lock()


def db_path() -> Path:
    return Path(os.environ.get("FABRIKANT_SESSIONLOG", str(config.FABRIKANT_HOME / "sessionlog.db"))).resolve()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Global connection (homelab scale) + WAL. Tests swap it via
    monkeypatch.setattr(sessionlog, "_conn", …)."""
    global _conn
    with _conn_guard:
        if _conn is None:
            p = path or db_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(str(p), check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.executescript(_SCHEMA)
            c.commit()
            _conn = c
        return _conn


def _ts() -> str:
    return datetime.now(UTC).isoformat()


def _sql_log() -> SqlSessionLog | None:
    """Session log of the SQL run store if it is active; otherwise None (file ``sessionlog.db``)."""
    if config.run_store_backend() != config.RUN_STORE_SQL:
        return None
    return STORE.backend().session_log


class SessionLog:
    """Map a fabrikant run (session) into the log.

    Used per run from the engine: start() → events/checkpoints/artifacts →
    finish(). All writes go through _redact; exceptions are swallowed
    (best effort, ADR 0002 invariant 5).
    """

    def __init__(self, session_id: str, project: str, loop: str, trigger: str = "", spec_hash: str = ""):
        self.session_id = session_id
        self.project = project
        self.loop = loop
        self.trigger = trigger
        self.spec_hash = spec_hash
        self._seq = 0

    # -- interne helper
    def _exec(self, sql: str, params: tuple = ()) -> None:
        try:
            c = connect()
            with _conn_guard:
                c.execute(sql, params)
                c.commit()
        except Exception:
            pass  # best effort: no log error fells a run

    def _fetchone(self, sql: str, params: tuple = ()):
        try:
            c = connect()
            with _conn_guard:
                return c.execute(sql, params).fetchone()
        except Exception:
            return None

    # -- public API
    def _sql(self, op: Callable[[SqlSessionLog], object]) -> bool:
        """Execute ``op`` on the SQL session log if active (best effort); True = SQL store active."""
        try:
            log = _sql_log()
        except Exception:
            return False
        if log is None:
            return False
        with contextlib.suppress(Exception):  # best effort: no log error fells a run
            op(log)
        return True

    def start(self) -> None:
        def _resume_seq(log: SqlSessionLog) -> None:
            self._seq = log.last_seq(self.session_id)

        if self._sql(_resume_seq):  # Session = Run: Status/Zeiten stehen im Run-Record
            self.event("run_start", data={"loop": self.loop, "trigger": self.trigger})
            return
        # Resume: attach the counter to the last event (no duplicate seqs),
        # status back to running (INSERT OR IGNORE would otherwise leave needs_approval
        # from the old run standing).
        row = self._fetchone("SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id=?", (self.session_id,))
        self._seq = int(row[0]) if row else 0
        exists = self._fetchone("SELECT 1 FROM sessions WHERE session_id=?", (self.session_id,))
        if exists:
            self.set_status("running")
        else:
            self._exec(
                "INSERT INTO sessions (session_id, project, loop, trigger, status, spec_hash, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (self.session_id, self.project, self.loop, self.trigger, self.spec_hash, _ts()),
            )
        self.event("run_start", data={"loop": self.loop, "trigger": self.trigger})

    def event(self, kind: str, step_name: str = "", step_type: str = "", status: str = "", **data) -> None:
        self._seq += 1
        blob = json.dumps(data, ensure_ascii=False, default=str)
        seq, ts, name, stype, payload = self._seq, _ts(), _redact(step_name), _redact(step_type), _redact(blob)
        if self._sql(lambda log: log.event(self.session_id, seq, ts, kind, name, stype, status, payload)):
            return
        self._exec(
            "INSERT INTO events (session_id, seq, ts, kind, step_name, step_type, status, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.session_id, self._seq, _ts(), kind, _redact(step_name), _redact(step_type), status, _redact(blob)),
        )

    def artifact(self, step_name: str, kind: str, path: str) -> None:
        name, akind, apath, ts = _redact(step_name), _redact(kind), _redact(str(path)), _ts()
        if self._sql(lambda log: log.artifact(self.session_id, name, akind, apath, ts)):
            return
        self._exec(
            "INSERT INTO artifacts (session_id, step_name, kind, path, created_at) VALUES (?, ?, ?, ?, ?)",
            (self.session_id, _redact(step_name), _redact(kind), _redact(str(path)), _ts()),
        )

    def checkpoint(self, step_idx: int, step_name: str, status: str, **data) -> None:
        # Plain INSERT (append-only, ADR invariant 1): on a step rerun
        # the old checkpoint is kept, the duplicate insert is swallowed.
        blob = json.dumps(data, ensure_ascii=False, default=str)
        name, payload = _redact(step_name), _redact(blob)
        if self._sql(lambda log: log.checkpoint(self.session_id, step_idx, name, status, payload)):
            return
        self._exec(
            "INSERT INTO checkpoints (session_id, step_idx, step_name, status, data) VALUES (?, ?, ?, ?, ?)",
            (self.session_id, step_idx, _redact(step_name), status, _redact(blob)),
        )

    def set_status(self, status: str) -> None:
        """Status update without run_end (e.g. needs_approval — the session continues).

        In the SQL store the session status is the run status (nothing to do)."""
        if self._sql(lambda log: None):
            return
        self._exec("UPDATE sessions SET status=? WHERE session_id=?", (status, self.session_id))

    def finish(self, status: str, tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0) -> None:
        self.event("run_end", status=status)
        if self._sql(lambda log: None):  # Status/end are in the run, sums from step_done events
            return
        self._exec(
            "UPDATE sessions SET status=?, finished_at=?, tokens_in=?, tokens_out=?, cost_usd=? WHERE session_id=?",
            (status, _ts(), int(tokens_in), int(tokens_out), float(cost_usd), self.session_id),
        )


def finish_orphaned_run(session_id: str, finished_at: str) -> None:
    """Mark a run abandoned by a server restart as finished in the projection.

    In the SQL store the projection is dropped: the reap ended the run itself."""
    with contextlib.suppress(Exception):
        if _sql_log() is not None:
            return
    try:
        c = connect()
        with _conn_guard:
            c.execute(
                "UPDATE sessions SET status='error', finished_at=? WHERE session_id=? AND status='running'",
                (finished_at, session_id),
            )
            c.commit()
    except sqlite3.Error as e:
        # best effort: no log error fells a run — but not silently
        print(t("sessionlog.fabrikant_sessionlog_orphan_run_not", session_id=session_id, e=e))


def session_totals(session_id: str) -> dict:
    """Aggregates from step_done events (usage per step) — the only
    total source, so agent runs are not counted twice
    (cost events are telemetry, not a summand)."""
    row = _fetch_totals(session_id)
    return row or {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}


def _step_done_blobs(session_id: str) -> list[str]:
    log = _sql_log()
    if log is not None:
        return log.event_data(session_id, "step_done")
    c = connect()
    with _conn_guard:
        rows = c.execute("SELECT data FROM events WHERE session_id=? AND kind='step_done'", (session_id,)).fetchall()
    return [blob for (blob,) in rows]


def _fetch_totals(session_id: str) -> dict | None:
    try:
        tin = tout = 0
        cost = 0.0
        for blob in _step_done_blobs(session_id):
            d = json.loads(blob)
            u = d.get("usage") or {}
            tin += int(u.get("prompt_tokens", 0))
            tout += int(u.get("completion_tokens", 0))
            cost += float(u.get("cost_usd", 0.0))
        return {"tokens_in": tin, "tokens_out": tout, "cost_usd": cost}
    except Exception:
        return None


# Agent-turn events of the transcript → role (user = prompt, tool = tool call).
TRANSCRIPT_ROLES = {"prompt": "user", "tool_call": "tool", "message": "assistant"}
TRANSCRIPT_PAGE = 500  # Default page size (lazy loading)


@dataclass(frozen=True)
class TranscriptEntry:
    """An agent turn in the transcript.

    role: ``user`` (prompt), ``assistant`` (answer) or ``tool`` (tool call).
    text: prompt/answer text (``user``/``assistant`` only).
    tool: tool payload (``tool`` only; name or full part)."""

    id: int
    ts: str
    role: str
    step: str | None
    text: str | None = None
    tool: object = None

    @classmethod
    def from_row(cls, eid: int, ts: str, kind: str, step: str | None, blob: str) -> TranscriptEntry:
        """SQLite-Event-Zeile → Eintrag; defektes JSON → leerer Inhalt."""
        try:
            data = json.loads(blob)
        except ValueError:
            data = {}
        base = cls(eid, ts, TRANSCRIPT_ROLES[kind], step)
        if kind == "tool_call":
            return replace(base, tool=data.get("tool", {}))
        return replace(base, text=data.get("text", ""))

    def to_dict(self) -> dict:
        """JSON form for the API (``text`` or ``tool`` depending on the role)."""
        out: dict = {"id": self.id, "ts": self.ts, "role": self.role, "step": self.step}
        if self.role == TRANSCRIPT_ROLES["tool_call"]:
            out["tool"] = self.tool
        else:
            out["text"] = self.text
        return out


def transcript(session_id: str, after: int = 0, limit: int = TRANSCRIPT_PAGE) -> list[TranscriptEntry]:
    """Agent turns of a session (prompt, tool calls, answers) in order.

    Paginated via the event ``id`` (``after`` = last known id). Contents are
    redacted at write time (``_redact``); read errors → empty list."""
    kinds = tuple(TRANSCRIPT_ROLES)
    sql = (
        "SELECT id, ts, kind, step_name, data FROM events "  # nosec B608 # only ? placeholders
        f"WHERE session_id=? AND id>? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id LIMIT ?"
    )
    try:
        log = _sql_log()
        if log is not None:
            rows = log.events(session_id, after, kinds, max(1, limit))
        else:
            c = connect()
            with _conn_guard:
                rows = c.execute(sql, (session_id, after, *kinds, max(1, limit))).fetchall()
    except Exception:  # Lesefehler (Datei-DB, Store, Konfiguration) → leere Liste
        return []
    return [TranscriptEntry.from_row(*row) for row in rows]


_DUMP_EVENT_COLS = ("id", "session_id", "seq", "ts", "kind", "step_name", "step_type", "status", "data")
_DUMP_ARTIFACT_COLS = ("id", "session_id", "step_name", "kind", "path", "created_at")
_DUMP_CHECKPOINT_COLS = ("session_id", "step_idx", "step_name", "status", "data")


def session_log_dump(session_id: str) -> dict:
    """Session log of a session as a dict (``events``/``checkpoints``/``artifacts``), both backends.

    SQL backend: reads the run store. JSON backend: reads ``sessionlog.db`` — a missing
    file yields an empty log and is NOT created. Read errors → empty log
    (best effort like the other read accesses)."""
    try:
        log = _sql_log()
        if log is not None:
            return log.dump(session_id)
        if not db_path().is_file():
            return {"events": [], "checkpoints": [], "artifacts": []}
        c = connect()
        with _conn_guard:
            events = [
                dict(zip(_DUMP_EVENT_COLS, row, strict=True))
                for row in c.execute(
                    "SELECT id, session_id, seq, ts, kind, step_name, step_type, status, data "
                    "FROM events WHERE session_id=? ORDER BY id",
                    (session_id,),
                )
            ]
            checkpoints = [
                dict(zip(_DUMP_CHECKPOINT_COLS, row, strict=True))
                for row in c.execute(
                    "SELECT session_id, step_idx, step_name, status, data FROM checkpoints WHERE session_id=?",
                    (session_id,),
                )
            ]
            artifacts = [
                dict(zip(_DUMP_ARTIFACT_COLS, row, strict=True))
                for row in c.execute(
                    "SELECT id, session_id, step_name, kind, path, created_at "
                    "FROM artifacts WHERE session_id=? ORDER BY id",
                    (session_id,),
                )
            ]
    except Exception:
        return {"events": [], "checkpoints": [], "artifacts": []}
    return {"events": events, "checkpoints": checkpoints, "artifacts": artifacts}


# ---------------------------------------------------------------- Importer
def import_runs(runs_dir: Path) -> int:
    """P0 run JSONs → SQLite (ADR 0002, invariant 4). Idempotent:
    existing sessions (native) are skipped. Best effort:
    connect error → 0, broken records are skipped, commit per
    record (an error loses only the record in progress). → imported count.
    Superseded by the import into the SQL store (#278)."""
    try:
        c = connect()
    except Exception:
        return 0
    imported = 0
    try:  # Read run JSON only via the (JSON) store; it skips broken records
        records = RunStore(runs_dir).load_records()
    except OSError:
        return 0
    for rec in sorted(records, key=lambda r: (r["project"], str(r.get("id", "")))):
        sid = rec.get("id")
        if not sid:
            continue
        try:
            with _conn_guard:
                if c.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone():
                    continue
                tin = tout = 0
                cost = 0.0
                for s in rec.get("steps", []):
                    u = s.get("usage") or {}
                    tin += int(u.get("prompt_tokens", 0))
                    tout += int(u.get("completion_tokens", 0))
                    cost += float(u.get("cost_usd", 0.0))
                c.execute(
                    "INSERT INTO sessions (session_id, project, loop, trigger, status, spec_hash, started_at, "
                    "finished_at, tokens_in, tokens_out, cost_usd, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,'import')",
                    (
                        sid,
                        rec.get("project", ""),
                        rec.get("loop", ""),
                        rec.get("trigger", ""),
                        rec.get("status", ""),
                        rec.get("spec_hash", ""),
                        rec.get("started", ""),
                        rec.get("finished", ""),
                        tin,
                        tout,
                        cost,
                    ),
                )
                for i, s in enumerate(rec.get("steps", [])):
                    st = s.get("status", "")
                    c.execute(
                        "INSERT INTO events (session_id, seq, ts, kind, step_name, step_type, status, data) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            sid,
                            i + 1,
                            rec.get("started", ""),
                            "step_done",
                            _redact(s.get("name", "")),
                            s.get("type", ""),
                            st,
                            _redact(
                                json.dumps({"duration_s": s.get("duration_s"), "usage": s.get("usage")}, default=str)
                            ),
                        ),
                    )
                    if st == "pass":
                        c.execute(
                            "INSERT INTO checkpoints (session_id, step_idx, step_name, status, data) VALUES (?,?,?,?,?)",
                            (
                                sid,
                                i,
                                _redact(s.get("name", "")),
                                "pass",
                                _redact(
                                    json.dumps(
                                        {"duration_s": s.get("duration_s"), "usage": s.get("usage")}, default=str
                                    )
                                ),
                            ),
                        )
                c.commit()
            imported += 1
        except Exception:
            continue
    return imported
