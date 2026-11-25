"""Relay subcommands = the shell steps of the generated loops (ADR 0037).

Every command returns a summary; the last line lands as step summary in the
searchable run history (`relay #<N> s<k> …`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .. import httpsafe, runs
from ..failures import StepFailure, classify_exception, parse_retry_after
from ..i18n import t
from ..runmodel import FailureKind, Run, RunStatus, StepStatus
from ..secrets import _redact
from . import archive, status
from .archive import ArchiveError, TrailArchive
from .config import LOOP_AUDIT, LOOP_SESSION, RUN_LOOPS, RelayConfig, fabrikant_url
from .github import GitHubError
from .model import (
    AUDIT_SECTION,
    STALL_LIMIT,
    Action,
    Brief,
    Decision,
    Handoff,
    HandoffError,
    RunOutcome,
    Status,
    StatusState,
    Task,
    TrailRef,
    evaluate,
    format_time,
    markdown_sections,
)
from .workspace import (
    AUDIT_REPORT,
    SESSION_DIR,
    CheckinResult,
    SessionPlan,
    Workspace,
    branch_name,
    handoff_path,
    relay_dir,
)

log = logging.getLogger("fabrikant.relay")

# The session stops starting new work units this many minutes before the hard end.
WRAP_UP_MINUTES = 10
# Run statuses in which a relay run blocks the dispatcher (one per project).
BUSY_STATUSES = frozenset({RunStatus.RUNNING, RunStatus.NEEDS_APPROVAL, RunStatus.QUEUED})
PUBLISH_STEPS = ("publish", "audit-publish")
SCOPE_REASON_PATHS = 5  # this many discarded paths are named in the block reason, the rest as a count
RESULT_MAX_CHARS = 200
_VERDICT_RE = re.compile(r"<!--\s*relay-audit\s+verdict=(pass|fail)\s*-->")


class StepSummary(str):
    """Step summary (the text) plus domain verdict, which `__main__` reports as a verdict line.

    A `str`, so callers and tests keep treating the summary like text."""

    verdict: str

    def __new__(cls, text: str, verdict: str = "") -> StepSummary:
        obj = super().__new__(cls, text)
        obj.verdict = verdict
        return obj


class RelayError(StepFailure, RuntimeError):
    """A relay step must not continue (reason in the message); class per ADR 0035."""


class GitHubPort(Protocol):
    """What the commands need from GitHub (fake in tests)."""

    def tasks(self, label: str) -> list[Task]: ...
    def task(self, number: int, label: str) -> Task: ...
    def file(self, path: str, ref: str) -> str | None: ...
    def status_state(self, number: int) -> StatusState: ...
    def upsert_status(self, number: int, body: str) -> None: ...
    def sync_labels(self, number: int, current: frozenset[str], add: set[str], remove: set[str]) -> None: ...
    def token(self) -> str: ...
    def commit_identity(self) -> tuple[str, str]: ...
    def create_ref(self, ref: str, sha: str) -> None: ...
    def ref_sha(self, ref: str) -> str | None: ...
    def open_pr(self, branch: str) -> int | None: ...
    def pr_body(self, number: int) -> str: ...
    def set_pr_body(self, number: int, body: str) -> None: ...


Starter = Callable[[str, str, int, str], str]  # (project, loop, issue, note) → run_id


@dataclass
class RelayContext:
    cfg: RelayConfig
    github: GitHubPort
    now: datetime
    workdir: Path = field(default_factory=Path.cwd)

    def workspace(self, issue: int) -> Workspace:
        return Workspace(self.workdir, issue)

    def decide(
        self, task: Task, handoff: Handoff | None, state: StatusState, recent: list[RunOutcome] | None = None
    ) -> Decision:
        cfg = self.cfg
        return evaluate(
            task,
            handoff,
            state,
            allowed=cfg.allowed,
            labels=cfg.labels,
            now=self.now,
            audit=cfg.audit,
            recent=recent,
            retry=cfg.retry,
        )

    def update_status(self, issue: int, handoff: Handoff | None, state: StatusState, max_sessions: int) -> None:
        body = status.render(self.cfg.repo, branch_name(issue), handoff_path(issue), handoff, state, max_sessions)
        self.github.upsert_status(issue, body)

    def push(self, ws: Workspace) -> None:
        ws.push(self.cfg.repo, self.github.token())

    def commit(self, ws: Workspace, paths: list[str], message: str) -> str:
        """Commit as the bot (relay.bot_login) — author and committer."""
        return ws.commit(paths, message, self.github.commit_identity())

    def archive(self, method: str, ws: Workspace | None = None) -> TrailArchive:
        """Archive method `method` (from the config or from an existing `TrailRef`).

        Without `ws` (dispatcher, no worktree) `git-ref` can only read, not restore."""
        if method == archive.METHOD_LOCAL:
            return archive.LocalArchive(self.cfg.project)
        if method == archive.METHOD_GIT_REF:
            root = ws.root if ws else self.workdir
            fetch = (lambda spec: ws.fetch(self.cfg.repo, self.github.token(), spec)) if ws else None
            return archive.GitRefArchive(self.github, self.cfg.repo, root, fetch)
        raise RelayError(f"unbekannte Archiv-Methode {method!r}", FailureKind.ENVIRONMENT)

    def trail_warning(self, issue: int) -> str:
        """Warning for the status comment: the relay branch's PR contains the trail (no archive)."""
        if self.cfg.trail_archive is not None:
            return ""
        pr = self.github.open_pr(branch_name(issue))
        if pr is None:
            return ""
        return f"PR #{pr} still contains the relay trail (`{relay_dir(issue)}/`): no `relay.trail_archive` configured"


