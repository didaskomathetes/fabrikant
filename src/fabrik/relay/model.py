"""Relay-Domänenmodell (ADR 0037): Brief (Issue), Handoff (Branch-Datei), Entscheidung pro Task.

Reine Logik ohne I/O — GitHub, Git und Run-API liegen in `github`/`workspace`/`commands`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from functools import cached_property

import yaml

from ..retry import RetryPolicy
from ..runmodel import FailureKind

DEFAULT_MAX_SESSIONS = 30
DEFAULT_SESSION_MINUTES = 30
# Sessions ohne messbaren Fortschritt in Folge → blocked (sonst verbrennt ein Task Sessions).
STALL_LIMIT = 2

# Pflicht-Abschnitte des Briefs (`## Goal` …); ohne sie startet keine Session.
REQUIRED_SECTIONS = ("goal", "session", "verify")
SETTINGS_SECTION = "settings"
AUDIT_ACCESS_SECTION = "audit access"
AUDIT_FACTS_SECTION = "audit facts"
LOG_SECTION = "log"
# Vom Agent bei `done` geschrieben bzw. von audit-publish gesetzt; beide zeigt der Status-Kommentar.
RESULT_SECTION = "result"
AUDIT_SECTION = "audit"

_HEADING_RE = re.compile(r"^##[ \t]+(\S.*)$", re.M)
_VERIFY_CMD_RE = re.compile(r"^```verify[ \t]*\n(.*?)^```", re.M | re.S)
_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


class Status(StrEnum):
    """Task-Status im Handoff (vom Agent gesetzt, von der Harness geprüft)."""

    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"


class Action(StrEnum):
    """Was der Dispatcher mit einem Task tut."""

    SESSION = "session"
    AUDIT = "audit"
    BLOCK = "block"
    SKIP = "skip"


class HandoffError(ValueError):
    """Handoff-Datei fehlt die Struktur (Front Matter, Pflichtfelder, Typen)."""


@dataclass(frozen=True)
class Labels:
    """GitHub-Labels eines Relay-Tasks; nur das Opt-in-Label ist konfigurierbar."""

    task: str = "fabrik-relay"
    blocked: str = "relay: blocked"
    done: str = "relay: done"
    audited: str = "relay: audited"


def normalize_login(login: str | None) -> str:
    """GitHub-Login vergleichbar machen (`app/x`, `x[bot]`, Groß/Klein → `x`)."""
    name = (login or "").strip().lower()
    name = name.removeprefix("app/")
    return name.removesuffix("[bot]")


def parse_time(value: object) -> datetime | None:
    """ISO-Zeit (str, YAML-datetime, `Z`-Suffix) → aware UTC-datetime; leer → None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as e:
            raise HandoffError(f"keine ISO-Zeit: {value!r}") from e
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def format_time(dt: datetime | None) -> str:
    """aware datetime → `2026-10-21T14:02:00Z` (leer für None)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def markdown_sections(text: str) -> dict[str, str]:
    """`## Heading`-Abschnitte → {heading (lower): Inhalt}; erster Abschnitt gewinnt."""
    text = text.replace("\r\n", "\n")
    matches = list(_HEADING_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.setdefault(m.group(1).strip().lower(), text[m.end() : end].strip())
    return sections


def _positive_int(raw: str | None, default: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    """`## Settings` des Briefs (`key: value`-Zeilen); unbekannte Keys werden ignoriert."""

    max_sessions: int = DEFAULT_MAX_SESSIONS
    session_minutes: int = DEFAULT_SESSION_MINUTES
    # Zusätzliche Pfade (Globs), die Sessions committen dürfen; das Relay-Verzeichnis immer.
    scope: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Settings:
        values: dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.strip().lstrip("-* ").partition(":")
            if sep:
                values[key.strip().lower()] = value.strip()
        scope = tuple(s.strip().strip("`") for s in values.get("scope", "").split(",") if s.strip().strip("`"))
        return cls(
            max_sessions=_positive_int(values.get("max_sessions"), DEFAULT_MAX_SESSIONS),
            session_minutes=_positive_int(values.get("session_minutes"), DEFAULT_SESSION_MINUTES),
            scope=scope,
        )


@dataclass(frozen=True)
class Brief:
    """Der Task-Auftrag = Issue-Titel + -Body (Markdown mit `##`-Abschnitten)."""

    title: str
    body: str

    @cached_property
    def sections(self) -> dict[str, str]:
        return markdown_sections(self.body)

    def missing_sections(self) -> list[str]:
        return [s for s in REQUIRED_SECTIONS if not self.sections.get(s)]

    @property
    def verify(self) -> str:
        return self.sections.get("verify", "")

    @property
    def verify_sha256(self) -> str:
        """Hash der `## Verify`-Fassung (Whitespace-normalisiert) — pinnt die Invarianten."""
        normalized = "\n".join(line.rstrip() for line in self.verify.strip().splitlines())
        return hashlib.sha256(normalized.encode()).hexdigest()

    @property
    def verify_command(self) -> str | None:
        """Optionaler ```verify-Block: deterministische Fortschrittsmessung (letzte Zeile)."""
        m = _VERIFY_CMD_RE.search(self.verify)
        return m.group(1).strip() if m and m.group(1).strip() else None

    @property
    def settings(self) -> Settings:
        return Settings.parse(self.sections.get(SETTINGS_SECTION, ""))


# Felder, die nur die Harness schreibt; Agent-Änderungen daran setzt der Check-in zurück.
HARNESS_FIELDS = ("issue", "session", "verify_sha256", "verify_result", "stalled", "last_session")
_TIME_FIELDS = ("not_before", "last_session")


@dataclass
class Handoff:
    """`.fabrik/relay/<N>/HANDOFF.md`: YAML-Front-Matter (Zustand) + Markdown (Gedächtnis)."""

    issue: int
    status: Status = Status.ACTIVE
    session: int = 0
    not_before: datetime | None = None
    progress: str = ""
    blocked_reason: str = ""
    verify_sha256: str = ""
    verify_result: str = ""
    stalled: int = 0
    last_session: datetime | None = None
    body: str = field(default="", repr=False)

    @classmethod
    def new(cls, issue: int, title: str, verify_sha256: str) -> Handoff:
        body = (
            f"# Handoff — #{issue} {title}\n\n"
            "## State\n_Nothing done yet._\n\n"
            "## Next\n_First session: read the brief, plan, start._\n\n"
            "## Audit facts\n_Targets, source snapshot/watermark, backup location — for the auditor._\n\n"
            "## Log\n"
        )
        return cls(issue=issue, verify_sha256=verify_sha256, body=body)

    @classmethod
    def parse(cls, text: str) -> Handoff:
        m = _FRONT_MATTER_RE.match(text.replace("\r\n", "\n"))
        if not m:
            raise HandoffError("Front Matter (--- … ---) fehlt")
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            raise HandoffError(f"Front Matter ist kein YAML: {e}") from e
        if not isinstance(meta, dict):
            raise HandoffError("Front Matter muss ein Mapping sein")
        known = {f.name for f in fields(cls)} - {"body"}
        values = {k: v for k, v in meta.items() if k in known}
        try:
            values["issue"] = int(values["issue"])
            values["session"] = int(values.get("session") or 0)
            values["stalled"] = int(values.get("stalled") or 0)
            values["status"] = Status(str(values.get("status") or Status.ACTIVE))
        except KeyError as e:
            raise HandoffError(f"Pflichtfeld fehlt: {e}") from e
        except (TypeError, ValueError) as e:
            raise HandoffError(f"ungültiger Wert: {e}") from e
        for name in _TIME_FIELDS:
            values[name] = parse_time(values.get(name))
        for name in ("progress", "blocked_reason", "verify_sha256", "verify_result"):
            values[name] = str(values.get(name) or "")
        return cls(**values, body=m.group(2))

    def render(self) -> str:
        meta = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "body"}
        meta["status"] = str(self.status)
        for name in _TIME_FIELDS:
            meta[name] = format_time(meta[name])
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
        return f"---\n{front}---\n{self.body}"

    def adopt_harness_fields(self, other: Handoff) -> list[str]:
        """Harness-Felder von `other` übernehmen; liefert die Namen, die abwichen."""
        changed = [name for name in HARNESS_FIELDS if getattr(self, name) != getattr(other, name)]
        for name in changed:
            setattr(self, name, getattr(other, name))
        return changed

    def section(self, name: str) -> str:
        return markdown_sections(self.body).get(name.lower(), "")

    def set_section(self, name: str, text: str) -> None:
        """`## <name>` ersetzen bzw. vor `## Log` einfügen (Log bleibt der letzte Abschnitt); leer = entfernen."""
        matches = list(_HEADING_RE.finditer(self.body))
        block = f"## {name}\n{text.strip()}\n" if text.strip() else ""
        idx = next((i for i, m in enumerate(matches) if m.group(1).strip().lower() == name.lower()), None)
        if idx is not None:
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(self.body)
            self.body = (
                self.body[: matches[idx].start()] + block + ("\n" + self.body[end:] if end < len(self.body) else "")
            )
            return
        log = next((m for m in matches if m.group(1).strip().lower() == LOG_SECTION), None)
        if not block:
            return
        if log is None:
            self.body = self.body.rstrip("\n") + f"\n\n{block}"
        else:
            self.body = self.body[: log.start()] + block + "\n" + self.body[log.start() :]

    def block(self, reason: str) -> None:
        self.status = Status.BLOCKED
        self.blocked_reason = reason

    def append_log(self, line: str) -> None:
        """Eine Zeile an `## Log` anhängen (Abschnitt wird bei Bedarf angelegt)."""
        entry = f"- {line}\n"
        matches = list(_HEADING_RE.finditer(self.body))
        log = next((i for i, m in enumerate(matches) if m.group(1).strip().lower() == LOG_SECTION), None)
        if log is None:
            self.body = self.body.rstrip("\n") + f"\n\n## Log\n{entry}"
            return
        end = matches[log + 1].start() if log + 1 < len(matches) else len(self.body)
        head, tail = self.body[:end].rstrip("\n"), self.body[end:]
        self.body = f"{head}\n{entry}" + (f"\n{tail}" if tail else "")


