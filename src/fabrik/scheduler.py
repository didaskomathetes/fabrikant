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
from typing import cast
from urllib.parse import quote

from . import config, cron, metrics, pause, retry_chains, schedule, slack
from .engine import _safe_run, start_priority_run
from .gitops import GITHUB_API, _gh_api, _gh_dependencies, _gh_token_safe, project_github
from .i18n import t as _t
from .runmodel import RunStatus
from .runs import loop_run_triggers
from .runstore import STORE, RunStoreError
from .specs import load_projects
from .steps import _notify

DEFAULT_POLL_S = 300  # Default-Poll-Intervall für on_issue/on_pr
GH_PER_PAGE = 50  # GitHub-Seitengröße für Issue-/PR-Polls
ACTIVE_RUN_STATUSES = ("running", "needs_approval")
RECONCILE_MAX_PAGES = 10  # Obergrenze Pagination im Reconcile-Modus (≤ 500 Issues/Poll)
PR_SEEN_MAX = 2 * RECONCILE_MAX_PAGES * GH_PER_PAGE  # Retention on_pr-Dedup-Historie (2 Poll-Fenster)
# Höchstens so lange warten die übrigen Trigger eines Projekts auf dessen `on_start`-Fires
# (z. B. wenn der Run hinter belegten Workern in der Queue hängt).
START_GATE_MAX_S = 120
DEFAULT_MAX_PER_POLL = 2  # on_issue reconcile: max. neue Runs pro Poll und Loop (ADR 0014)


def _issues_url(repo: str, labels: list[str], direction: str, page: int = 1) -> str:
    url = (
        f"{GITHUB_API}/repos/{repo}/issues?state=open&per_page={GH_PER_PAGE}"
        f"&sort=created&direction={direction}&page={page}"
    )
    if labels:
        # quote: Labels mit Leerzeichen ("ready to develop") wären sonst
        # ein ungültiger URL (urllib wirft "control characters").
        url += "&labels=" + ",".join(quote(x) for x in labels)
    return url


def _gh_get_list(url: str, tok: str) -> list[dict]:
    """GET auf einen GitHub-Listen-Endpoint (JSON-Array)."""
    return cast(list[dict], _gh_api("GET", url, tok))


def _only_issues(items: list[dict]) -> list[dict]:
    """Die Issues-API liefert auch PRs — die werden herausgefiltert."""
    return [it for it in items if "pull_request" not in it]


def _iter_open_issues_oldest_first(repo: str, tok: str, labels: list[str]):
    """Offene Issues, älteste zuerst, lazy über bis zu RECONCILE_MAX_PAGES Seiten."""
    for page in range(1, RECONCILE_MAX_PAGES + 1):
        res = _gh_get_list(_issues_url(repo, labels, "asc", page), tok)
        yield from _only_issues(res)
        if len(res) < GH_PER_PAGE:
            return


def _pulls_url(repo: str, state: str, labels: list[str], page: int = 1) -> str:
    url = (
        f"{GITHUB_API}/repos/{repo}/pulls?state={state}&per_page={GH_PER_PAGE}&sort=updated&direction=desc&page={page}"
    )
    if labels:
        # Server-Filter ist OR-Semantik (nur Payload-Reduktion); die
        # AND-Semantik (PR hat ALLE Labels) wird client-seitig erzwungen.
        url += "&labels=" + ",".join(quote(x) for x in labels)
    return url


def _list_pulls(repo: str, tok: str, state: str, labels: list[str]) -> list[dict]:
    """PRs eines States, paginiert bis zu RECONCILE_MAX_PAGES Seiten (≤ 500 PRs)."""
    out: list[dict] = []
    for page in range(1, RECONCILE_MAX_PAGES + 1):
        res = _gh_get_list(_pulls_url(repo, state, labels, page), tok)
        out.extend(res)
        if len(res) < GH_PER_PAGE:
            break
    return out