# ── Dispatch ─────────────────────────────────────────────────────────────────


def start_run(project: str, loop: str, issue: int, note: str) -> str:
    """Start/queue a run via the running fabrikant API (the dispatcher step ends immediately)."""
    payload = {"project": project, "loop": loop, "note": note, "vars": {"issue_number": str(issue)}}
    headers = {"Content-Type": "application/json", "User-Agent": "fabrikant-relay"}
    if token := os.environ.get("FABRIKANT_TOKEN", "").strip():
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{fabrikant_url()}/api/runs", data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with httpsafe.urlopen(req, timeout=30) as r:
            return str(json.loads(r.read().decode()).get("run_id", "?"))
    except urllib.error.URLError as e:  # HTTPError is a URLError
        failure = classify_exception(e)
        if isinstance(e, urllib.error.HTTPError):
            body = e.read().decode("utf-8", "replace").strip()[:200]
            raise RelayError(f"fabrikant-API /api/runs: HTTP {e.code} {body}", failure.kind, failure.retry_after) from e
        raise RelayError(
            t("relay.commands.fabrikant_api_api_runs_not", v1=fabrikant_url(), v2=e.reason), failure.kind
        ) from e


def relay_runs(project: str) -> list[Run]:
    """All relay runs of the project (complete history, one scan per dispatcher tick).

    No window of the newest N: a running relay run would drop out with many runs of other loops,
    and the dispatcher would start a second one."""
    return [r for r in runs.load_runs(project) if r.loop in RUN_LOOPS]


def busy_runs(history: list[Run]) -> list[str]:
    """Active/waiting relay runs (at most one at a time per project)."""
    return [f"{r.id} ({r.status})" for r in history if r.status in BUSY_STATUSES]


def _outcome(run: Run) -> RunOutcome:
    published = any(s.name in PUBLISH_STEPS and s.status is StepStatus.PASS for s in run.steps)
    # Parse tolerantly (invalid → None): a broken record must not stop the dispatcher.
    return RunOutcome(
        run.id, published, run.failure_kind, parse_retry_after(run.finished), parse_retry_after(run.retry_after)
    )


def recent_outcomes(history: list[Run]) -> dict[int, list[RunOutcome]]:
    """Finished relay runs per issue (for the failure brake)."""
    by_issue: dict[int, list[RunOutcome]] = {}
    for run in history:
        issue = str(run.vars.get("issue_number", ""))
        if run.is_terminal and issue.isdigit():
            by_issue.setdefault(int(issue), []).append(_outcome(run))
    return by_issue


def _remote_handoff(ctx: RelayContext, issue: int, state: StatusState, from_archive: bool = True) -> Handoff | None:
    """Handoff from the branch; if the trail is archived (and removed from the branch), from the archive.

    `from_archive=False` for finished tasks: their decision does not depend on the handoff, an
    archive read per tick would be wasted.

    `ArchiveError` if the state block names an archive that is not readable."""
    text = ctx.github.file(handoff_path(issue), branch_name(issue))
    if text is None and state.archive is not None and from_archive:
        text = ctx.archive(state.archive.method).read_handoff(state.archive)
        if text is None:
            raise ArchiveError(
                t("relay.commands.archive_contains_no_handoff", v1=state.archive.name, v2=state.archive.uri)
            )
    return Handoff.parse(text) if text is not None else None