@dataclass(frozen=True)
class Task:
    """GitHub-Sicht auf ein Relay-Issue (wer hat Label/Brief zuletzt angefasst?)."""

    number: int
    title: str
    body: str
    labels: frozenset[str]
    author: str
    created_at: datetime | None = None
    label_actor: str | None = None
    label_at: datetime | None = None
    body_editor: str | None = None
    body_edited_at: datetime | None = None

    @property
    def brief(self) -> Brief:
        return Brief(self.title, self.body)

    def authorization_error(self, allowed: frozenset[str], label: str) -> str | None:
        """None, wenn der Brief ausgeführt werden darf.

        Der Brief ist eine Anweisung an einen Agent mit Shell auf dem Host: das Opt-in-Label
        muss von einem `allowed`-Login stammen UND die aktuelle Body-Fassung muss von einem
        `allowed`-Login sein — oder das Label wurde nach der letzten Bearbeitung neu gesetzt
        (Neu-Labeln = Freigabe der aktuellen Fassung). Ohne das Label (entfernt = angehalten) nie,
        auch nicht für einen von Hand gestarteten Run."""
        if label not in self.labels:
            return f"Label `{label}` fehlt (Task angehalten)"
        allowed_norm = {normalize_login(a) for a in allowed}
        if normalize_login(self.label_actor) not in allowed_norm:
            return f"Label `{label}` von {self.label_actor or '?'} gesetzt (nicht in relay.allowed)"
        editor = self.body_editor or self.author
        if normalize_login(editor) in allowed_norm:
            return None
        edited_at = self.body_edited_at or self.created_at
        if self.label_at and edited_at and self.label_at >= edited_at:
            return None
        return f"Brief zuletzt von {editor} bearbeitet — Label `{label}` neu setzen gibt die Fassung frei"


