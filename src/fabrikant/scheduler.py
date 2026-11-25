"""Scheduler: cron + on_file (Debounce) + on_issue + on_pr (GitHub-Poll) + on_slack (Slack-Poll)."""

from __future__ import annotations

import contextlib
import glob as globmod
import os
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import config, cron, metrics, pause, retry_chains, schedule, slack
from .engine import _safe_run, start_priority_run
from .gitops import project_github
from .hosting import Provider, get_provider
from .i18n import t as _t
from .runmodel import RunStatus
from .runs import loop_run_triggers
from .runstore import STORE, RunStoreError
from .specs import load_projects
from .steps import _notify

DEFAULT_POLL_S = 300  # Default poll interval for on_issue/on_pr
GH_PER_PAGE = 50  # GitHub page size for issue/PR polls
ACTIVE_RUN_STATUSES = ("running", "needs_approval")
RECONCILE_MAX_PAGES = 10  # Obergrenze Pagination im Reconcile-Modus (≤ 500 Issues/Poll)
PR_SEEN_MAX = 2 * RECONCILE_MAX_PAGES * GH_PER_PAGE  # Retention on_pr-Dedup-Historie (2 Poll-Fenster)
# The project's other triggers wait at most this long for its `on_start` fires
# (e.g. if the run hangs in the queue behind busy workers).
START_GATE_MAX_S = 120
DEFAULT_MAX_PER_POLL = 2  # on_issue reconcile: max. new runs per poll and loop (ADR 0014)


def _iter_open_issues_oldest_first(repo: str, provider: Provider, labels: list[str]):
    """Open issues, oldest first, lazily over up to RECONCILE_MAX_PAGES pages.

    Returns the raw provider payloads (``Issue.raw``) — the other helpers
    (`issue_vars`, `_priority_rank`, …) work on the dictionaries. PRs that the
    issues API also returns are filtered out via ``is_pr`` (ADR 0062)."""
    for page in range(1, RECONCILE_MAX_PAGES + 1):
        issues = provider.list_issues(
            repo, state="open", labels=labels, sort="created", direction="asc", page=page, per_page=GH_PER_PAGE
        )
        yield from [it.raw for it in issues if not it.is_pr]
        if len(issues) < GH_PER_PAGE:
            return


def _list_pulls(repo: str, provider: Provider, state: str, labels: list[str]) -> list[dict]:
    """PRs of a state, paginated up to RECONCILE_MAX_PAGES pages (≤ 500 PRs).

    Returns the raw provider payloads (``PullRequest.raw``). The server-side label filter
    has OR semantics (payload reduction only); the AND semantics (PR has ALL
    labels) is enforced client-side in `_check_pr`."""
    out: list[dict] = []
    for page in range(1, RECONCILE_MAX_PAGES + 1):
        prs = provider.list_prs(repo, state=state, labels=labels, page=page, per_page=GH_PER_PAGE)
        out.extend(pr.raw for pr in prs)
        if len(prs) < GH_PER_PAGE:
            break
    return out


def _pr_trigger(num: int, head_sha: str = "") -> str:
    """Trigger string of an on_pr run: ``on_pr:#N`` or ``on_pr:#N@<sha>`` (ADR 0023).

    Without SHA (missing in the API response) → PR key ``on_pr:#N``; the caller
    then additionally matches it against all ``on_pr:#N@…`` records."""
    return f"on_pr:#{num}@{head_sha}" if head_sha else f"on_pr:#{num}"


def _cron_chain_pending(project: str, loop: str, spec: dict) -> bool:
    """Does the loop's cron trigger have a ``pending`` retry chain (ADR 0036)?

    An unreadable chain store does not stop the cron triggers (False + log)."""
    tkey = retry_chains.trigger_key(spec, loop, "cron")
    if not tkey:
        return False
    try:
        chain = retry_chains.ChainStore().get(project, loop, tkey)
    except (OSError, ValueError) as e:
        print(_t("scheduler.fabrikant_retry_chain_unreadable", project=project, loop=loop, e=e))
        return False
    return chain is not None and chain.state is retry_chains.ChainState.PENDING


