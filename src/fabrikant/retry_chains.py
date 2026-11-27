"""Retry chains (ADR 0036): retry state per (project, loop, trigger).

A chain holds ``(project, loop, trigger_key)``, ``state``, ``next_attempt_at``,
``last_run_id`` and ``claimed_run_id`` — nothing more. Attempt count and history
live on the runs (``attempt``, ``retry_of``) and are derived via ``last_run_id``.
The state machine is small (``pending → exhausted | resolved |
dismissed``, ``exhausted → pending | resolved | dismissed``); the runs keep
their own state machine from ADR 0034.

Storage (JSON phase, until the SQL store #277): ``state/retry-chains.json`` under
the exclusive file lock (:mod:`fabrikant.filelock`), atomic write
(temp file + ``rename``). Uniqueness (at most one ``pending`` chain
per key) and claim (``claimed_run_id`` ``NULL`` → run id, conditional on
``state = 'pending'`` and ``next_attempt_at <= now``) are the same operations
as in the SQL store (ADR 0036, decision 3).

The claim belongs to the running attempt: only the run with ``claimed_run_id``
decides about the chain when it fails, and every end of an attempt
(failure, rejection, restart, start not possible) releases it again.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import urllib.error
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from . import config, cron, pause
from .failures import parse_kind, parse_retry_after
from .filelock import locked
from .gitops import SPEC_ENV_FILE_KEY, project_github
from .i18n import t as _t
from .retry import RetryPolicy
from .runmodel import FAILED_RUN_STATUSES, FailureKind, InvalidTransition, RunStatus, check_transition
from .runs import RunClaim, get_run, load_records
from .runstore import STORE, RunStoreError, attempt_of
from .specs import load_projects

# Canonical trigger keys (ADR 0036, terms): stable over the lifetime
# of the job — not the id of the single tick/event/delivery.
_ISSUE_TRIGGER_RE = re.compile(r"^on_issue:#(\d+)$")
_PR_TRIGGER_RE = re.compile(r"^on_pr:#(\d+)")
_PR_UPDATE_TRIGGER_RE = re.compile(r"^on_pr:#\d+@u")
_GH_KEY_RE = re.compile(r"^(issue|pr):(.+)#(\d+)$")

# A due attempt that could not start (no slot, loop paused,
# API error) is postponed by this time — otherwise every
# scheduler tick (2 s) would claim it again and query GitHub each time.
ATTEMPT_START_DEFER = timedelta(minutes=1)


class ChainState(StrEnum):
    """State of a retry chain (ADR 0036, decision 4)."""

    PENDING = "pending"
    EXHAUSTED = "exhausted"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


# Allowed chain transitions. Terminal: resolved, dismissed.
CHAIN_TRANSITIONS: dict[ChainState, frozenset[ChainState]] = {
    ChainState.PENDING: frozenset({ChainState.EXHAUSTED, ChainState.RESOLVED, ChainState.DISMISSED}),
    ChainState.EXHAUSTED: frozenset({ChainState.PENDING, ChainState.RESOLVED, ChainState.DISMISSED}),
    ChainState.RESOLVED: frozenset(),
    ChainState.DISMISSED: frozenset(),
}
_ACTIVE_STATES = frozenset({ChainState.PENDING, ChainState.EXHAUSTED})


class ClaimConflict(InvalidTransition):
    """The chain's claim does not (or no longer) belong to the expected run."""


class RetryBlocked(ValueError):
    """*Retry now* is not allowed (loop disabled) or could not start."""


class ChainStoreCorrupt(ValueError):
    """``retry-chains.json`` is not parseable. Mutations abort instead of
    overwriting the store with the rest (all other chains would be gone)."""


@dataclass(frozen=True, slots=True)
class RetryChain:
    """A retry chain. Timestamps are ISO strings (``""`` = not set)."""

    project: str
    loop: str
    trigger_key: str
    state: ChainState
    next_attempt_at: str = ""
    last_run_id: str = ""
    claimed_run_id: str = ""  # "" = frei
    created: str = ""
    updated: str = ""

    @property
    def key(self) -> str:
        """Unique key in the store (``::`` is forbidden in names, specs)."""
        return _store_key(self.project, self.loop, self.trigger_key)

    @property
    def is_claimed(self) -> bool:
        return bool(self.claimed_run_id)

    @property
    def is_active(self) -> bool:
        """``pending`` or ``exhausted`` (not finished)."""
        return self.state in _ACTIVE_STATES


def _store_key(project: str, loop: str, trigger_key: str) -> str:
    return f"{project}::{loop}::{trigger_key}"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


_UNSET = object()  # Sentinel: "leave field unchanged" (ChainStore.transition)