@dataclass(frozen=True)
class RunOutcome:
    """Ein beendeter Relay-Run eines Tasks aus der Run-Historie."""

    run_id: str  # beginnt mit dem Startzeitpunkt → lexikografisch sortierbar
    published: bool  # publish/audit-publish lief durch (Fortschritt ist auf dem Branch)
    failure_kind: FailureKind | None = None  # ADR 0035; None = `unknown` (Altbestand / nicht klassifiziert)
    finished: datetime | None = None
    retry_after: datetime | None = None  # ADR 0035: frühester sinnvoller Versuch (nur bei `transient`)


def failure_streak(recent: list[RunOutcome], acknowledged: str = "") -> list[RunOutcome]:
    """Unveröffentlichte Runs am Stück (neueste zuerst), ohne die bereits quittierten.

    `acknowledged` = neueste Run-Id, für die schon geblockt wurde; Label entfernen setzt so neu auf."""
    streak = []
    for outcome in sorted(recent, key=lambda o: o.run_id, reverse=True):
        if outcome.published or (acknowledged and outcome.run_id <= acknowledged):
            break
        streak.append(outcome)
    return streak


def _retry_decision(streak: list[RunOutcome], policy: RetryPolicy | None, now: datetime) -> Decision | None:
    """Fehlerbremse: None = normal weiter; sonst BLOCK (kein/erschöpfter Retry) oder SKIP (Backoff).

    `max` zählt zusätzliche Versuche: n gescheiterte Runs am Stück = n - 1 verbrauchte Retries."""
    if not streak:
        return None
    last = streak[0]
    kind = last.failure_kind or FailureKind.UNKNOWN
    ack = last.run_id
    if policy is None or not policy.allows(kind) or last.finished is None:
        # Ohne Endzeit lässt sich kein Backoff berechnen → wie nicht wiederholbar behandeln (fail closed).
        return Decision(
            Action.BLOCK,
            f"Run {last.run_id} scheiterte (`{kind}`), keine automatische Wiederholung "
            "— Label entfernen versucht es erneut",
            failures_seen=ack,
        )
    attempt = len(streak)  # nächster Versuch (1-basiert)
    if attempt > policy.max:
        return Decision(
            Action.BLOCK,
            f"Wiederholungen erschöpft ({policy.max} Retries, zuletzt {last.run_id}, "
            f"`{kind}`) — Label entfernen versucht es erneut",
            failures_seen=ack,
        )
    due = policy.next_attempt_at(attempt, last.finished, last.retry_after)
    if due > now:
        return Decision(Action.SKIP, f"Retry {attempt}/{policy.max} nach `{kind}` nicht vor {format_time(due)}")
    return None