def _apply_block(ctx: RelayContext, task: Task, handoff: Handoff | None, state: StatusState, d: Decision) -> None:
    labels = ctx.cfg.labels
    ctx.github.sync_labels(task.number, task.labels, {labels.blocked}, set())
    state.note = d.reason
    if d.verify_pending:
        state.verify_pending = d.verify_pending
    if d.failures_seen:
        state.failures_seen = d.failures_seen
    ctx.update_status(task.number, handoff, state, task.brief.settings.max_sessions)


def dispatch(ctx: RelayContext, start: Starter = start_run) -> str:
    """Evaluate all relay issues, apply blocks, start at most ONE due run."""
    history = relay_runs(ctx.cfg.project)
    if busy := busy_runs(history):
        return t("relay.commands.relay_already_running", v1=", ".join(busy))
    outcomes = recent_outcomes(history)
    lines: list[str] = []
    due: list[tuple[datetime, Task, Handoff | None, Decision]] = []
    for task in ctx.github.tasks(ctx.cfg.label):
        state = ctx.github.status_state(task.number)
        try:
            handoff = _remote_handoff(ctx, task.number, state, from_archive=ctx.cfg.labels.done not in task.labels)
        except HandoffError as e:
            handoff, d = None, Decision(Action.BLOCK, t("relay.commands.handoff_on_the_branch_invalid", e=e))
        except ArchiveError as e:
            handoff, d = None, Decision(Action.BLOCK, t("relay.commands.trail_archive_unreadable", e=e))
        except GitHubError as e:
            # Transient API error isolated per task: log, skip the task (try again
            # on the next tick) — the remaining tasks in the tick are processed further.
            log.warning(t("relay.commands.relay_s_github_error_while"), task.number, e)
            lines.append(t("relay.commands.skip_github_error_while_reading", v1=task.number))
            continue
        else:
            d = ctx.decide(task, handoff, state, outcomes.get(task.number))
        lines.append(f"#{task.number}: {d.action} — {d.reason}")
        if d.action is Action.BLOCK:
            _apply_block(ctx, task, handoff, state, d)
        elif d.action is Action.SKIP and d.notify and state.note != d.reason:
            state.note = d.reason
            ctx.update_status(task.number, handoff, state, task.brief.settings.max_sessions)
        elif d.action in (Action.SESSION, Action.AUDIT):
            waited = (
                handoff.last_session
                if handoff and handoff.last_session
                else datetime.min.replace(tzinfo=ctx.now.tzinfo)
            )
            due.append((waited, task, handoff, d))
    if not due:
        return "\n".join([*lines, t("relay.commands.relay_nothing_due")])
    _, task, handoff, d = min(due, key=lambda item: (item[0], item[1].number))
    if d.action is Action.SESSION:
        loop, note = LOOP_SESSION, f"relay #{task.number} s{(handoff.session if handoff else 0) + 1}"
    else:
        loop, note = LOOP_AUDIT, f"relay #{task.number} audit"
    run_id = start(ctx.cfg.project, loop, task.number, note)
    return "\n".join([*lines, t("relay.commands.started", note=note, loop=loop, run_id=run_id)])


# ── Session ──────────────────────────────────────────────────────────────────


def _session_md(cfg: RelayConfig, task: Task, plan: SessionPlan) -> str:
    rel = f".fabrikant/relay/{plan.issue}"
    scope = ", ".join(f"`{s}`" for s in plan.scope) or "nothing else"
    forbid = ", ".join(f"`{s}`" for s in plan.forbid) or "–"
    verify = f"```sh\n{plan.verify_command}\n```" if plan.verify_command else "_none — report progress in the handoff_"
    return f"""# Relay session {plan.session} of {plan.max_sessions} — #{plan.issue} {task.title}

- Repository: `{cfg.repo}` · branch `{branch_name(plan.issue)}` (fabrikant commits and pushes after the session)
- Started: {plan.started} · **no new work units after {plan.soft_deadline}** · **finished by {plan.hard_deadline}**
  (UTC — check with `date -u +%FT%TZ`)
- Brief (your instructions): `{SESSION_DIR}/BRIEF.md`
- Handoff (state + memory): `{handoff_path(plan.issue)}`
- Record every script / SQL you execute: `{rel}/scripts/`
- This session's log: `{rel}/sessions/s{plan.session}.md`
- Committable: `{rel}/`, {scope} · never: {forbid}
- Verify (run by fabrikant after the session):
{verify}
"""


