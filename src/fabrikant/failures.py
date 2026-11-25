"""Failure classification of failed steps and runs (ADR 0035).

Classification happens at the source: steps report ``failure_kind`` (and for ``transient`` optionally
``retry_after``) in the step result (:func:`failed`) or raise a :class:`StepFailure`. Typed
exceptions (HTTP status, network errors, missing files) are mapped by :func:`classify_exception`.

Text classification exists only as a narrowly bounded fallback for output of foreign CLIs
(agent/LLM CLIs, ``git``) — exclusively in this module (:func:`classify_agent_output`,
:func:`classify_git_output`). The default is always :attr:`FailureKind.UNKNOWN`.
"""

from __future__ import annotations

import re
import signal
import urllib.error
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from .runmodel import FAILED_STEP_STATUSES, FailureKind

# Shell exit codes that mean a broken environment: 126 = not executable, 127 = not found.
ENVIRONMENT_EXIT_CODES = frozenset({126, 127})
# sysexits.h: scripts report their failure class themselves via the exit code (takes precedence over the spec's `failure_kind`).
EX_TEMPFAIL = 75  # temporary error, try again later
EX_CONFIG = 78  # Konfigurationsfehler
_EXIT_CODE_KINDS = {
    **dict.fromkeys(ENVIRONMENT_EXIT_CODES, FailureKind.ENVIRONMENT),
    EX_TEMPFAIL: FailureKind.TRANSIENT,
    EX_CONFIG: FailureKind.ENVIRONMENT,
}
# HTTP statuses where a later attempt plausibly changes something (timeout, rate limit, server error).
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429})
AUTH_HTTP_STATUSES = frozenset({401, 403})
_SERVER_ERROR_MIN = 500
# 5xx that a retry does not heal (method or HTTP version not supported by the server).
_PERMANENT_SERVER_STATUSES = frozenset({501, 505})
# Classes a spec may set for exit codes ≠ 0 of a shell step (`failure_kind:`); `interrupted`
# only arises from external abort, `unknown` is the default anyway.
SPEC_FAILURE_KINDS = frozenset({"transient", "environment", "outcome"})
# Death by signal n: exit 128+n (shell convention) or -n (``subprocess``); highest signal number on Linux.
_SIGNAL_EXIT_BASE = 128
_MAX_SIGNAL = 64

_F = FailureKind


class Headers(Protocol):
    """HTTP header access (``Mapping`` or ``email.message.Message`` from ``HTTPError.headers``)."""

    def get(self, name: str, /) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Failure:
    """Class of a failure plus earliest sensible retry time (only for ``transient``)."""

    kind: FailureKind = FailureKind.UNKNOWN
    retry_after: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind is not FailureKind.TRANSIENT and self.retry_after is not None:
            object.__setattr__(self, "retry_after", None)  # ADR 0035: only allowed for transient

    def fields(self) -> dict[str, str]:
        """Fields for step result or record: ``failure_kind`` and, if known, ``retry_after`` (ISO, UTC)."""
        out = {"failure_kind": self.kind.value}
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after.astimezone(UTC).isoformat()
        return out


class StepFailure(Exception):
    """Typed error with class; the engine takes ``failure`` instead of interpreting the text."""

    def __init__(self, message: str, kind: FailureKind = FailureKind.UNKNOWN, retry_after: datetime | None = None):
        super().__init__(message)
        self.failure = Failure(kind, retry_after)


class ConfigError(StepFailure, ValueError):
    """Invalid step/spec configuration (``environment``); remains a ``ValueError`` for existing callers."""

    def __init__(self, message: str):
        super().__init__(message, FailureKind.ENVIRONMENT)


class SetupError(StepFailure, RuntimeError):
    """Run work environment cannot be established (subpath missing in worktree, project/loop unknown, spec
    broken) → ``environment``; remains a ``RuntimeError`` for existing callers."""

    def __init__(self, message: str):
        super().__init__(message, FailureKind.ENVIRONMENT)


