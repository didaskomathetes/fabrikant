"""Run store (ADR 0034): operations with precondition, JSON backend and active backend (``STORE``).

:class:`BaseRunStore` defines the operations backend-neutrally; :class:`RunStore` is the
JSON backend (`runs/<project>/<id>.json`), :class:`fabrikant.runstore_sql.SqlRunStore` the relational one
(step 3). ``STORE`` delegates at call time to the backend chosen via ``FABRIKANT_RUN_STORE``.

Step 1 (read access): loading is tolerant of legacy data but never silent: a
record that cannot be mapped into the model is reported with a reason (:class:`LoadReport`).

Step 2 (write access): all run-record write operations run as a transaction with
precondition via the store (ADR 0034 no. 2): project lock → read on-disk state → check
precondition (allowed status transition per ``RUN_TRANSITIONS``, optionally expected on-disk status
``expect`` as compare-and-set) → write redacted atomically (temp file + `Path.replace`).
The same status is an allowed "transition" only for non-terminal statuses (checkpoints,
approve updates); a terminal record is not overwritten again. An illegal or stale transition raises
:class:`PreconditionError` and leaves the record unchanged. Outside the store no
run JSON is written directly any more.

Defaults for missing or wrongly typed fields:

- ``id`` and ``status`` (valid :class:`RunStatus`) are mandatory; ``project`` is the directory name
  (storage location takes precedence over the field in the record).
- Text fields (``loop``, ``trigger``, ``note``, ``spec_hash``, ``started``, ``finished``, ``error``,
  ``waiting_step``, ``approval_queued``, ``retry_of``, ``worktree_cleanup_error``): ``""``;
  non-strings → ``""``.
- ``started_by``/``approved_by``/``rejected_by``: ``None`` (records before authentication, ADR 0030).
- ``resumed``: ``False``; ``vars``: ``{}``, only scalar values (str/int/float/bool) remain;
  read-only (:class:`types.MappingProxyType`), because :class:`~fabrikant.runmodel.Run` is immutable.
- ``steps``: ``[]``; non-objects in the list are skipped, a step without a valid status is
  a load error. ``name``/``type``/``summary``/``output``: ``""``; ``duration_s``: ``None``.
- ``usage``: missing numbers ``0``, ``cost_usd`` ``None``. ``review``: ``round`` 1, ``pr`` ``None``.
- ``approval``: ``reason`` missing/unknown → ``tier`` if ``waiting_step == "tier-approval"``, otherwise ``step``.
  The :class:`ApprovalStatus` is not stored in the record and is derived: run ``rejected`` →
  ``rejected``; run ``needs_approval`` → ``queued`` (``approval_queued`` set) or ``pending``;
  otherwise ``approved`` if the run was resumed (``resumed``/``approved_by``), else ``pending``.
- ``failure_kind`` (ADR 0035): for failed runs (``fail``/``error``) or steps (``fail``/``timeout``/
  ``error``) missing or unknown → ``unknown`` (legacy data, no backfill); otherwise ``None``.
  ``retry_after``: ``""``.
- ``attempt`` (ADR 0036): ``0``; only non-negative integers remain, otherwise ``0``.
- ``steps_output`` and unknown fields are ignored.
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

from . import config, filelock
from .failures import Failure, parse_kind
from .i18n import t
from .runmodel import (
    FAILED_RUN_STATUSES,
    FAILED_STEP_STATUSES,
    RUN_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    TIER_APPROVAL_STEP,
    Approval,
    ApprovalReason,
    ApprovalStatus,
    FailureKind,
    Finding,
    ReviewResult,
    Run,
    RunStatus,
    Step,
    StepStatus,
    Usage,
    VerifyCheck,
)
from .runquery import RunQuery, sort_key
from .secrets import _redact_record

_E = TypeVar("_E", bound=Enum)

# Lock file per project directory (``<name>.lock``, no ``*.json`` → never loaded as a record).
_PROJECT_LOCK = ".runs"


class RecordError(ValueError):
    """A record cannot be mapped into the model (text = reason)."""


class RunStoreError(Exception):
    """Error writing a run record (broken on-disk state, I/O)."""


class PreconditionError(RunStoreError):
    """Precondition of a write operation violated: the run's on-disk status (or state)
    does not allow the transition. The record stays unchanged (ADR 0034 no. 2)."""


def _require_status(raw: dict, run_id: str, status: RunStatus) -> None:
    """On-disk status must be ``status`` (otherwise :class:`PreconditionError`)."""
    actual = raw.get("status")
    if actual != status.value:
        raise PreconditionError(
            t("runstore.run_on_disk_status_expected", run_id=run_id, actual=actual, v3=status.value)
        )


def valid_run_id(run_id: str) -> bool:
    """Run ids land directly in the file path (`runs/<project>/<id>.json|log`) → reject path traversal."""
    return bool(run_id) and "/" not in run_id and "\\" not in run_id and ".." not in run_id


def valid_project(project: str) -> bool:
    """Project names end up as a path component under RUNS_DIR → allow only a single segment."""
    return bool(project) and project not in (".", "..") and "/" not in project and "\\" not in project


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _opt_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _enum(enum_cls: type[_E], value: Any, what: str) -> _E:
    try:
        return enum_cls(value)
    except ValueError:
        raise RecordError(f"{what}: unknown status {value!r}") from None


def _parse_usage(d: Any) -> Usage | None:
    if not isinstance(d, dict):
        return None
    return Usage(
        model=_text(d.get("model")),
        provider=_text(d.get("provider")),
        prompt_tokens=_int(d.get("prompt_tokens")),
        completion_tokens=_int(d.get("completion_tokens")),
        total_tokens=_int(d.get("total_tokens")),
        cost_usd=_num(d.get("cost_usd")),
        cache_read_tokens=_int(d.get("cache_read_tokens")),
        cache_write_tokens=_int(d.get("cache_write_tokens")),
        billing=_text(d.get("billing")),
    )


def _pr_number(value: Any) -> int | None:
    """PR number as int (records store int or numeric string); otherwise None."""
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_review(d: Any) -> ReviewResult | None:
    if not isinstance(d, dict):
        return None
    return ReviewResult(
        verdict=_text(d.get("verdict")),
        model=_text(d.get("model")),
        summary=_text(d.get("summary")),
        pr=_pr_number(d.get("pr")),
        round=_int(d.get("round"), 1),
        findings=tuple(
            Finding(_text(f.get("severity")), _text(f.get("text")), _text(f.get("suggestion")))
            for f in _dicts(d.get("findings"))
        ),
    )


def _failure_kind(d: Mapping[str, Any], failed: bool) -> FailureKind | None:
    """``failure_kind`` only for failed runs/steps; legacy data without the field → ``unknown``."""
    return parse_kind(d.get("failure_kind")) if failed else None


def _parse_step(d: Mapping[str, Any], index: int) -> Step:
    status = _enum(StepStatus, d.get("status"), f"Step {index}")
    return Step(
        name=_text(d.get("name")),
        type=_text(d.get("type")),
        status=status,
        duration_s=_num(d.get("duration_s")),
        summary=_text(d.get("summary")),
        output=_text(d.get("output")),
        usage=_parse_usage(d.get("usage")),
        review=_parse_review(d.get("review")),
        verify=tuple(
            VerifyCheck(_text(c.get("name")), bool(c.get("ok")), _text(c.get("output")))
            for c in _dicts(d.get("verify"))
        ),
        failure_kind=_failure_kind(d, status in FAILED_STEP_STATUSES),
        retry_after=_text(d.get("retry_after")),
        verdict=_text(d.get("verdict")),
    )


def _approval_status(run_status: RunStatus, rec: Mapping[str, Any]) -> ApprovalStatus:
    if run_status is RunStatus.REJECTED:
        return ApprovalStatus.REJECTED
    if run_status is RunStatus.NEEDS_APPROVAL:
        return ApprovalStatus.QUEUED if _text(rec.get("approval_queued")) else ApprovalStatus.PENDING
    if rec.get("resumed") is True or _text(rec.get("approved_by")):
        return ApprovalStatus.APPROVED
    return ApprovalStatus.PENDING


def _parse_approval(d: Any, run_status: RunStatus, rec: Mapping[str, Any]) -> Approval | None:
    if not isinstance(d, dict):
        return None
    try:
        reason = ApprovalReason(_text(d.get("reason")))
    except ValueError:
        tier = rec.get("waiting_step") == TIER_APPROVAL_STEP
        reason = ApprovalReason.TIER if tier else ApprovalReason.STEP
    tier_no = d.get("tier")
    return Approval(
        status=_approval_status(run_status, rec),
        reason=reason,
        kind=_text(d.get("kind")),
        what=_text(d.get("what")),
        step=_text(d.get("step")),
        issue=_text(d.get("issue")),
        condition=_text(d.get("condition")),
        why=_text(d.get("why")),
        tier=tier_no if isinstance(tier_no, int) and not isinstance(tier_no, bool) else None,
    )


def attempt_of(record: Mapping[str, Any] | None) -> int:
    """0-based retry attempt count from a run record (ADR 0036).

    Single source for the ``attempt`` field: only non-negative integers
    remain; missing, broken (string, bool) or negative → ``0``. Shared by
    :func:`parse_run` and :mod:`fabrikant.retry_chains`."""
    if not isinstance(record, Mapping):
        return 0
    attempt = record.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        return 0
    return attempt


def parse_run(rec: Mapping[str, Any], project: str | None = None) -> Run:
    """Run record (dict from the JSON) → :class:`Run`; :class:`RecordError` with reason if impossible.

    ``project``: directory name, takes precedence over the field in the record. Defaults: see module doc."""
    run_id = rec.get("id")
    if not isinstance(run_id, str) or not run_id:
        raise RecordError(t("runstore.id_missing_or_not_a"))
    proj = project or _text(rec.get("project"))
    if not proj:
        raise RecordError(t("runstore.project_missing"))
    status = _enum(RunStatus, rec.get("status"), "Run")
    steps = tuple(_parse_step(s, i) for i, s in enumerate(_dicts(rec.get("steps")), 1))
    raw_vars = rec.get("vars")
    # MappingProxyType: Run.vars stays read-only (Run is frozen, ADR 0034).
    run_vars = MappingProxyType(
        {k: v for k, v in raw_vars.items() if isinstance(v, (str, int, float, bool))}
        if isinstance(raw_vars, dict)
        else {}
    )
    return Run(
        id=run_id,
        project=proj,
        loop=_text(rec.get("loop")),
        status=status,
        started=_text(rec.get("started")),
        finished=_text(rec.get("finished")),
        trigger=_text(rec.get("trigger")),
        note=_text(rec.get("note")),
        spec_hash=_text(rec.get("spec_hash")),
        steps=steps,
        approval=_parse_approval(rec.get("approval"), status, rec),
        waiting_step=_text(rec.get("waiting_step")),
        resumed=rec.get("resumed") is True,
        approval_queued=_text(rec.get("approval_queued")),
        started_by=_opt_text(rec.get("started_by")),
        approved_by=_opt_text(rec.get("approved_by")),
        rejected_by=_opt_text(rec.get("rejected_by")),
        error=_text(rec.get("error")),
        failure_kind=_failure_kind(rec, status in FAILED_RUN_STATUSES),
        retry_after=_text(rec.get("retry_after")),
        retry_of=_text(rec.get("retry_of")),
        replay_of=_text(rec.get("replay_of")),
        golden=_text(rec.get("golden")),
        attempt=attempt_of(rec),
        worktree_cleanup_error=_text(rec.get("worktree_cleanup_error")),
        vars=run_vars,
    )


@dataclass(frozen=True)
class LoadFailure:
    """A record that cannot be loaded: location (file or ``<project>/<id>``) + reason."""

    path: Path
    reason: str


@dataclass
class LoadReport:
    """Result of a full pass over all records: number loaded + failures with reason."""

    loaded: int = 0
    failures: list[LoadFailure] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Number of records read."""
        return self.loaded + len(self.failures)