class ChainStore:
    """Read/write access to the retry chains (JSON + file lock).

    All mutations run under the exclusive lock on the store file
    (cross-process, ``flock``) and write atomically. ``path`` is
    primarily overridable (tests); default ``config.STATE_DIR /
    "retry-chains.json"`` (at call time)."""

    def __init__(self, path: Path | None = None):
        self._path = path

    @property
    def root(self) -> Path:
        return self._path or config.STATE_DIR / "retry-chains.json"

    def _load(self) -> dict[str, dict]:
        p = self.root
        if not p.is_file():
            return {}
        # Read errors (OSError) and broken content propagate: an empty
        # fallback would delete all chains on the next _save.
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ChainStoreCorrupt(f"{p}: {e}") from e
        if not isinstance(data, dict):
            raise ChainStoreCorrupt(_t("retry_chains.not_a_json_object", p=p))
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save(self, chains: dict[str, dict]) -> None:
        p = self.root
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(chains, indent=1, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())  # before the rename: no truncated store after a power outage
        tmp.replace(p)

    @staticmethod
    def _parse(d: dict) -> RetryChain | None:
        try:
            state = ChainState(str(d.get("state")))
        except ValueError:
            return None
        return RetryChain(
            project=d.get("project", ""),
            loop=d.get("loop", ""),
            trigger_key=d.get("trigger_key", ""),
            state=state,
            next_attempt_at=str(d.get("next_attempt_at", "")),
            last_run_id=str(d.get("last_run_id", "")),
            claimed_run_id=str(d.get("claimed_run_id", "")),
            created=str(d.get("created", "")),
            updated=str(d.get("updated", "")),
        )

    def all(self) -> list[RetryChain]:
        """All chains (unsorted); broken entries are skipped."""
        return [c for c in map(self._parse, self._load().values()) if c is not None]

    def get(self, project: str, loop: str, trigger_key: str) -> RetryChain | None:
        with locked(self.root):
            return self._parse(self._load().get(_store_key(project, loop, trigger_key), {}))

    def create(
        self,
        project: str,
        loop: str,
        trigger_key: str,
        *,
        next_attempt_at: datetime | None,
        last_run_id: str,
        now: datetime,
        state: ChainState = ChainState.PENDING,
    ) -> RetryChain:
        """Create a new chain (default ``pending``; backfill: ``exhausted``).

        ValueError if a ``pending`` chain already exists for the key
        (uniqueness, ADR 0036 decision 3) or ``state`` does not fit
        ``next_attempt_at`` (``pending`` needs a time,
        ``exhausted`` carries none). Finished chains (resolved/dismissed/
        exhausted) do not block — a later failure opens a new
        chain."""
        if state is ChainState.PENDING and next_attempt_at is None:
            raise ValueError(
                _t("retry_chains.pending_chain_needs_next_attempt", project=project, loop=loop, trigger_key=trigger_key)
            )
        if state is ChainState.EXHAUSTED and next_attempt_at is not None:
            raise ValueError(
                _t("retry_chains.exhausted_chain_carries_no_next", project=project, loop=loop, trigger_key=trigger_key)
            )
        key = _store_key(project, loop, trigger_key)
        with locked(self.root):
            chains = self._load()
            existing = self._parse(chains.get(key, {}))
            if existing is not None and existing.state is ChainState.PENDING:
                raise ValueError(_t("retry_chains.chain_is_already_pending", key=key))
            chain = RetryChain(
                project=project,
                loop=loop,
                trigger_key=trigger_key,
                state=state,
                next_attempt_at=_iso(next_attempt_at) if next_attempt_at is not None else "",
                last_run_id=last_run_id,
                created=_iso(now),
                updated=_iso(now),
            )
            chains[key] = asdict(chain)
            self._save(chains)
        return chain

    def claim(
        self, project: str, loop: str, trigger_key: str, run_id: str, now: datetime, *, force: bool = False
    ) -> bool:
        """Claim a due attempt (ADR 0036, decision 3).

        Normal (``force=False``): succeeds only if the chain is ``pending``,
        ``next_attempt_at <= now`` holds and ``claimed_run_id`` is free — in
        a single transaction. Exactly one starter wins; all others
        get False and start nothing.

        ``force=True`` (UI *retry now*): only from ``exhausted``, without
        due check. The chain becomes ``pending`` again in the same transaction
        (full budget, ``next_attempt_at`` empty) — the claim stays with the new
        run until it ends."""
        key = _store_key(project, loop, trigger_key)
        with locked(self.root):
            chains = self._load()
            chain = self._parse(chains.get(key, {}))
            if chain is None or chain.is_claimed:
                return False
            fields: dict[str, Any] = {"claimed_run_id": run_id, "updated": _iso(now)}
            if force:
                if chain.state is not ChainState.EXHAUSTED:
                    return False
                fields |= {"state": ChainState.PENDING, "next_attempt_at": ""}
            else:
                if chain.state is not ChainState.PENDING:
                    return False
                due = _parse_iso(chain.next_attempt_at)
                if due is None or due > now:
                    return False
            chains[key] = {**asdict(chain), **fields}
            self._save(chains)
        return True

    def transition(
        self,
        project: str,
        loop: str,
        trigger_key: str,
        *,
        to: ChainState | None,
        now: datetime,
        next_attempt_at: datetime | None | object = _UNSET,
        last_run_id: str | None = None,
        expect_claim: str | None = None,
    ) -> RetryChain | None:
        """Atomic state change in a single transaction (one lock).

        Reads the chain, checks the transition (:func:`check_transition`) and
        writes the result — without a gap in which another process could
        write in between. ``to=None`` = state unchanged. ``next_attempt_at``:
        ``_UNSET`` (default) = unchanged, ``None`` = delete, ``datetime`` =
        set. ``last_run_id=None`` = unchanged. The claim is always
        released.

        ``expect_claim`` (compare-and-set): if set, ``claimed_run_id``
        must equal exactly this value (``""`` = free), otherwise
        :class:`ClaimConflict` — so nobody overwrites another attempt's
        claim.

        None if the chain does not exist; :class:`InvalidTransition` on
        forbidden transitions (e.g. target state from a stale state)."""
        key = _store_key(project, loop, trigger_key)
        with locked(self.root):
            chains = self._load()
            chain = self._parse(chains.get(key, {}))
            if chain is None:
                return None
            if expect_claim is not None and chain.claimed_run_id != expect_claim:
                raise ClaimConflict(
                    _t(
                        "retry_chains.chain_claim_instead_of",
                        key=key,
                        v2=chain.claimed_run_id,
                        expect_claim=expect_claim,
                    )
                )
            target = chain.state if to is None else to
            if target is not chain.state:
                check_transition(CHAIN_TRANSITIONS, chain.state, target)
            next_at = chain.next_attempt_at
            if next_attempt_at is not _UNSET:
                next_at = _iso(cast(datetime, next_attempt_at)) if next_attempt_at is not None else ""
            updated = RetryChain(
                project=chain.project,
                loop=chain.loop,
                trigger_key=chain.trigger_key,
                state=target,
                next_attempt_at=next_at,
                last_run_id=last_run_id if last_run_id is not None else chain.last_run_id,
                claimed_run_id="",
                created=chain.created,
                updated=_iso(now),
            )
            chains[key] = asdict(updated)
            self._save(chains)
        return updated

    def release_claim(
        self,
        project: str,
        loop: str,
        trigger_key: str,
        run_id: str,
        now: datetime,
        *,
        to: ChainState | None = None,
        next_attempt_at: datetime | None | object = _UNSET,
    ) -> RetryChain | None:
        """Release the claim of ``run_id`` (compare-and-release, one transaction).

        If another run holds the claim or the state has changed in parallel so
        that ``to`` is no longer allowed, nothing happens (None)."""
        try:
            return self.transition(
                project, loop, trigger_key, to=to, now=now, next_attempt_at=next_attempt_at, expect_claim=run_id
            )
        except InvalidTransition:
            return None

    def set_state(
        self, project: str, loop: str, trigger_key: str, state: ChainState, now: datetime
    ) -> RetryChain | None:
        """Change state without a running attempt (``resolved``/``dismissed``).

        ``dismissed`` deletes ``next_attempt_at`` (UI *discard*)."""
        return self.transition(
            project,
            loop,
            trigger_key,
            to=state,
            now=now,
            next_attempt_at=None if state is ChainState.DISMISSED else _UNSET,
        )

    def due(self, now: datetime) -> list[RetryChain]:
        """Due, unclaimed ``pending`` chains (``next_attempt_at <= now``)."""
        out = []
        for chain in self.all():
            if chain.state is not ChainState.PENDING or chain.is_claimed:
                continue
            due = _parse_iso(chain.next_attempt_at)
            if due is not None and due <= now:
                out.append(chain)
        return out