def _pr_trigger(num: int, head_sha: str = "") -> str:
    """Trigger-String eines on_pr-Runs: ``on_pr:#N`` bzw. ``on_pr:#N@<sha>`` (ADR 0023).

    Ohne SHA (fehlt in der API-Antwort) → PR-Schlüssel ``on_pr:#N``; der Aufrufer
    gleicht ihn dann zusätzlich gegen alle ``on_pr:#N@…``-Records ab."""
    return f"on_pr:#{num}@{head_sha}" if head_sha else f"on_pr:#{num}"


def _cron_chain_pending(project: str, loop: str, spec: dict) -> bool:
    """Hat der Cron-Trigger des Loops eine ``pending``-Retry-Kette (ADR 0036)?

    Ein unlesbarer Ketten-Store hält die Cron-Trigger nicht an (False + Log)."""
    tkey = retry_chains.trigger_key(spec, loop, "cron")
    if not tkey:
        return False
    try:
        chain = retry_chains.ChainStore().get(project, loop, tkey)
    except (OSError, ValueError) as e:
        print(_t("scheduler.fabrik_retry_chain_unreadable", project=project, loop=loop, e=e))
        return False
    return chain is not None and chain.state is retry_chains.ChainState.PENDING


def issue_vars(issue: dict) -> dict:
    """Trigger-Variablen eines on_issue-Runs aus einem GitHub-Issue (untrusted!)."""
    return {
        "issue_number": issue["number"],
        "issue_title": issue.get("title", ""),
        "issue_body": issue.get("body") or "",
    }


def pr_vars(pr: dict) -> dict:
    """Trigger-Variablen eines on_pr-Runs aus einem GitHub-PR (untrusted!)."""
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
    """Event-Modus: Issues, die seit dem letzten Poll neu sind (Erst-Poll: keine)."""
    ids = {it["number"] for it in issues}
    if st["seen"] is None:
        st["seen"] = ids  # initialisieren, nicht feuern
        return []
    new = ids - st["seen"]
    # Union (nicht Replace): ein Issue, das aus dem 50er-Fenster fällt und
    # später wieder auftaucht (Re-Label, Fenster-Shift), darf nicht erneut
    # feuern. Issue-Nummern sind kleine Ints — das Set wächst bounded.
    st["seen"] = st["seen"] | ids
    return [it for it in issues if it["number"] in new]


def _priority_rank(issue: dict, priority_labels: list[str]) -> int:
    """Index des ersten Labels aus `priority_labels`, das das Issue trägt.

    Ohne Treffer `len(priority_labels)`: unpriorisierte Issues kommen zuletzt.
    """
    names = {str(lbl.get("name", "")) for lbl in issue.get("labels") or [] if isinstance(lbl, dict)}
    return next((i for i, p in enumerate(priority_labels) if p in names), len(priority_labels))


def _by_priority(issues: list[dict], priority_labels: list[str]) -> list[dict]:
    """Sortiert nach Prioritäts-Label (ADR 0021), innerhalb einer Stufe älteste
    (kleinste Issue-Nummer) zuerst — unabhängig von der Eingabe-Reihenfolge,
    denn der Event-Modus liest neueste zuerst."""
    if not priority_labels:
        return issues
    return sorted(issues, key=lambda it: (_priority_rank(it, priority_labels), it["number"]))


# Close-Gründe, mit denen ein Blocker als erledigt gilt; None = Alt-Issues ohne Grund.
RESOLVED_STATE_REASONS = (None, "completed")


def _unresolved_blockers(repo: str, tok: str, issue: dict) -> list[str]:
    """Blocker von `issue`, die nicht erledigt sind (native Dependencies, ADR 0031):
    `["#9", "#12 (not_planned)"]`.

    Erledigt = geschlossen als `completed`. Ein als `duplicate`/`not_planned`
    geschlossener Blocker blockiert weiter — die Vorarbeit fehlt ja noch; ein Mensch
    (oder der Duplikat-Close mit `duplicate_of`, der die Relation umhängt) muss
    entscheiden. `issue_dependencies_summary` aus der Listen-Antwort spart den Call
    für Issues ohne Blocker. API-Fehler propagieren (fail closed).
    """
    summary = issue.get("issue_dependencies_summary") or {}
    if not summary.get("total_blocked_by"):
        return []
    out = []
    for b in _gh_dependencies(repo, issue["number"], tok):
        if b.get("state") == "open":
            out.append(f"#{b['number']}")
        elif b.get("state_reason") not in RESOLVED_STATE_REASONS:
            out.append(f"#{b['number']} ({b['state_reason']})")
    return out