def _check_transition(raw: Mapping[str, Any], record: Mapping[str, Any], expect: RunStatus | None = None) -> None:
    """Stored status → record status must be in ``RUN_TRANSITIONS`` or identical for a
    non-terminal status (checkpoint/approve updates write the same
    status). ``expect``: the stored status must be exactly this (compare-and-set on the
    state the caller read — e.g. ``needs_approval → running`` rules out a
    second resume, ``queued → error`` does not overwrite a run that has meanwhile started).

    Unknown stored status → :class:`RunStoreError` (broken record);
    illegal transition → :class:`PreconditionError`."""
    cur, tgt = raw.get("status"), record.get("status")
    if not isinstance(cur, str) or not isinstance(tgt, str):
        raise RunStoreError(f"corrupt run record: unknown status {cur!r}")
    try:
        current = RunStatus(cur)
        target = RunStatus(tgt)
    except ValueError as e:
        raise RunStoreError(f"corrupt run record: unknown status {cur!r}") from e
    run_id = record.get("id") or "?"
    if expect is not None and current is not expect:
        raise PreconditionError(
            t("runstore.run_on_disk_status_expected_2", run_id=run_id, v2=current.value, v3=expect.value)
        )
    same = target is current and current not in TERMINAL_RUN_STATUSES
    if not same and target not in RUN_TRANSITIONS[current]:
        raise PreconditionError(t("runstore.run_illegal_transition", run_id=run_id, v2=current.value, v3=target.value))