# ---------------------------------------------------------------- policy resolution


def policy_for(spec: dict, loop_name: str) -> RetryPolicy | None:
    """Effective retry policy of a loop (ADR 0036, decision 1).

    Loop ``retry`` replaces the project block completely (no field merge);
    ``max: 0`` at loop level disables a project policy for the loop.
    None = no retry. Broken blocks are spec errors (specs.py validates);
    here parsing is tolerant (None)."""
    loop = (spec.get("loops") or {}).get(loop_name) or {}
    raw = loop.get("retry") if "retry" in loop else spec.get("retry")
    if raw is None:
        return None
    return RetryPolicy.parse(raw, f"loop {loop_name}.retry", [])


def trigger_key(spec: dict, loop_name: str, trigger: str) -> str | None:
    """Canonical trigger key (ADR 0036, terms); None = no chain.

    - Cron: ``cron:<schedule id>`` (cron expression — stable across all ticks).
    - Issue: ``issue:<owner>/<repo>#<number>``.
    - PR: ``pr:<owner>/<repo>#<number>`` (head SHA is part of the trigger string,
      not part of the key).
    - Events without issue/PR/schedule reference: no key → no chain.

    The repo in the key (``repo`` of the first ``on_issue`` or
    ``on_pr`` trigger, otherwise project ``github``) is also the repo from which
    an attempt reloads the issue/PR."""
    if trigger == "cron":
        cron_expr = next((str(t["cron"]) for t in _loop_triggers(spec, loop_name, "cron") if t.get("cron")), "")
        return f"cron:{cron_expr}" if cron_expr else None
    for regex, ttype, kind in ((_ISSUE_TRIGGER_RE, "on_issue", "issue"), (_PR_TRIGGER_RE, "on_pr", "pr")):
        m = regex.match(trigger or "")
        if m:
            repo = _trigger_repo(spec, loop_name, ttype)
            return f"{kind}:{repo}#{m.group(1)}" if repo else None
    return None


def key_for_record(spec: dict | None, loop_name: str, record: dict) -> str:
    """Chain key of a run ("" = no chain).

    The record field ``trigger_key`` (written at run start) wins —
    a spec change during the chain (cron expression, repo) thus does not
    orphan it. Only legacy records without the field are recomputed from ``spec``."""
    key = str(record.get("trigger_key") or "")
    if key or not spec:
        return key
    return trigger_key(spec, loop_name, str(record.get("trigger") or "")) or ""


def _loop_triggers(spec: dict, loop_name: str, ttype: str) -> list[dict]:
    """Triggers of a type of a loop, in spec order."""
    loop = (spec.get("loops") or {}).get(loop_name) or {}
    return [t for t in loop.get("triggers") or [] if isinstance(t, dict) and t.get("type") == ttype]


def _trigger_repo(spec: dict, loop_name: str, ttype: str) -> str:
    """Repo of the first ``ttype`` trigger (trigger ``repo`` before project ``github``)."""
    trigs = _loop_triggers(spec, loop_name, ttype)
    return str((trigs[0].get("repo") if trigs else None) or spec.get("github") or "")


def pr_accepts_closed(spec: dict, loop_name: str) -> bool:
    """Does the (key-giving, first) ``on_pr`` trigger also fire on
    closed PRs (``states: [.., closed]``)? Then a
    closed/merged PR does not end the chain."""
    trigs = _loop_triggers(spec, loop_name, "on_pr")
    return bool(trigs) and "closed" in {str(s) for s in trigs[0].get("states") or []}


# ---------------------------------------------------------------- Cron-Intervall


_cron_interval_cache: dict[str, timedelta | None] = {}
# Simulation window: the leap year 2028 (Feb 29 lies in it) plus one
# week, so year change and weekday cycle are covered as well.
_CRON_SIM_START = datetime(2028, 1, 1)
_CRON_SIM_DAYS = 366 + 7


def cron_min_interval(expr: str) -> timedelta | None:
    """Shortest distance between two ticks of a cron expression (cached).

    Same field semantics as :func:`fabrikant.cron.matches`. Instead of
    simulating minute by minute, the times of a day and the
    matching days are determined separately: the minimum is the smallest distance
    within a day or between the last tick of a day and the first
    tick of the next matching day. None if the cron is invalid or there are fewer than
    two ticks in the window (e.g. only Feb 29) — then the
    interval validation is skipped."""
    if expr not in _cron_interval_cache:
        parts = str(expr).split()
        interval = None
        if len(parts) == 5:
            # like cron_matches: invalid (also step 0) → no tick
            with contextlib.suppress(ValueError, IndexError, ZeroDivisionError):
                interval = _min_tick_gap(*parts)
        _cron_interval_cache[expr] = interval
    return _cron_interval_cache[expr]


def _min_tick_gap(f_min: str, f_hour: str, f_dom: str, f_mon: str, f_dow: str) -> timedelta | None:
    times = cron.day_times(f_min, f_hour)
    if not times:
        return None
    best = min((b - a for a, b in zip(times, times[1:], strict=False)), default=None)
    prev_last: datetime | None = None
    for i in range(_CRON_SIM_DAYS):
        day = _CRON_SIM_START + timedelta(days=i)
        if not cron.day_matches(f_dom, f_mon, f_dow, day):
            continue
        if prev_last is not None:
            gap = day + times[0] - prev_last
            best = gap if best is None else min(best, gap)
        prev_last = day + times[-1]
    return best if prev_last is not None else None


def validate_retry_cron(spec: dict, lname: str, ldef: dict, policy: RetryPolicy | None) -> tuple[list[str], list[str]]:
    """Cron interval validation (ADR 0036, decision 2).

    An actually used wait time (``delay(1..max)``) ≥ interval →
    error (the tick would be earlier than the retry); backoff values beyond
    ``max`` are never used and do not count. The whole chain (sum of the
    first ``max`` wait times) longer than the interval → warning (ticks
    are skipped)."""
    errs: list[str] = []
    warns: list[str] = []
    if policy is None:
        return errs, warns
    delays = [policy.delay(i) for i in range(1, policy.max + 1)]
    for trig in ldef.get("triggers") or []:
        if not isinstance(trig, dict) or trig.get("type") != "cron":
            continue
        interval = cron_min_interval(str(trig.get("cron") or ""))
        if interval is None:
            continue
        for value in sorted(set(delays)):
            if value >= interval:
                errs.append(f"loop {lname}: retry.backoff-Wert {value} >= Cron-Intervall {interval}")
        total = sum(delays, timedelta(0))
        if total > interval:
            warns.append(_t("retry_chains.loop_retry_chain_longer_than", lname=lname, total=total, interval=interval))
    return errs, warns


