"""Fehlerklassifikation gescheiterter Steps und Runs (ADR 0035).

Klassifiziert wird an der Quelle: Steps melden ``failure_kind`` (und bei ``transient`` optional
``retry_after``) im Step-Result (:func:`failed`) oder werfen eine :class:`StepFailure`. Typisierte
Exceptions (HTTP-Status, Netzfehler, fehlende Dateien) ordnet :func:`classify_exception` zu.

Text-Klassifikation gibt es nur als eng begrenzten Fallback für Ausgaben fremder CLIs
(Agent-/LLM-CLIs, ``git``) — ausschließlich in diesem Modul (:func:`classify_agent_output`,
:func:`classify_git_output`). Default ist immer :attr:`FailureKind.UNKNOWN`.
"""

from __future__ import annotations

import re
import urllib.error
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from .runmodel import FAILED_STEP_STATUSES, FailureKind

# Exit-Codes der Shell, die eine kaputte Umgebung bedeuten: 126 = nicht ausführbar, 127 = nicht gefunden.
ENVIRONMENT_EXIT_CODES = frozenset({126, 127})
# sysexits.h: Skripte melden ihre Fehlerklasse selbst über den Exit-Code (Vorrang vor `failure_kind` der Spec).
EX_TEMPFAIL = 75  # temporärer Fehler, später erneut versuchen
EX_CONFIG = 78  # Konfigurationsfehler
_EXIT_CODE_KINDS = {
    **dict.fromkeys(ENVIRONMENT_EXIT_CODES, FailureKind.ENVIRONMENT),
    EX_TEMPFAIL: FailureKind.TRANSIENT,
    EX_CONFIG: FailureKind.ENVIRONMENT,
}
# HTTP-Status, bei denen ein späterer Versuch plausibel etwas ändert (Timeout, Rate-Limit, Serverfehler).
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429})
AUTH_HTTP_STATUSES = frozenset({401, 403})
_SERVER_ERROR_MIN = 500
# 5xx, die ein Retry nicht heilt (Methode bzw. HTTP-Version vom Server nicht unterstützt).
_PERMANENT_SERVER_STATUSES = frozenset({501, 505})
# Klassen, die eine Spec für Exit-Codes ≠ 0 eines shell-Steps setzen darf (`failure_kind:`); `interrupted`
# entsteht nur durch Abbruch von außen, `unknown` ist ohnehin der Default.
SPEC_FAILURE_KINDS = frozenset({"transient", "environment", "outcome"})
# Tod durch Signal n: Exit 128+n (Shell-Konvention) bzw. -n (``subprocess``); höchste Signalnummer unter Linux.
_SIGNAL_EXIT_BASE = 128
_MAX_SIGNAL = 64

_F = FailureKind


class Headers(Protocol):
    """HTTP-Header-Zugriff (``Mapping`` oder ``email.message.Message`` aus ``HTTPError.headers``)."""

    def get(self, name: str, /) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Failure:
    """Klasse eines Fehlers plus frühester sinnvoller Wiederholungszeitpunkt (nur bei ``transient``)."""

    kind: FailureKind = FailureKind.UNKNOWN
    retry_after: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind is not FailureKind.TRANSIENT and self.retry_after is not None:
            object.__setattr__(self, "retry_after", None)  # ADR 0035: nur bei transient zulässig

    def fields(self) -> dict[str, str]:
        """Felder für Step-Result bzw. Record: ``failure_kind`` und, falls bekannt, ``retry_after`` (ISO, UTC)."""
        out = {"failure_kind": self.kind.value}
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after.astimezone(UTC).isoformat()
        return out


class StepFailure(Exception):
    """Typisierter Fehler mit Klasse; die Engine übernimmt ``failure`` statt den Text zu deuten."""

    def __init__(self, message: str, kind: FailureKind = FailureKind.UNKNOWN, retry_after: datetime | None = None):
        super().__init__(message)
        self.failure = Failure(kind, retry_after)


class ConfigError(StepFailure, ValueError):
    """Ungültige Step-/Spec-Konfiguration (``environment``); bleibt ein ``ValueError`` für bestehende Aufrufer."""

    def __init__(self, message: str):
        super().__init__(message, FailureKind.ENVIRONMENT)