def _existing(raw: dict | None, run_id: str) -> dict:
    if raw is None:
        raise PreconditionError(t("runstore.run_does_not_exist", run_id=run_id))
    return raw


# Mutation of a record within a store transaction: stored state (None = missing) → new state.
Mutation = Callable[[dict | None], dict]


class BaseRunStore(ABC):
    """Backend-neutral run-store operations (ADR 0034 no. 3).

    Every write operation is a :data:`Mutation` that the backend executes atomically in :meth:`_modify`:
    read stored state exclusively → check precondition → write redacted. An
    illegal or stale transition raises :class:`PreconditionError` and leaves the run
    unchanged. Read operations return raw records (``dict``, for engine/API) or the
    typed model (:class:`~fabrikant.runmodel.Run`)."""

    # ------------------------------------------------------------ Backend-Primitive
    @abstractmethod
    def _modify(self, project: str, run_id: str, mutate: Mutation) -> dict:
        """Apply ``mutate`` exclusively to the stored state and store the result
        redacted (in place, see :func:`fabrikant.secrets._redact_record`); returns it.
        Invalid project/run ids → :class:`RunStoreError`."""

    @abstractmethod
    def get_record(self, run_id: str, project: str | None = None) -> dict | None:
        """Raw record by id (optionally in the project); None if unknown or id invalid."""

    @abstractmethod
    def load_records(self, project: str | None = None, status: RunStatus | None = None) -> list[dict]:
        """Raw records (without ``steps_output``, ``project`` = storage location) of all or one project,
        optionally only with ``status``; unsorted. Unreadable records are skipped."""

    @abstractmethod
    def load_report(self, project: str | None = None) -> tuple[list[Run], LoadReport]:
        """Load all runs typed; those that cannot be mapped land in the report with reason."""

    # ------------------------------------------------------------ Lesen
    def load_all(self, project: str | None = None) -> list[Run]:
        """All loadable runs (unsorted); unloadable records are skipped (see :meth:`load_report`)."""
        return self.load_report(project)[0]

    def get(self, run_id: str, project: str | None = None) -> Run | None:
        """Run by id (optionally in the project); None if unknown, invalid or not loadable."""
        try:
            rec = self.get_record(run_id, project)
            return parse_run(rec, project or None) if rec is not None else None
        except (RecordError, RunStoreError):
            return None

    def loop_triggers(self, project: str, loop: str, statuses: tuple[str, ...] | None = None) -> set[str]:
        """Trigger strings of all runs of ``project``/``loop`` (optionally only with one of the ``statuses``)."""
        return {
            str(r.get("trigger", ""))
            for r in self.load_records(project)
            if r.get("loop") == loop and (statuses is None or r.get("status") in statuses)
        }

    def search(self, query: RunQuery) -> list[Run]:
        """Hits of ``query``, newest first (:func:`~fabrikant.runquery.sort_key`), at most
        ``query.limit + 1`` (the surplus one indicates a next page).

        Records with an unknown status cannot be represented in the model and are therefore not
        in the hits (visible only via :meth:`load_report`, ADR 0034)."""
        hits = sorted(filter(query.matches, self.load_all(query.project)), key=sort_key, reverse=True)
        return hits[: query.limit + 1]

    # ------------------------------------------------------------ Writing (ADR 0034, step 2)
    def _write_with_precondition(
        self,
        project: str,
        run_id: str,
        record: dict,
        *,
        create: bool = False,
        allow_missing: bool = False,
        expect: RunStatus | None = None,
    ) -> dict:
        """Write a record if the precondition holds on the stored state.

        ``create=True``: the run must not exist (new creation). ``allow_missing=True``: it
        may be missing (new creation without existence check, e.g. queued placeholder that `run_loop`
        overwrites at start). Otherwise the stored status must be able to transition
        to the record status per ``RUN_TRANSITIONS`` (see :func:`_check_transition`, ``expect``:
        expected status). Returns the passed record; :class:`PreconditionError` if violated."""

        def mutate(raw: dict | None) -> dict:
            if create:
                if raw is not None:
                    raise PreconditionError(
                        t("runstore.run_already_exists_status", run_id=run_id, v2=raw.get("status"))
                    )
            elif raw is None:
                if not allow_missing:
                    raise PreconditionError(t("runstore.run_does_not_exist", run_id=run_id))
            else:
                _check_transition(raw, record, expect)
            return record

        return self._modify(project, run_id, mutate)

    def create(self, project: str, run_id: str, record: dict) -> dict:
        """Create a new record; the run must not exist (otherwise :class:`PreconditionError`)."""
        return self._write_with_precondition(project, run_id, record, create=True)

    def write(
        self,
        project: str,
        run_id: str,
        record: dict,
        *,
        allow_missing: bool = False,
        expect: RunStatus | None = None,
    ) -> dict:
        """Write a record (engine checkpoints); the transition from the stored status must be allowed per
        ``RUN_TRANSITIONS`` (or identical for a non-terminal status).

        ``allow_missing=True``: the run may also be missing (new creation, e.g. queued placeholder
        that `run_loop` overwrites at start). ``expect``: the stored status must be exactly
        this (otherwise :class:`PreconditionError`)."""
        return self._write_with_precondition(project, run_id, record, allow_missing=allow_missing, expect=expect)

    def approve(self, project: str, run_id: str, record: dict, actor: str | None = None) -> dict:
        """Approval audit: set ``approved_by`` (status stays ``needs_approval``).

        Only from ``needs_approval``. ``approved_by`` is pure audit, no lock: if the
        resume fails afterwards (or is discarded), it may be approved or rejected again.
        Only the resume itself is exclusive (``needs_approval → running`` with ``expect``, see
        :meth:`write`) — a concurrent reject wins or loses there."""
        if actor:
            record["approved_by"] = actor
        record["status"] = RunStatus.NEEDS_APPROVAL.value
        return self._write_with_precondition(project, run_id, record, expect=RunStatus.NEEDS_APPROVAL)

    def reject(self, project: str, run_id: str, record: dict, actor: str | None = None) -> dict:
        """Decide rejection: set status ``rejected`` + ``rejected_by``.

        Only from ``needs_approval`` — also with a waiting approval (``approval_queued``):
        the worker checks the status after acquiring the slot and no longer resumes. The
        queue marker is removed."""
        if actor:
            record["rejected_by"] = actor
        record["status"] = RunStatus.REJECTED.value
        record.pop("approval_queued", None)
        return self._write_with_precondition(project, run_id, record, expect=RunStatus.NEEDS_APPROVAL)

    def mark_approval_queued(self, project: str, run_id: str, record: dict, actor: str | None = None) -> dict:
        """Approval queued (no slot free): set ``approval_queued`` + ``approved_by`` (ADR 0015).

        Only from ``needs_approval`` (otherwise :class:`PreconditionError`)."""
        record["approval_queued"] = config.utcnow().isoformat()
        if actor:
            record["approved_by"] = actor
        record["status"] = RunStatus.NEEDS_APPROVAL.value
        return self._write_with_precondition(project, run_id, record, expect=RunStatus.NEEDS_APPROVAL)

    def revoke_approval(self, project: str, run_id: str) -> dict:
        """Revoke approval without a resume having begun (worker not started or
        discarded): clear ``approved_by``, remove ``approval_queued``.

        Read-modify-write, only from ``needs_approval`` — a run that has meanwhile been resumed or
        rejected stays untouched (:class:`PreconditionError`)."""

        def mutate(raw: dict | None) -> dict:
            raw = _existing(raw, run_id)
            _require_status(raw, run_id, RunStatus.NEEDS_APPROVAL)
            raw["approved_by"] = None
            raw.pop("approval_queued", None)
            return raw

        return self._modify(project, run_id, mutate)

    def update_fields(self, project: str, run_id: str, fields: dict) -> dict:
        """Complement an existing record with ``fields`` (read-modify-write).

        The stored state is re-read and only the passed fields are set —
        intermediate changes (e.g. ``approved_by``/``approval_queued`` by an
        approve) are not lost. The status stays unchanged; a missing run
        raises :class:`PreconditionError`."""

        def mutate(raw: dict | None) -> dict:
            raw = _existing(raw, run_id)
            raw.update(fields)
            return raw

        return self._modify(project, run_id, mutate)

    def reap(self, project: str, run_id: str, *, error: str) -> dict:
        """Abort a running run from outside (reaper, dead-man's switch): status ``error`` + ``interrupted``.

        Read-modify-write: only the error fields are applied to the current stored state,
        an intermediate engine checkpoint is not lost. Only from
        ``running`` — a run the engine has already ended is not overwritten
        (otherwise :class:`PreconditionError`)."""

        def mutate(raw: dict | None) -> dict:
            raw = _existing(raw, run_id)
            _require_status(raw, run_id, RunStatus.RUNNING)
            raw["status"] = RunStatus.ERROR.value
            raw["error"] = error
            raw.update(Failure(FailureKind.INTERRUPTED).fields())
            raw["finished"] = config.utcnow().isoformat()
            return raw

        return self._modify(project, run_id, mutate)