def checkout(ctx: RelayContext, issue: int) -> str:
    """Prepare the session: re-check authorization, advance the handoff, session files."""
    task = ctx.github.task(issue, ctx.cfg.label)
    ws = ctx.workspace(issue)
    state = ctx.github.status_state(issue)
    restored = restore_trail(ctx, ws, state)
    ws.prepare()
    handoff = ws.read_handoff()
    d = ctx.decide(task, handoff, state)
    if d.action is not Action.SESSION:
        raise RelayError(t("relay.commands.no_session_for", issue=issue, v2=d.action, v3=d.reason))
    brief = task.brief
    notes = []
    if handoff is None:
        handoff = Handoff.new(issue, task.title, brief.verify_sha256)
    if d.repin:
        handoff.verify_sha256 = brief.verify_sha256
        notes.append(t("relay.commands.adopted_new_verify_version"))
    if handoff.status is not Status.ACTIVE:
        notes.append(f"fortgesetzt nach `{handoff.status}` (Label entfernt)")
        handoff.status, handoff.blocked_reason = Status.ACTIVE, ""
    handoff.session += 1
    handoff.last_session = ctx.now
    minutes = min(brief.settings.session_minutes, ctx.cfg.max_session_minutes)
    hard = ctx.now + timedelta(minutes=minutes)
    plan = SessionPlan(
        issue=issue,
        session=handoff.session,
        max_sessions=brief.settings.max_sessions,
        started=format_time(ctx.now),
        soft_deadline=format_time(hard - timedelta(minutes=min(WRAP_UP_MINUTES, minutes // 2))),
        hard_deadline=format_time(hard),
        verify_command=brief.verify_command,
        scope=brief.settings.scope,
        forbid=ctx.cfg.forbid,
        head=ws.head_sha(),
    )
    ws.write_handoff(handoff)
    ws.save_plan(plan, handoff)
    ws.write_text("BRIEF.md", f"# {task.title} (#{issue})\n\n{brief.body}\n")
    ws.write_text("SESSION.md", _session_md(ctx.cfg, task, plan))
    if restored:
        notes.insert(0, restored)
    detail = "; ".join(notes) or d.reason
    return f"relay #{issue} s{plan.session}/{plan.max_sessions}: Checkout ({detail}) · Ende {plan.hard_deadline}"


def run_verify(command: str, cwd: Path, timeout: int) -> str:
    """Run the verify command → last output line (progress evidence), redacted."""
    try:
        p = subprocess.run(["sh", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s"
    out = [line for line in (p.stdout or "").splitlines() if line.strip()]
    err = [line for line in (p.stderr or "").splitlines() if line.strip()]
    if p.returncode != 0:
        result = f"exit {p.returncode}: {(err or out or [''])[-1]}"
    else:
        result = out[-1] if out else "(no output)"
    return _redact(result.strip())[:RESULT_MAX_CHARS]


def _evidence(handoff: Handoff, has_verify: bool) -> str:
    return handoff.verify_result if has_verify else handoff.progress


def checkin(ctx: RelayContext, issue: int, verify: Callable[[str, Path, int], str] = run_verify) -> str:
    """After the agent session: check/repair handoff, verify, stall, scope, log line."""
    ws = ctx.workspace(issue)
    plan, before = ws.load_plan()
    notes: list[str] = ws.settle_session(plan.head, ctx.cfg.base)
    handoff, error = ws.read_handoff_or_none()
    if handoff is None:
        handoff = Handoff.parse(before.render())
        handoff.block(t("relay.commands.handoff_invalid_after_the_session", error=error))
        notes.append(t("relay.commands.handoff_invalid"))
        invalid = True
    else:
        invalid = False
    if tampered := handoff.adopt_harness_fields(before):
        notes.append(t("relay.commands.harness_fields_reset", v1=", ".join(tampered)))
    if handoff.section(AUDIT_SECTION) != before.section(AUDIT_SECTION):
        # Only audit-publish writes `## Audit`; otherwise an unchecked verdict would stand in the status comment.
        handoff.set_section(AUDIT_SECTION.title(), before.section(AUDIT_SECTION))
        notes.append(t("relay.commands.audit_reset"))
    # Invalid handoff → blocked immediately; verify (expensive, with side effects) is skipped.
    if plan.verify_command and not invalid:
        handoff.verify_result = verify(plan.verify_command, ws.root, ctx.cfg.verify_timeout)
    if handoff.status is Status.ACTIVE:
        unchanged = _evidence(handoff, bool(plan.verify_command)) == _evidence(before, bool(plan.verify_command))
        handoff.stalled = before.stalled + 1 if unchanged else 0
        if handoff.stalled >= STALL_LIMIT:
            handoff.block(t("relay.commands.no_measurable_progress_in_sessions", v1=handoff.stalled))
    else:
        handoff.stalled = 0
    log = ws.session_log(plan.session)
    if not log.is_file():
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"# Session {plan.session}\n\n_No session log written (timeout or crash?)._\n", encoding="utf-8")
        notes.append(t("relay.commands.no_session_log"))
    _, outside = ws.partition(plan.scope, plan.forbid)
    if outside:
        # Discard, so the same paths do not block again after unblocking.
        ws.revert(outside)
        more = f" (+{len(outside) - SCOPE_REASON_PATHS} weitere)" if len(outside) > SCOPE_REASON_PATHS else ""
        handoff.block(
            t(
                "relay.commands.changes_outside_the_scope_discarded",
                v1=", ".join(outside[:SCOPE_REASON_PATHS]),
                more=more,
            )
        )
    if handoff.status is Status.ACTIVE and handoff.session >= plan.max_sessions:
        handoff.block(f"max_sessions ({plan.max_sessions}) erreicht")
    handoff.append_log(
        f"s{plan.session} · {format_time(ctx.now)} · {handoff.status} · progress: {handoff.progress or '–'}"
        + (f" · verify: {handoff.verify_result}" if plan.verify_command else "")
        + (f" · {'; '.join(notes)}" if notes else "")
    )
    ws.write_handoff(handoff)
    ws.save_checkin(CheckinResult(str(handoff.status), tuple(notes), tuple(outside)))
    return f"relay #{issue} s{plan.session}: {handoff.status}" + (
        f" — {handoff.blocked_reason}" if handoff.status is Status.BLOCKED else ""
    )


def _sync_status_labels(ctx: RelayContext, task: Task, st: Status) -> None:
    labels = ctx.cfg.labels
    target = {Status.ACTIVE: set(), Status.BLOCKED: {labels.blocked}, Status.DONE: {labels.done}}[st]
    ctx.github.sync_labels(task.number, task.labels, target, {labels.blocked, labels.done} - target)


def publish(ctx: RelayContext, issue: int) -> StepSummary:
    """Commit in-scope changes, push as builder, set labels + status comment."""
    ws = ctx.workspace(issue)
    plan, _ = ws.load_plan()
    handoff = ws.read_handoff()
    if handoff is None:
        raise RelayError(t("relay.commands.handoff_missing_did_checkin_run"))
    inside, _ = ws.partition(plan.scope, plan.forbid)
    sha = ctx.commit(ws, inside, f"relay #{issue}: session {plan.session} ({handoff.status})")
    ctx.push(ws)
    task = ctx.github.task(issue, ctx.cfg.label)
    _sync_status_labels(ctx, task, handoff.status)
    state = ctx.github.status_state(issue)
    state.verify_pending, state.last_commit, state.note = None, sha, ""
    state.trail_warning = ctx.trail_warning(issue)
    ctx.update_status(issue, handoff, state, plan.max_sessions)
    verify = f" · verify: {handoff.verify_result}" if handoff.verify_result else ""
    return StepSummary(
        f"relay #{issue} s{plan.session}/{plan.max_sessions} {handoff.status} · "
        f"progress: {handoff.progress or '–'}{verify} · {sha}",
        verdict=str(handoff.status),
    )


# ── Audit ────────────────────────────────────────────────────────────────────


def _audit_brief(task: Task, handoff: Handoff) -> str:
    brief: Brief = task.brief
    parts = [
        f"# Audit brief — #{task.number} {task.title}",
        "",
        "## Goal",
        brief.sections.get("goal", ""),
        "",
        "## Verify (fixed invariants)",
        brief.verify,
        "",
        "## Audit access",
        brief.sections.get("audit access", "_not specified — use read-only access only_"),
        "",
        "## Audit facts (from the handoff)",
        handoff.section("audit facts") or "_none_",
    ]
    return "\n".join(parts) + "\n"


def audit_prepare(ctx: RelayContext, issue: int) -> str:
    """Prepare the blind phase: write the audit brief, hide all branch changes."""
    task = ctx.github.task(issue, ctx.cfg.label)
    ws = ctx.workspace(issue)
    state = ctx.github.status_state(issue)
    restore_trail(ctx, ws, state)
    handoff = ws.read_handoff()
    if handoff is None:
        raise RelayError(t("relay.commands.no_handoff_on_the_branch", issue=issue))
    d = ctx.decide(task, handoff, state)
    if d.action is not Action.AUDIT:
        raise RelayError(t("relay.commands.no_audit_for", issue=issue, v2=d.action, v3=d.reason))
    ws.prepare()
    ws.save_snapshot(handoff)
    ws.write_text("AUDIT_BRIEF.md", _audit_brief(task, handoff))
    (ws.session / "audit" / "blind").mkdir(parents=True, exist_ok=True)
    hidden = ws.hide(ws.branch_changes(ctx.cfg.base), ctx.cfg.base)
    return t("relay.commands.relay_audit_branch_file_s", issue=issue, v2=len(hidden))


def audit_reveal(ctx: RelayContext, issue: int) -> str:
    restored = ctx.workspace(issue).reveal()
    return f"relay #{issue} audit: {len(restored)} Datei(en) wiederhergestellt"


def parse_verdict(report: str | None) -> str:
    """Letzter `<!-- relay-audit verdict=… -->`-Marker; fehlt er → fail."""
    found = _VERDICT_RE.findall(report or "")
    return found[-1] if found else "fail"


def audit_publish(ctx: RelayContext, issue: int) -> StepSummary:
    """Commit + push the report; pass → `relay: audited`, fail → back to blocked.

    The reviewer writes only the report: the handoff is the state before the audit (snapshot from
    `audit-prepare`), all other changes in the worktree are discarded. With a report the
    verdict is bound to the trail hash (last line of AUDIT.md, state block); on `pass` and
    configured `relay.trail_archive` the trail is archived and removed from the branch."""
    ws = ctx.workspace(issue)
    if ws.merge_head():
        # A merge left behind by the reviewer would otherwise be committed entirely as an "audit" commit.
        ws.git("merge", "--quit")
    handoff = ws.load_snapshot()
    report = ws.publish_audit()
    verdict = parse_verdict(report)
    reason = "" if report else t("relay.commands.no_report")
    handoff.append_log(f"audit · {format_time(ctx.now)} · {verdict}{reason}")
    summary = markdown_sections(report or "").get("summary", "")
    handoff.set_section(
        AUDIT_SECTION.title(),
        f"**{verdict}**{reason} · {format_time(ctx.now)} · details: `{AUDIT_REPORT}`"
        + (f"\n\n{summary}" if summary else ""),
    )
    if verdict == "fail":
        handoff.block(f"Audit: fail{reason} — siehe {AUDIT_REPORT}")
    ws.write_handoff(handoff)
    inside, foreign = ws.split_changes(ws.audit_paths())
    if foreign:
        ws.revert(foreign)
    digest = archive.bind_report(ws, inside) if report is not None else None
    sha = ctx.commit(ws, inside, f"relay #{issue}: audit ({verdict})")
    ctx.push(ws)
    task = ctx.github.task(issue, ctx.cfg.label)
    labels = ctx.cfg.labels
    if verdict == "pass":
        ctx.github.sync_labels(issue, task.labels, {labels.audited}, set())
    else:
        ctx.github.sync_labels(issue, task.labels, {labels.blocked}, {labels.done, labels.audited})
    state = ctx.github.status_state(issue)
    state.audit, state.last_commit, state.note = verdict, sha, ""
    state.trail_sha256 = digest or ""
    outcome = ""
    if verdict == "pass" and ctx.cfg.trail_archive is not None:
        outcome = archive_and_strip(ctx, ws, state, digest)
    else:
        state.trail_warning = ctx.trail_warning(issue)
    ctx.update_status(issue, handoff, state, task.brief.settings.max_sessions)
    return StepSummary(
        f"relay #{issue} audit: {verdict}{reason} · {state.last_commit}" + (f" · {outcome}" if outcome else ""),
        verdict=verdict,
    )


def archive_and_strip(ctx: RelayContext, ws: Workspace, state: StatusState, digest: str | None) -> str:
    """Archive the pushed audit commit, verify the archive, then remove the trail via a delete commit.

    Removal happens only after a verified archive; if the archive fails, the trail stays on the
    branch and the status comment warns (the audit verdict stays valid)."""
    cfg_archive = ctx.cfg.trail_archive
    assert cfg_archive is not None
    issue = ws.issue
    archiver = ctx.archive(cfg_archive.type, ws)
    commit = ws.git("rev-parse", "HEAD").strip()
    start = state.archive.index + 1 if state.archive else 1
    try:
        ref = archive.archive_next(archiver, ws, commit, start, ctx.now)
        if digest and ref.sha256 != digest:
            raise ArchiveError(f"Hash des Archivs {ref.sha256[:12]}… ≠ gebunden {digest[:12]}…")
        archive.verify(archiver, ref)
    except (ArchiveError, StepFailure, urllib.error.URLError) as e:
        log.warning(t("relay.commands.relay_s_trail_not_archived"), issue, e)
        state.trail_warning = f"trail not archived ({_redact(str(e))[:RESULT_MAX_CHARS]}): the branch still contains it"
        return t("relay.commands.trail_not_archived")
    removed = archive.remove_trail(ws)
    # Commit the trail path explicitly: `git add -A` stages the deletion deterministically. Without a path
    # the no-op commit would only be a `_staged()` check on the index state after `remove_trail`.
    state.last_commit = ctx.commit(
        ws,
        [relay_dir(issue)] if removed else [],
        f"relay #{issue}: archive trail ({ref.name}), remove it from the branch",
    )
    ctx.push(ws)
    state.archive, state.trail_warning = ref, ""
    return f"Trail archiviert ({ref.name}, {len(removed)} Datei(en) entfernt)" + sync_pr_trail(ctx, issue, ref)


def sync_pr_trail(ctx: RelayContext, issue: int, ref: TrailRef | None) -> str:
    """Set (`ref`) or remove (`None`) the trail block in the relay branch's open PR.

    Only a hint for humans and bots: if writing fails, the archive stays valid (the status
    comment links it anyway), the step continues."""
    try:
        pr = ctx.github.open_pr(branch_name(issue))
        if pr is None:
            return ""
        block = None
        if ref is not None:
            ref_name = archive.GitRefArchive.ref_name(issue, ref.index) if ref.method == archive.METHOD_GIT_REF else ""
            block = status.pr_trail_block(ctx.cfg.repo, issue, relay_dir(issue), ref, ref_name)
        body = ctx.github.pr_body(pr)
        new = status.with_pr_trail(body, block)
        if new != body:
            ctx.github.set_pr_body(pr, new)
        return f" · PR #{pr}: Trail-Link " + ("gesetzt" if ref else "entfernt")
    except (GitHubError, urllib.error.URLError) as e:
        log.warning(t("relay.commands.relay_s_pr_body_not"), issue, e)
        return t("relay.commands.pr_body_not_updated")


def restore_trail(ctx: RelayContext, ws: Workspace, state: StatusState) -> str:
    """Restore the archived trail onto the branch (commit) if the handoff is missing; the hash is checked.

    Without a readable archive or with a modified one, the step fails (`environment`) instead of continuing
    with a different trail."""
    ref: TrailRef | None = state.archive
    if ref is None or ws.handoff_file.is_file():
        return ""
    try:
        files = archive.verify(ctx.archive(ref.method, ws), ref)
    except (ArchiveError, StepFailure, urllib.error.URLError) as e:
        raise RelayError(
            t("relay.commands.trail_archive_cannot_be_restored", v1=ref.name, e=e), FailureKind.ENVIRONMENT
        ) from e
    archive.write_tree(ws.relay, files)
    ctx.commit(ws, [f"{relay_dir(ws.issue)}/"], f"relay #{ws.issue}: restore trail from {ref.name}")
    # The trail is back on the branch: the archive note in the PR would now be wrong.
    return t("relay.commands.trail_restored_from", v1=ref.name) + sync_pr_trail(ctx, ws.issue, None)
