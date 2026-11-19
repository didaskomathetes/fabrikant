"""Typisiertes Run-Domänenmodell + Zustandsautomaten (ADR 0034, Schritt 1).

Run, Step, Approval, Usage und Review-Ergebnis sind unveränderliche Dataclasses mit
Enums für alle Status-Werte — unabhängig von der Persistenz. Erlaubte Statusübergänge
stehen pro Automat in genau einer Tabelle (``RUN_TRANSITIONS``, ``STEP_TRANSITIONS``,
``APPROVAL_TRANSITIONS``); :func:`check_transition` erzwingt sie und wirft
:class:`InvalidTransition`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TypeVar


class RunStatus(StrEnum):
    """Status eines Runs (Werte = Strings im Run-JSON)."""

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
    SKIPPED = "skipped"  # hinter einem `done`-Step nicht mehr ausgeführt (Run vorzeitig erfolgreich)


class ApprovalStatus(StrEnum):
    """Zustand einer Freigabe. ``QUEUED`` = akzeptiert, wartet auf einen Run-Slot (ADR 0015)."""

    PENDING = "pending"
    QUEUED = "queued"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalReason(StrEnum):
    """Warum der Run pausiert: Step-Gate, dynamisches Gate (ADR 0010), Tier-Freigabe (T2+) oder
    Kritikalität (``human_gate``) oder Eskalation an einen Menschen (ADR 0059)."""

    STEP = "step"
    GATE = "gate"
    TIER = "tier"
    CRITICALITY = "criticality"
    ESCALATION = "escalation"


class FailureKind(StrEnum):
    """Klasse eines gescheiterten Steps/Runs (ADR 0035); bestimmt die Reaktion, nicht die Quelle."""

    INTERRUPTED = "interrupted"  # von außen abgebrochen (Reaper, Dead-Man's-Switch)
    TRANSIENT = "transient"  # externe Abhängigkeit kurz nicht verfügbar
    ENVIRONMENT = "environment"  # Umgebung/Konfiguration kaputt — braucht einen Menschen
    OUTCOME = "outcome"  # echtes negatives Ergebnis (Tests rot, Quality Gate, Review)
    UNKNOWN = "unknown"  # nicht klassifiziert — nie automatisch behandeln


# Gescheiterte Status (ADR 0035: nur sie tragen `failure_kind`). StrEnum: `"fail" in FAILED_RUN_STATUSES` gilt.
FAILED_RUN_STATUSES = frozenset({RunStatus.FAIL, RunStatus.ERROR})
FAILED_STEP_STATUSES = frozenset({StepStatus.FAIL, StepStatus.TIMEOUT, StepStatus.ERROR})

TIER_APPROVAL_STEP = "tier-approval"  # Name des synthetischen Tier-Gate-Steps (waiting_step)

_S, _R = RunStatus, StepStatus
_A = ApprovalStatus

# Erlaubte Run-Übergänge (ADR 0034 Nr. 2). Terminal: pass, fail, error, rejected.
RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    _S.QUEUED: frozenset({_S.RUNNING, _S.ERROR}),  # ERROR: Start gescheitert (Loop unbekannt, Worker-Start)
    _S.RUNNING: frozenset({_S.NEEDS_APPROVAL, _S.PASS, _S.FAIL, _S.ERROR}),  # ERROR auch: Reaper, Stale-Check
    _S.NEEDS_APPROVAL: frozenset({_S.RUNNING, _S.REJECTED, _S.ERROR}),  # RUNNING = Resume nach Approve
    _S.PASS: frozenset(),
    _S.FAIL: frozenset(),
    _S.ERROR: frozenset(),
    _S.REJECTED: frozenset(),
}

# Erlaubte Step-Übergänge. ``WAITING`` ist auch Startzustand (Gate vor der Ausführung),
# ``SKIPPED`` nur Startzustand (Steps hinter einem ``done``-Step).
STEP_TRANSITIONS: Mapping[StepStatus, frozenset[StepStatus]] = {
    _R.RUNNING: frozenset({_R.WAITING, _R.PASS, _R.FAIL, _R.TIMEOUT, _R.ERROR}),
    _R.WAITING: frozenset({_R.RUNNING}),  # Resume führt den pausierten Step erneut aus
    _R.PASS: frozenset(),
    _R.FAIL: frozenset(),
    _R.TIMEOUT: frozenset(),
    _R.ERROR: frozenset(),
    _R.SKIPPED: frozenset(),
}

# Erlaubte Approval-Übergänge. ``QUEUED → PENDING``: Worker-Start scheiterte, Freigabe zurückgenommen.
APPROVAL_TRANSITIONS: Mapping[ApprovalStatus, frozenset[ApprovalStatus]] = {
    _A.PENDING: frozenset({_A.QUEUED, _A.APPROVED, _A.REJECTED}),
    _A.QUEUED: frozenset({_A.APPROVED, _A.REJECTED, _A.PENDING}),
    _A.APPROVED: frozenset(),
    _A.REJECTED: frozenset(),
}

TERMINAL_RUN_STATUSES = frozenset(s for s, targets in RUN_TRANSITIONS.items() if not targets)


class InvalidTransition(ValueError):
    """Unerlaubter Statusübergang eines Zustandsautomaten."""


_E = TypeVar("_E", bound=StrEnum)


def check_transition(table: Mapping[_E, frozenset[_E]], current: _E, target: _E) -> _E:
    """Prüft ``current → target`` gegen ``table``; liefert ``target`` oder wirft :class:`InvalidTransition`."""
    if target not in table.get(current, frozenset()):
        raise InvalidTransition(f"{type(current).__name__}: {current.value} → {target.value} nicht erlaubt")
    return target


@dataclass(frozen=True, slots=True)
class Usage:
    """Token-/Kosten-Verbrauch eines Steps (LLM/Agent). ``cost_usd`` ist None, wenn unbekannt.

    ``prompt_tokens`` enthält den Cache-Anteil; ``cache_read_tokens``/``cache_write_tokens`` schlüsseln
    ihn auf (0 = nicht gemeldet, z. B. ältere Runs) — siehe :mod:`fabrik.tokens`."""

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
    failure_kind: FailureKind | None = None  # nur bei fail/timeout/error gesetzt
    retry_after: str = ""  # ISO-Zeitpunkt (UTC), nur bei transient
    verdict: str = ""  # fachliches Ergebnis des Steps (z. B. Audit `fail`), unabhängig vom Step-Status


# Fachliches Ergebnis eines Steps (Verdikt), getrennt vom technischen Status: ein Step kann
# erfolgreich laufen und ein `fail`-Verdikt veröffentlichen (z. B. relay-audit). Shell-Steps
# melden es über eine eigene Ausgabezeile `::fabrik-verdict::<wort>`; die UI zeigt es als Badge.
VERDICT_MARKER = "::fabrik-verdict::"
_VERDICT_RE = re.compile(r"[a-z][a-z_-]{0,19}")
_VERDICT_LINE_RE = re.compile(r"^" + re.escape(VERDICT_MARKER) + r"(\S*)[ \t]*(?:\r?\n|\Z)", re.M)


def verdict_line(verdict: str) -> str:
    """Ausgabezeile, mit der ein Shell-Step sein Verdikt meldet (ValueError bei ungültigem Wort)."""
    if not _VERDICT_RE.fullmatch(verdict):
        raise ValueError(f"ungültiges Verdikt {verdict!r} (a-z, _, -; max. 20 Zeichen)")
    return f"{VERDICT_MARKER}{verdict}"


def split_verdict(text: str) -> tuple[str, str]:
    """(Text ohne Verdikt-Zeilen, letztes gültiges Verdikt oder ``""``)."""
    found = [v for v in _VERDICT_LINE_RE.findall(text) if _VERDICT_RE.fullmatch(v)]
    return _VERDICT_LINE_RE.sub("", text), (found[-1] if found else "")


@dataclass(frozen=True, slots=True)
class Approval:
    """Freigabe-Anfrage eines pausierten (oder früher pausierten) Runs."""

    status: ApprovalStatus
    reason: ApprovalReason
    kind: str = ""  # Eskalationsart (ADR 0059), sonst leer
    what: str = ""
    step: str = ""
    issue: str = ""
    condition: str = ""
    why: str = ""
    tier: int | None = None


@dataclass(frozen=True, slots=True)
class Run:
    """Ein Run. Zeitstempel sind ISO-Strings wie im Record (``""`` = nicht gesetzt)."""

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
    approval_queued: str = ""  # Zeitstempel, ab wann die Freigabe auf einen Slot wartet
    started_by: str | None = None
    approved_by: str | None = None
    rejected_by: str | None = None
    error: str = ""
    failure_kind: FailureKind | None = None  # nur bei fail/error gesetzt (ADR 0035)
    retry_after: str = ""  # ISO-Zeitpunkt (UTC), nur bei transient
    retry_of: str = ""  # Run-Id des vorherigen Versuchs (ADR 0036); "" = kein Retry
    attempt: int = 0  # 0-basierte Versuchszahl (ADR 0036); 0 = erster Run
    worktree_cleanup_error: str = ""
    # Schreibgeschützt (MappingProxyType): der Run ist frozen, ``vars`` auch —
    # ``run.vars["x"] = 1`` wirft TypeError. :func:`fabrik.runstore.parse_run` verpackt
    # die geparsten Werte; der Default ist eine leere, schreibgeschützte Mapping.
    vars: Mapping[str, str | int | float | bool] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def verdict(self) -> str:
        """Fachliches Ergebnis des Runs: Verdikt des letzten Steps, der eins gemeldet hat."""
        return next((s.verdict for s in reversed(self.steps) if s.verdict), "")

    @property
    def is_terminal(self) -> bool:
        """True, wenn der Run nicht mehr weiterläuft."""
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def awaiting_tier_approval(self) -> bool:
        """True, wenn der Run am synthetischen Tier-Gate hängt."""
        return self.status is RunStatus.NEEDS_APPROVAL and self.waiting_step == TIER_APPROVAL_STEP