class GitError(StepFailure, RuntimeError):
    """Failed ``git`` call; the class comes from :func:`classify_git_output` (stderr)."""

    def __init__(self, message: str, output: str):
        super().__init__(message, classify_git_output(output))


def failed(summary: str, kind: FailureKind | Failure, **extra: Any) -> dict:
    """Step-Result ``ok: False`` mit Fehlerklasse (ADR 0035): ``failed("…", FailureKind.OUTCOME, output=…)``."""
    failure = kind if isinstance(kind, Failure) else Failure(kind)
    return {"ok": False, "summary": summary, **extra, **failure.fields()}


# Exit code with which a custom script reports its class (inverse of :func:`classify_exit_code`).
_KIND_EXIT_CODES = {FailureKind.TRANSIENT: EX_TEMPFAIL, FailureKind.ENVIRONMENT: EX_CONFIG}
EXIT_UNKNOWN = 1


def exit_code(kind: FailureKind) -> int:
    """Class → exit code for Python shell steps: ``transient`` → 75, ``environment`` → 78, otherwise 1.

    ``interrupted`` only arises from outside, ``outcome`` has no exit-code convention (→ ``unknown``)."""
    return _KIND_EXIT_CODES.get(kind, EXIT_UNKNOWN)


def classify_exit_code(code: int | None) -> FailureKind:
    """Shell exit code → class: 126/127 (not executable/found) and 78 (EX_CONFIG) → ``environment``,
    75 (EX_TEMPFAIL) → ``transient``; anything else the shell cannot interpret → ``unknown``."""
    return _EXIT_CODE_KINDS.get(code, _F.UNKNOWN) if code is not None else _F.UNKNOWN


def is_signal_exit(code: int) -> bool:
    """True if the process died from a signal (OOM kill, SIGTERM, …): says nothing about its result."""
    return code < 0 or _SIGNAL_EXIT_BASE < code <= _SIGNAL_EXIT_BASE + _MAX_SIGNAL


def describe_exit(code: int) -> str:
    """Exit code for humans: ``exit 1``, ``signal SIGSEGV`` (``subprocess``: -11) or
    ``exit 139 (SIGSEGV)`` (shell/``docker run``: 128+n). Unknown signal numbers stay numbers."""
    if not is_signal_exit(code):
        return f"exit {code}"
    number = -code if code < 0 else code - _SIGNAL_EXIT_BASE
    try:
        name = signal.Signals(number).name
    except ValueError:
        name = f"signal {number}"
    return f"signal {name}" if code < 0 else f"exit {code} ({name})"


def _retry_after_header(headers: Headers | None, now: datetime) -> datetime | None:
    """``Retry-After`` (seconds or HTTP date) or GitHub's ``X-RateLimit-Reset`` (epoch) → point in time.

    ``X-RateLimit-Reset`` counts only for an exhausted quota (``X-RateLimit-Remaining: 0``): GitHub sends
    the headers with every response, so a 5xx would otherwise carry the rate-limit window's reset as ``retry_after``."""
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if raw:
        if raw.strip().isdigit():
            return now + timedelta(seconds=int(raw.strip()))
        try:
            return parsedate_to_datetime(raw).astimezone(UTC)
        except (TypeError, ValueError):
            return None
    reset = headers.get("X-RateLimit-Reset")
    if _rate_limit_exhausted(headers) and reset and reset.strip().isdigit():
        return datetime.fromtimestamp(int(reset.strip()), UTC)
    return None


def _rate_limit_exhausted(headers: Headers) -> bool:
    """GitHub's primary rate limit is exhausted (``X-RateLimit-Remaining: 0``)."""
    return (headers.get("X-RateLimit-Remaining") or "").strip() == "0"


def _rate_limited(headers: Headers | None) -> bool:
    """GitHub also reports rate limits as 403 — recognizable by ``Retry-After`` or ``X-RateLimit-Remaining: 0``."""
    if headers is None:
        return False
    return bool(headers.get("Retry-After")) or _rate_limit_exhausted(headers)