# ---------------------------------------------------------------- Ketten-Updates


@dataclass(frozen=True, slots=True)
class RunEnd:
    """The facts of a finished (or paused) run that are relevant for the chain."""

    run_id: str
    status: str | None
    kind: FailureKind
    attempt: int = 0
    retry_of: str = ""
    finished: datetime | None = None
    retry_after: datetime | None = None

    @classmethod
    def from_record(cls, record: dict, now: datetime) -> RunEnd:
        return cls(
            run_id=str(record.get("id") or ""),
            status=record.get("status"),
            kind=parse_kind(record.get("failure_kind")),
            attempt=attempt_of(record),
            retry_of=str(record.get("retry_of") or ""),
            finished=_parse_iso(str(record.get("finished") or "")) or now,
            retry_after=parse_retry_after(record.get("retry_after")),
        )

    def belongs_to(self, chain: RetryChain | None) -> bool:
        """Is the run an attempt of this chain?

        Yes if it holds the claim — or (without claim, e.g. legacy data) if
        it is the successor of the chain's last run."""
        if chain is None:
            return False
        if chain.is_claimed:
            return chain.claimed_run_id == self.run_id
        return bool(self.retry_of) and self.retry_of == chain.last_run_id


class ChainAction(StrEnum):
    NONE = "none"  # nichts schreiben
    CREATE = "create"  # neue Kette
    TRANSITION = "transition"  # Zustandswechsel (+ Claim-Freigabe)


@dataclass(frozen=True, slots=True)
class ChainDecision:
    """Result of :func:`_decide_outcome`.

    ``target=None`` for ``TRANSITION``: state stays (only fields/claim).
    ``next_attempt_at``: ``_UNSET`` = unchanged, ``None`` = delete,
    ``datetime`` = set. ``attempt``: attempt count for :func:`_chain_info`.
    ``in_chain=False``: the run belongs to no chain — alerts as without retry."""

    action: ChainAction
    target: ChainState | None = None
    next_attempt_at: datetime | None | object = _UNSET
    last_run_id: str | None = None
    attempt: int = 0
    in_chain: bool = True


_NOT_IN_CHAIN = ChainDecision(ChainAction.NONE, in_chain=False)


def _chain_info(chain: RetryChain | None, policy: RetryPolicy | None, decision: ChainDecision) -> dict[str, Any]:
    """Info for UI/alerts: chain present? State, next attempt, budget."""
    if chain is None or not decision.in_chain:
        return {"in_chain": False}
    return {
        "in_chain": True,
        "state": chain.state.value,
        "next_attempt_at": chain.next_attempt_at,
        "attempt": decision.attempt,
        "max": policy.max if policy else 0,
    }


def _decide_outcome(chain: RetryChain | None, policy: RetryPolicy | None, run: RunEnd) -> ChainDecision:
    """Pure chain decision at the end of a run (ADR 0036, decision 4).

    - ``pass`` (of any run) → ``resolved``.
    - If *another* run holds the claim, only that one decides — do nothing.
    - ``rejected`` of an attempt → ``dismissed``.
    - Not yet finished (``needs_approval``): do nothing, the claim stays.
    - Failure of an attempt of a finished chain (e.g. discarded during the
      attempt): plans nothing any more.
    - Failure of a foreign run while the chain is ``pending`` (e.g.
      manual run in backoff): the chain keeps planning unchanged, the budget
      stays.
    - Failure without policy, with a class outside ``on`` or ``n >= max``:
      an attempt exhausts the chain; otherwise no chain (classic
      ``run_failed``).
    - Otherwise: ``n < max`` → ``pending`` with backoff (new chain if no
      active one exists; an ``exhausted`` chain is reopened with full
      budget)."""
    belongs = run.belongs_to(chain)
    active = chain is not None and chain.is_active
    if run.status == RunStatus.PASS:
        return (
            ChainDecision(ChainAction.TRANSITION, ChainState.RESOLVED, attempt=run.attempt) if active else _NOT_IN_CHAIN
        )
    if chain is not None and chain.is_claimed and not belongs:
        return _NOT_IN_CHAIN
    if run.status == RunStatus.REJECTED:
        if belongs and active:
            return ChainDecision(
                ChainAction.TRANSITION, ChainState.DISMISSED, next_attempt_at=None, attempt=run.attempt
            )
        return _NOT_IN_CHAIN
    if run.status not in FAILED_RUN_STATUSES:
        return ChainDecision(ChainAction.NONE, attempt=run.attempt, in_chain=belongs)
    if belongs and not active:
        return ChainDecision(ChainAction.NONE, attempt=run.attempt)  # discarded/resolved: no alert
    if chain is not None and chain.state is ChainState.PENDING and not belongs:
        return _NOT_IN_CHAIN
    if policy is None or not policy.allows(run.kind) or run.attempt >= policy.max:
        if belongs:
            return ChainDecision(ChainAction.TRANSITION, ChainState.EXHAUSTED, None, run.run_id, attempt=run.attempt)
        return _NOT_IN_CHAIN
    next_at = policy.next_attempt_at(run.attempt + 1, run.finished or config.utcnow(), run.retry_after)
    action = ChainAction.TRANSITION if active else ChainAction.CREATE
    return ChainDecision(action, ChainState.PENDING, next_at, run.run_id, attempt=run.attempt + 1)


