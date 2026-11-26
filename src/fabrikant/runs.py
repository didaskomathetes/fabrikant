"""Run records: run slots (max_parallel, dedup, priority), directories, list/get.

Reading goes exclusively through the run store (:data:`fabrikant.runstore.STORE`, ADR 0034) — JSON or
SQL backend. Run logs (``runs/<project>/<id>.log``) remain files."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import TypedDict

from . import config, metrics, sessionlog
from .i18n import t
from .runmodel import Run, RunStatus, StepStatus
from .runstore import STORE, RecordError, RunStoreError, parse_run, valid_project, valid_run_id

LOCK_KEY_SEP = "::"  # separates project and loop in the scope key; forbidden in names (specs._validate_spec)

# Status mark per step status (CLI `runs show`, `fabrikant run`).
STEP_MARKS = {"pass": "✔", "fail": "✖", "timeout": "⏱", "error": "✖", "waiting": "⏸", StepStatus.SKIPPED.value: "–"}


def uses_worktree(loop: dict) -> bool:
    """Loop needs a run worktree (default). ``worktree: false`` = no (ADR 0020)."""
    return loop.get("worktree", True) is not False


def loop_lock_key(project: str, loop_name: str, loop: dict | None) -> str:
    """Scope key of a loop from its spec definition (ADR 0028/0020).

    Worktree loops share the project's slots (key = project name,
    limited by ``max_parallel``). Loops with ``worktree: false`` occupy
    no slot: own key per loop, they run in parallel to all
    other loops. Unknown loop (``None``) → project scope (safe default)."""
    if isinstance(loop, dict) and not uses_worktree(loop):
        return f"{project}{LOCK_KEY_SEP}{loop_name}"
    return project


def lock_key(project: str, loop_name: str) -> str:
    """Scope key for ``project``/``loop_name`` per the current spec state.

    Unknown project/loop or broken spec → project scope."""
    from .specs import load_projects  # local import: specs → branches → … (avoid import cycle)

    spec = load_projects().get(project) or {}
    loop = (spec.get("loops") or {}).get(loop_name) if not spec.get("_error") else None
    return loop_lock_key(project, loop_name, loop)


class RunClaim:
    """Occupied run slot (ADR 0028); `release()` is idempotent."""

    def __init__(self, project: str, scope: str, key: tuple[str, str, str]):
        self.scope = scope
        self._project, self._key, self._released = project, key, False

    @property
    def uses_slot(self) -> bool:
        """True if the claim occupies a ``max_parallel`` slot (worktree loop)."""
        return self.scope == self._project

    def release(self) -> None:
        with _slots_cond:
            if self._released:
                return
            self._released = True
            _active_keys.discard(self._key)
            if self.uses_slot:
                _slots_used[self._project] = max(0, _slots_used.get(self._project, 0) - 1)
            _slots_cond.notify_all()


_slots_cond = threading.Condition()
_slots_used: dict[str, int] = {}
_active_keys: set[tuple[str, str, str]] = set()
# Priority waiters per scope (approved resumes, queued manual runs): new
# trigger runs yield to them, otherwise they starve (ADR 0015).
_priority_waiters: dict[str, int] = {}


def max_parallel_for(project: str, spec: dict | None = None) -> int:
    """Allowed parallel worktree runs of a project.

    From `max_parallel` of the spec (default `config.DEFAULT_MAX_PARALLEL`). Specs
    without `repo:` work without isolation directly in `root` → fixed 1 (ADR 0028).
    `spec` already loaded → no repeated `load_projects()`."""
    from .specs import load_projects

    try:
        if spec is None:
            spec = load_projects().get(project) or {}
        if spec and not spec.get("repo"):
            return 1
        value = int(spec.get("max_parallel", config.DEFAULT_MAX_PARALLEL))
    except (OSError, ValueError, TypeError):
        value = config.DEFAULT_MAX_PARALLEL
    return max(1, value)


def dedupe_key(variables: dict | None) -> str:
    """Issue/PR reference of a run ("" = loop without reference, e.g. cron)."""
    for k in config.DEDUPE_VARS:
        if variables and variables.get(k) not in (None, ""):
            return f"{k}={variables[k]}"
    return ""


def mark_priority_waiter(scope: str, delta: int) -> None:
    """Count priority waiters of a scope (:func:`lock_key`) (+1 when queueing, -1 on abort)."""
    with _slots_cond:
        n = _priority_waiters.get(scope, 0) + delta
        if n > 0:
            _priority_waiters[scope] = n
        else:
            _priority_waiters.pop(scope, None)
        _slots_cond.notify_all()


def paused_dedupe_keys(project: str, loop: str, exclude_run_id: str | None = None) -> set[str]:
    """Issue/PR references (:func:`dedupe_key`) of paused `needs_approval` runs of loop `loop`.

    `exclude_run_id` leaves out the run that is being resumed itself —
    its own record would otherwise block the resume."""
    keys: set[str] = set()
    for r in STORE.load_records(project, RunStatus.NEEDS_APPROVAL):
        if r.get("id") == exclude_run_id or r.get("loop") != loop:
            continue
        vars_ = r.get("vars")
        key = dedupe_key(vars_ if isinstance(vars_, dict) else None)
        if key:
            keys.add(key)
    return keys


def claim_run(
    project: str,
    loop: str,
    variables: dict | None = None,
    *,
    blocking: bool = False,
    registered: bool = False,
    exclude_run_id: str | None = None,
) -> RunClaim | None:
    """Reserves a run (ADR 0028): dedup + slot (max_parallel) for worktree loops.

    None if for loop + issue/PR a run is already active or paused
    (`needs_approval`) (dedup), the limit
    is reached or a priority waiter in the same scope is waiting (ADR 0015) and
    `blocking=False`. `blocking=True` is a priority claim: it waits for a
    free slot **and** for the end of an active run of the same loop +
    issue/PR (resume after approval, queued manual run), and new
    non-blocking claims yield to it meanwhile. ``registered=True``: the
    caller has already registered synchronously via :func:`mark_priority_waiter`
    (before the thread start, so trigger runs yield immediately); the entry
    is removed again here. `exclude_run_id` (resume): the resumed run
    itself does not block its own claim."""
    scope = lock_key(project, loop)
    needs_slot = scope == project
    limit = max_parallel_for(project) if needs_slot else 0
    key = (project, loop, dedupe_key(variables))
    # Paused runs release their claim but keep occupying the reference; the
    # resume (blocking or exclude_run_id) is exempt. File I/O deliberately
    # outside the lock.
    if not blocking and key[2] and key[2] in paused_dedupe_keys(project, loop, exclude_run_id):
        return None
    with _slots_cond:
        if blocking and not registered:
            _priority_waiters[scope] = _priority_waiters.get(scope, 0) + 1
        try:
            while True:
                free = key not in _active_keys and (not needs_slot or _slots_used.get(project, 0) < limit)
                if free and (blocking or not _priority_waiters.get(scope)):
                    break
                if not blocking:
                    return None
                _slots_cond.wait()
        finally:
            if blocking:
                n = _priority_waiters.get(scope, 0) - 1
                if n > 0:
                    _priority_waiters[scope] = n
                else:
                    _priority_waiters.pop(scope, None)
        _active_keys.add(key)
        if needs_slot:
            _slots_used[project] = _slots_used.get(project, 0) + 1
    return RunClaim(project, scope, key)


def has_priority_waiter(scope: str) -> bool:
    """True if a priority claim is waiting in the scope (:func:`lock_key`)."""
    with _slots_cond:
        return bool(_priority_waiters.get(scope))


def slot_snapshot() -> dict:
    """Slots & waiters per scope (schedule view): used/limit context without spec access.

    `slots_used`/`priority_waiters` per scope, `active` = active (project, loop,
    dedupe) keys — the UI can derive from them what a waiting run is
    concretely waiting for (slot free? same loop+issue active?)."""
    with _slots_cond:
        return {
            "slots_used": dict(_slots_used),
            "active": [list(k) for k in _active_keys],
            "priority_waiters": {k: v for k, v in _priority_waiters.items() if v},
        }


def runs_dir_for(project: str) -> Path:
    d = config.RUNS_DIR / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_runs(project: str | None = None) -> list[Run]:
    """Typed runs of all or one project (unsorted); unloadable records are skipped.

    Read path of the :class:`~fabrikant.runstore.RunStore`; ``project`` is the directory name. Unloadable
    records (with reason) are provided by ``STORE.load_report()``."""
    return STORE.load_all(project)


def find_run(run_id: str, project: str | None = None) -> Run | None:
    """Typed run by id (optionally in the project); None if unknown or unloadable."""
    return STORE.get(run_id, project)


def pending_approval_runs(project: str | None = None) -> list[Run]:
    """Runs in status ``needs_approval`` (approval open or queued), newest first.

    Typed counterpart to :func:`pending_approvals` (``list[dict]``). The dict-based
    function is replaced by this one in step 2 (ADR 0034, #276) and removed —
    callers should use :func:`pending_approval_runs`."""
    out = [r for r in load_runs(project) if r.status is RunStatus.NEEDS_APPROVAL]
    out.sort(key=lambda r: r.started, reverse=True)
    return out


class LastRun(TypedDict):
    """JSON projection of the most recent run of a loop (``failure_kind`` ``""`` = none)."""

    status: str
    finished: str
    id: str
    failure_kind: str


def last_run_per_loop(runs: Iterable[Run]) -> dict[str, LastRun]:
    """Most recent run (by ``started``) per loop as {loop: LastRun}.

    Expects *all* runs of a project (:func:`load_runs` or :func:`parse_runs`),
    not just the N newest — otherwise rarely running loops appear in the UI
    as "never ran". Runs without loop are skipped; unloadable records
    (missing ``id``, unknown status) were already discarded during parsing."""
    newest: dict[str, Run] = {}
    for r in runs:
        cur = newest.get(r.loop)
        if r.loop and (cur is None or r.started > cur.started):
            newest[r.loop] = r
    return {
        loop: {
            "status": r.status.value,
            "finished": r.finished,
            "id": r.id,
            "failure_kind": r.failure_kind.value if r.failure_kind else "",
        }
        for loop, r in newest.items()
    }


def _started_key(r: dict) -> str:
    """Sort key: `started` as string (damaged non-strings sort as empty)."""
    started = r.get("started")
    return started if isinstance(started, str) else ""


def list_runs(project: str | None = None, limit: int | None = 50) -> list[dict]:
    """Run records (without steps_output), newest first; limit=None = all."""
    # Collect all runs, then sort globally by start time and only then
    # truncate. (Formerly: early return per project directory → with several
    # projects the "newest" of the first directory came first, no matter
    # how old they were.)
    out = STORE.load_records(project)
    out.sort(key=_started_key, reverse=True)
    return out[:limit]


def load_records(project: str | None = None) -> list[dict]:
    """Raw run records (without steps_output) of all or one project, unsorted."""
    return STORE.load_records(project)


def parse_runs(records: Iterable[dict], project: str) -> list[Run]:
    """Raw records of `project` → typed runs; unloadable ones are skipped (like :func:`load_runs`)."""
    out = []
    for rec in records:
        try:
            out.append(parse_run(rec, project))
        except RecordError:
            continue
    return out


def select_pending_approvals(records: Iterable[dict]) -> list[dict]:
    """Records in status `needs_approval` with valid `id` from `records`, newest first."""
    out = [r for r in records if r.get("status") == "needs_approval" and isinstance(r.get("id"), str) and r["id"]]
    out.sort(key=_started_key, reverse=True)
    return out


def pending_approvals(project: str | None = None) -> list[dict]:
    """Open approval requests: runs in status `needs_approval`, newest first.

    .. note::
        Typed counterpart: :func:`pending_approval_runs` (``list[Run]``). This
        dict-based function is removed in step 2 (ADR 0034, #276) —
        new callers should use :func:`pending_approval_runs`.

    `steps_output` is removed as in :func:`list_runs`. project=None →
    all projects. The UI shows from it the count per project and the
    expandable list (click opens the run-log modal with the
    approval context). Records without a valid (non-empty string) `id` are
    skipped so a damaged record does not topple `/api/projects`.
    Whoever has the records loaded anyway uses :func:`select_pending_approvals`."""
    return select_pending_approvals(load_records(project))


def loop_run_triggers(project: str, loop: str, statuses: tuple[str, ...] | None = None) -> set[str]:
    """Trigger strings (e.g. "on_issue:#12") of all runs of `project`/`loop`.

    Persistent source for reconcile triggers (survives restarts). `statuses`
    optionally filters by run status; without a filter every run counts. Unreadable
    records are skipped by the store; unexpected errors (e.g.
    PermissionError, DB error) are NOT swallowed but propagated to the
    calling error path (on_issue poll or scheduler loop) —
    an incomplete set would break the reconcile idempotence (double run)."""
    if not valid_project(project):
        return set()
    return STORE.loop_triggers(project, loop, statuses)


def reap_orphaned_runs() -> list[str]:
    """Server start: orphaned 'running' records → error (no alert, log only).

    Single node: runs live only in the server process, so a 'running' record before
    the start is dead. `needs_approval` stays (paused, resumable).
    Every cleaned-up run updates its retry chain (ADR 0036: the
    restart consumes the attempt, the next one is planned after backoff).
    Returns the cleaned-up run ids."""
    from . import retry_chains  # local: retry_chains imports get_run from this module
    from .specs import load_projects

    reaped: list[str] = []
    projects: dict[str, dict] | None = None  # load once, only when a run is cleaned up
    try:
        running = STORE.load_records(status=RunStatus.RUNNING)
    except (OSError, RunStoreError) as e:
        print(t("runs.fabrikant_orphaned_runs_unreadable", e=e))
        running = []
    for r in running:
        project, run_id = r["project"], r.get("id", "")
        # Via the store: precondition `running` — a run that has meanwhile a
        # different status is not overwritten (PreconditionError → skip).
        try:
            r = STORE.reap(project, run_id, error=t("runs.restart_error"))
        except (RunStoreError, OSError):
            continue
        sessionlog.finish_orphaned_run(run_id, r["finished"])
        if projects is None:
            projects = load_projects()
        retry_chains.record_run_end(project, r, projects)
        metrics.FABRIKANT_REAPED_RUNS_TOTAL.inc(reason="restart")
        reaped.append(run_id)
        print(f"[fabrikant] orphaned run {run_id} → error (restart)")
    # Release remaining claims of runs that are no longer running (ADR 0036,
    # decision 3) — otherwise the chain would stay claimed forever.
    retry_chains.release_stale_claims()
    return reaped


_valid_run_id = valid_run_id
_valid_project = valid_project


def get_run(run_id: str, project: str | None = None) -> dict | None:
    """Raw run record by id; first in the project, otherwise across all projects (fallback).

    None if id/project is invalid, the run is unknown or its record is broken (is logged)."""
    if not _valid_run_id(run_id) or (project and not _valid_project(project)):
        return None
    try:
        found = STORE.get_record(run_id, project) if project else None
        return found if found is not None else STORE.get_record(run_id)
    except RunStoreError as e:
        print(t("runs.fabrikant_run_unreadable", run_id=run_id, e=e))
        return None


def export_run(run_id: str, project: str | None = None) -> dict | None:
    """Run record + session log as a JSON-capable dict (``fabrikant runs export``).

    ``session_log`` contains ``events``/``checkpoints``/``artifacts`` (both backends).
    None if the run is unknown (like :func:`get_run`)."""
    rec = get_run(run_id, project)
    if rec is None:
        return None
    return {**rec, "session_log": sessionlog.session_log_dump(run_id)}


def show_run(run_id: str, project: str | None = None) -> str | None:
    """Human-readable single run (``fabrikant runs show``): fields, steps with status mark,
    session-log counts. None if the run is unknown (like :func:`get_run`)."""
    rec = get_run(run_id, project)
    if rec is None:
        return None
    lines = [
        f"{rec['id']}  {rec['project']}/{rec['loop']}  {rec['status']}",
        t("runs.started_ended", v1=rec.get("started", ""), v2=rec.get("finished", "")),
        f"  Trigger: {rec.get('trigger', '')}" + (f"  Notiz: {rec['note']}" if rec.get("note") else ""),
    ]
    for s in rec.get("steps", []):
        lines.append(
            f"  {STEP_MARKS.get(s.get('status'), '?')} {s.get('name', '')} [{s.get('type', '')}] {s.get('summary', '')}"
        )
    log = sessionlog.session_log_dump(run_id)
    lines.append(
        f"  Session log: {len(log['events'])} events, {len(log['checkpoints'])} checkpoints, "
        f"{len(log['artifacts'])} artifacts"
    )
    return "\n".join(lines)


def get_run_log_path(run_id: str, project: str | None = None) -> Path | None:
    """Path of the log file (`runs/<project>/<id>.log`); None on invalid id/project or missing file."""
    if not _valid_run_id(run_id):
        return None
    if project:
        if not _valid_project(project):
            return None
        f = config.RUNS_DIR / project / f"{run_id}.log"
        return f if f.is_file() else None
    if config.RUNS_DIR.is_dir():
        for d in config.RUNS_DIR.iterdir():
            f = d / f"{run_id}.log"
            if f.is_file():
                return f
    return None


def get_run_log(run_id: str, project: str | None = None) -> str | None:
    """Raw run log (`runs/<project>/<id>.log`) as text.

    None if the id is invalid or no log file exists (e.g.
    runs that ran before the log feature)."""
    f = get_run_log_path(run_id, project)
    return f.read_text(encoding="utf-8") if f else None