def classify_http(status: int, headers: Headers | None = None, now: datetime | None = None) -> Failure:
    """HTTP status (+ headers) → class.

    408/425/429/5xx (except 501/505) and rate-limited 403 → ``transient`` (with ``retry_after`` from the header);
    other 401/403 → ``environment``; the rest (404, 409, 422, 501, …) → ``unknown`` (domain-specific per step)."""
    now = now or datetime.now(UTC)
    server_error = status >= _SERVER_ERROR_MIN and status not in _PERMANENT_SERVER_STATUSES
    if status in TRANSIENT_HTTP_STATUSES or server_error or (status == 403 and _rate_limited(headers)):
        return Failure(_F.TRANSIENT, _retry_after_header(headers, now))
    if status in AUTH_HTTP_STATUSES:
        return Failure(_F.ENVIRONMENT)
    return Failure(_F.UNKNOWN)


def classify_exception(exc: BaseException) -> Failure:
    """Exception → class, exclusively via the type (and typed fields like the HTTP status)."""
    if isinstance(exc, StepFailure):
        return exc.failure
    if isinstance(exc, urllib.error.HTTPError):
        return classify_http(exc.code, exc.headers)
    if isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError)):
        return Failure(_F.TRANSIENT)
    if isinstance(exc, (FileNotFoundError, PermissionError, NotADirectoryError)):
        return Failure(_F.ENVIRONMENT)  # fehlendes Tool/File oder fehlende Rechte
    # Remainder, also subprocess.TimeoutExpired: whether a command hung or was merely slow is open.
    return Failure(_F.UNKNOWN)


# ---------------------------------------------------------------- text fallback (foreign CLIs only)
# Order matters (ADR 0035 no. 1): limits with announced reset → transient, before exhausted
# quotas/auth → environment (including "usage limit reached" without reset), before the remaining transient patterns.
# Status codes only with context ("API Error: 401", "status 503"): otherwise every number in the agent text matches.
_HTTP_CODE_CONTEXT = r"(?:error|status|http)\W{0,3}"
_AGENT_SELF_RESETTING = re.compile(
    r"limit will reset|limit reached\W{1,5}resets (?:at |in )?\d|quota exceeded\b[^\n]{0,160}?\bper (?:second|minute|hour|day)\b",
    re.IGNORECASE,
)
_AGENT_ENVIRONMENT = re.compile(
    r"insufficient_quota|check your (?:plan and )?billing|billing (?:details|issue|error|problem)"
    r"|credit balance is too low|payment required|quota exceeded|usage limit reached"
    r"|hit your usage limit|usage_limit_reached|credits_depleted"
    rf"|invalid api key|authentication_error|{_HTTP_CODE_CONTEXT}401\b|401 unauthorized"
    r"|model \S+ (?:from --model flag )?is not available|model not found",
    re.IGNORECASE,
)
_AGENT_TRANSIENT = re.compile(
    rf"{_HTTP_CODE_CONTEXT}(?:429|50[0234]|529)\b|rate.?limit|too many requests|overloaded|internal server error"
    r"|bad gateway"
    r"|service unavailable|gateway time-?out|provider\.transport|connection (?:lost|reset|refused|closed)"
    r"|econnreset|etimedout|socket hang up|temporarily unavailable",
    re.IGNORECASE,
)
# The model's context window is full (Claude Code, Codex/OpenAI, opencode). A retry with the same prompt
# fails again → ``outcome``; the agent step warns that the work needs rescoping.
_AGENT_CONTEXT_EXHAUSTED = re.compile(
    r"prompt is too long|context[_ ]length[_ ]exceeded|maximum context length"
    r"|exceeds? (?:the |its )?(?:model'?s? )?context window"
    r"|context (?:window|limit) (?:exceeded|reached|exhausted|is full)|context ?overflow",
    re.IGNORECASE,
)
# Claude Code reports an exhausted usage window as "Claude AI usage limit reached|<epoch>".
_USAGE_LIMIT_RESET = re.compile(r"usage limit reached\|(\d{9,11})", re.IGNORECASE)


