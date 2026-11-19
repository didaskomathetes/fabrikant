"""Run-Records: Run-Slots (max_parallel, Dedup, Vorrang), Verzeichnisse, List/Get.

Gelesen wird ausschließlich über den Run-Store (:data:`fabrik.runstore.STORE`, ADR 0034) — JSON- oder
SQL-Backend. Run-Logs (``runs/<projekt>/<id>.log``) bleiben Dateien."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import TypedDict

from . import config, metrics, sessionlog
from .i18n import t
from .runmodel import Run, RunStatus, StepStatus
from .runstore import STORE, RecordError, RunStoreError, parse_run, valid_project, valid_run_id

LOCK_KEY_SEP = "::"  # trennt Projekt und Loop im Scope-Schlüssel; in Namen verboten (specs._validate_spec)

# Status-Marke je Step-Status (CLI `runs show`, `fabrik run`).
STEP_MARKS = {"pass": "✔", "fail": "✖", "timeout": "⏱", "error": "✖", "waiting": "⏸", StepStatus.SKIPPED.value: "–"}


def uses_worktree(loop: dict) -> bool:
    """Loop braucht einen Run-Worktree (Default). ``worktree: false`` = nein (ADR 0020)."""
    return loop.get("worktree", True) is not False


def loop_lock_key(project: str, loop_name: str, loop: dict | None) -> str:
    """Scope-Schlüssel eines Loops aus dessen Spec-Definition (ADR 0028/0020).

    Worktree-Loops teilen die Slots des Projekts (Schlüssel = Projektname,
    begrenzt durch ``max_parallel``). Loops mit ``worktree: false`` belegen
    keinen Slot: eigener Schlüssel pro Loop, sie laufen parallel zu allen
    anderen Loops. Unbekannter Loop (``None``) → Projekt-Scope (sicherer Default)."""
    if isinstance(loop, dict) and not uses_worktree(loop):
        return f"{project}{LOCK_KEY_SEP}{loop_name}"
    return project


def lock_key(project: str, loop_name: str) -> str:
    """Scope-Schlüssel für ``project``/``loop_name`` nach aktuellem Spec-Stand.

    Unbekanntes Projekt/Loop oder defekte Spec → Projekt-Scope."""
    from .specs import load_projects  # lokal: specs → branches → … (Import-Zyklus vermeiden)

    spec = load_projects().get(project) or {}
    loop = (spec.get("loops") or {}).get(loop_name) if not spec.get("_error") else None
    return loop_lock_key(project, loop_name, loop)


class RunClaim:
    """Belegter Run-Slot (ADR 0028); `release()` ist idempotent."""

    def __init__(self, project: str, scope: str, key: tuple[str, str, str]):
        self.scope = scope
        self._project, self._key, self._released = project, key, False

    @property
    def uses_slot(self) -> bool:
        """True, wenn der Claim einen ``max_parallel``-Slot belegt (Worktree-Loop)."""
        return self.scope == self._project

    def release(self) -> None:
        with _slots_cond:
            if self._released:
                return
            self._released = True
            _active_keys.discard(self._key)
            if self.uses_slot:
                _slots_used[self._project] = max(0, _slots_used.get(self._project, 0) - 1)
            _slots_cond.notify_all()


_slots_cond = threading.Condition()
_slots_used: dict[str, int] = {}
_active_keys: set[tuple[str, str, str]] = set()
# Vorrang-Warter pro Scope (freigegebene Resumes, eingereihte manuelle Runs): neue
# Trigger-Runs weichen ihnen aus, sonst verhungern sie (ADR 0015).
_priority_waiters: dict[str, int] = {}


def max_parallel_for(project: str, spec: dict | None = None) -> int:
    """Erlaubte parallele Worktree-Runs eines Projekts.

    Aus `max_parallel` der Spec (Default `config.DEFAULT_MAX_PARALLEL`). Specs
    ohne `repo:` arbeiten ohne Isolation direkt im `root` → fest 1 (ADR 0028).
    `spec` bereits geladen → kein erneutes `load_projects()`."""
    from .specs import load_projects

    try:
        if spec is None:
            spec = load_projects().get(project) or {}
        if spec and not spec.get("repo"):
            return 1
        value = int(spec.get("max_parallel", config.DEFAULT_MAX_PARALLEL))
    except (OSError, ValueError, TypeError):
        value = config.DEFAULT_MAX_PARALLEL
    return max(1, value)


def dedupe_key(variables: dict | None) -> str:
    """Issue-/PR-Bezug eines Runs ("" = Loop ohne Bezug, z. B. Cron)."""
    for k in config.DEDUPE_VARS:
        if variables and variables.get(k) not in (None, ""):
            return f"{k}={variables[k]}"
    return ""


def mark_priority_waiter(scope: str, delta: int) -> None:
    """Vorrang-Warter eines Scopes (:func:`lock_key`) zählen (+1 beim Einreihen, -1 bei Abbruch)."""
    with _slots_cond:
        n = _priority_waiters.get(scope, 0) + delta
        if n > 0:
            _priority_waiters[scope] = n
        else:
            _priority_waiters.pop(scope, None)
        _slots_cond.notify_all()


def paused_dedupe_keys(project: str, loop: str, exclude_run_id: str | None = None) -> set[str]:
    """Issue-/PR-Bezüge (:func:`dedupe_key`) pausierter `needs_approval`-Runs von Loop `loop`.

    `exclude_run_id` lässt den Run selbst aus, der gerade fortgesetzt wird —
    sein eigener Record würde sonst das Resume blockieren."""
    keys: set[str] = set()
    for r in STORE.load_records(project, RunStatus.NEEDS_APPROVAL):
        if r.get("id") == exclude_run_id or r.get("loop") != loop:
            continue
        vars_ = r.get("vars")
        key = dedupe_key(vars_ if isinstance(vars_, dict) else None)
        if key:
            keys.add(key)
    return keys


def claim_run(
    project: str,
    loop: str,
    variables: dict | None = None,
    *,
    blocking: bool = False,
    registered: bool = False,
    exclude_run_id: str | None = None,
) -> RunClaim | None:
    """Reserviert einen Run (ADR 0028): Dedup + Slot (max_parallel) für Worktree-Loops.

    None, wenn für Loop + Issue/PR bereits ein Run aktiv oder pausiert
    (`needs_approval`) ist (Dedup), das Limit
    erreicht ist oder ein Vorrang-Warter im selben Scope wartet (ADR 0015) und
    `blocking=False`. `blocking=True` ist ein Vorrang-Claim: er wartet auf einen
    freien Slot **und** auf das Ende eines aktiven Runs desselben Loops +
    Issue/PR (Resume nach Approval, eingereihter manueller Run), und neue
    nicht-blockierende Claims weichen ihm solange aus. ``registered=True``: der
    Aufrufer hat sich schon synchron per :func:`mark_priority_waiter` eingetragen
    (vor dem Thread-Start, damit Trigger-Runs sofort ausweichen); der Eintrag
    wird hier wieder abgebaut. `exclude_run_id` (Resume): der fortgesetzte Run
    selbst blockiert seinen eigenen Claim nicht."""
    scope = lock_key(project, loop)
    needs_slot = scope == project
    limit = max_parallel_for(project) if needs_slot else 0
    key = (project, loop, dedupe_key(variables))
    # Pausierte Runs geben ihren Claim frei, belegen den Bezug aber weiter; der
    # Resume (blocking bzw. exclude_run_id) ist ausgenommen. Datei-I/O bewusst
    # außerhalb des Locks.
    if not blocking and key[2] and key[2] in paused_dedupe_keys(project, loop, exclude_run_id):
        return None
    with _slots_cond:
        if blocking and not registered:
            _priority_waiters[scope] = _priority_waiters.get(scope, 0) + 1
        try:
            while True:
                free = key not in _active_keys and (not needs_slot or _slots_used.get(project, 0) < limit)
                if free and (blocking or not _priority_waiters.get(scope)):
                    break
                if not blocking:
                    return None
                _slots_cond.wait()
        finally:
            if blocking:
                n = _priority_waiters.get(scope, 0) - 1
                if n > 0:
                    _priority_waiters[scope] = n
                else:
                    _priority_waiters.pop(scope, None)
        _active_keys.add(key)
        if needs_slot:
            _slots_used[project] = _slots_used.get(project, 0) + 1
    return RunClaim(project, scope, key)


def has_priority_waiter(scope: str) -> bool:
    """True, wenn im Scope (:func:`lock_key`) ein Vorrang-Claim wartet."""
    with _slots_cond:
        return bool(_priority_waiters.get(scope))


def slot_snapshot() -> dict:
    """Slots & Wartende je Scope (Schedule-View): used/limit-Kontext ohne Spec-Zugriff.

    `slots_used`/`priority_waiters` pro Scope, `active` = aktive (project, loop,
    dedupe)-Keys — die UI kann daraus ableiten, auf was ein wartender Run
    konkret wartet (Slot frei? gleicher Loop+Issue aktiv?)."""
    with _slots_cond:
        return {
            "slots_used": dict(_slots_used),
            "active": [list(k) for k in _active_keys],
            "priority_waiters": {k: v for k, v in _priority_waiters.items() if v},
        }


def runs_dir_for(project: str) -> Path:
    d = config.RUNS_DIR / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_runs(project: str | None = None) -> list[Run]:
    """Typisierte Runs aller bzw. eines Projekts (unsortiert); nicht ladbare Records werden übersprungen.

    Lesepfad des :class:`~fabrik.runstore.RunStore`; ``project`` ist der Verzeichnisname. Nicht ladbare
    Records (mit Grund) liefert ``STORE.load_report()``."""
    return STORE.load_all(project)


def find_run(run_id: str, project: str | None = None) -> Run | None:
    """Typisierter Run per Id (optional im Projekt); None, wenn unbekannt oder nicht ladbar."""
    return STORE.get(run_id, project)


def pending_approval_runs(project: str | None = None) -> list[Run]:
    """Runs im Status ``needs_approval`` (Freigabe offen oder eingereiht), neueste zuerst.

    Typisiertes Pendant zu :func:`pending_approvals` (``list[dict]``). Die dict-basierte
    Funktion wird in Schritt 2 (ADR 0034, #276) durch diese ersetzt und entfernt —
    Caller sollen :func:`pending_approval_runs` verwenden."""
    out = [r for r in load_runs(project) if r.status is RunStatus.NEEDS_APPROVAL]
    out.sort(key=lambda r: r.started, reverse=True)
    return out


class LastRun(TypedDict):
    """JSON-Projektion des jüngsten Runs eines Loops (``failure_kind`` ``""`` = keiner)."""

    status: str
    finished: str
    id: str
    failure_kind: str


def last_run_per_loop(runs: Iterable[Run]) -> dict[str, LastRun]:
    """Jüngster Run (nach ``started``) je Loop als {loop: LastRun}.

    Erwartet *alle* Runs eines Projekts (:func:`load_runs` bzw. :func:`parse_runs`),
    nicht nur die N neuesten — sonst erscheinen selten laufende Loops in der UI
    als "nie gelaufen". Runs ohne Loop werden übersprungen; nicht ladbare Records
    (fehlende ``id``, unbekannter Status) wurden beim Parsen bereits verworfen."""
    newest: dict[str, Run] = {}
    for r in runs:
        cur = newest.get(r.loop)
        if r.loop and (cur is None or r.started > cur.started):
            newest[r.loop] = r
    return {
        loop: {
            "status": r.status.value,
            "finished": r.finished,
            "id": r.id,
            "failure_kind": r.failure_kind.value if r.failure_kind else "",
        }
        for loop, r in newest.items()
    }


def _started_key(r: dict) -> str:
    """Sortierschlüssel: `started` als String (beschädigte Nicht-Strings sortieren als leer)."""
    started = r.get("started")
    return started if isinstance(started, str) else ""


def list_runs(project: str | None = None, limit: int | None = 50) -> list[dict]:
    """Run-Records (ohne steps_output), neueste zuerst; limit=None = alle."""
    # Alle Runs sammeln, dann global nach Startzeit sortieren und erst dann
    # truncieren. (Früher: early-return pro Projekt-Verzeichnis → bei mehreren
    # Projekten landeten die "neuesten" des ersten Verzeichnisses vorne, egal
    # wie alt sie waren.)
    out = STORE.load_records(project)
    out.sort(key=_started_key, reverse=True)
    return out[:limit]


def load_records(project: str | None = None) -> list[dict]:
    """Rohe Run-Records (ohne steps_output) aller bzw. eines Projekts, unsortiert."""
    return STORE.load_records(project)


def parse_runs(records: Iterable[dict], project: str) -> list[Run]:
    """Rohe Records von `project` → typisierte Runs; nicht ladbare werden übersprungen (wie :func:`load_runs`)."""
    out = []
    for rec in records:
        try:
            out.append(parse_run(rec, project))
        except RecordError:
            continue
    return out


def select_pending_approvals(records: Iterable[dict]) -> list[dict]:
    """Records im Status `needs_approval` mit gültiger `id` aus `records`, neueste zuerst."""
    out = [r for r in records if r.get("status") == "needs_approval" and isinstance(r.get("id"), str) and r["id"]]
    out.sort(key=_started_key, reverse=True)
    return out


def pending_approvals(project: str | None = None) -> list[dict]:
    """Offene Freigabe-Anfragen: Runs im Status `needs_approval`, neueste zuerst.

    .. note::
        Typisiertes Pendant: :func:`pending_approval_runs` (``list[Run]``). Diese
        dict-basierte Funktion wird in Schritt 2 (ADR 0034, #276) entfernt —
        neue Caller sollen :func:`pending_approval_runs` verwenden.

    `steps_output` wird entfernt wie bei :func:`list_runs`. project=None →
    alle Projekte. Die UI zeigt daraus die Anzahl pro Projekt und die
    aufklappbare Liste (Klick öffnet das Run-Log-Modal mit dem
    Approval-Kontext). Records ohne gültige (nicht-leere String-)`id` werden
    übersprungen, damit ein beschädigtes Record `/api/projects` nicht kippt.
    Wer die Records ohnehin geladen hat, nimmt :func:`select_pending_approvals`."""
    return select_pending_approvals(load_records(project))


def loop_run_triggers(project: str, loop: str, statuses: tuple[str, ...] | None = None) -> set[str]:
    """Trigger-Strings (z. B. "on_issue:#12") aller Runs von `project`/`loop`.

    Persistente Quelle für Reconcile-Trigger (überlebt Restarts). `statuses`
    filtert optional nach Run-Status; ohne Filter zählt jeder Run. Nicht
    lesbare Records überspringt der Store; unerwartete Fehler (z. B.
    PermissionError, DB-Fehler) werden NICHT geschluckt, sondern an den
    aufrufenden Fehlerpfad propagiert (on_issue-Poll bzw. Scheduler-Loop) —
    ein unvollständiges Set würde die Reconcile-Idempotenz brechen (Doppel-Run)."""
    if not valid_project(project):
        return set()
    return STORE.loop_triggers(project, loop, statuses)


def reap_orphaned_runs() -> list[str]:
    """Server-Start: verwaiste 'running'-Records → error (kein Alert, nur Log).

    Single-Node: Runs leben nur im Server-Prozess, ein 'running'-Record vor dem
    Start ist daher tot. `needs_approval` bleibt (pausiert, fortsetzbar).
    Jeder bereinigte Run aktualisiert seine Retry-Kette (ADR 0036: der
    Neustart verbraucht den Versuch, der nächste wird nach Backoff geplant).
    Gibt die bereinigten Run-Ids zurück."""
    from . import retry_chains  # lokal: retry_chains importiert get_run aus diesem Modul
    from .specs import load_projects

    reaped: list[str] = []
    projects: dict[str, dict] | None = None  # einmal laden, erst wenn ein Run bereinigt wird
    try:
        running = STORE.load_records(status=RunStatus.RUNNING)
    except (OSError, RunStoreError) as e:
        print(t("runs.fabrik_orphaned_runs_unreadable", e=e))
        running = []
    for r in running:
        project, run_id = r["project"], r.get("id", "")
        # Über den Store: Vorbedingung `running` — ein Run, der inzwischen einen
        # anderen Status hat, wird nicht überschrieben (PreconditionError → überspringen).
        try:
            r = STORE.reap(project, run_id, error=t("runs.restart_error"))
        except (RunStoreError, OSError):
            continue
        sessionlog.finish_orphaned_run(run_id, r["finished"])
        if projects is None:
            projects = load_projects()
        retry_chains.record_run_end(project, r, projects)
        metrics.FABRIK_REAPED_RUNS_TOTAL.inc(reason="restart")
        reaped.append(run_id)
        print(f"[fabrik] verwaister Run {run_id} → error (Restart)")
    # Übrige Claims von Runs, die nicht mehr laufen, freigeben (ADR 0036,
    # Entscheidung 3) — sonst würde die Kette für immer claimed bleiben.
    retry_chains.release_stale_claims()
    return reaped


_valid_run_id = valid_run_id
_valid_project = valid_project


def get_run(run_id: str, project: str | None = None) -> dict | None:
    """Roher Run-Record per Id; zuerst im Projekt, sonst über alle Projekte (Fallback).

    None, wenn Id/Projekt ungültig, der Run unbekannt oder sein Record defekt ist (wird geloggt)."""
    if not _valid_run_id(run_id) or (project and not _valid_project(project)):
        return None
    try:
        found = STORE.get_record(run_id, project) if project else None
        return found if found is not None else STORE.get_record(run_id)
    except RunStoreError as e:
        print(t("runs.fabrik_run_unreadable", run_id=run_id, e=e))
        return None


def export_run(run_id: str, project: str | None = None) -> dict | None:
    """Run-Record + Session-Log als JSON-fähiges Dict (``fabrik runs export``).

    ``session_log`` enthält ``events``/``checkpoints``/``artifacts`` (beide Backends).
    None, wenn der Run unbekannt ist (wie :func:`get_run`)."""
    rec = get_run(run_id, project)
    if rec is None:
        return None
    return {**rec, "session_log": sessionlog.session_log_dump(run_id)}


def show_run(run_id: str, project: str | None = None) -> str | None:
    """Menschlich lesbarer Einzelrun (``fabrik runs show``): Felder, Steps mit Status-Marke,
    Session-Log-Zählungen. None, wenn der Run unbekannt ist (wie :func:`get_run`)."""
    rec = get_run(run_id, project)
    if rec is None:
        return None
    lines = [
        f"{rec['id']}  {rec['project']}/{rec['loop']}  {rec['status']}",
        t("runs.started_ended", v1=rec.get("started", ""), v2=rec.get("finished", "")),
        f"  Trigger: {rec.get('trigger', '')}" + (f"  Notiz: {rec['note']}" if rec.get("note") else ""),
    ]
    for s in rec.get("steps", []):
        lines.append(
            f"  {STEP_MARKS.get(s.get('status'), '?')} {s.get('name', '')} [{s.get('type', '')}] {s.get('summary', '')}"
        )
    log = sessionlog.session_log_dump(run_id)
    lines.append(
        f"  Session-Log: {len(log['events'])} Events, {len(log['checkpoints'])} Checkpoints, "
        f"{len(log['artifacts'])} Artefakte"
    )
    return "\n".join(lines)


def get_run_log_path(run_id: str, project: str | None = None) -> Path | None:
    """Pfad der Log-Datei (`runs/<projekt>/<id>.log`); None bei ungültiger Id/Projekt oder fehlender Datei."""
    if not _valid_run_id(run_id):
        return None
    if project:
        if not _valid_project(project):
            return None
        f = config.RUNS_DIR / project / f"{run_id}.log"
        return f if f.is_file() else None
    if config.RUNS_DIR.is_dir():
        for d in config.RUNS_DIR.iterdir():
            f = d / f"{run_id}.log"
            if f.is_file():
                return f
    return None


def get_run_log(run_id: str, project: str | None = None) -> str | None:
    """Rohes Run-Log (`runs/<projekt>/<id>.log`) als Text.

    None, wenn die Id ungültig ist oder keine Log-Datei existiert (z. B.
    Runs, die vor dem Log-Feature gelaufen sind)."""
    f = get_run_log_path(run_id, project)
    return f.read_text(encoding="utf-8") if f else None