def on_run_finished(spec: dict, project: str, loop_name: str, record: dict) -> dict[str, Any]:
    """Chain update at the end of a run (ADR 0036, decision 4).

    The decision (:func:`_decide_outcome`) is pure; it is applied
    atomically (:meth:`ChainStore.transition` with compare-and-set on the
    read claim / :meth:`ChainStore.create`). If the chain changes
    between read and write (parallel end, new claim), it is decided once
    more — a conflict does not abort the run.

    The chain key comes from the run record (:func:`key_for_record`).

    Returns :func:`_chain_info` (``in_chain`` controls, among other things, the
    ``run_failed`` alert damping, ADR 0036 decision 6)."""
    key = key_for_record(spec, loop_name, record)
    if not key:
        return {"in_chain": False}  # no canonical trigger → no chain
    policy = policy_for(spec, loop_name)
    store = ChainStore()
    now = config.utcnow()
    run = RunEnd.from_record(record, now)

    for _ in range(2):  # one conflict retry on a parallel state change
        chain = store.get(project, loop_name, key)
        decision = _decide_outcome(chain, policy, run)
        try:
            if decision.action is ChainAction.CREATE:
                chain = store.create(
                    project,
                    loop_name,
                    key,
                    next_attempt_at=cast(datetime, decision.next_attempt_at),
                    last_run_id=cast(str, decision.last_run_id),
                    now=now,
                )
            elif decision.action is ChainAction.TRANSITION:
                chain = store.transition(
                    project,
                    loop_name,
                    key,
                    to=decision.target,
                    now=now,
                    next_attempt_at=decision.next_attempt_at,
                    last_run_id=decision.last_run_id,
                    expect_claim=chain.claimed_run_id if chain is not None else None,
                )
        except ChainStoreCorrupt:
            raise  # no conflict: broken store → on_run_finished_safe reports it
        except ValueError:  # pending chain created in parallel / InvalidTransition / ClaimConflict
            continue
        return _chain_info(chain, policy, decision)
    return {"in_chain": False}


def on_run_finished_safe(spec: dict, project: str, loop_name: str, record: dict) -> dict[str, Any]:
    """Best-effort :func:`on_run_finished` for the run-end paths (engine,
    stale-run check, reaper, rejection): store errors (e.g. full
    file system) are logged, not propagated — the chain bookkeeping
    must not skip the run's alert or make the server start
    fail."""
    try:
        return on_run_finished(spec, project, loop_name, record)
    except (OSError, ValueError) as e:
        print(_t("retry_chains.fabrikant_retry_chain_run_end", project=project, loop_name=loop_name, e=e))
        return {"in_chain": False}


def record_run_end(project: str, record: dict, projects: Mapping[str, dict] | None = None) -> dict[str, Any]:
    """:func:`on_run_finished` for callers without a spec at hand (reaper,
    rejection at the approval gate). Unknown project → no chain.
    ``projects``: already loaded specs (callers with many runs load them
    once instead of per run); otherwise :func:`load_projects`.
    Best-effort (see :func:`on_run_finished_safe`)."""
    spec = (load_projects() if projects is None else projects).get(project)
    if not spec:
        return {"in_chain": False}
    return on_run_finished_safe(spec, project, str(record.get("loop") or ""), record)


# ---------------------------------------------------------------- Versuche starten


def _trigger_for_key(trigger_key: str, initial_vars: dict | None = None, *, per_update: bool = False) -> str:
    """Trigger string for an attempt from the canonical key.

    PR attempts carry the current reconcile key the scheduler checks: the head SHA
    (``on_pr:#N@<sha>``, ADR 0023) or, for a run of a `reconcile: updated` loop
    (``per_update``), the observed ``updated_at`` (``on_pr:#N@u<stamp>``) — without it a
    retry on the current PR state would trigger a second run. SHA/stamp come from the
    (freshly loaded) trigger variables of the attempt."""
    m = _GH_KEY_RE.match(trigger_key)
    if m:
        kind, _repo, num = m.groups()
        if kind == "pr":
            # lokal: scheduler importiert engine (Zyklus)
            from .scheduler import _pr_trigger, _pr_update_trigger

            v = initial_vars or {}
            if per_update:
                return _pr_update_trigger(int(num), str(v.get("pr_updated_at") or ""))
            return _pr_trigger(int(num), str(v.get("pr_head_sha") or ""))
        return f"on_{kind}:#{num}"
    return "cron"


def _is_update_trigger(trigger: str) -> bool:
    """Is ``trigger`` a `reconcile: updated` key (``on_pr:#N@u<stamp>``)? A head SHA is hex,
    so ``@u`` is unambiguous."""
    return bool(_PR_UPDATE_TRIGGER_RE.match(trigger or ""))


def _attempt_vars(trigger_key: str, *, accept_closed: bool = False) -> tuple[dict | None, bool]:
    """Fresh trigger variables for an attempt (issue/PR content may have
    changed; an attempt passes through all gates again, ADR 0036 no. 5).

    Returns ``(vars, api_error)``: ``vars=None`` means the trigger no longer
    applies (issue/PR gone or closed) — except ``api_error=True``
    (transient, chain stays pending). ``accept_closed`` (:func:`pr_accepts_closed`):
    a closed PR still counts. Cron triggers have no variables:
    ``({}, False)``."""
    from .scheduler import issue_vars, pr_vars  # lokal: scheduler importiert engine (Zyklus)

    m = _GH_KEY_RE.match(trigger_key)
    if not m:
        return {}, False
    kind, repo, num = m.groups()
    data, api_error = _gh_get(repo, kind, num, accept_closed=accept_closed and kind == "pr")
    if data is None:
        return None, api_error
    return (issue_vars(data) if kind == "issue" else pr_vars(data)), False


def _dedupe_vars(trigger_key: str) -> dict | None:
    """Issue/PR reference of a key for the run claim (:func:`fabrikant.runs.dedupe_key`),
    without a GitHub lookup."""
    m = _GH_KEY_RE.match(trigger_key)
    if not m:
        return None
    kind, _repo, num = m.groups()
    return {f"{kind}_number": int(num)}


_GH_ENDPOINTS = {"issue": "issues", "pr": "pulls"}


def _gh_get(repo: str, kind: str, num: str, *, accept_closed: bool = False) -> tuple[dict | None, bool]:
    """Load the open issue or open PR ``num`` from ``repo``.

    Returns ``(data, api_error)``:

    - ``(dict, False)``: present and open (or closed with ``accept_closed``);
    - ``(None, False)``: definitively gone (HTTP 404, closed, merged);
    - ``(None, True)``: API error or no token (transient — chain stays pending)."""
    from .gitops import _gh_api, _gh_token_safe

    label = f"{repo} {kind} #{num}"
    tok = _gh_token_safe()
    if not tok:
        return None, True
    try:
        data = _gh_api("GET", f"https://api.github.com/repos/{repo}/{_GH_ENDPOINTS[kind]}/{num}", tok)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, False
        print(_t("retry_chains.fabrikant_retry_could_not_be", label=label, e=e))
        return None, True
    except Exception as e:
        print(_t("retry_chains.fabrikant_retry_could_not_be", label=label, e=e))
        return None, True
    if not isinstance(data, dict) or "number" not in data:
        return None, False
    if data.get("state") == "closed" and not accept_closed:
        return None, False
    return data, False