@dataclass(frozen=True)
class TrailRef:
    """Verweis auf ein archiviertes Trail-Exemplar (ADR 0037, Nachtrag 2026-11-03).

    `sha256` ist der kanonische Inhalts-Hash des Trails (`archive.trail_digest`); an ihn, nicht an den
    Ablageort, bindet das Audit-Verdikt. `index` ist die fortlaufende Archiv-Nummer (`audit-<k>`)."""

    method: str
    issue: int
    index: int
    uri: str
    sha256: str
    commit: str
    created: str

    @property
    def name(self) -> str:
        return f"audit-{self.index}"

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: object) -> TrailRef | None:
        """Tolerant: unvollständig/kaputt → None (ein kaputter Zustandsblock darf nicht crashen)."""
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                method=str(data["method"]),
                issue=int(data["issue"]),
                index=int(data["index"]),
                uri=str(data["uri"]),
                sha256=str(data["sha256"]),
                commit=str(data["commit"]),
                created=str(data.get("created") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class StatusState:
    """Maschinenlesbarer Teil des Status-Kommentars (Dispatcher ↔ Sessions)."""

    verify_pending: str | None = None  # neue Verify-Fassung, für die schon geblockt wurde
    audit: str = ""  # "", "pass", "fail"
    last_commit: str = ""
    note: str = ""  # Grund, warum der Dispatcher nicht startet (z. B. Autorisierung)
    failures_seen: str = ""  # neueste gescheiterte Run-Id, für die schon geblockt wurde
    trail_sha256: str = ""  # Hash des Trails, an den das letzte Audit-Verdikt gebunden ist
    archive: TrailRef | None = None  # letztes Trail-Archiv; gesetzt → Trail ist vom Branch entfernt
    trail_warning: str = ""  # PR enthält den Trail noch (kein `trail_archive` konfiguriert)

    def to_dict(self) -> dict:
        return {
            "verify_pending": self.verify_pending,
            "audit": self.audit,
            "last_commit": self.last_commit,
            "note": self.note,
            "failures_seen": self.failures_seen,
            "trail_sha256": self.trail_sha256,
            "archive": self.archive.to_dict() if self.archive else None,
            "trail_warning": self.trail_warning,
        }

    @classmethod
    def from_dict(cls, data: object) -> StatusState:
        if not isinstance(data, dict):
            return cls()
        return cls(
            verify_pending=data.get("verify_pending") or None,
            audit=str(data.get("audit") or ""),
            last_commit=str(data.get("last_commit") or ""),
            note=str(data.get("note") or ""),
            failures_seen=str(data.get("failures_seen") or ""),
            trail_sha256=str(data.get("trail_sha256") or ""),
            archive=TrailRef.from_dict(data.get("archive")),
            trail_warning=str(data.get("trail_warning") or ""),
        )


@dataclass(frozen=True)
class Decision:
    """Ergebnis von `evaluate` für einen Task."""

    action: Action
    reason: str = ""
    repin: bool = False  # neue Verify-Fassung übernehmen (Mensch hat entblockt)
    notify: bool = False  # SKIP-Grund im Status-Kommentar zeigen
    verify_pending: str | None = None
    failures_seen: str = ""


def evaluate(
    task: Task,
    handoff: Handoff | None,
    state: StatusState,
    *,
    allowed: frozenset[str],
    labels: Labels,
    now: datetime,
    audit: bool = True,
    recent: list[RunOutcome] | None = None,
    retry: RetryPolicy | None = None,
) -> Decision:
    """Entscheidet deterministisch, ob für `task` jetzt eine Session/ein Audit läuft.

    `recent`: beendete Relay-Runs des Tasks (Run-Historie); `retry`: Policy für gescheiterte Runs."""
    if err := task.authorization_error(allowed, labels.task):
        return Decision(Action.SKIP, err, notify=True)
    brief = task.brief
    if missing := brief.missing_sections():
        return Decision(
            Action.BLOCK, "Brief unvollständig: " + ", ".join(f"`## {s.title()}`" for s in missing) + " fehlt"
        )
    if labels.blocked in task.labels:
        return Decision(Action.SKIP, "blocked (Label)")
    if held := _retry_decision(failure_streak(recent or [], state.failures_seen), retry, now):
        return held
    if labels.done in task.labels:
        if audit and labels.audited not in task.labels:
            return Decision(Action.AUDIT, "fertig — Audit ausstehend")
        return Decision(Action.SKIP, "fertig")
    if handoff is None:
        return Decision(Action.SESSION, "erste Session")
    repin = False
    if handoff.verify_sha256 != brief.verify_sha256:
        if state.verify_pending != brief.verify_sha256:
            return Decision(
                Action.BLOCK,
                "`## Verify` wurde geändert — Label entfernen übernimmt die neue Fassung",
                verify_pending=brief.verify_sha256,
            )
        repin = True
    max_sessions = brief.settings.max_sessions
    if handoff.session >= max_sessions:
        return Decision(
            Action.BLOCK, f"max_sessions ({max_sessions}) erreicht — in `## Settings` erhöhen, dann Label entfernen"
        )
    if handoff.status is Status.ACTIVE and handoff.not_before and handoff.not_before > now:
        return Decision(Action.SKIP, f"nicht vor {format_time(handoff.not_before)}")
    return Decision(Action.SESSION, "fällig", repin=repin)