class SetupError(StepFailure, RuntimeError):
    """Arbeitsumgebung des Runs nicht herstellbar (Subpfad fehlt im Worktree, Projekt/Loop unbekannt, Spec
    defekt) → ``environment``; bleibt ein ``RuntimeError`` für bestehende Aufrufer."""

    def __init__(self, message: str):
        super().__init__(message, FailureKind.ENVIRONMENT)


class GitError(StepFailure, RuntimeError):
    """Gescheiterter ``git``-Aufruf; die Klasse kommt aus :func:`classify_git_output` (stderr)."""

    def __init__(self, message: str, output: str):
        super().__init__(message, classify_git_output(output))


def failed(summary: str, kind: FailureKind | Failure, **extra: Any) -> dict:
    """Step-Result ``ok: False`` mit Fehlerklasse (ADR 0035): ``failed("…", FailureKind.OUTCOME, output=…)``."""
    failure = kind if isinstance(kind, Failure) else Failure(kind)
    return {"ok": False, "summary": summary, **extra, **failure.fields()}


# Exit-Code, mit dem ein eigenes Skript seine Klasse meldet (Umkehrung von :func:`classify_exit_code`).
_KIND_EXIT_CODES = {FailureKind.TRANSIENT: EX_TEMPFAIL, FailureKind.ENVIRONMENT: EX_CONFIG}
EXIT_UNKNOWN = 1


def exit_code(kind: FailureKind) -> int:
    """Klasse → Exit-Code für Python-Shell-Steps: ``transient`` → 75, ``environment`` → 78, sonst 1.

    ``interrupted`` entsteht nur von außen, ``outcome`` kennt keine Exit-Code-Konvention (→ ``unknown``)."""
    return _KIND_EXIT_CODES.get(kind, EXIT_UNKNOWN)


def classify_exit_code(code: int | None) -> FailureKind:
    """Shell-Exit-Code → Klasse: 126/127 (nicht ausführbar/gefunden) und 78 (EX_CONFIG) → ``environment``,
    75 (EX_TEMPFAIL) → ``transient``; alles andere kann die Shell nicht deuten → ``unknown``."""
    return _EXIT_CODE_KINDS.get(code, _F.UNKNOWN) if code is not None else _F.UNKNOWN


def is_signal_exit(code: int) -> bool:
    """True, wenn der Prozess durch ein Signal starb (OOM-Kill, SIGTERM, …): sagt nichts über sein Ergebnis."""
    return code < 0 or _SIGNAL_EXIT_BASE < code <= _SIGNAL_EXIT_BASE + _MAX_SIGNAL


def _retry_after_header(headers: Headers | None, now: datetime) -> datetime | None:
    """``Retry-After`` (Sekunden oder HTTP-Datum) bzw. GitHubs ``X-RateLimit-Reset`` (Epoch) → Zeitpunkt.

    ``X-RateLimit-Reset`` zählt nur bei erschöpftem Kontingent (``X-RateLimit-Remaining: 0``): GitHub sendet
    die Header bei jeder Antwort, ein 5xx hätte sonst den Reset des Rate-Limit-Fensters als ``retry_after``."""
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
    """GitHubs primäres Rate-Limit ist erschöpft (``X-RateLimit-Remaining: 0``)."""
    return (headers.get("X-RateLimit-Remaining") or "").strip() == "0"


def _rate_limited(headers: Headers | None) -> bool:
    """GitHub meldet Rate-Limits auch als 403 — erkennbar an ``Retry-After`` bzw. ``X-RateLimit-Remaining: 0``."""
    if headers is None:
        return False
    return bool(headers.get("Retry-After")) or _rate_limit_exhausted(headers)


def classify_http(status: int, headers: Headers | None = None, now: datetime | None = None) -> Failure:
    """HTTP-Status (+ Header) → Klasse.

    408/425/429/5xx (außer 501/505) und rate-limitierte 403 → ``transient`` (mit ``retry_after`` aus dem Header);
    sonstige 401/403 → ``environment``; Rest (404, 409, 422, 501, …) → ``unknown`` (fachlich je Step)."""
    now = now or datetime.now(UTC)
    server_error = status >= _SERVER_ERROR_MIN and status not in _PERMANENT_SERVER_STATUSES
    if status in TRANSIENT_HTTP_STATUSES or server_error or (status == 403 and _rate_limited(headers)):
        return Failure(_F.TRANSIENT, _retry_after_header(headers, now))
    if status in AUTH_HTTP_STATUSES:
        return Failure(_F.ENVIRONMENT)
    return Failure(_F.UNKNOWN)