@dataclass(frozen=True, slots=True)
class _Attempt:
    """An attempt of a chain that holds the chain claim.

    Scheduled attempts run in the worker pool (:meth:`run`): no free slot,
    pause or API error postpone them by :data:`ATTEMPT_START_DEFER`
    without consuming budget. ``manual`` (UI *retry now*,
    :meth:`run_manual`) waits like other manual runs with priority for a
    slot (ADR 0015); pause does not block (only ``disabled``). If it cannot
    start, the chain is back to ``exhausted`` and under the already
    reported run id there is an ``error`` record with the reason.
    ``accept_closed``: see :func:`pr_accepts_closed`. ``actor``:
    triggering user → ``started_by`` (audit, ADR 0030). ``github_env_file``: token file
    of the project for reloading the trigger variables (``project_github``)."""

    chain: RetryChain
    run_id: str
    attempt: int
    manual: bool = False
    accept_closed: bool = False
    actor: str | None = None
    github_env_file: str | None = None

    def blocked_reason(self) -> str | None:
        c = self.chain
        if self.manual:
            return pause.manual_block_reason(c.project, c.loop)
        state = pause.effective_state(c.project, c.loop)
        return None if state == pause.ACTIVE else state

    def abort(self, reason: str) -> None:
        """Attempt does not start: release the claim without consuming budget.

        No-op on the chain if the claim no longer belongs to us."""
        c = self.chain
        print(_t("retry_chains.fabrikant_retry_attempt_not_started", v1=self.run_id, v2=c.key, reason=reason))
        now = config.utcnow()
        store = ChainStore()
        if self.manual:
            store.release_claim(
                c.project, c.loop, c.trigger_key, self.run_id, now, to=ChainState.EXHAUSTED, next_attempt_at=None
            )
            self._record_not_started(reason)
        else:
            store.release_claim(
                c.project, c.loop, c.trigger_key, self.run_id, now, next_attempt_at=now + ATTEMPT_START_DEFER
            )

    def dismiss(self) -> None:
        """Trigger no longer applies (issue/PR gone or closed)."""
        c = self.chain
        ChainStore().release_claim(
            c.project,
            c.loop,
            c.trigger_key,
            self.run_id,
            config.utcnow(),
            to=ChainState.DISMISSED,
            next_attempt_at=None,
        )
        print(_t("retry_chains.fabrikant_retry_chain_trigger_no", v1=c.key))
        if self.manual:
            self._record_not_started(_t("retry_chains.trigger_no_longer_exists"))

    def _per_update(self) -> bool:
        """Was the retried run keyed per PR update (``on_pr:#N@u<stamp>``)? The attempt then
        uses the same key form, so the scheduler's reconcile check sees it."""
        c = self.chain
        last = get_run(c.last_run_id, c.project) if c.last_run_id else None
        return _is_update_trigger(str((last or {}).get("trigger") or ""))

    def _record_not_started(self, reason: str) -> None:
        """``error`` record under the run id of a manual attempt that the
        UI has already received — otherwise it would point to a run that never exists.
        An existing record (the run has begun) stays untouched."""
        c = self.chain
        if get_run(self.run_id, c.project) is not None:
            return
        now = config.utcnow().isoformat()
        record: dict[str, Any] = {
            "id": self.run_id,
            "project": c.project,
            "loop": c.loop,
            "trigger": _trigger_for_key(c.trigger_key, per_update=self._per_update()),
            "note": "",
            "started": now,
            "finished": now,
            "status": RunStatus.ERROR.value,
            "error": _t("retry_chains.retry_not_started", reason=reason),
            "steps": [],
            "started_by": self.actor,
            "approved_by": None,
            "rejected_by": None,
            "attempt": self.attempt,
            "retry_of": c.last_run_id,
            "trigger_key": c.trigger_key,
        }
        try:
            STORE.create(c.project, self.run_id, record)
        except (OSError, RunStoreError) as e:
            print(_t("retry_chains.fabrikant_retry_attempt_record_not", v1=self.run_id, e=e))

    def _prepare(self) -> dict | None:
        """Check gates, load trigger variables, re-check claim ownership.

        Returns the trigger variables; None if the attempt does not start
        (chain already filed/postponed/discarded)."""
        c = self.chain
        reason = self.blocked_reason()
        if reason:
            self.abort(reason)
            return None
        with project_github({SPEC_ENV_FILE_KEY: self.github_env_file}):
            initial_vars, api_error = _attempt_vars(c.trigger_key, accept_closed=self.accept_closed)
        if initial_vars is None:
            if api_error:
                self.abort(_t("retry_chains.github_not_reachable"))
            else:
                self.dismiss()
            return None
        # The claim may have been released between claim and start
        # (discard/state change release it unconditionally): the chain
        # has then already ended — do not start.
        fresh = ChainStore().get(c.project, c.loop, c.trigger_key)
        if fresh is None or fresh.claimed_run_id != self.run_id:
            self.abort(_t("retry_chains.claim_no_longer_held"))
            return None
        return initial_vars

    def _execute(self, scope: str, initial_vars: dict) -> None:
        """Execute the loop with a held run slot. The record carries the
        key of this chain (``chain_key``), not one computed from the
        (meanwhile possibly changed) spec."""
        from .engine import run_loop

        c = self.chain
        try:
            run_loop(
                c.project,
                c.loop,
                trigger=_trigger_for_key(c.trigger_key, initial_vars, per_update=self._per_update()),
                initial_vars=initial_vars,
                run_id=self.run_id,
                attempt=self.attempt,
                retry_of=c.last_run_id,
                held_scope=scope,
                actor=self.actor,
                chain_key=c.trigger_key,
            )
        except Exception as e:
            print(f"[fabrikant] Retry-Versuch {self.run_id} fehlgeschlagen: {e}")
            if not self._finish_crashed(e):
                self.abort(f"Start fehlgeschlagen: {e}")

    def _finish_crashed(self, exc: Exception) -> bool:
        """``run_loop`` aborted after the record was created (e.g.
        ``_persist`` fails): end the record as ``error`` and book the chain
        — the attempt consumes budget, otherwise a permanent crash would run
        endlessly every minute. False if there is no record (the
        attempt has not begun)."""
        from .failures import classify_exception

        c = self.chain
        record = get_run(self.run_id, c.project)
        if record is None:
            return False
        if record.get("status") == RunStatus.RUNNING:
            record.update(
                status=RunStatus.ERROR.value,
                error=str(exc),
                finished=config.utcnow().isoformat(),
                **classify_exception(exc).fields(),
            )
            try:
                STORE.write(c.project, self.run_id, record, expect=RunStatus.RUNNING)
            except (OSError, RunStoreError) as e:
                print(_t("retry_chains.fabrikant_retry_attempt_record_not", v1=self.run_id, e=e))
        record_run_end(c.project, record)
        return True

    def run(self) -> None:
        """Scheduled attempt (worker pool): prepare, claim the run slot
        non-blockingly, execute the loop."""
        from .runs import claim_run

        initial_vars = self._prepare()
        if initial_vars is None:
            return
        claim = claim_run(self.chain.project, self.chain.loop, initial_vars)
        if claim is None:
            self.abort(_t("retry_chains.no_run_slot_free"))
            return
        try:
            self._execute(claim.scope, initial_vars)
        finally:
            claim.release()

    def run_manual(self, held: RunClaim) -> None:
        """Manual attempt in the priority worker (:func:`fabrikant.engine.spawn_priority_worker`),
        which already holds the run slot ``held`` and releases it afterwards."""
        initial_vars = self._prepare()
        if initial_vars is not None:
            self._execute(held.scope, initial_vars)