def _unprocessed_issues(
    repo: str,
    tok: str,
    labels: list[str],
    done: set[str],
    limit: int,
    priority_labels: list[str] | None = None,
    blocked: dict[int, list[str]] | None = None,
) -> list[dict]:
    """Reconcile-Modus: bis zu `limit` offene Issues, deren Trigger nicht in `done`
    (Trigger-Strings bisheriger Runs, s. `loop_run_triggers`) steht — höchste
    Priorität zuerst, innerhalb einer Stufe älteste zuerst.

    Ohne `priority_labels` wird lazy paginiert (Abbruch beim `limit`-ten
    Kandidaten). Mit Prioritäten müssen alle Seiten (≤ RECONCILE_MAX_PAGES)
    gelesen werden, sonst verdeckt ein altes p3-Issue ein neues p0-Issue.

    `blocked` (dict, `skip_blocked`, ADR 0031): Issues mit unerledigten Blockern
    werden übersprungen und mit ihren Blockern dort eingetragen. Sie belegen
    kein `limit`-Kontingent und feuern, sobald alle Blocker erledigt sind.
    """
    todo: list[dict] = []
    for it in _iter_open_issues_oldest_first(repo, tok, labels):
        if f"on_issue:#{it['number']}" in done:
            continue
        if blocked is not None:
            blockers = _unresolved_blockers(repo, tok, it)
            if blockers:
                blocked[it["number"]] = blockers
                continue
        todo.append(it)
        if not priority_labels and len(todo) >= limit:
            break
    return _by_priority(todo, priority_labels or [])[:limit]


def _log_blocked(st: dict, repo: str, blocked: dict[int, list[str]] | None) -> None:
    """Loggt blockierte Issues nur bei Änderung (sonst eine Zeile pro Poll und Issue)."""
    if blocked is None:
        return
    prev = st.get("blocked") or {}
    for num, blockers in blocked.items():
        if prev.get(num) != blockers:
            print(_t("scheduler.fabrik_issue_waiting_for_blocker", repo=repo, num=num, v3=", ".join(blockers)))
    st["blocked"] = blocked


def _bounded_seen(seen: dict, keep: set, limit: int = PR_SEEN_MAX) -> dict:
    """Kürzt die Dedup-Historie auf `limit` Einträge; `keep` (aktuell gepollte PRs)
    bleibt immer erhalten, übrige Einträge zählen nach PR-Nummer (neueste zuerst)."""
    if len(seen) <= limit:
        return seen
    room = max(limit - len(keep), 0)
    old = sorted((n for n in seen if n not in keep), reverse=True)[:room]
    return {n: seen[n] for n in sorted(keep | set(old))}


