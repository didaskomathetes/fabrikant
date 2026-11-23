"""Typed run domain model + state machines (ADR 0034, step 1).

Run, step, approval, usage and review result are immutable dataclasses with
enums for all status values — independent of persistence. Allowed status transitions
live in exactly one table per state machine (``RUN_TRANSITIONS``, ``STEP_TRANSITIONS``,
``APPROVAL_TRANSITIONS``); :func:`check_transition` enforces them and raises
:class:`InvalidTransition`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TypeVar

from .i18n import t


class RunStatus(StrEnum):
    """Status of a run (values = strings in the run JSON)."""

    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_APPROVAL = "needs_approval"
    PASS = "pass"  # nosec B105 # Status-Wert, kein Passwort
    FAIL = "fail"
    ERROR = "error"
    REJECTED = "rejected"


class StepStatus(StrEnum):
    """Status eines Steps."""

    RUNNING = "running"
    WAITING = "waiting"
    PASS = "pass"  # nosec B105 # Status-Wert, kein Passwort
    FAIL = "fail"
    TIMEOUT = "timeout"
    ERROR = "error"
    SKIPPED = "skipped"  # not executed any more behind a `done` step (run successful early)


class ApprovalStatus(StrEnum):
    """State of an approval. ``QUEUED`` = accepted, waiting for a run slot (ADR 0015)."""

    PENDING = "pending"
    QUEUED = "queued"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalReason(StrEnum):
    """Why the run pauses: step gate, dynamic gate (ADR 0010), tier approval (T2+),
    criticality (``human_gate``) or escalation to a human (ADR 0059)."""

    STEP = "step"
    GATE = "gate"
    TIER = "tier"
    CRITICALITY = "criticality"
    ESCALATION = "escalation"


class FailureKind(StrEnum):
    """Class of a failed step/run (ADR 0035); determines the reaction, not the source."""

    INTERRUPTED = "interrupted"  # aborted from outside (reaper, dead-man's switch)
    TRANSIENT = "transient"  # external dependency briefly unavailable
    ENVIRONMENT = "environment"  # Umgebung/Konfiguration kaputt — braucht einen Menschen
    OUTCOME = "outcome"  # echtes negatives Ergebnis (Tests rot, Quality Gate, Review)
    UNKNOWN = "unknown"  # not classified — never handle automatically


# Failed statuses (ADR 0035: only they carry `failure_kind`). StrEnum: `"fail" in FAILED_RUN_STATUSES` holds.
FAILED_RUN_STATUSES = frozenset({RunStatus.FAIL, RunStatus.ERROR})
FAILED_STEP_STATUSES = frozenset({StepStatus.FAIL, StepStatus.TIMEOUT, StepStatus.ERROR})

TIER_APPROVAL_STEP = "tier-approval"  # Name des synthetischen Tier-Gate-Steps (waiting_step)

_S, _R = RunStatus, StepStatus
_A = ApprovalStatus

# Allowed run transitions (ADR 0034 no. 2). Terminal: pass, fail, error, rejected.
RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    _S.QUEUED: frozenset({_S.RUNNING, _S.ERROR}),  # ERROR: Start gescheitert (Loop unbekannt, Worker-Start)
    _S.RUNNING: frozenset({_S.NEEDS_APPROVAL, _S.PASS, _S.FAIL, _S.ERROR}),  # ERROR auch: Reaper, Stale-Check
    _S.NEEDS_APPROVAL: frozenset({_S.RUNNING, _S.REJECTED, _S.ERROR}),  # RUNNING = Resume nach Approve
    _S.PASS: frozenset(),
    _S.FAIL: frozenset(),
    _S.ERROR: frozenset(),
    _S.REJECTED: frozenset(),
}

# Allowed step transitions. ``WAITING`` is also a start state (gate before execution),
# ``SKIPPED`` only a start state (steps behind a ``done`` step).
STEP_TRANSITIONS: Mapping[StepStatus, frozenset[StepStatus]] = {
    _R.RUNNING: frozenset({_R.WAITING, _R.PASS, _R.FAIL, _R.TIMEOUT, _R.ERROR}),
    _R.WAITING: frozenset({_R.RUNNING}),  # Resume executes the paused step again
    _R.PASS: frozenset(),
    _R.FAIL: frozenset(),
    _R.TIMEOUT: frozenset(),
    _R.ERROR: frozenset(),
    _R.SKIPPED: frozenset(),
}

# Allowed approval transitions. ``QUEUED → PENDING``: worker start failed, approval revoked.
APPROVAL_TRANSITIONS: Mapping[ApprovalStatus, frozenset[ApprovalStatus]] = {
    _A.PENDING: frozenset({_A.QUEUED, _A.APPROVED, _A.REJECTED}),
    _A.QUEUED: frozenset({_A.APPROVED, _A.REJECTED, _A.PENDING}),
    _A.APPROVED: frozenset(),
    _A.REJECTED: frozenset(),
}

TERMINAL_RUN_STATUSES = frozenset(s for s, targets in RUN_TRANSITIONS.items() if not targets)


class InvalidTransition(ValueError):
    """Illegal status transition of a state machine."""


_E = TypeVar("_E", bound=StrEnum)


def check_transition(table: Mapping[_E, frozenset[_E]], current: _E, target: _E) -> _E:
    """Checks ``current → target`` against ``table``; returns ``target`` or raises :class:`InvalidTransition`."""
    if target not in table.get(current, frozenset()):
        raise InvalidTransition(t("runmodel.not_allowed", v1=type(current).__name__, v2=current.value, v3=target.value))
    return target


@dataclass(frozen=True, slots=True)
class Usage:
    """Token/cost consumption of a step (LLM/agent). ``cost_usd`` is None if unknown.

    ``prompt_tokens`` includes the cache share; ``cache_read_tokens``/``cache_write_tokens`` break
    it down (0 = not reported, e.g. older runs) — see :mod:`fabrik.tokens`."""

    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    billing: str = ""  # api | subscription | unknown (fabrik.billing); "" = Altbestand


@dataclass(frozen=True, slots=True)
class Finding:
    """Ein Review-Befund; ``suggestion``: optionaler kleiner Fix-Vorschlag (``pr_review``, ADR 0039)."""

    severity: str = ""
    text: str = ""
    suggestion: str = ""


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """Ergebnis eines ``pr_review``-Steps (``verdict``: ``approved`` | ``changes_requested``)."""

    verdict: str = ""
    model: str = ""
    summary: str = ""
    pr: int | None = None
    round: int = 1
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class VerifyCheck:
    """Ergebnis eines ``verify``-Checks."""

    name: str
    ok: bool
    output: str = ""


@dataclass(frozen=True, slots=True)
class Step:
    """Ein Step eines Runs."""

    name: str
    type: str
    status: StepStatus
    duration_s: float | None = None
    summary: str = ""
    output: str = ""
    usage: Usage | None = None
    review: ReviewResult | None = None
    verify: tuple[VerifyCheck, ...] = ()
    failure_kind: FailureKind | None = None  # set only on fail/timeout/error
    retry_after: str = ""  # ISO timestamp (UTC), only for transient
    verdict: str = ""  # domain result of the step (e.g. audit `fail`), independent of the step status


# Domain result of a step (verdict), separate from the technical status: a step can
# run successfully and publish a `fail` verdict (e.g. relay-audit). Shell steps
# report it via their own output line `::fabrik-verdict::<word>`; the UI shows it as a badge.
VERDICT_MARKER = "::fabrik-verdict::"
_VERDICT_RE = re.compile(r"[a-z][a-z_-]{0,19}")
_VERDICT_LINE_RE = re.compile(r"^" + re.escape(VERDICT_MARKER) + r"(\S*)[ \t]*(?:\r?\n|\Z)", re.M)


def verdict_line(verdict: str) -> str:
    """Output line with which a shell step reports its verdict (ValueError on an invalid word)."""
    if not _VERDICT_RE.fullmatch(verdict):
        raise ValueError(t("runmodel.invalid_verdict_a_z_max", verdict=verdict))
    return f"{VERDICT_MARKER}{verdict}"


def split_verdict(text: str) -> tuple[str, str]:
    """(text without verdict lines, last valid verdict or ``""``)."""
    found = [v for v in _VERDICT_LINE_RE.findall(text) if _VERDICT_RE.fullmatch(v)]
    return _VERDICT_LINE_RE.sub("", text), (found[-1] if found else "")


@dataclass(frozen=True, slots=True)
class Approval:
    """Approval request of a paused (or formerly paused) run."""

    status: ApprovalStatus
    reason: ApprovalReason
    kind: str = ""  # escalation kind (ADR 0059), otherwise empty

    what: str = ""
    step: str = ""
    issue: str = ""
    condition: str = ""
    why: str = ""
    tier: int | None = None


@dataclass(frozen=True, slots=True)
class Run:
    """A run. Timestamps are ISO strings as in the record (``""`` = not set)."""

    id: str
    project: str
    loop: str
    status: RunStatus
    started: str = ""
    finished: str = ""
    trigger: str = ""
    note: str = ""
    spec_hash: str = ""
    steps: tuple[Step, ...] = ()
    approval: Approval | None = None
    waiting_step: str = ""
    resumed: bool = False
    approval_queued: str = ""  # Timestamp since when the approval has been waiting for a slot
    started_by: str | None = None
    approved_by: str | None = None
    rejected_by: str | None = None
    error: str = ""
    failure_kind: FailureKind | None = None  # set only on fail/error (ADR 0035)
    retry_after: str = ""  # ISO timestamp (UTC), only for transient
    retry_of: str = ""  # Run id of the previous attempt (ADR 0036); "" = no retry
    replay_of: str = ""  # Run id of the original if this is a replay (ADR 0061); "" = no replay

    attempt: int = 0  # 0-basierte Versuchszahl (ADR 0036); 0 = erster Run
    worktree_cleanup_error: str = ""
    # Read-only (MappingProxyType): the run is frozen, ``vars`` too —
    # ``run.vars["x"] = 1`` raises TypeError. :func:`fabrik.runstore.parse_run` wraps
    # the parsed values; the default is an empty, read-only mapping.
    vars: Mapping[str, str | int | float | bool] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def verdict(self) -> str:
        """Domain result of the run: verdict of the last step that reported one."""
        return next((s.verdict for s in reversed(self.steps) if s.verdict), "")

    @property
    def is_terminal(self) -> bool:
        """True if the run no longer continues."""
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def awaiting_tier_approval(self) -> bool:
        """True if the run is stuck at the synthetic tier gate."""
        return self.status is RunStatus.NEEDS_APPROVAL and self.waiting_step == TIER_APPROVAL_STEP