def _submit(attempt: _Attempt) -> None:
    from .engine import _get_launcher  # local: engine imports this module

    _get_launcher().submit(attempt.run)


def _spawn_manual(attempt: _Attempt) -> None:
    """Start a manual attempt with priority: immediately, or it waits for the
    slot (like manual runs, ADR 0015). RuntimeError if the thread does not start."""
    from .engine import spawn_priority_worker  # local: engine imports this module

    c = attempt.chain
    spawn_priority_worker(c.project, c.loop, _dedupe_vars(c.trigger_key), work=attempt.run_manual)


def _end_chain(store: ChainStore, chain: RetryChain, to: ChainState, now: datetime, why: str) -> None:
    """Unclaimed Kette beenden (parallel weiterentwickelt → nichts tun)."""
    with contextlib.suppress(InvalidTransition):
        store.transition(
            chain.project, chain.loop, chain.trigger_key, to=to, now=now, next_attempt_at=None, expect_claim=""
        )
        print(f"[fabrikant] Retry chain {chain.key}: {why} → {to}")


def start_due_attempts(projects: dict[str, dict] | None = None) -> list[str]:
    """Start due attempts (ADR 0036, decision 5) — also after restart.

    Exactly one starter per chain (the claim succeeds for exactly one). Paused/
    disabled loops wait until release and consume no budget.
    A deleted loop ends the chain as ``dismissed``; if retry is meanwhile
    disabled for the loop, it is filed as ``exhausted``. Whether issue/PR
    still apply is checked by the attempt in the worker (:meth:`_Attempt.run`) — the
    scheduler tick makes no GitHub lookups. ``projects``: already loaded
    specs (scheduler tick); None = load yourself. Returns the started run ids."""
    store = ChainStore()
    now = config.utcnow()
    started: list[str] = []
    due = store.due(now)
    if not due:
        return started
    if projects is None:
        projects = load_projects()
    for chain in due:
        spec = projects.get(chain.project)
        if spec is None or spec.get("_error"):
            continue  # Project missing/broken: chain stays pending (next tick)
        if chain.loop not in (spec.get("loops") or {}):
            _end_chain(store, chain, ChainState.DISMISSED, now, _t("retry_chains.loop_no_longer_exists"))
            continue
        if policy_for(spec, chain.loop) is None:
            _end_chain(store, chain, ChainState.EXHAUSTED, now, _t("retry_chains.retry_disabled_for_the_loop"))
            continue
        if pause.trigger_blocked(chain.project, chain.loop):
            continue  # paused/disabled: waits, no budget consumption
        # Derive the attempt number from the last run (ADR 0036, decision 3).
        last = get_run(chain.last_run_id, chain.project) if chain.last_run_id else None
        attempt = attempt_of(last) + 1
        run_id = f"{config.now_id()}-{chain.loop}"
        if not store.claim(chain.project, chain.loop, chain.trigger_key, run_id, now):
            continue  # another starter got the attempt
        started.append(run_id)
        print(_t("retry_chains.fabrikant_retry_attempt_for_run", attempt=attempt, v2=chain.key, run_id=run_id))
        _submit(
            _Attempt(
                chain,
                run_id,
                attempt,
                accept_closed=pr_accepts_closed(spec, chain.loop),
                github_env_file=spec.get(SPEC_ENV_FILE_KEY),
            )
        )
    return started


def release_stale_claims() -> int:
    """Restart: release claims of runs that are no longer running (ADR 0036,
    decision 3). The reaper (ADR 0035) has previously ended the runs as
    ``interrupted`` and updated the chain; what remains are claims
    whose run is missing or whose end never reached the chain. A finished
    run is booked afterwards (:func:`record_run_end`), otherwise the claim is only
    released. Returns the number of released claims."""
    store = ChainStore()
    now = config.utcnow()
    freed = 0
    for chain in store.all():
        if not chain.is_claimed:
            continue
        run = get_run(chain.claimed_run_id, chain.project)
        status = run.get("status") if run is not None else None
        if status in (RunStatus.RUNNING, RunStatus.NEEDS_APPROVAL):
            continue  # attempt still running
        if run is not None and status in (*FAILED_RUN_STATUSES, RunStatus.PASS, RunStatus.REJECTED):
            record_run_end(chain.project, run)
        if chain.next_attempt_at == "":
            # Forced claim (retry now): the attempt never wrote a
            # run record → back to exhausted. A
            # pending chain without next_attempt_at would never be due again
            # (cron loop dead).
            store.release_claim(
                chain.project,
                chain.loop,
                chain.trigger_key,
                chain.claimed_run_id,
                now,
                to=ChainState.EXHAUSTED,
                next_attempt_at=None,
            )
        else:
            store.release_claim(chain.project, chain.loop, chain.trigger_key, chain.claimed_run_id, now)
        freed += 1
        print(_t("retry_chains.fabrikant_retry_chain_claim_released", v1=chain.key, v2=chain.claimed_run_id))
    return freed


# ---------------------------------------------------------------- Bestandsabgleich


@dataclass(slots=True)
class BackfillSummary:
    """Result of :func:`backfill_chains` (counts per effect)."""

    pending: int = 0
    exhausted: int = 0
    skipped: int = 0