def is_context_exhausted(text: str) -> bool:
    """True if an agent/LLM CLI reports that its context window is exhausted."""
    return bool(text) and _AGENT_CONTEXT_EXHAUSTED.search(text) is not None


def classify_agent_output(text: str) -> Failure:
    """Fallback for error texts of the agent/LLM CLIs (not controlled by us, ADR 0035 no. 3)."""
    if not text:
        return Failure(_F.UNKNOWN)
    if m := _USAGE_LIMIT_RESET.search(text):
        return Failure(_F.TRANSIENT, datetime.fromtimestamp(int(m.group(1)), UTC))
    if is_context_exhausted(text):
        return Failure(_F.OUTCOME)
    if _AGENT_SELF_RESETTING.search(text):
        return Failure(_F.TRANSIENT)
    if _AGENT_ENVIRONMENT.search(text):
        return Failure(_F.ENVIRONMENT)
    if _AGENT_TRANSIENT.search(text):
        return Failure(_F.TRANSIENT)
    return Failure(_F.UNKNOWN)


_GIT_ENVIRONMENT = re.compile(
    r"authentication failed|invalid username or token|permission denied \(publickey\)"
    r"|repository not found|could not read username|returned error: 40[13]"
    r"|ssl certificate problem|certificate verify failed|could not resolve proxy|proxy connect aborted",
    re.IGNORECASE,
)
_GIT_TRANSIENT = re.compile(
    r"could not resolve host|temporary failure in name resolution|failed to connect|connection timed out"
    r"|connection reset|operation timed out|early eof|rpc failed|remote end hung up"
    r"|returned error: (?:429|5\d\d)",
    re.IGNORECASE,
)


def classify_git_output(text: str) -> FailureKind:
    """Fallback for ``git`` stderr (exit code is almost always 128): network → ``transient``, auth → ``environment``."""
    if _GIT_ENVIRONMENT.search(text or ""):
        return _F.ENVIRONMENT
    if _GIT_TRANSIENT.search(text or ""):
        return _F.TRANSIENT
    return _F.UNKNOWN


# ---------------------------------------------------------------- Lesen aus Step-Results/Records
def parse_kind(value: object) -> FailureKind:
    """Wert aus Step-Result/Record → Klasse; fehlend/unbekannt → ``unknown`` (Altbestand, ADR 0035)."""
    try:
        return FailureKind(str(value)) if value else _F.UNKNOWN
    except ValueError:
        return _F.UNKNOWN


def parse_retry_after(value: object) -> datetime | None:
    """ISO timestamp → aware UTC datetime; invalid/missing → None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def from_result(res: Mapping[str, Any]) -> Failure:
    """Class of a step result (``failure_kind``/``retry_after``); ``unknown`` if not given."""
    return Failure(parse_kind(res.get("failure_kind")), parse_retry_after(res.get("retry_after")))


def run_failure(steps: Iterable[Mapping[str, Any]], aborted_by: int | None) -> Failure:
    """Class of the run (ADR 0035 no. 2): that of the aborting step (index ``aborted_by``), otherwise that of the
    first failed step. ``retry_after`` = latest value of *all* failed steps (even non-
    decisive ones; ADR 0035: contradictions are resolved by the maximum), only for ``transient``."""
    failed_steps = [(i, s) for i, s in enumerate(steps) if s.get("status") in FAILED_STEP_STATUSES]
    if not failed_steps:
        return Failure(_F.UNKNOWN)
    decisive = next((s for i, s in failed_steps if i == aborted_by), failed_steps[0][1])
    kind = parse_kind(decisive.get("failure_kind"))
    times = [t for _, s in failed_steps if (t := parse_retry_after(s.get("retry_after"))) is not None]
    return Failure(kind, max(times) if times else None)
