"""SQL run store (ADR 0034, step 3): SQLAlchemy Core on the schema from :mod:`fabrikant.runschema`.

Same operations and preconditions as the JSON backend (:class:`~fabrikant.runstore.BaseRunStore`);
every write operation is a DB transaction: read run row exclusively (SQLite: ``BEGIN IMMEDIATE``,
otherwise ``SELECT … FOR UPDATE``) → check precondition → update run row and replace its child
rows. The session log (ADR 0002) lives in the same table space (:class:`SqlSessionLog`).

Record ↔ rows (:func:`encode_record`, :func:`decode_record`): the store accepts the engine's run records
(``dict``) strictly — an unknown field or a wrong type raises
:class:`InvalidRecordError` instead of being silently lost; status, mandatory fields and foreign keys
are additionally checked by the DB (``CHECK``/``NOT NULL``/``FOREIGN KEY``). Deviations from the JSON format:

- ``None`` and missing fields are equivalent (``NULL``); when read, the field is missing. Exceptions:
  ``started`` (``""``), ``steps`` (``[]``) and ``started_by``/``approved_by``/``rejected_by`` (``None``)
  are always present.
- ``steps_output`` (legacy) is discarded. Review findings and verify checks keep only the
  model fields (``severity``/``text``/``suggestion`` or ``name``/``ok``/``output``) — they are
  foreign payloads (LLM).
- ``review.pr`` as a numeric string becomes a number; empty lists (``verify``, ``permissions``) and
  empty variable dicts are dropped.

SQLite: WAL, ``busy_timeout`` and foreign keys are set per connection; ``lower()`` is
replaced by Python's ``str.lower`` (Unicode, like the reference search in :mod:`fabrikant.runquery`).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    Table,
    and_,
    create_engine,
    delete,
    event,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    true,
    tuple_,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import StaticPool

from . import config
from .i18n import t as _t
from .runmodel import FAILED_RUN_STATUSES, FailureKind, Run, RunStatus
from .runquery import UTC_SORT_FORMAT, RunQuery, utc_sort_text
from .runschema import (
    RECORD_CHILD_TABLES,
    VarScope,
    VarType,
    approval_permissions,
    approvals,
    review_findings,
    review_results,
    run_artifacts,
    run_checkpoints,
    run_events,
    run_vars,
    runs,
    step_usage,
    steps,
    verify_checks,
)
from .runstore import (
    BaseRunStore,
    LoadFailure,
    LoadReport,
    Mutation,
    PreconditionError,
    RecordError,
    RunStoreError,
    _approval_status,
    _check_ids,
    parse_run,
    valid_project,
    valid_run_id,
)
from .secrets import _redact_record

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_WRITE_OPTION = "fabrikant_write"  # Execution-Option: Transaktion als Schreib-Transaktion beginnen


class InvalidRecordError(RunStoreError):
    """The record cannot be mapped into the relational schema (unknown field, wrong type,
    DB constraint violated). Nothing is written."""


# ---------------------------------------------------------------- Record → Zeilen
_TEXT, _OPT_TEXT, _BOOL, _INT, _NUM = "text", "opt_text", "bool", "int", "num"

# Scalar run fields → column type (column name = field name). id/project/status come from the operation.
_RUN_FIELDS: Mapping[str, str] = {
    "loop": _TEXT,
    "status": _TEXT,
    "started": _TEXT,
    "finished": _TEXT,
    "trigger": _TEXT,
    "note": _TEXT,
    "spec_hash": _TEXT,
    "waiting_step": _TEXT,
    "resumed": _BOOL,
    "approval_queued": _TEXT,
    "started_by": _OPT_TEXT,
    "approved_by": _OPT_TEXT,
    "rejected_by": _OPT_TEXT,
    "error": _TEXT,
    "failure_kind": _TEXT,
    "retry_after": _TEXT,
    "retry_of": _TEXT,
    "replay_of": _TEXT,
    "golden": _TEXT,
    "attempt": _INT,
    "trigger_key": _TEXT,
    "worktree_cleanup_error": _TEXT,
    "scratch_cleanup_error": _TEXT,
    "state_merge_error": _TEXT,
}
_RUN_NESTED = ("steps", "approval", *VarScope)
_RUN_IGNORED = frozenset({"id", "project", "steps_output"})
_ALWAYS_PRESENT = ("started_by", "approved_by", "rejected_by")  # read also as None

_STEP_FIELDS: Mapping[str, str] = {
    "name": _TEXT,
    "type": _TEXT,
    "status": _TEXT,
    "duration_s": _NUM,
    "summary": _TEXT,
    "output": _TEXT,
    "failure_kind": _TEXT,
    "retry_after": _TEXT,
    "verdict": _TEXT,
}
_STEP_NESTED = ("usage", "review", "verify")

_USAGE_FIELDS: Mapping[str, str] = {
    "model": _TEXT,
    "provider": _TEXT,
    "prompt_tokens": _INT,
    "completion_tokens": _INT,
    "total_tokens": _INT,
    "cost_usd": _NUM,
    "cache_read_tokens": _INT,
    "cache_write_tokens": _INT,
    "billing": _TEXT,
}
_REVIEW_FIELDS: Mapping[str, str] = {"verdict": _TEXT, "model": _TEXT, "summary": _TEXT, "round": _INT}
_FINDING_FIELDS = ("severity", "text", "suggestion")  # Modellfelder eines Findings (Rest: LLM-Fremdfelder)
_APPROVAL_FIELDS: Mapping[str, str] = {
    "reason": _TEXT,
    "kind": _TEXT,
    "step": _TEXT,
    "condition": _TEXT,
    "why": _TEXT,
    "what": _TEXT,
    "issue": _TEXT,
    "pr": _TEXT,
    "pr_title": _TEXT,
    "tier": _INT,
}

_TYPE_CHECKS: Mapping[str, Callable[[Any], bool]] = {
    _TEXT: lambda v: isinstance(v, str),
    _OPT_TEXT: lambda v: isinstance(v, str),
    _BOOL: lambda v: isinstance(v, bool),
    _INT: lambda v: isinstance(v, int) and not isinstance(v, bool),
    _NUM: lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}

_VAR_TYPES: Mapping[type, VarType] = {bool: VarType.BOOL, int: VarType.INT, float: VarType.FLOAT, str: VarType.STR}


def _scalar_columns(data: Mapping[str, Any], spec: Mapping[str, str], where: str) -> dict[str, Any]:
    """Take over scalar fields per ``spec`` type-checked (``None`` = NULL)."""
    out: dict[str, Any] = {}
    for name, kind in spec.items():
        value = data.get(name)
        if value is not None and not _TYPE_CHECKS[kind](value):
            raise InvalidRecordError(
                _t("runstore_sql.expected_is", where=where, name=name, kind=kind, v4=type(value).__name__)
            )
        out[name] = float(value) if kind == _NUM and value is not None else value
    return out


def _reject_unknown(data: Mapping[str, Any], known: Sequence[str] | frozenset[str], where: str) -> None:
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise InvalidRecordError(f"{where}: unbekannte Felder {', '.join(unknown)}")


def _as_mapping(value: Any, where: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidRecordError(_t("runstore_sql.object_expected_is", where=where, v2=type(value).__name__))
    return value


def _as_list(value: Any, where: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidRecordError(_t("runstore_sql.list_expected_is", where=where, v2=type(value).__name__))
    return value


def _pr_number(value: Any, where: str) -> int | None:
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise InvalidRecordError(_t("runstore_sql.pr_pr_number_expected_is", where=where, value=value))


def _encode_var(value: Any, where: str) -> tuple[VarType, str]:
    var_type = _VAR_TYPES.get(type(value))
    if var_type is None:
        raise InvalidRecordError(_t("runstore_sql.scalar_value_expected_is", where=where, v2=type(value).__name__))
    if var_type is VarType.BOOL:
        return var_type, "true" if value else "false"
    return var_type, value if var_type is VarType.STR else repr(value)


def _decode_var(var_type: str, value: str) -> str | int | float | bool:
    if var_type == VarType.BOOL:
        return value == "true"
    if var_type == VarType.INT:
        return int(value)
    if var_type == VarType.FLOAT:
        return float(value)
    return value


@dataclass
class RecordRows:
    """A run record as rows per table (run row + child rows in insertion order)."""

    run: dict[str, Any]
    children: dict[Table, list[dict[str, Any]]] = field(default_factory=lambda: {t: [] for t in RECORD_CHILD_TABLES})


def _encode_step(rows: RecordRows, run_id: str, idx: int, step: Any) -> None:
    where = f"steps[{idx}]"
    data = _as_mapping(step, where)
    if data is None:
        raise InvalidRecordError(_t("runstore_sql.object_expected_is_none", where=where))
    _reject_unknown(data, (*_STEP_FIELDS, *_STEP_NESTED), where)
    key = {"run_id": run_id, "step_idx": idx}
    rows.children[steps].append({"run_id": run_id, "idx": idx, **_scalar_columns(data, _STEP_FIELDS, where)})
    usage = _as_mapping(data.get("usage"), f"{where}.usage")
    if usage is not None:
        _reject_unknown(usage, tuple(_USAGE_FIELDS), f"{where}.usage")
        rows.children[step_usage].append({**key, **_scalar_columns(usage, _USAGE_FIELDS, f"{where}.usage")})
    review = _as_mapping(data.get("review"), f"{where}.review")
    if review is not None:
        _reject_unknown(review, (*_REVIEW_FIELDS, "pr", "findings"), f"{where}.review")
        cols = _scalar_columns(review, _REVIEW_FIELDS, f"{where}.review")
        rows.children[review_results].append({**key, **cols, "pr": _pr_number(review.get("pr"), f"{where}.review")})
        for seq, finding in enumerate(_as_list(review.get("findings"), f"{where}.review.findings")):
            f = finding if isinstance(finding, Mapping) else {"text": finding}
            rows.children[review_findings].append(
                {**key, "seq": seq} | {k: None if f.get(k) is None else str(f[k]) for k in _FINDING_FIELDS}
            )
    for seq, check in enumerate(_as_list(data.get("verify"), f"{where}.verify")):
        c = _as_mapping(check, f"{where}.verify[{seq}]") or {}
        output = c.get("output")
        rows.children[verify_checks].append(
            {**key, "seq": seq, "name": c.get("name"), "ok": bool(c.get("ok")), "output": output and str(output)}
        )


def encode_record(record: Mapping[str, Any], project: str, run_id: str) -> RecordRows:
    """Run record → :class:`RecordRows`; :class:`InvalidRecordError` on unknown fields/wrong types.

    ``project``/``run_id`` come from the operation (storage location); a deviating ``id`` in the record is rejected."""
    if record.get("id") not in (None, run_id):
        raise InvalidRecordError(f"Record-id {record.get('id')!r} ≠ Run-Id {run_id!r}")
    _reject_unknown(record, (*_RUN_FIELDS, *_RUN_NESTED, *_RUN_IGNORED), "run")
    cols = _scalar_columns(record, _RUN_FIELDS, "run")
    cols["started"] = cols["started"] or ""
    cols["started_utc"] = utc_sort_text(cols["started"])
    rows = RecordRows({"id": run_id, "project": project, **cols})
    for idx, step in enumerate(_as_list(record.get("steps"), "steps")):
        _encode_step(rows, run_id, idx, step)
    approval = _as_mapping(record.get("approval"), "approval")
    if approval is not None:
        _reject_unknown(approval, (*_APPROVAL_FIELDS, "permissions"), "approval")
        status = _approval_status(_run_status(record), record)
        rows.children[approvals].append(
            {"run_id": run_id, "status": status.value, **_scalar_columns(approval, _APPROVAL_FIELDS, "approval")}
        )
        for seq, perm in enumerate(_as_list(approval.get("permissions"), "approval.permissions")):
            if not isinstance(perm, str):
                raise InvalidRecordError(
                    _t("runstore_sql.approval_permissions_text_expected_is", seq=seq, v2=type(perm).__name__)
                )
            rows.children[approval_permissions].append({"run_id": run_id, "seq": seq, "permission": perm})
    for scope in VarScope:
        variables = _as_mapping(record.get(scope.value), scope.value) or {}
        for name, value in variables.items():
            var_type, text = _encode_var(value, f"{scope.value}.{name}")
            rows.children[run_vars].append(
                {"run_id": run_id, "scope": scope.value, "name": name, "type": var_type.value, "value": text}
            )
    return rows


def _run_status(record: Mapping[str, Any]) -> RunStatus:
    status = record.get("status")
    try:
        return RunStatus(str(status))
    except ValueError:
        raise InvalidRecordError(f"run.status: unbekannter Status {status!r}") from None


# ---------------------------------------------------------------- Zeilen → Record
def _present(row: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    return {n: row[n] for n in names if row[n] is not None}


@dataclass
class _Children:
    """Kind-Zeilen mehrerer Runs, gruppiert nach Run-Id (bzw. Step)."""

    steps: dict[str, list[Mapping]] = field(default_factory=dict)
    usage: dict[tuple[str, int], Mapping] = field(default_factory=dict)
    reviews: dict[tuple[str, int], Mapping] = field(default_factory=dict)
    findings: dict[tuple[str, int], list[Mapping]] = field(default_factory=dict)
    checks: dict[tuple[str, int], list[Mapping]] = field(default_factory=dict)
    approvals: dict[str, Mapping] = field(default_factory=dict)
    permissions: dict[str, list[str]] = field(default_factory=dict)
    vars: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def fetch(cls, conn: Connection, run_ids: Any) -> _Children:
        """Load all child rows of the runs from ``run_ids`` (list or subquery)."""
        c = cls()

        def rows(table: Table, *order: Any) -> Iterator[Mapping]:
            yield from conn.execute(select(table).where(table.c.run_id.in_(run_ids)).order_by(*order)).mappings()

        for r in rows(steps, steps.c.run_id, steps.c.idx):
            c.steps.setdefault(r["run_id"], []).append(r)
        for r in rows(step_usage):
            c.usage[r["run_id"], r["step_idx"]] = r
        for r in rows(review_results):
            c.reviews[r["run_id"], r["step_idx"]] = r
        for r in rows(review_findings, review_findings.c.seq):
            c.findings.setdefault((r["run_id"], r["step_idx"]), []).append(r)
        for r in rows(verify_checks, verify_checks.c.seq):
            c.checks.setdefault((r["run_id"], r["step_idx"]), []).append(r)
        for r in rows(approvals):
            c.approvals[r["run_id"]] = r
        for r in rows(approval_permissions, approval_permissions.c.seq):
            c.permissions.setdefault(r["run_id"], []).append(r["permission"])
        for r in rows(run_vars, run_vars.c.name):
            c.vars.setdefault((r["run_id"], r["scope"]), {})[r["name"]] = _decode_var(r["type"], r["value"])
        return c

    def step(self, row: Mapping) -> dict[str, Any]:
        key = (row["run_id"], row["idx"])
        out = {"name": row["name"], "type": row["type"], "status": row["status"]}
        out |= _present(row, ("duration_s", "summary", "output", "failure_kind", "retry_after", "verdict"))
        if (usage := self.usage.get(key)) is not None:
            out["usage"] = _present(usage, tuple(_USAGE_FIELDS))
        if (review := self.reviews.get(key)) is not None:
            out["review"] = _present(review, (*_REVIEW_FIELDS, "pr")) | {
                "findings": [_present(f, _FINDING_FIELDS) for f in self.findings.get(key, [])]
            }
        if key in self.checks:
            out["verify"] = [
                {"name": c["name"], "ok": bool(c["ok"])} | _present(c, ("output",)) for c in self.checks[key]
            ]
        return out

    def record(self, row: Mapping) -> dict[str, Any]:
        run_id = row["id"]
        rec: dict[str, Any] = {"id": run_id, "project": row["project"]}
        rec |= _present(row, tuple(_RUN_FIELDS))
        rec |= {name: row[name] for name in _ALWAYS_PRESENT}
        rec["started"] = row["started"]
        if row["resumed"] is not None:
            rec["resumed"] = bool(row["resumed"])
        rec["steps"] = [self.step(s) for s in self.steps.get(run_id, [])]
        if (appr := self.approvals.get(run_id)) is not None:
            rec["approval"] = _present(appr, tuple(_APPROVAL_FIELDS))
            if run_id in self.permissions:
                rec["approval"]["permissions"] = self.permissions[run_id]
        for scope in VarScope:
            if (run_id, scope.value) in self.vars:
                rec[scope.value] = self.vars[run_id, scope.value]
        return rec


_MAX_IN_LIST = 500  # up to this many run ids as IN list, above that as subquery


def decode_records(conn: Connection, where: ColumnElement[bool], order: Sequence[Any] = ()) -> list[dict]:
    """Records of all runs that satisfy ``where`` (child rows via subquery, one query per table)."""
    run_rows = conn.execute(select(runs).where(where).order_by(*order)).mappings().all()
    if not run_rows:
        return []
    ids = [r["id"] for r in run_rows] if len(run_rows) <= _MAX_IN_LIST else select(runs.c.id).where(where)
    children = _Children.fetch(conn, ids)
    return [children.record(r) for r in run_rows]


# ---------------------------------------------------------------- Engine + Migration
def _sqlite_lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _configure_sqlite(engine: Engine) -> None:
    """Per connection: WAL, busy_timeout, foreign keys, Unicode ``lower``; control transactions ourselves
    (``BEGIN IMMEDIATE`` for writes — the lock applies from transaction start, no upgrade deadlock)."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.isolation_level = None  # pysqlite: kein implizites BEGIN
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={int(config.SQLITE_BUSY_TIMEOUT_MS)}")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
        dbapi_conn.create_function("lower", 1, _sqlite_lower, deterministic=True)

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        write = conn.get_execution_options().get(_WRITE_OPTION)
        conn.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")