def _check_ids(project: str, run_id: str) -> None:
    """Invalid project/run ids (path traversal) → :class:`RunStoreError`."""
    if not valid_project(project):
        raise RunStoreError(t("runstore.invalid_project_name", project=project))
    if not valid_run_id(run_id):
        raise RunStoreError(t("runstore.invalid_run_id", run_id=run_id))


# File identity for the parse cache: (mtime_ns, size, inode).
FileStamp = tuple[int, int, int]


class RunStore(BaseRunStore):
    """JSON backend: run records under ``runs_dir`` (default ``config.RUNS_DIR``, at call time).

    Writes run under a project lock (lock file + thread lock) and write atomically
    (temp file + `Path.replace`).

    Reading caches parsed runs per file, valid as long as ``(mtime_ns, size, inode)`` stays the same:
    search/lists would otherwise rescan all records on every request (JSON + ``parse_run`` ≈ O(n)).
    Atomic writes create a new inode, so external changes are noticed too."""

    def __init__(self, runs_dir: Path | None = None):
        self._runs_dir = runs_dir
        self._parsed: dict[Path, tuple[FileStamp, Run]] = {}
        self._parsed_lock = threading.Lock()

    @property
    def root(self) -> Path:
        """Root directory of the records."""
        return self._runs_dir or config.RUNS_DIR

    def _dirs(self, project: str | None) -> list[Path]:
        if project:
            return [self.root / project] if valid_project(project) else []
        return sorted(self.root.iterdir()) if self.root.is_dir() else []

    def _files(self, project: str | None) -> Iterator[Path]:
        for d in self._dirs(project):
            if d.is_dir():
                yield from d.glob("*.json")

    def _read(self, f: Path) -> Run:
        """File → run (cached, see class); :class:`RecordError` on broken JSON/structure.
        Other I/O errors propagate."""
        try:
            st = f.stat()
        except FileNotFoundError:
            raise RecordError("file vanished") from None
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        cached = self._parsed.get(f)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        run = self._parse_file(f)
        with self._parsed_lock:
            self._parsed[f] = (stamp, run)
        return run

    @staticmethod
    def _parse_file(f: Path) -> Run:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RecordError("file vanished") from None
        except ValueError as e:  # JSONDecodeError and UnicodeDecodeError
            raise RecordError(t("runstore.not_valid_json", e=e)) from e
        if not isinstance(data, dict):
            raise RecordError(t("runstore.expected_object_is", v1=type(data).__name__))
        return parse_run(data, f.parent.name)

    def load_report(self, project: str | None = None) -> tuple[list[Run], LoadReport]:
        """Load all records; unloadable ones land in the report with reason instead of being silently lost."""
        runs: list[Run] = []
        report = LoadReport()
        seen: set[Path] = set()
        for f in self._files(project):
            seen.add(f)
            try:
                runs.append(self._read(f))
            except RecordError as e:
                report.failures.append(LoadFailure(f, str(e)))
        self._forget_unseen(set(self._dirs(project)), seen)
        report.loaded = len(runs)
        return runs, report

    def _forget_unseen(self, scanned: set[Path], seen: set[Path]) -> None:
        """Remove cache entries of deleted records (file in a scanned directory but no longer there)."""
        with self._parsed_lock:
            for f in [f for f in self._parsed if f.parent in scanned and f not in seen]:
                del self._parsed[f]

    def get_record(self, run_id: str, project: str | None = None) -> dict | None:
        """Raw record by id (``project`` = storage location); corrupt record → :class:`RunStoreError`."""
        if not valid_run_id(run_id):
            return None
        for d in self._dirs(project):
            raw = self._read_raw(d / f"{run_id}.json") if d.is_dir() else None
            if raw is not None:
                raw["project"] = d.name
                return raw
        return None

    def load_records(self, project: str | None = None, status: RunStatus | None = None) -> list[dict]:
        out = []
        for f in self._files(project):
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
            except (
                ValueError,
                FileNotFoundError,
            ) as e:  # broken JSON / race with deletion; other I/O errors propagate
                print(t("runstore.fabrikant_run_record_skipped", v1=f.name, e=e))
                continue
            if not isinstance(r, dict):
                print(t("runstore.fabrikant_run_record_skipped_expected", v1=f.name, v2=type(r).__name__))
                continue
            if status is not None and r.get("status") != status.value:
                continue
            r.pop("steps_output", None)
            r["project"] = f.parent.name  # Storage location takes precedence (like parse_run)
            out.append(r)
        return out

    def raw_records(self) -> Iterator[tuple[Path, dict | RunStoreError]]:
        """Every record file raw, sorted (migration source, #278): (path, record) or
        (path, error with reason) for broken files; ones deleted meanwhile drop out."""
        for f in sorted(self._files(None)):
            try:
                raw = self._read_raw(f)
            except RunStoreError as e:
                yield f, e
                continue
            if raw is not None:
                yield f, raw

    # ------------------------------------------------------------ Write access
    def _path(self, project: str, run_id: str) -> Path:
        """Path of the run record; invalid project/run ids raise :class:`RunStoreError`."""
        _check_ids(project, run_id)
        return self.root / project / f"{run_id}.json"

    def _read_raw(self, path: Path) -> dict | None:
        """Raw on-disk record (dict) or None if the file is missing. Broken JSON → :class:`RunStoreError`."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError as e:  # JSONDecodeError and UnicodeDecodeError
            raise RunStoreError(f"corrupt run record {path.name}: {e}") from e
        if not isinstance(data, dict):
            raise RunStoreError(t("runstore.broken_run_record_expected_object", v1=path.name, v2=type(data).__name__))
        return data

    def _atomic_write(self, path: Path, record: dict) -> None:
        """Write a record atomically (temp file + `Path.replace`); `OSError` propagates.

        Redaction happens **in place**: the passed record loses its secrets, so that
        no in-memory state (e.g. the engine's `rn.record`) carries tokens that only the on-disk copy
        would have masked."""
        path.parent.mkdir(parents=True, exist_ok=True)
        _redact_record(record)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _lock(self, project: str) -> contextlib.AbstractContextManager[None]:
        """Exclusive lock for all records of a project (``runs/<project>/.runs.lock``).

        One lock per project instead of per run: the lock file and the thread lock in
        :mod:`fabrikant.filelock` exist only once per project (not per run id, which would never
        be cleaned up). The protected sections are short (read + atomic write)."""
        return filelock.locked(self.root / project / _PROJECT_LOCK)

    def _modify(self, project: str, run_id: str, mutate: Mutation) -> dict:
        path = self._path(project, run_id)
        with self._lock(project):
            record = mutate(self._read_raw(path))
            self._atomic_write(path, record)
        return record


class ActiveRunStore:
    """Delegates every access to the configured backend (``FABRIKANT_RUN_STORE``, at call time).

    Modules bind ``STORE`` at import; the backend (JSON or SQL, :func:`fabrikant.config.run_store_backend`)
    is only resolved on attribute access — tests and switching (#278) need no re-import."""

    def backend(self) -> BaseRunStore:
        """Active backend; the SQL store is created (and migrated) once per DB URL."""
        if config.run_store_backend() == config.RUN_STORE_SQL:
            from .runstore_sql import store_for_url  # local: load SQLAlchemy only when used

            return store_for_url(config.db_url())
        return _JSON_STORE

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend(), name)


_JSON_STORE = RunStore()

# Shared store instance: runs are only read and written via the store (ADR 0034).
STORE: Any = ActiveRunStore()