def backfill_chains(
    projects: dict[str, dict] | None = None,
    store: ChainStore | None = None,
    now: datetime | None = None,
) -> BackfillSummary:
    """One-off backfill (#290): chains for triggers that have completely failed.

    For every loop with a retry policy, the run records are grouped by canonical
    trigger keys (:func:`key_for_record`). A group in which
    **every** run is ``fail``/``error`` and for which no chain exists yet
    gets one:

    - class of the last run (by ``started``) is in ``on`` of the loop's
      policy → ``pending``, due after the backoff before attempt 1, measured from the
      end of the last run (not before ``now`` — the attempt starts at the
      next scheduler tick, overdue ones immediately); a reported
      ``retry_after`` of the last run shifts the due time, capped at
      ``max_wait`` (same semantics as :meth:`RetryPolicy.next_attempt_at`);
    - every other class (``environment``/``outcome``/``unknown``/none) →
      ``exhausted`` — lands in the UI list "Filed away" for review.

    The failed legacy runs do not count against the budget: the first attempt
    of the chain is attempt 1 (``attempt`` of the last run + 1; legacy runs carry
    0 or no field), so the full budget of ``max`` further attempts. Whether
    the trigger still exists (issue closed, PR merged) is not checked by the
    backfill — the attempt asks GitHub on the first tick and ends
    the chain as ``dismissed`` if the trigger is gone.

    Idempotent: existing chains (any state), keys without a chain (e.g.
    ``manual``) and groups with a non-failed run (``pass``,
    ``rejected``, ``queued``, …) are skipped — running or discarded
    chains are not touched by the backfill. ``projects``/``store``/``now`` can be overridden
    for tests; the default loads specs/store/time from the environment."""
    if projects is None:
        projects = load_projects()
    if store is None:
        store = ChainStore()
    if now is None:
        now = config.utcnow()
    summary = BackfillSummary()
    for name, spec in projects.items():
        if spec.get("_error"):
            continue
        # Effective policy per loop (loop ``retry`` replaces the project block) —
        # resolve once, then keep (None = no retry).
        policies = {lname: pol for lname in spec.get("loops") or {} if (pol := policy_for(spec, lname)) is not None}
        if not policies:
            continue
        groups: dict[str, dict[str, list[dict]]] = {lname: {} for lname in policies}
        for r in load_records(name):
            groups_by_key = groups.get(str(r.get("loop") or ""))
            if groups_by_key is None:
                continue
            key = key_for_record(spec, str(r.get("loop") or ""), r)
            if key:
                groups_by_key.setdefault(key, []).append(r)
        for lname, groups_by_key in groups.items():
            policy = policies[lname]
            for key, group in groups_by_key.items():
                if store.get(name, lname, key) is not None:
                    summary.skipped += 1
                    continue
                if any(str(r.get("status")) not in FAILED_RUN_STATUSES for r in group):
                    summary.skipped += 1
                    continue
                last = max(group, key=lambda r: str(r.get("started") or ""))
                # Effective policy of the loop (not the global retry classes):
                # an `on` subset must not be widened in the reconciliation.
                kind = parse_kind(last.get("failure_kind"))
                if policy.allows(kind):
                    finished = _parse_iso(str(last.get("finished") or "")) or now
                    due = policy.next_attempt_at(1, finished, parse_retry_after(last.get("retry_after")))
                    store.create(
                        name, lname, key, next_attempt_at=max(due, now), last_run_id=str(last.get("id") or ""), now=now
                    )
                    summary.pending += 1
                    print(f"[fabrikant] Reconciliation {name}/{lname} {key}: chain pending (class {kind.value})")
                else:
                    store.create(
                        name,
                        lname,
                        key,
                        next_attempt_at=None,
                        last_run_id=str(last.get("id") or ""),
                        now=now,
                        state=ChainState.EXHAUSTED,
                    )
                    summary.exhausted += 1
                    print(
                        f"[fabrikant] Bestandsabgleich {name}/{lname} {key}: Kette exhausted "
                        f"(class {kind.value if kind else 'none'})"
                    )
    return summary


# ---------------------------------------------------------------- UI-Aktionen


def dismiss_chain(project: str, loop: str, trigger_key: str, actor: str = "unknown") -> RetryChain | None:
    """UI *discard* (from ``pending`` or ``exhausted``): chain ``dismissed``,
    ``next_attempt_at`` deleted. A running attempt runs to the end but
    plans nothing any more (ADR 0036, decision 4) — :meth:`RunEnd.belongs_to`
    recognizes it by ``retry_of``."""
    store = ChainStore()
    chain = store.get(project, loop, trigger_key)
    if chain is None or not chain.is_active:
        return None
    try:
        updated = store.set_state(project, loop, trigger_key, ChainState.DISMISSED, config.utcnow())
    except InvalidTransition:
        return store.get(project, loop, trigger_key)  # parallel beendet
    print(f"[fabrikant] Retry chain {chain.key} dismissed ({actor})")
    return updated


def retry_now(project: str, loop: str, trigger_key: str, actor: str | None = None) -> str | None:
    """UI *retry now* (only from ``exhausted``): run with ``attempt = 0``,
    ``retry_of = last_run_id``; chain ``pending`` again with full budget
    (ADR 0036, decision 4). If no slot is free, the run waits with
    priority (ADR 0015). The run holds the claim until it ends; if it cannot
    start, the chain is back to ``exhausted`` and the run id points to
    an ``error`` record with the reason. ``actor``: user name →
    ``started_by`` (audit, ADR 0030).

    Returns the run id; None if the chain is missing or not ``exhausted``.
    :class:`RetryBlocked` if the loop is disabled or the worker
    does not start."""
    store = ChainStore()
    chain = store.get(project, loop, trigger_key)
    if chain is None or chain.state is not ChainState.EXHAUSTED:
        return None
    reason = pause.manual_block_reason(project, loop)
    if reason:
        raise RetryBlocked(reason)
    run_id = f"{config.now_id()}-{loop}"
    if not store.claim(project, loop, trigger_key, run_id, config.utcnow(), force=True):
        return None
    print(f"[fabrikant] Retry chain {chain.key}: retrying now ({actor or 'api'}, run {run_id})")
    spec = load_projects().get(project) or {}
    attempt = _Attempt(
        chain,
        run_id,
        0,
        manual=True,
        accept_closed=pr_accepts_closed(spec, loop),
        actor=actor,
        github_env_file=spec.get(SPEC_ENV_FILE_KEY),
    )
    try:
        _spawn_manual(attempt)
    except RuntimeError as e:
        attempt.abort(f"Worker-Start fehlgeschlagen: {e}")
        raise RetryBlocked(f"Worker-Start fehlgeschlagen: {e}") from e
    return run_id