def make_engine(url: str) -> Engine:
    """Engine for ``url``; SQLite files get their directory and the connection pragmas.

    In-memory SQLite (``sqlite://``) shares a single connection (otherwise each would see its own empty DB)."""
    parsed = make_url(url)
    is_sqlite = parsed.get_backend_name() == "sqlite"
    in_memory = is_sqlite and parsed.database in (None, "", ":memory:")
    if is_sqlite and not in_memory:
        Path(str(parsed.database)).parent.mkdir(parents=True, exist_ok=True)
    if in_memory:
        engine = create_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(url)
    if is_sqlite:
        _configure_sqlite(engine)
    return engine


def alembic_config() -> Config:
    """Alembic configuration without ``alembic.ini``: scripts from ``fabrikant/migrations``."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def migrate(engine: Engine, revision: str = "head") -> None:
    """Schema per Alembic auf ``revision`` bringen (exklusive Transaktion; parallele Starts warten)."""
    cfg = alembic_config()
    with engine.connect() as conn:
        conn.execution_options(**{_WRITE_OPTION: True})
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, revision)


# ---------------------------------------------------------------- Store
@contextlib.contextmanager
def _db_errors() -> Iterator[None]:
    """Report SQLAlchemy errors as store errors (callers catch only :class:`RunStoreError`)."""
    try:
        yield
    except IntegrityError as e:
        raise InvalidRecordError(f"DB-Constraint verletzt: {e.orig}") from e
    except SQLAlchemyError as e:
        raise RunStoreError(_t("runstore_sql.db_error", e=e)) from e


class SqlRunStore(BaseRunStore):
    """Run store on an SQL DB (SQLite in the homelab; Postgres follows, #279)."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.session_log = SqlSessionLog(self)

    @contextlib.contextmanager
    def write_tx(self) -> Iterator[Connection]:
        """Write transaction (SQLite: ``BEGIN IMMEDIATE``). Integrity errors → :class:`InvalidRecordError`,
        other DB errors → :class:`RunStoreError`; errors roll the transaction back."""
        with _db_errors(), self.engine.connect() as conn:
            conn.execution_options(**{_WRITE_OPTION: True})
            with conn.begin():
                yield conn

    @contextlib.contextmanager
    def read_tx(self) -> Iterator[Connection]:
        """Read transaction (consistent snapshot over several queries); DB errors → :class:`RunStoreError`."""
        with _db_errors(), self.engine.connect() as conn, conn.begin():
            yield conn

    # ------------------------------------------------------------ Schreiben
    def _modify(self, project: str, run_id: str, mutate: Mutation) -> dict:
        _check_ids(project, run_id)
        with self.write_tx() as conn:
            current = self._locked_record(conn, run_id)
            if current is not None and current["project"] != project:
                raise PreconditionError(
                    _t("runstore_sql.run_belongs_to_project_not", run_id=run_id, v2=current["project"], project=project)
                )
            record = mutate(current)
            encode_record(record, project, run_id)  # check first: the redaction assumes correctly typed records
            _redact_record(record)
            self._save(conn, encode_record(record, project, run_id), exists=current is not None)
        return record

    def _locked_record(self, conn: Connection, run_id: str) -> dict | None:
        conn.execute(select(runs.c.id).where(runs.c.id == run_id).with_for_update()).all()
        found = decode_records(conn, runs.c.id == run_id)
        return found[0] if found else None

    @staticmethod
    def _save(conn: Connection, rows: RecordRows, *, exists: bool) -> None:
        run_id = rows.run["id"]
        if exists:
            conn.execute(update(runs).where(runs.c.id == run_id).values(**rows.run))
            # Replace child rows; usage/reviews/checks/permissions hang off it via ON DELETE CASCADE.
            for table in (steps, approvals, run_vars):
                conn.execute(delete(table).where(table.c.run_id == run_id))
        else:
            conn.execute(insert(runs).values(**rows.run))
        for table, table_rows in rows.children.items():
            if table_rows:
                conn.execute(insert(table), table_rows)

    # ------------------------------------------------------------ Lesen
    def get_record(self, run_id: str, project: str | None = None) -> dict | None:
        if not valid_run_id(run_id) or (project and not valid_project(project)):
            return None
        where = runs.c.id == run_id
        if project:
            where = and_(where, runs.c.project == project)
        with self.read_tx() as conn:
            found = decode_records(conn, where)
        return found[0] if found else None

    @staticmethod
    def _filter(project: str | None, status: RunStatus | None = None) -> ColumnElement[bool]:
        conds: list[ColumnElement[bool]] = [true()]
        if project:
            conds.append(runs.c.project == project if valid_project(project) else literal(False))
        if status is not None:
            conds.append(runs.c.status == status.value)
        return and_(*conds)

    def load_records(self, project: str | None = None, status: RunStatus | None = None) -> list[dict]:
        with self.read_tx() as conn:
            return decode_records(conn, self._filter(project, status))

    def load_report(self, project: str | None = None) -> tuple[list[Run], LoadReport]:
        out: list[Run] = []
        report = LoadReport()
        for rec in self.load_records(project):
            try:
                out.append(parse_run(rec, rec["project"]))
            except RecordError as e:  # DB constraints rule this out; never lose silently
                report.failures.append(LoadFailure(Path(rec["project"], rec["id"]), str(e)))
        report.loaded = len(out)
        return out, report

    def loop_triggers(self, project: str, loop: str, statuses: tuple[str, ...] | None = None) -> set[str]:
        where = and_(self._filter(project), runs.c.loop == loop)
        if statuses is not None:
            where = and_(where, runs.c.status.in_(statuses))
        with self.read_tx() as conn:
            return {t for (t,) in conn.execute(select(func.coalesce(runs.c.trigger, "")).where(where).distinct())}

    def search(self, query: RunQuery) -> list[Run]:
        """Indexed ``SELECT`` with the same semantics as :meth:`RunQuery.matches`."""
        order = (runs.c.started.desc(), runs.c.project.desc(), runs.c.id.desc())
        with self.read_tx() as conn:
            ids = [
                rid
                for (rid,) in conn.execute(
                    select(runs.c.id).where(_search_filter(query)).order_by(*order).limit(query.limit + 1)
                )
            ]
            records = decode_records(conn, runs.c.id.in_(ids), order) if ids else []
        return [parse_run(r, r["project"]) for r in records]


def _word_filter(word: str) -> ColumnElement[bool]:
    """Word as substring (case-insensitive) in id, loop, trigger, status, note or a step summary."""
    cols = (runs.c.id, runs.c.loop, runs.c.trigger, runs.c.status, runs.c.note)
    in_run = [func.lower(c).contains(word, autoescape=True) for c in cols]
    in_steps = exists().where(steps.c.run_id == runs.c.id, func.lower(steps.c.summary).contains(word, autoescape=True))
    return or_(*in_run, in_steps)


def _search_filter(q: RunQuery) -> ColumnElement[bool]:
    conds: list[ColumnElement[bool]] = [SqlRunStore._filter(q.project)]
    if q.loop:
        conds.append(runs.c.loop == q.loop)
    if q.statuses:
        conds.append(runs.c.status.in_(sorted(q.statuses)))
    if q.failure_kinds:
        failed = sorted(s.value for s in FAILED_RUN_STATUSES)
        effective = func.coalesce(runs.c.failure_kind, FailureKind.UNKNOWN.value)
        conds.append(and_(runs.c.status.in_(failed), effective.in_(sorted(q.failure_kinds))))
    if q.after is not None:
        conds.append(tuple_(runs.c.started, runs.c.project, runs.c.id) < tuple_(*map(literal, q.after)))
    if q.lower is not None:
        conds.append(runs.c.started_utc >= q.lower.strftime(UTC_SORT_FORMAT))
    if q.upper is not None:
        upper = q.upper.strftime(UTC_SORT_FORMAT)
        conds.append(runs.c.started_utc < upper if q.upper_exclusive else runs.c.started_utc <= upper)
    conds += [_word_filter(w) for w in q.words]
    return and_(*conds)


class SqlSessionLog:
    """Session log (ADR 0002) in the SQL store: events, artifacts, checkpoints hang off the run.

    The session status and the token sums are not duplicated here: status is that of the run,
    sums come from the ``step_done`` events. Errors propagate (:class:`RunStoreError`) — the
    best-effort handling is done by :mod:`fabrikant.sessionlog`."""

    def __init__(self, store: SqlRunStore):
        self._store = store

    def last_seq(self, run_id: str) -> int:
        """Highest event sequence of a run (0 = no events)."""
        with self._store.read_tx() as conn:
            seq = conn.execute(
                select(func.coalesce(func.max(run_events.c.seq), 0)).where(run_events.c.run_id == run_id)
            ).scalar_one()
        return int(seq)

    def _insert(self, table: Table, **values: Any) -> None:
        with self._store.write_tx() as conn:
            conn.execute(insert(table).values(**values))

    def event(self, run_id: str, seq: int, ts: str, kind: str, step_name: str, step_type: str, status: str, data: str):
        """Append an event (``data``: opaque, redacted JSON text)."""
        self._insert(
            run_events,
            run_id=run_id,
            seq=seq,
            ts=ts,
            kind=kind,
            step_name=step_name,
            step_type=step_type,
            status=status,
            data=data,
        )

    def artifact(self, run_id: str, step_name: str, kind: str, path: str, created_at: str) -> None:
        """Artefakt eines Steps registrieren."""
        self._insert(run_artifacts, run_id=run_id, step_name=step_name, kind=kind, path=path, created_at=created_at)

    def checkpoint(self, run_id: str, step_idx: int, step_name: str, status: str, data: str) -> None:
        """Append a checkpoint; a second one for the same step violates the primary key (append-only)."""
        self._insert(run_checkpoints, run_id=run_id, step_idx=step_idx, step_name=step_name, status=status, data=data)

    def event_data(self, run_id: str, kind: str) -> list[str]:
        """``data`` aller Events ``kind`` eines Runs, in Reihenfolge."""
        stmt = (
            select(run_events.c.data)
            .where(run_events.c.run_id == run_id, run_events.c.kind == kind)
            .order_by(run_events.c.seq)
        )
        with self._store.read_tx() as conn:
            return list(conn.execute(stmt).scalars())

    def events(self, run_id: str, after: int, kinds: Sequence[str], limit: int) -> list[tuple]:
        """(id, ts, kind, step_name, data) of the events ``kinds`` with ``id > after``, at most ``limit``."""
        stmt = (
            select(run_events.c.id, run_events.c.ts, run_events.c.kind, run_events.c.step_name, run_events.c.data)
            .where(run_events.c.run_id == run_id, run_events.c.id > after, run_events.c.kind.in_(kinds))
            .order_by(run_events.c.id)
            .limit(limit)
        )
        with self._store.read_tx() as conn:
            return [tuple(r) for r in conn.execute(stmt)]

    def dump(self, run_id: str) -> dict:
        """Complete session log of a run as a JSON-capable dict
        (``events``/``checkpoints``/``artifacts``, one dict per row)."""
        with self._store.read_tx() as conn:
            events = [
                dict(r)
                for r in conn.execute(
                    select(run_events).where(run_events.c.run_id == run_id).order_by(run_events.c.id)
                ).mappings()
            ]
            checkpoints = [
                dict(r)
                for r in conn.execute(
                    select(run_checkpoints)
                    .where(run_checkpoints.c.run_id == run_id)
                    .order_by(run_checkpoints.c.step_idx)
                ).mappings()
            ]
            artifacts = [
                dict(r)
                for r in conn.execute(
                    select(run_artifacts).where(run_artifacts.c.run_id == run_id).order_by(run_artifacts.c.id)
                ).mappings()
            ]
        return {"events": events, "checkpoints": checkpoints, "artifacts": artifacts}


_STORES: dict[str, SqlRunStore] = {}
_STORES_GUARD = threading.Lock()


def store_for_url(url: str) -> SqlRunStore:
    """Store per DB URL (created once and migrated to ``head``, then reused)."""
    with _STORES_GUARD:
        store = _STORES.get(url)
        if store is None:
            engine = make_engine(url)
            try:
                migrate(engine)
            except SQLAlchemyError as e:
                engine.dispose()
                raise RunStoreError(
                    _t("runstore_sql.run_store_db_not_usable", v1=make_url(url).render_as_string(), e=e)
                ) from e
            store = _STORES[url] = SqlRunStore(engine)
        return store


def dispose_stores() -> None:
    """Close all cached stores (tests, process end)."""
    with _STORES_GUARD:
        for store in _STORES.values():
            store.engine.dispose()
        _STORES.clear()


__all__ = [
    "InvalidRecordError",
    "RecordRows",
    "SqlRunStore",
    "SqlSessionLog",
    "decode_records",
    "dispose_stores",
    "encode_record",
    "make_engine",
    "migrate",
    "store_for_url",
]