def _is_start_cron(trig: dict) -> bool:
    """Cron-Trigger mit `on_start: true` (feuert einmal nach Serverstart, vor den übrigen Triggern)."""
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
    """Fire-Entscheidung für eine gepollte PR: Art des Feuer-Ereignisses oder
    ``None`` (nicht feuern). Reine Funktion ohne I/O — die einzige Stelle mit
    der Reconcile-vs-Event-Logik (ADR 0011) und `reconcile: head` (ADR 0023).

    `prev`: Dedup-Historie `{num: (state, updated_at)}` des letzten Polls;
    ``None`` = Erst-Poll. `floor`: Wasserstand — PR-Nummern ≤ `floor` gelten als
    gesehen, auch wenn sie aus `prev` verdrängt wurden. `done`: Trigger-Strings
    bisheriger Runs (nur im Reconcile-Modus relevant).

    - Reconcile-Modus: ``None``, wenn der Trigger (bei `per_head` inkl.
      `head_sha`) bereits in `done` steht — ohne `head_sha` zählt auch jeder
      ``on_pr:#N@…``-Record als erledigt (ADR 0023). Sonst ``"neu"`` (PR unbekannt
      und über `floor`) oder ``"head <sha[:7]">`` (`per_head`) bzw.
      ``"reconcile"`` (bekannte PR nachholen).
    - Event-Modus: ``None`` beim Erst-Poll, bei unveränderter PR (State +
      `updated_at` gleich) und für unter `floor` verdrängte PRs. Sonst
      ``"neu"``, ``"state-wechsel <alt>→<neu>"`` oder ``"aktualisiert"``.
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
        return None  # unverändert
    if old is None and num <= floor:
        return None  # verdrängte, schon gesehene PR (Wasserstand)
    if old is None:
        return "neu"
    if old[0] != state:
        return f"state-wechsel {old[0]}→{state}"
    return "aktualisiert"


class Scheduler(threading.Thread):
    """Tickt alle 2 s; feuert cron- und on_file-Triggers."""

    def __init__(self):
        super().__init__(daemon=True, name="fabrik-scheduler")
        self._last_cron: dict[tuple, str] = {}  # (proj, loop) -> Minute, die zuletzt gefeuert wurde
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
        # One-time-Store (Schedule-View) darf Cron/Datei/Issue/PR nicht blockieren.
        try:
            self._fire_onetimes(local)
        except (OSError, ValueError, TypeError, KeyError) as e:
            print(_t("scheduler.fabrik_one_time_runs_not", e=e))
        projects = load_projects()
        for name, spec in projects.items():
            if spec.get("_error"):
                continue
            with project_github(spec):  # Polls mit den Tokens des Projekts
                self._tick_project(name, spec, now, local)
        # Fällige Retry-Versuche starten (ADR 0036, Entscheidung 5) — auch nach
        # Neustart; je Kette genau ein Starter (atomarer Claim).
        try:
            retry_chains.start_due_attempts(projects)
        except (OSError, ValueError) as e:  # z. B. defekter Ketten-Store: Trigger laufen weiter
            print(_t("scheduler.fabrik_retry_attempts_not_started", e=e))
        self._first_tick = False

    def _check_cron(self, name: str, lname: str, spec: dict, trig: dict, minute: str, local: datetime) -> None:
        """Cron-Trigger: höchstens ein Fire pro Minute.

        `on_start: true` feuert zusätzlich einmal pro Serverprozess, sobald der Trigger aktiv ist
        (erster Tick bzw. erster Tick nach Resume) — für Loops, deren Ergebnis beim Start schon
        fehlen kann, z. B. ein abgelaufenes 1h-Token nach einer Server-Pause (token-refresh)."""
        key = (name, lname)
        if self._last_cron.get(key) == minute:
            return
        on_start = bool(trig.get("on_start")) and key not in self._started_cron
        if not (on_start or cron_matches(trig.get("cron", ""), local)):
            return
        self._started_cron.add(key)
        self._last_cron[key] = minute
        # Pending Retry-Kette: Tick überspringen — die Kette
        # plant den nächsten Versuch (ADR 0036, Entscheidung 2).
        if _cron_chain_pending(name, lname, spec):
            print(_t("scheduler.fabrik_cron_tick_skipped_retry", name=name, lname=lname))
            return
        if not on_start:
            _safe_run(name, lname, "cron")
            return
        done = threading.Event()
        self._start_gates.setdefault(name, []).append((done, time.monotonic() + START_GATE_MAX_S))
        _safe_run(name, lname, "cron", done=done)

    def _start_gate_open(self, name: str) -> bool:
        """True, wenn keine `on_start`-Fires des Projekts mehr laufen (oder ihre Wartezeit um ist)."""
        now = time.monotonic()
        pending = []
        for done, deadline in self._start_gates.get(name, []):
            if done.is_set():
                continue
            if now >= deadline:
                print(_t("scheduler.fabrik_on_start_run_not", name=name, v2=START_GATE_MAX_S))
                continue
            pending.append((done, deadline))
        self._start_gates[name] = pending
        return not pending

    def _fire_onetimes(self, local: datetime) -> None:
        """One-time-Overrides (Schedule-View): fällige Einträge feuern wie manuelle Runs.

        `disabled` verwerfen (manuelle Runs sind dort verboten, ADR 0019); `paused`
        erlaubt sie. `pop_due` hat fällige Einträge bereits entfernt; ein Start-Fehler
        setzt den Eintrag mit Backoff erneut an (`schedule.requeue_failed`, begrenzte
        Versuche, ein inzwischen neu angesetzter Eintrag gewinnt)."""
        for entry in schedule.pop_due(local):
            name, lname = entry["project"], entry["loop"]
            if pause.effective_state(name, lname) == pause.DISABLED:
                print(f"[fabrik] One-time {name}/{lname} verworfen: disabled")
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
                print(_t("scheduler.fabrik_one_time_started", name=name, lname=lname, run_id=run_id))
            except Exception as e:
                retry = schedule.requeue_failed(entry, local)
                outcome = f"neuer Versuch {retry['at']}" if retry else "verworfen"
                print(_t("scheduler.fabrik_one_time_not_started", name=name, lname=lname, outcome=outcome, e=e))

    def _tick_project(self, name: str, spec: dict, now: datetime, local: datetime) -> None:
        """Trigger aller Loops eines Projekts prüfen (Cron, Datei, Issue, PR).

        Cron-Trigger mit `on_start` zuerst. Solange deren Start-Fire läuft (z. B. token-refresh nach
        einer Server-Pause), warten alle übrigen Trigger des Projekts — sonst lesen sie im selben Tick
        noch das abgelaufene Token (401). Ein aufgeschobener Cron feuert im nächsten Tick derselben
        Minute nach (`_last_cron` bleibt bis dahin unberührt)."""
        minute = now.strftime("%Y%m%d%H%M")
        loops = [
            (lname, ldef.get("triggers") or [])
            for lname, ldef in spec.get("loops", {}).items()
            if not pause.trigger_blocked(name, lname)  # paused/disabled: kein Trigger, kein Poll (ADR 0019)
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
                if ttype == "cron" and not _is_start_cron(trig):  # Start-Crons: schon oben geprüft
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
            # Datei geändert → Debounce-Fenster neu starten, noch nicht gefeuert.
            self._seen_files[key] = {"mtime": newest, "stable_since": now, "fired": False}
            return
        # mtime unverändert: genau einmal feuern, wenn sie seit `debounce`
        # Sekunden stabil ist (nicht wiederholt bei jedem Poll).
        if not state["fired"] and (now - state["stable_since"]) > debounce:
            self._seen_files[key] = {"mtime": newest, "stable_since": now, "fired": True}
            _safe_run(name, lname, f"on_file:{pattern}")

    def _check_issue(self, name: str, lname: str, spec: dict, trig: dict):
        """Pollt GitHub-Issues (optional label-gefiltert, AND); neue Issue → Loop-Start.

        Ohne `reconcile` event-basiert: der erste Poll initialisiert nur das
        In-Memory-`seen`-Set; danach feuern neu aufgetauchte Issues.

        `reconcile: true` (ADR 0014): feuert für jedes passende offene Issue,
        für das der Loop noch NIE einen Run hatte (Status egal, kein Auto-Retry)
        — geprüft über die Run-Records, überlebt also Restarts und arbeitet den
        Bestand ab. Höchstens `max_per_poll` (Default DEFAULT_MAX_PER_POLL)
        Runs pro Poll, älteste Issues zuerst.

        `priority_labels` (ADR 0021, z. B. `[p0, p1, p2, p3]`): Kandidaten
        werden in beiden Modi nach dem ersten passenden Label dieser Liste
        sortiert; Issues ohne Prioritäts-Label kommen zuletzt.

        `skip_blocked: true` (ADR 0031, nur mit `reconcile`): Issues mit unerledigten
        "blocked by"-Dependencies starten nicht; sie bleiben ohne Run-Record und
        feuern beim ersten Poll, nachdem ihr letzter Blocker als `completed`
        geschlossen wurde.
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
        try:
            tok = _gh_token_safe()
            if not tok:
                return
            if trig.get("reconcile"):
                max_per_poll = max(1, int(trig.get("max_per_poll", DEFAULT_MAX_PER_POLL)))
                blocked: dict[int, list[str]] | None = {} if trig.get("skip_blocked") else None
                done = loop_run_triggers(name, lname)
                todo = _unprocessed_issues(repo, tok, labels, done, max_per_poll, priority_labels, blocked)
                _log_blocked(st, repo, blocked)
            else:
                new = _new_issues(st, _only_issues(_gh_get_list(_issues_url(repo, labels, "desc"), tok)))
                todo = _by_priority(new, priority_labels)
        except Exception as e:
            print(f"[fabrik] on_issue Poll {repo} fehlgeschlagen: {e}")
            return
        for it in todo:
            print(f"[fabrik] neues Issue {repo}#{it['number']}: {it['title']}")
            # Issue-Content als Variable (untrusted! — nur markiert in LLM-Prompts,
            # Triage → deterministisches Mapping, keine freie LLM-Aktion)
            _safe_run(name, lname, f"on_issue:#{it['number']}", initial_vars=issue_vars(it))

    def _check_slack(self, name: str, lname: str, trig: dict, client: slack.SlackClient | None = None):
        """Pollt Whitelist-Channels nach Alerts (ADR 0040); jeder neue Treffer → genau ein Run.

        Start über `_safe_run` mit `alert_text/alert_channel/alert_ts` — derselbe
        Pfad wie `on_issue`. Ohne Token in `token_env` passiert nichts. Ein Alert
        bleibt `pending` (State-Datei), bis sein Run geclaimt ist; solange ein
        Start-Versuch läuft, wird er nicht erneut eingereiht."""
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
            print(f"[fabrik] on_slack Poll {name}/{lname} fehlgeschlagen: {type(e).__name__}")
            return
        for alert in alerts:
            self._start_slack_run(name, lname, path, alert)

    def _start_slack_run(self, name: str, lname: str, path: Path, alert: slack.Alert) -> None:
        """Einen Alert-Run einreihen; `_slack_pending` verhindert Doppel-Einreihung bis Claim/Verwerfen."""
        pending_key = (name, lname, alert.trigger)
        if pending_key in self._slack_pending:
            return
        print(f"[fabrik] neuer Slack-Alert {alert.channel}@{alert.ts}")
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
        """Pollt GitHub-PRs (States aus `states`, Default: `open`); neue ODER
        aktualisierte PR (updated_at-Change) ODER State-Wechsel (z. B.
        open→closed bei `states: [open, closed]`) → Loop-Start.

        `updated_at` ändert sich bei neuen Commits, Titel/Body-Edit, Label- und
        Review-Changes — d. h. 'create/update' im Sinne des Triggers. Der erste
        Poll initialisiert nur (nicht feuern), damit bestehende PRs nicht sofort
        alle gescannt werden. Dedup: eine PR feuert nur bei Change
        (State + updated_at) — erneutes Pollen derselben PR löst keinen
        zweiten Run aus.

        `reconcile: true` macht den Trigger zu einem Reconciling-Controller:
        Der erste Poll feuert AUCH auf bestehende PRs, und jeder Poll feuert nur
        auf PRs, für die der Loop noch NIE einen Run hatte (Status egal, aus
        den Run-Records — überlebt Restarts) — damit ein PR genau einen
        Approval-Run hat (ADR 0011). Ohne `reconcile` bleibt das
        Event-Verhalten (Erst-Poll initialisiert, feuert auf neu/aktualisiert/
        State-Wechsel).

        `reconcile: head` (ADR 0023) reconciled pro Head-SHA statt pro PR
        (Trigger `on_pr:#N@<sha>`): jeder neue Push feuert genau einmal,
        Kommentare/Labels ohne Push nicht; ein verworfener Trigger (Lock belegt,
        kein Run-Record) wird beim nächsten Poll nachgeholt. Fehlt `head.sha`,
        gilt die PR als erledigt, sobald irgendein Run für sie existiert.

        Die Fire-Entscheidung pro PR rechnet der reine Helfer `_pr_fire_kind`
        (ohne I/O); hier bleiben nur Polling/Throttling, Label-Filter,
        Dedup-State (`seen`/`floor`) und das Run-Starten via `_safe_run`.
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
        # Dedup (Reihenfolge bleibt): `states: [open, open]` würde sonst die API
        # zweimal aufrufen und dieselben PRs doppelt in `prs` hängen → doppelte Runs.
        states = list(dict.fromkeys(str(s) for s in (trig.get("states") or ["open"])))
        prs: list[dict] = []
        try:
            tok = _gh_token_safe()
            if not tok:
                return
            for state in states:
                prs.extend(_list_pulls(repo, tok, state, required_labels))
        except Exception as e:
            print(f"[fabrik] on_pr Poll {repo} fehlgeschlagen: {e}")
            return
        # AND-Semantik: PR muss ALLE geforderten Labels haben — die GitHub-API
        # filtert nur OR. Ohne dies würde z. B. 'needs approval' auch
        # Nicht-fabrik-PRs treffen (ADR 0011).
        if required_labels:
            prs = [
                pr for pr in prs if set(required_labels) <= {str(lbl.get("name", "")) for lbl in pr.get("labels", [])}
            ]
        # State + updated_at pro PR tracken: State-Wechsel (z. B. open→closed)
        # ist ein eigenes Feuer-Ereignis, Update (gleicher State, neues
        # updated_at) ebenfalls; unverändert → kein Fire (Dedup).
        cur = {pr["number"]: (pr.get("state", "open"), pr.get("updated_at", "")) for pr in prs}
        prev = st["seen"]
        # Merge (nicht Replace): eine PR, die aus dem paginierten Fenster fällt und
        # unverändert zurückkehrt, darf nicht erneut feuern. Retention: auf
        # PR_SEEN_MAX Einträge begrenzt (aktuelle PRs + höchste/neueste Nummern der
        # Historie). Verdrängte Nummern wandern in den Wasserstand `floor`: PR-Nummern
        # wachsen monoton, eine unbekannte Nummer ≤ floor ist daher nie neu, sondern
        # eine verdrängte, bereits gesehene PR → kein Re-Fire (Zustand wird still
        # neu gelernt; eine Änderung während der Abwesenheit geht dabei verloren).
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
            print(f"[fabrik] PR {repo}#{num} ({kind}): {pr.get('title', '')}")
            _safe_run(name, lname, _pr_trigger(num, head_sha if per_head else ""), initial_vars=pr_vars(pr))

    def _check_stale_runs(self):
        """Dead-Man's-Switch: Runs in 'running' älter als Xh → error + Alert."""
        limit_h = float(os.environ.get("FABRIK_STALE_HOURS", "6"))
        now_ts = time.time()
        projects: dict[str, dict] | None = None  # einmal laden, erst wenn ein Run bereinigt wird
        for r in STORE.load_records(status=RunStatus.RUNNING):
            try:
                age_h = (now_ts - datetime.fromisoformat(r["started"]).timestamp()) / 3600
            except Exception:
                continue
            if age_h < limit_h:
                continue
            # Über den Store: Vorbedingung `running` — ein Run, den die Engine inzwischen
            # beendet hat, wird nicht überschrieben (PreconditionError → überspringen).
            try:
                r = STORE.reap(r["project"], r["id"], error=f"stale: >{limit_h:.0f}h in 'running' — Dead-Man's-Switch")
            except (RunStoreError, OSError):
                continue
            metrics.FABRIK_REAPED_RUNS_TOTAL.inc(reason="stale")
            print(f"[fabrik] STALE RUN {r['id']} ({age_h:.1f}h) → error")
            if projects is None:
                projects = load_projects()
            spec = projects.get(r["project"], {})
            chain_info = retry_chains.on_run_finished_safe(spec, r["project"], r.get("loop", ""), r)
            sent = _notify(spec, r, retry_state=chain_info.get("state"))
            if sent:
                print(f"[fabrik] Alerts gesendet: {sent}")