def issue_vars(issue: dict) -> dict:
    """Trigger variables of an on_issue run from a GitHub issue (untrusted!)."""
    return {
        "issue_number": issue["number"],
        "issue_title": issue.get("title", ""),
        "issue_body": issue.get("body") or "",
    }


def pr_vars(pr: dict) -> dict:
    """Trigger variables of an on_pr run from a GitHub PR (untrusted!)."""
    head = pr.get("head") or {}
    return {
        "pr_number": pr["number"],
        "pr_title": pr.get("title", ""),
        "pr_url": pr.get("html_url", ""),
        "pr_head_ref": head.get("ref", ""),
        "pr_head_sha": head.get("sha", ""),
        "pr_body": pr.get("body") or "",
    }


# Cron-Semantik zentral in `cron` (Scheduler, Retry-Validierung, Schedule-View).
cron_matches = cron.matches


def _new_issues(st: dict, issues: list[dict]) -> list[dict]:
    """Event mode: issues that are new since the last poll (first poll: none)."""
    ids = {it["number"] for it in issues}
    if st["seen"] is None:
        st["seen"] = ids  # initialisieren, nicht feuern
        return []
    new = ids - st["seen"]
    # Union (not replace): an issue that falls out of the window of 50 and
    # later reappears (re-label, window shift) must not fire
    # again. Issue numbers are small ints — the set grows bounded.
    st["seen"] = st["seen"] | ids
    return [it for it in issues if it["number"] in new]


def _priority_rank(issue: dict, priority_labels: list[str]) -> int:
    """Index of the first label from `priority_labels` that the issue carries.

    Without a hit `len(priority_labels)`: unprioritized issues come last.
    """
    names = {str(lbl.get("name", "")) for lbl in issue.get("labels") or [] if isinstance(lbl, dict)}
    return next((i for i, p in enumerate(priority_labels) if p in names), len(priority_labels))


def _by_priority(issues: list[dict], priority_labels: list[str]) -> list[dict]:
    """Sorted by priority label (ADR 0021), within a level oldest
    (smallest issue number) first — independent of the input order,
    since event mode reads newest first."""
    if not priority_labels:
        return issues
    return sorted(issues, key=lambda it: (_priority_rank(it, priority_labels), it["number"]))


# Close reasons with which a blocker counts as done; None = legacy issues without a reason.
RESOLVED_STATE_REASONS = (None, "completed")


def _unresolved_blockers(repo: str, provider: Provider, issue: dict) -> list[str]:
    """Blockers of `issue` that are not done (native dependencies, ADR 0031):
    `["#9", "#12 (not_planned)"]`.

    Done = closed as `completed`. A blocker closed as `duplicate`/`not_planned`
    keeps blocking — the prerequisite work is still missing; a human
    (or the duplicate close with `duplicate_of`, which re-hangs the relation) must
    decide. `issue_dependencies_summary` from the list response saves the call
    for issues without blockers. API errors propagate (fail closed).

    Native dependencies are a GitHub feature; on GitLab the provider raises
    `HostingNotSupported` (documented gap, ADR 0062) — caught by the poll.
    """
    summary = issue.get("issue_dependencies_summary") or {}
    if not summary.get("total_blocked_by"):
        return []
    out = []
    for b in provider.issue_dependencies(repo, issue["number"]):
        if b.state == "open":
            out.append(f"#{b.number}")
        elif b.raw.get("state_reason") not in RESOLVED_STATE_REASONS:
            out.append(f"#{b.number} ({b.raw.get('state_reason')})")
    return out


def _label_names(item: dict) -> set[str]:
    """Label names of an issue/PR from the REST response."""
    return {str(lbl.get("name", "")) for lbl in item.get("labels") or [] if isinstance(lbl, dict)}


def _without_excluded(items: list[dict], exclude: list[str]) -> list[dict]:
    """Items without any of the `exclude_labels` (e.g. `needs human`, ADR 0059)."""
    if not exclude:
        return items
    return [it for it in items if not _label_names(it) & set(exclude)]


