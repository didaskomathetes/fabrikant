"""Relay domain model (ADR 0037): brief (issue), handoff (branch file), decision per task.

Pure logic without I/O — GitHub, git and run API live in `github`/`workspace`/`commands`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from functools import cached_property

import yaml

from ..i18n import t
from ..retry import RetryPolicy
from ..runmodel import FailureKind

DEFAULT_MAX_SESSIONS = 30
DEFAULT_SESSION_MINUTES = 30
# Sessions without measurable progress in a row → blocked (otherwise a task burns sessions).
STALL_LIMIT = 2

# Required sections of the brief (`## Goal` …); without them no session starts.
REQUIRED_SECTIONS = ("goal", "session", "verify")
SETTINGS_SECTION = "settings"
AUDIT_ACCESS_SECTION = "audit access"
AUDIT_FACTS_SECTION = "audit facts"
LOG_SECTION = "log"
# Written by the agent on `done` or set by audit-publish; the status comment shows both.
RESULT_SECTION = "result"
AUDIT_SECTION = "audit"

_HEADING_RE = re.compile(r"^##[ \t]+(\S.*)$", re.M)
_VERIFY_CMD_RE = re.compile(r"^```verify[ \t]*\n(.*?)^```", re.M | re.S)
_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


class Status(StrEnum):
    """Task status in the handoff (set by the agent, checked by the harness)."""

    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"


class Action(StrEnum):
    """What the dispatcher does with a task."""

    SESSION = "session"
    AUDIT = "audit"
    BLOCK = "block"
    SKIP = "skip"


class HandoffError(ValueError):
    """The handoff file lacks structure (front matter, required fields, types)."""


@dataclass(frozen=True)
class Labels:
    """GitHub labels of a relay task; only the opt-in label is configurable."""

    task: str = "fabrikant-relay"
    blocked: str = "relay: blocked"
    done: str = "relay: done"
    audited: str = "relay: audited"


def normalize_login(login: str | None) -> str:
    """Make a GitHub login comparable (`app/x`, `x[bot]`, upper/lower case → `x`)."""
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
            raise HandoffError(t("relay.model.not_an_iso_time", value=value)) from e
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def format_time(dt: datetime | None) -> str:
    """aware datetime → `2026-10-21T14:02:00Z` (empty for None)."""
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
    """`## Settings` of the brief (`key: value` lines); unknown keys are ignored."""

    max_sessions: int = DEFAULT_MAX_SESSIONS
    session_minutes: int = DEFAULT_SESSION_MINUTES
    # Additional paths (globs) sessions may commit; the relay directory always.
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
    """The task brief = issue title + body (Markdown with `##` sections)."""

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
        """Hash of the `## Verify` version (whitespace-normalized) — pins the invariants."""
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


# Fields only the harness writes; agent changes to them are reset by the check-in.
HARNESS_FIELDS = ("issue", "session", "verify_sha256", "verify_result", "stalled", "last_session")
_TIME_FIELDS = ("not_before", "last_session")


@dataclass
class Handoff:
    """`.fabrikant/relay/<N>/HANDOFF.md`: YAML front matter (state) + Markdown (memory)."""

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
            raise HandoffError(t("relay.model.front_matter_missing"))
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            raise HandoffError(t("relay.model.front_matter_is_not_yaml", e=e)) from e
        if not isinstance(meta, dict):
            raise HandoffError(t("relay.model.front_matter_must_be_a"))
        known = {f.name for f in fields(cls)} - {"body"}
        values = {k: v for k, v in meta.items() if k in known}
        try:
            values["issue"] = int(values["issue"])
            values["session"] = int(values.get("session") or 0)
            values["stalled"] = int(values.get("stalled") or 0)
            values["status"] = Status(str(values.get("status") or Status.ACTIVE))
        except KeyError as e:
            raise HandoffError(t("relay.model.required_field_missing", e=e)) from e
        except (TypeError, ValueError) as e:
            raise HandoffError(t("relay.model.invalid_value", e=e)) from e
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
        """Adopt harness fields from `other`; returns the names that differed."""
        changed = [name for name in HARNESS_FIELDS if getattr(self, name) != getattr(other, name)]
        for name in changed:
            setattr(self, name, getattr(other, name))
        return changed

    def section(self, name: str) -> str:
        return markdown_sections(self.body).get(name.lower(), "")

    def set_section(self, name: str, text: str) -> None:
        """Replace `## <name>` or insert before `## Log` (log stays the last section); empty = remove."""
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
        """Append a line to `## Log` (section is created if needed)."""
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
    """GitHub view of a relay issue (who touched label/brief last?)."""

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
        """None if the brief may be executed.

        The brief is an instruction to an agent with a shell on the host: the opt-in label
        must come from an `allowed` login AND the current body version must be from an
        `allowed` login — or the label was set again after the last edit
        (re-labeling = approval of the current version). Without the label (removed = halted) never,
        not even for a manually started run."""
        if label not in self.labels:
            return t("relay.model.label_missing_task_halted", label=label)
        allowed_norm = {normalize_login(a) for a in allowed}
        if normalize_login(self.label_actor) not in allowed_norm:
            return t("relay.model.label_set_by_not_in", label=label, v2=self.label_actor or "?")
        editor = self.body_editor or self.author
        if normalize_login(editor) in allowed_norm:
            return None
        edited_at = self.body_edited_at or self.created_at
        if self.label_at and edited_at and self.label_at >= edited_at:
            return None
        return t("relay.model.brief_last_edited_by_re", editor=editor, label=label)


@dataclass(frozen=True)
class RunOutcome:
    """A finished relay run of a task from the run history."""

    run_id: str  # starts with the start time → lexicographically sortable
    published: bool  # publish/audit-publish went through (progress is on the branch)
    failure_kind: FailureKind | None = None  # ADR 0035; None = `unknown` (legacy / unclassified)
    finished: datetime | None = None
    retry_after: datetime | None = None  # ADR 0035: earliest sensible attempt (only for `transient`)


def failure_streak(recent: list[RunOutcome], acknowledged: str = "") -> list[RunOutcome]:
    """Unpublished runs in a row (newest first), without the already acknowledged ones.

    `acknowledged` = newest run id for which blocking already happened; removing the label thus starts afresh."""
    streak = []
    for outcome in sorted(recent, key=lambda o: o.run_id, reverse=True):
        if outcome.published or (acknowledged and outcome.run_id <= acknowledged):
            break
        streak.append(outcome)
    return streak


def _retry_decision(streak: list[RunOutcome], policy: RetryPolicy | None, now: datetime) -> Decision | None:
    """Failure brake: None = continue normally; otherwise BLOCK (no/exhausted retry) or SKIP (backoff).

    `max` counts additional attempts: n failed runs in a row = n - 1 retries used."""
    if not streak:
        return None
    last = streak[0]
    kind = last.failure_kind or FailureKind.UNKNOWN
    ack = last.run_id
    if policy is None or not policy.allows(kind) or last.finished is None:
        # Without an end time no backoff can be computed → treat as not retryable (fail closed).
        return Decision(
            Action.BLOCK,
            t("relay.model.run_failed_no_automatic_retry", v1=last.run_id, kind=kind),
            failures_seen=ack,
        )
    attempt = len(streak)  # next attempt (1-based)
    if attempt > policy.max:
        return Decision(
            Action.BLOCK,
            t("relay.model.retries_exhausted_retries_last_removing", v1=policy.max, v2=last.run_id, kind=kind),
            failures_seen=ack,
        )
    due = policy.next_attempt_at(attempt, last.finished, last.retry_after)
    if due > now:
        return Decision(
            Action.SKIP,
            t("relay.model.retry_after_not_before", attempt=attempt, v2=policy.max, kind=kind, v4=format_time(due)),
        )
    return None


@dataclass(frozen=True)
class TrailRef:
    """Reference to an archived trail copy (ADR 0037, addendum 2026-11-03).

    `sha256` is the canonical content hash of the trail (`archive.trail_digest`); the audit verdict binds to it, not to
    the storage location. `index` is the sequential archive number (`audit-<k>`)."""

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
        """Tolerant: incomplete/broken → None (a broken state block must not crash)."""
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

    verify_pending: str | None = None  # new verify version for which blocking already happened
    audit: str = ""  # "", "pass", "fail"
    last_commit: str = ""
    note: str = ""  # Reason why the dispatcher does not start (e.g. authorization)
    failures_seen: str = ""  # newest failed run id for which blocking already happened
    trail_sha256: str = ""  # Hash of the trail to which the last audit verdict is bound
    archive: TrailRef | None = None  # latest trail archive; set → the trail is removed from the branch
    trail_warning: str = ""  # PR still contains the trail (no `trail_archive` configured)

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
    """Result of `evaluate` for a task."""

    action: Action
    reason: str = ""
    repin: bool = False  # adopt new verify version (a human unblocked)
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
    """Decides deterministically whether a session/audit runs for `task` now.

    `recent`: finished relay runs of the task (run history); `retry`: policy for failed runs."""
    if err := task.authorization_error(allowed, labels.task):
        return Decision(Action.SKIP, err, notify=True)
    brief = task.brief
    if missing := brief.missing_sections():
        return Decision(
            Action.BLOCK,
            t("relay.model.brief_incomplete")
            + ", ".join(f"`## {s.title()}`" for s in missing)
            + t("relay.model.missing"),
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
                t("relay.model.verify_was_changed_removing_the"),
                verify_pending=brief.verify_sha256,
            )
        repin = True
    max_sessions = brief.settings.max_sessions
    if handoff.session >= max_sessions:
        return Decision(Action.BLOCK, t("relay.model.max_sessions_reached_raise_it", max_sessions=max_sessions))
    if handoff.status is Status.ACTIVE and handoff.not_before and handoff.not_before > now:
        return Decision(Action.SKIP, t("relay.model.not_before", v1=format_time(handoff.not_before)))
    return Decision(Action.SESSION, t("relay.model.due"), repin=repin)