def classify_exception(exc: BaseException) -> Failure:
    """Exception → Klasse, ausschließlich über den Typ (und typisierte Felder wie den HTTP-Status)."""
    if isinstance(exc, StepFailure):
        return exc.failure
    if isinstance(exc, urllib.error.HTTPError):
        return classify_http(exc.code, exc.headers)
    if isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError)):
        return Failure(_F.TRANSIENT)
    if isinstance(exc, (FileNotFoundError, PermissionError, NotADirectoryError)):
        return Failure(_F.ENVIRONMENT)  # fehlendes Tool/File oder fehlende Rechte
    # Rest, auch subprocess.TimeoutExpired: ob ein Befehl hing oder nur langsam war, ist offen.
    return Failure(_F.UNKNOWN)


# ---------------------------------------------------------------- Text-Fallback (nur fremde CLIs)
# Reihenfolge zählt (ADR 0035 Nr. 1): Limits mit angekündigtem Reset → transient, vor erschöpften
# Quotas/Auth → environment (darunter "usage limit reached" ohne Reset), vor den übrigen transienten Mustern.
# Status-Codes nur mit Kontext ("API Error: 401", "status 503"): sonst trifft jede Zahl im Agent-Text.
_HTTP_CODE_CONTEXT = r"(?:error|status|http)\W{0,3}"
_AGENT_SELF_RESETTING = re.compile(
    r"limit will reset|limit reached\W{1,5}resets (?:at |in )?\d|quota exceeded\b[^\n]{0,160}?\bper (?:second|minute|hour|day)\b",
    re.IGNORECASE,
)
_AGENT_ENVIRONMENT = re.compile(
    r"insufficient_quota|check your (?:plan and )?billing|billing (?:details|issue|error|problem)"
    r"|credit balance is too low|payment required|quota exceeded|usage limit reached"
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
# Claude Code meldet ein erschöpftes Nutzungsfenster als "Claude AI usage limit reached|<epoch>".
_USAGE_LIMIT_RESET = re.compile(r"usage limit reached\|(\d{9,11})", re.IGNORECASE)


def classify_agent_output(text: str) -> Failure:
    """Fallback für Fehlertexte der Agent-/LLM-CLIs (nicht von uns kontrolliert, ADR 0035 Nr. 3)."""
    if not text:
        return Failure(_F.UNKNOWN)
    if m := _USAGE_LIMIT_RESET.search(text):
        return Failure(_F.TRANSIENT, datetime.fromtimestamp(int(m.group(1)), UTC))
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
    """Fallback für ``git``-stderr (Exit-Code ist fast immer 128): Netz → ``transient``, Auth → ``environment``."""
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
    """ISO-Zeitpunkt → aware UTC-datetime; ungültig/fehlend → None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def from_result(res: Mapping[str, Any]) -> Failure:
    """Klasse eines Step-Results (``failure_kind``/``retry_after``); ohne Angabe ``unknown``."""
    return Failure(parse_kind(res.get("failure_kind")), parse_retry_after(res.get("retry_after")))


def run_failure(steps: Iterable[Mapping[str, Any]], aborted_by: int | None) -> Failure:
    """Klasse des Runs (ADR 0035 Nr. 2): die des abbrechenden Steps (Index ``aborted_by``), sonst die des
    ersten gescheiterten Steps. ``retry_after`` = spätester Wert *aller* gescheiterten Steps (auch nicht
    maßgeblicher; ADR 0035: Widersprüche löst das Maximum), nur bei ``transient``."""
    failed_steps = [(i, s) for i, s in enumerate(steps) if s.get("status") in FAILED_STEP_STATUSES]
    if not failed_steps:
        return Failure(_F.UNKNOWN)
    decisive = next((s for i, s in failed_steps if i == aborted_by), failed_steps[0][1])
    kind = parse_kind(decisive.get("failure_kind"))
    times = [t for _, s in failed_steps if (t := parse_retry_after(s.get("retry_after"))) is not None]
    return Failure(kind, max(times) if times else None)