def _unprocessed_issues(
    repo: str,
    provider: Provider,
    labels: list[str],
    done: set[str],
    limit: int,
    priority_labels: list[str] | None = None,
    blocked: dict[int, list[str]] | None = None,
    exclude: list[str] | None = None,
) -> list[dict]:
    """Reconcile mode: up to `limit` open issues whose trigger is not in `done`
    (trigger strings of previous runs, see `loop_run_triggers`) — highest
    priority first, within a level oldest first.

    Without `priority_labels` it paginates lazily (abort at the `limit`-th
    candidate). With priorities all pages (≤ RECONCILE_MAX_PAGES) must
    be read, otherwise an old p3 issue would hide a new p0 issue.

    `blocked` (dict, `skip_blocked`, ADR 0031): issues with unfinished blockers
    are skipped and entered there with their blockers. They occupy
    no `limit` quota and fire as soon as all blockers are done.

    `exclude` (`exclude_labels`): issues with one of these labels are skipped without
    occupying quota; they fire as soon as the label is removed.
    """
    todo: list[dict] = []
    for it in _iter_open_issues_oldest_first(repo, provider, labels):
        if f"on_issue:#{it['number']}" in done or not _without_excluded([it], exclude or []):
            continue
        if blocked is not None:
            blockers = _unresolved_blockers(repo, provider, it)
            if blockers:
                blocked[it["number"]] = blockers
                continue
        todo.append(it)
        if not priority_labels and len(todo) >= limit:
            break
    return _by_priority(todo, priority_labels or [])[:limit]


def _log_blocked(st: dict, repo: str, blocked: dict[int, list[str]] | None) -> None:
    """Logs blocked issues only on change (otherwise one line per poll and issue)."""
    if blocked is None:
        return
    prev = st.get("blocked") or {}
    for num, blockers in blocked.items():
        if prev.get(num) != blockers:
            print(_t("scheduler.fabrikant_issue_waiting_for_blocker", repo=repo, num=num, v3=", ".join(blockers)))
    st["blocked"] = blocked


def _bounded_seen(seen: dict, keep: set, limit: int = PR_SEEN_MAX) -> dict:
    """Trims the dedup history to `limit` entries; `keep` (currently polled PRs)
    is always kept, remaining entries count by PR number (newest first)."""
    if len(seen) <= limit:
        return seen
    room = max(limit - len(keep), 0)
    old = sorted((n for n in seen if n not in keep), reverse=True)[:room]
    return {n: seen[n] for n in sorted(keep | set(old))}


def _is_start_cron(trig: dict) -> bool:
    """Cron triggers with `on_start: true` (fire once after server start, before the other triggers)."""
    return trig.get("type") == "cron" and bool(trig.get("on_start"))


def _pr_fire_kind(
    prev: dict[int, tuple[str, str]] | None,
    state: str,
    updated_at: str,
    num: int,
    floor: int,
    reconcile: bool,
    per_head: bool,
    head_sha: str,
    done: set[str],
) -> str | None:
    """Fire decision for a polled PR: kind of fire event or
    ``None`` (do not fire). Pure function without I/O — the only place with
    the reconcile-vs-event logic (ADR 0011) and `reconcile: head` (ADR 0023).

    `prev`: dedup history `{num: (state, updated_at)}` of the last poll;
    ``None`` = first poll. `floor`: watermark — PR numbers ≤ `floor` count as
    seen, even if they were displaced from `prev`. `done`: trigger strings
    of previous runs (relevant only in reconcile mode).

    - Reconcile mode: ``None`` if the trigger (with `per_head` incl.
      `head_sha`) is already in `done` — without `head_sha` every
      ``on_pr:#N@…`` record also counts as done (ADR 0023). Otherwise ``"neu"`` (PR unknown
      and above `floor`) or ``"head <sha[:7]">`` (`per_head`) or
      ``"reconcile"`` (catch up a known PR).
    - Event mode: ``None`` on the first poll, for an unchanged PR (state +
      `updated_at` equal) and for PRs displaced below `floor`. Otherwise
      ``"neu"``, ``"state-wechsel <old>→<new>"`` or ``"aktualisiert"``.
    """
    if reconcile:
        trigger = _pr_trigger(num, head_sha if per_head else "")
        if trigger in done or (per_head and not head_sha and any(t.startswith(f"{trigger}@") for t in done)):
            return None
        if prev is None or (num not in prev and num > floor):
            return "neu"
        return f"head {head_sha[:7]}" if per_head and head_sha else "reconcile"
    if prev is None:
        return None  # Erst-Poll: initialisieren, nicht feuern
    old = prev.get(num)
    if old == (state, updated_at):
        return None  # unchanged
    if old is None and num <= floor:
        return None  # displaced, already seen PR (watermark)
    if old is None:
        return "neu"
    if old[0] != state:
        return f"state-wechsel {old[0]}→{state}"
    return "aktualisiert"


class Scheduler(threading.Thread):
    """Ticks every 2 s; fires cron and on_file triggers."""

    def __init__(self):
        super().__init__(daemon=True, name="fabrikant-scheduler")
        self._last_cron: dict[tuple, str] = {}  # (proj, loop) -> minute that last fired
        self._seen_files: dict[str, dict] = {}  # "proj:loop:glob" -> {mtime, stable_since, fired}
        self._issue_state: dict[tuple, dict] = {}  # (proj, loop) -> {"ts": float, "seen": set}
        self._pr_state: dict[tuple, dict] = {}  # (proj, loop) -> {"ts": float, "seen": {num: updated_at}}
        self._slack_ts: dict[tuple, float] = {}  # (proj, loop) -> letzter Poll
        self._slack_pending: set[tuple[str, str, str]] = set()
        self._first_tick = True
        self._tick_n = 0
        self._started_cron: set[tuple] = set()  # (proj, loop) mit erledigtem `on_start`-Fire
        # proj -> laufende `on_start`-Fires (Event gesetzt = erledigt, Deadline = monotonic)
        self._start_gates: dict[str, list[tuple[threading.Event, float]]] = {}

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            time.sleep(2)

    def tick(self):
        self._tick_n += 1
        if self._tick_n % 60 == 0:  # ~alle 2 min
            self._check_stale_runs()
        pause.resume_expired()
        now = config.utcnow()
        local = datetime.now()
        # The one-time store (schedule view) must not block cron/file/issue/PR.
        try:
            self._fire_onetimes(local)
        except (OSError, ValueError, TypeError, KeyError) as e:
            print(_t("scheduler.fabrikant_one_time_runs_not", e=e))
        projects = load_projects()
        for name, spec in projects.items():
            if spec.get("_error"):
                continue
            with project_github(spec):  # Polls with the project's tokens
                self._tick_project(name, spec, now, local)
        # Start due retry attempts (ADR 0036, decision 5) — also after
        # restart; exactly one starter per chain (atomic claim).
        try:
            retry_chains.start_due_attempts(projects)
        except (OSError, ValueError) as e:  # z. B. defekter Ketten-Store: Trigger laufen weiter
            print(_t("scheduler.fabrikant_retry_attempts_not_started", e=e))
        self._first_tick = False

    def _check_cron(self, name: str, lname: str, spec: dict, trig: dict, minute: str, local: datetime) -> None:
        """Cron trigger: at most one fire per minute.

        `on_start: true` additionally fires once per server process as soon as the trigger is active
        (first tick or first tick after resume) — for loops whose result may already be
        missing at start, e.g. an expired 1h token after a server pause (token-refresh)."""
        key = (name, lname)
        if self._last_cron.get(key) == minute:
            return
        on_start = bool(trig.get("on_start")) and key not in self._started_cron
        if not (on_start or cron_matches(trig.get("cron", ""), local)):
            return
        self._started_cron.add(key)
        self._last_cron[key] = minute
        # Pending retry chain: skip the tick — the chain
        # plans the next attempt (ADR 0036, decision 2).
        if _cron_chain_pending(name, lname, spec):
            print(_t("scheduler.fabrikant_cron_tick_skipped_retry", name=name, lname=lname))
            return
        if not on_start:
            _safe_run(name, lname, "cron")
            return
        done = threading.Event()
        self._start_gates.setdefault(name, []).append((done, time.monotonic() + START_GATE_MAX_S))
        _safe_run(name, lname, "cron", done=done)

    def _start_gate_open(self, name: str) -> bool:
        """True if no `on_start` fires of the project are running any more (or their wait time is over)."""
        now = time.monotonic()
        pending = []
        for done, deadline in self._start_gates.get(name, []):
            if done.is_set():
                continue
            if now >= deadline:
                print(_t("scheduler.fabrikant_on_start_run_not", name=name, v2=START_GATE_MAX_S))
                continue
            pending.append((done, deadline))
        self._start_gates[name] = pending
        return not pending

    def _fire_onetimes(self, local: datetime) -> None:
        """One-time overrides (schedule view): due entries fire like manual runs.

        Discard on `disabled` (manual runs are forbidden there, ADR 0019); `paused`
        allows them. `pop_due` has already removed due entries; a start error
        reschedules the entry with backoff (`schedule.requeue_failed`, limited
        attempts, an entry meanwhile newly scheduled wins)."""
        for entry in schedule.pop_due(local):
            name, lname = entry["project"], entry["loop"]
            if pause.effective_state(name, lname) == pause.DISABLED:
                print(f"[fabrikant] One-time {name}/{lname} verworfen: disabled")
                continue
            run_id = config.now_id() + "-" + lname
            try:
                start_priority_run(
                    name,
                    lname,
                    "once",
                    run_id=run_id,
                    note=f"one-time (geplant {entry['at']})" + (f": {entry['note']}" if entry.get("note") else ""),
                    actor="scheduler",
                )
                print(_t("scheduler.fabrikant_one_time_started", name=name, lname=lname, run_id=run_id))
            except Exception as e:
                retry = schedule.requeue_failed(entry, local)
                outcome = f"neuer Versuch {retry['at']}" if retry else "verworfen"
                print(_t("scheduler.fabrikant_one_time_not_started", name=name, lname=lname, outcome=outcome, e=e))

    def _tick_project(self, name: str, spec: dict, now: datetime, local: datetime) -> None:
        """Check the triggers of all loops of a project (cron, file, issue, PR).

        Cron triggers with `on_start` first. As long as their start fire is running (e.g. token-refresh after
        a server pause), all other triggers of the project wait — otherwise they would still read
        the expired token (401) in the same tick. A deferred cron fires in the next tick of the same
        minute (`_last_cron` stays untouched until then)."""
        minute = now.strftime("%Y%m%d%H%M")
        loops = [
            (lname, ldef.get("triggers") or [])
            for lname, ldef in spec.get("loops", {}).items()
            if not pause.trigger_blocked(name, lname)  # paused/disabled: no trigger, no poll (ADR 0019)
        ]
        for lname, triggers in loops:
            for trig in triggers:
                if _is_start_cron(trig):
                    self._check_cron(name, lname, spec, trig, minute, local)
        if not self._start_gate_open(name):
            return
        for lname, triggers in loops:
            for trig in triggers:
                ttype = trig.get("type")
                if ttype == "cron" and not _is_start_cron(trig):  # Start crons: already checked above
                    self._check_cron(name, lname, spec, trig, minute, local)
                elif ttype == "on_file":
                    self._check_file(name, lname, spec, trig, local)
                elif ttype == "on_issue":
                    self._check_issue(name, lname, spec, trig)
                elif ttype == "on_pr":
                    self._check_pr(name, lname, spec, trig)
                elif ttype == "on_slack":
                    self._check_slack(name, lname, trig)

    def _check_file(self, name: str, lname: str, spec: dict, trig: dict, local: datetime):
        pattern = str(trig.get("glob", "*"))
        base = Path(spec["root"])
        debounce = float(trig.get("debounce", 60))
        key = f"{name}:{lname}:{pattern}"
        newest: float | None = None  # None = kein (lesbarer) Treffer
        for path in globmod.glob(str(base / pattern), recursive=True):
            with contextlib.suppress(OSError):
                mtime = Path(path).stat().st_mtime
                newest = mtime if newest is None else max(newest, mtime)
        if newest is None:
            return
        now = local.timestamp()
        state = self._seen_files.get(key)
        if state is None:
            # Erst initialisieren (neueste mtime + Zeitpunkt merken), nicht feuern.
            self._seen_files[key] = {"mtime": newest, "stable_since": now, "fired": False}
            return
        if newest != state["mtime"]:
            # File changed → restart the debounce window, not fired yet.
            self._seen_files[key] = {"mtime": newest, "stable_since": now, "fired": False}
            return
        # mtime unchanged: fire exactly once when it has been stable for `debounce`
        # seconds (not repeatedly on every poll).
        if not state["fired"] and (now - state["stable_since"]) > debounce:
            self._seen_files[key] = {"mtime": newest, "stable_since": now, "fired": True}
            _safe_run(name, lname, f"on_file:{pattern}")

    def _check_issue(self, name: str, lname: str, spec: dict, trig: dict):
        """Polls GitHub issues (optionally label-filtered, AND); new issue → loop start.

        Without `reconcile` event-based: the first poll only initializes the
        in-memory `seen` set; afterwards newly appeared issues fire.

        `reconcile: true` (ADR 0014): fires for every matching open issue
        for which the loop has NEVER had a run (status irrelevant, no auto-retry)
        — checked via the run records, so it survives restarts and works off the
        backlog. At most `max_per_poll` (default DEFAULT_MAX_PER_POLL)
        runs per poll, oldest issues first.

        `priority_labels` (ADR 0021, e.g. `[p0, p1, p2, p3]`): candidates
        are sorted in both modes by the first matching label of this list;
        issues without a priority label come last.

        `skip_blocked: true` (ADR 0031, only with `reconcile`): issues with unfinished
        "blocked by" dependencies do not start; they stay without a run record and
        fire on the first poll after their last blocker has been closed as `completed`.

        `exclude_labels` (ADR 0059, e.g. `[needs human]`): issues with one of these labels
        do not start. In reconcile mode they fire as soon as the label is gone; in event mode
        they count as seen.
        """
        repo = trig.get("repo") or spec.get("github")
        if not repo:
            return
        poll = float(trig.get("poll", DEFAULT_POLL_S))
        key = (name, lname)
        st = self._issue_state.setdefault(key, {"ts": 0.0, "seen": None, "blocked": {}})
        if time.time() - st["ts"] < poll:
            return
        st["ts"] = time.time()
        labels = [str(x) for x in (trig.get("labels") or [])]
        priority_labels = [str(x) for x in (trig.get("priority_labels") or [])]
        exclude = [str(x) for x in (trig.get("exclude_labels") or [])]
        try:
            provider = get_provider(repo, spec=spec)
            if not provider.has_credentials():
                return
            if trig.get("reconcile"):
                max_per_poll = max(1, int(trig.get("max_per_poll", DEFAULT_MAX_PER_POLL)))
                blocked: dict[int, list[str]] | None = {} if trig.get("skip_blocked") else None
                done = loop_run_triggers(name, lname)
                todo = _unprocessed_issues(
                    repo, provider, labels, done, max_per_poll, priority_labels, blocked, exclude
                )
                _log_blocked(st, repo, blocked)
            else:
                issues = provider.list_issues(
                    repo, state="open", labels=labels, sort="created", direction="desc", per_page=GH_PER_PAGE
                )
                new = _new_issues(st, [it.raw for it in issues if not it.is_pr])
                todo = _by_priority(_without_excluded(new, exclude), priority_labels)
        except Exception as e:
            print(f"[fabrikant] on_issue Poll {repo} fehlgeschlagen: {e}")
            return
        for it in todo:
            print(f"[fabrikant] neues Issue {repo}#{it['number']}: {it['title']}")
            # Issue content as variable (untrusted! — only marked in LLM prompts,
            # triage → deterministic mapping, no free LLM action)
            _safe_run(name, lname, f"on_issue:#{it['number']}", initial_vars=issue_vars(it))

    def _check_slack(self, name: str, lname: str, trig: dict, client: slack.SlackClient | None = None):
        """Polls whitelist channels for alerts (ADR 0040); every new hit → exactly one run.

        Start via `_safe_run` with `alert_text/alert_channel/alert_ts` — the same
        path as `on_issue`. Without a token in `token_env` nothing happens. An alert
        stays `pending` (state file) until its run is claimed; while a
        start attempt is running, it is not queued again."""
        key = (name, lname)
        if time.time() - self._slack_ts.get(key, 0.0) < float(trig.get("poll", slack.DEFAULT_POLL_S)):
            return
        self._slack_ts[key] = time.time()
        if client is None:
            token = slack.token_from_env(trig)
            if not token:
                return
            client = slack.WebApiClient(token)
        path = slack.state_path(name, lname)
        try:
            alerts = slack.poll(client, trig, path, f"{time.time():.6f}", include_pending=True, defer_seen=True)
        except Exception as e:
            print(f"[fabrikant] on_slack Poll {name}/{lname} fehlgeschlagen: {type(e).__name__}")
            return
        for alert in alerts:
            self._start_slack_run(name, lname, path, alert)

    def _start_slack_run(self, name: str, lname: str, path: Path, alert: slack.Alert) -> None:
        """Enqueue an alert run; `_slack_pending` prevents double-enqueueing until claim/discard."""
        pending_key = (name, lname, alert.trigger)
        if pending_key in self._slack_pending:
            return
        print(f"[fabrikant] neuer Slack-Alert {alert.channel}@{alert.ts}")
        self._slack_pending.add(pending_key)

        def claimed() -> None:
            try:
                slack.mark_started(path, alert)
            finally:
                self._slack_pending.discard(pending_key)

        _safe_run(
            name,
            lname,
            alert.trigger,
            initial_vars=alert.variables(),
            on_claimed=claimed,
            on_rejected=lambda: self._slack_pending.discard(pending_key),
        )

    def _check_pr(self, name: str, lname: str, spec: dict, trig: dict):
        """Polls GitHub PRs (states from `states`, default: `open`); new OR
        updated PR (updated_at change) OR state change (e.g.
        open→closed with `states: [open, closed]`) → loop start.

        `updated_at` changes on new commits, title/body edits, label and
        review changes — i.e. 'create/update' in the sense of the trigger. The first
        poll only initializes (does not fire), so existing PRs are not
        all scanned immediately. Dedup: a PR fires only on change
        (state + updated_at) — polling the same PR again does not trigger a
        second run.

        `reconcile: true` turns the trigger into a reconciling controller:
        the first poll ALSO fires on existing PRs, and every poll fires only
        on PRs for which the loop has NEVER had a run (status irrelevant, from
        the run records — survives restarts) — so a PR has exactly one
        approval run (ADR 0011). Without `reconcile` the event behavior stays
        (first poll initializes, fires on new/updated/state change).

        `reconcile: head` (ADR 0023) reconciles per head SHA instead of per PR
        (trigger `on_pr:#N@<sha>`): every new push fires exactly once,
        comments/labels without a push do not; a discarded trigger (lock busy,
        no run record) is caught up on the next poll. If `head.sha` is missing,
        the PR counts as done as soon as any run exists for it.

        The fire decision per PR is computed by the pure helper `_pr_fire_kind`
        (without I/O); only polling/throttling, label filter,
        dedup state (`seen`/`floor`) and run starting via `_safe_run` remain here.

        `exclude_labels` (ADR 0059, e.g. `[needs human]`): PRs with one of these labels do not
        fire; removing the label is itself a change and fires again afterwards.
        """
        repo = trig.get("repo") or spec.get("github")
        if not repo:
            return
        poll = float(trig.get("poll", DEFAULT_POLL_S))
        key = (name, lname)
        st = self._pr_state.setdefault(key, {"ts": 0.0, "seen": None})
        if time.time() - st["ts"] < poll:
            return
        st["ts"] = time.time()
        reconcile = bool(trig.get("reconcile"))
        per_head = trig.get("reconcile") == config.RECONCILE_PER_HEAD
        required_labels = [str(x) for x in (trig.get("labels") or [])]
        # Dedup (order kept): `states: [open, open]` would otherwise call the API
        # twice and add the same PRs twice to `prs` → duplicate runs.
        states = list(dict.fromkeys(str(s) for s in (trig.get("states") or ["open"])))
        prs: list[dict] = []
        try:
            provider = get_provider(repo, spec=spec)
            if not provider.has_credentials():
                return
            for state in states:
                prs.extend(_list_pulls(repo, provider, state, required_labels))
        except Exception as e:
            print(f"[fabrikant] on_pr Poll {repo} fehlgeschlagen: {e}")
            return
        # AND semantics: the PR must have ALL required labels — the GitHub API
        # filters only OR. Without this e.g. 'needs approval' would also hit
        # non-fabrikant PRs (ADR 0011).
        if required_labels:
            prs = [pr for pr in prs if set(required_labels) <= _label_names(pr)]
        prs = _without_excluded(prs, [str(x) for x in (trig.get("exclude_labels") or [])])
        # Track state + updated_at per PR: a state change (e.g. open→closed)
        # is its own fire event, an update (same state, new
        # updated_at) likewise; unchanged → no fire (dedup).

        cur = {pr["number"]: (pr.get("state", "open"), pr.get("updated_at", "")) for pr in prs}
        prev = st["seen"]
        # Merge (not replace): a PR that falls out of the paginated window and
        # returns unchanged must not fire again. Retention: limited to
        # PR_SEEN_MAX entries (current PRs + highest/newest numbers of the
        # history). Displaced numbers move into the watermark `floor`: PR numbers
        # grow monotonically, so an unknown number ≤ floor is never new but
        # a displaced, already seen PR → no re-fire (state is silently
        # relearned; a change during the absence is lost in the process).
        merged = {**(prev or {}), **cur}
        st["seen"] = _bounded_seen(merged, keep=set(cur))
        evicted = merged.keys() - st["seen"].keys()
        floor = max([st.get("floor", 0), *evicted])
        st["floor"] = floor
        done = loop_run_triggers(name, lname) if reconcile else set()
        for pr in prs:
            num = pr["number"]
            state = pr.get("state", "open")
            upd = pr.get("updated_at", "")
            head_sha = (pr.get("head") or {}).get("sha", "")
            kind = _pr_fire_kind(prev, state, upd, num, floor, reconcile, per_head, head_sha, done)
            if kind is None:
                continue
            print(f"[fabrikant] PR {repo}#{num} ({kind}): {pr.get('title', '')}")
            _safe_run(name, lname, _pr_trigger(num, head_sha if per_head else ""), initial_vars=pr_vars(pr))

    def _check_stale_runs(self):
        """Dead-man's switch: runs in 'running' older than X h → error + alert."""
        limit_h = float(os.environ.get("FABRIKANT_STALE_HOURS", "6"))
        now_ts = time.time()
        projects: dict[str, dict] | None = None  # load once, only when a run is cleaned up
        for r in STORE.load_records(status=RunStatus.RUNNING):
            try:
                age_h = (now_ts - datetime.fromisoformat(r["started"]).timestamp()) / 3600
            except Exception:
                continue
            if age_h < limit_h:
                continue
            # Via the store: precondition `running` — a run the engine has meanwhile
            # ended is not overwritten (PreconditionError → skip).
            try:
                r = STORE.reap(r["project"], r["id"], error=f"stale: >{limit_h:.0f}h in 'running' — Dead-Man's-Switch")
            except (RunStoreError, OSError):
                continue
            metrics.FABRIKANT_REAPED_RUNS_TOTAL.inc(reason="stale")
            print(f"[fabrikant] STALE RUN {r['id']} ({age_h:.1f}h) → error")
            if projects is None:
                projects = load_projects()
            spec = projects.get(r["project"], {})
            chain_info = retry_chains.on_run_finished_safe(spec, r["project"], r.get("loop", ""), r)
            sent = _notify(spec, r, retry_state=chain_info.get("state"))
            if sent:
                print(f"[fabrikant] Alerts gesendet: {sent}")
