"""Run-Engine: run_loop (Gate/Approval/Resume), Step-Dispatch, Worker-Pool.

Stateless pro Run: alle Laufzeitdaten im Record (runs/<projekt>/<id>.json),
keine In-Memory-Session-State. Das ist die P2-Voraussetzung (stateless Worker).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import agent_state, config, connections, pause, retry_chains
from .approval import ApprovalRequest
from .context import _PLACEHOLDER_RE, _cap, _cond_matches, _substitute, tier_policy
from .failures import Failure, FailureKind, SetupError, classify_exception, failed, from_result, run_failure
from .gitops import (
    WorktreeLease,
    _worktree_sync_check,
    cleanup_run_worktree,
    ensure_worktree,
    project_github,
    run_worktree_path,
)
from .launcher import LauncherStatus, RunLauncher, create_launcher
from .runmodel import FAILED_RUN_STATUSES, RunStatus, StepStatus
from .runs import (
    RunClaim,
    claim_run,
    get_run,
    list_runs,
    lock_key,
    loop_lock_key,
    loop_run_triggers,
    mark_priority_waiter,
    runs_dir_for,
    uses_worktree,
)
from .runstore import STORE, PreconditionError, RunStoreError
from .secrets import _redact, _runtime_secrets
from .sessionlog import SessionLog, session_totals
from .specs import load_projects
from .steps import (
    _notify,
    _step_agent,
    _step_alert_route,
    _step_escalation,
    _step_git,
    _step_http,
    _step_human_gate,
    _step_issue,
    _step_llm,
    _step_notify,
    _step_pr,
    _step_pr_merge,
    _step_pr_review,
    _step_review,
    _step_shell,
    _step_state,
    _step_verify,
)
from .steps.extra_context import effective_step


def _trigger_link(spec: dict, trigger: str, kind: str, path: str) -> str:
    """GitHub-Link zum Trigger-Objekt (``on_<kind>:#N``, optional ``@sha``), falls Repo bekannt."""
    m = re.match(rf"on_{kind}:#(\d+)", trigger or "")
    repo = spec.get("github")
    if not m or not repo:
        return ""
    return f"https://github.com/{repo}/{path}/{m.group(1)}"


def _issue_link(spec: dict, trigger: str) -> str:
    """GitHub-Link zum Trigger-Issue (on_issue:#N), falls Repo bekannt.

    Nur für on_issue-Triggers; sonst leer (kein Issue-Kontext). Der Link gibt
    dem Approver/Debugging den direkten Weg zum Ursprung des Runs."""
    return _trigger_link(spec, trigger, "issue", "issues")


def _pr_link(spec: dict, trigger: str, run_ctx: dict | None) -> str:
    """Link zur PR des Runs: `pr_url` aus den Run-Variablen (on_pr-Scheduler,
    pr-Step), sonst aus dem on_pr-Trigger + Spec-Repo (ältere Runs ohne `pr_url`)."""
    url = str((run_ctx or {}).get("pr_url") or "")
    return url or _trigger_link(spec, trigger, "pr", "pull")


def _format_permissions(perms: list[dict | str] | None) -> list[str]:
    """Permission-Rules (step.permissions) als lesbare Liste, z. B. 'edit * → allow'.

    Zeigt dem Approver, WELCHE Rechte der Agent-Schritt bekommt (Containment)."""
    out = []
    for p in perms or []:
        if isinstance(p, dict):
            out.append(f"{p.get('permission', '?')} {p.get('pattern', '*')} → {p.get('action', '?')}")
        else:
            out.append(str(p))
    return out


def _as_list(v: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Spec-Wert als Liste (``labels: x`` und ``labels: [x, y]`` sind beide erlaubt)."""
    if v in (None, ""):
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _issue_intent(step: dict) -> str:
    """Issue-Step konkret: Nummer + Label-Diff, damit der Approver sieht, was sich ändert
    (z. B. ``Issue #42: +ready for development −needs approval``)."""
    head = f"Issue #{step['number']}" if step.get("number") else f"Issue {step.get('repo', '')}"
    action = step.get("action", "")
    if action != "label":
        return f"{head}: {action}"
    diff = [f"+{lbl}" for lbl in _as_list(step.get("labels"))]
    diff += [f"−{lbl}" for lbl in _as_list(step.get("remove_labels"))]
    return f"{head}: {' '.join(diff) or 'Labels unverändert'}"


def _step_intent(step: dict, run_ctx: dict | None = None) -> str:
    """Kurzbeschreibung, was der Step tun wird (Approval-Kontext: 'was').

    Deterministisch aus der Spec abgeleitet — kein LLM, kein untrusted Content.
    Mit run_ctx werden {Variablen} (z. B. {issue_number}, {label}) auf die
    Laufzeitwerte aufgelöst, damit der Approver den tatsächlichen Befehl sieht.
    Unbekannte Platzhalter bleiben unverändert (sicher: kein Crash, kein Falschwert)."""
    t = step.get("type")
    if t == "shell":
        cmd = " ".join(str(step.get("cmd", "")).split())
        intent = f"Shell-Befehl: {cmd}"
    elif t == "agent":
        with contextlib.suppress(connections.InvalidConnection):
            step = connections.apply_to_step(step, with_secret=False)  # nur Anzeige: kein Entschlüsseln
        backend = (step.get("backend") or os.environ.get("FABRIK_AGENT_BACKEND", "")).strip()
        model = (step.get("model") or os.environ.get("FABRIK_AGENT_MODEL", "")).strip()
        bits = [f"Agent-Backend: {backend or '(unset)'}"]
        if model:
            bits.append(f"model: {model}")
        intent = " · ".join(bits)
    elif t == "git":
        parts = []
        if step.get("fetch"):
            parts.append("fetch")
        if step.get("branch"):
            parts.append(f"branch {step['branch']}")
        if step.get("commit"):
            parts.append("commit")
        if step.get("push"):
            parts.append("push")
        intent = f"Git: {', '.join(parts) or 'kein-op'}"
    elif t == "pr":
        intent = f"PR anlegen: {step.get('repo', '')} (base {step.get('base', 'main')})"
    elif t == "http":
        intent = f"HTTP {step.get('method', 'GET')} {step.get('url', '')}"
    elif t == "llm":
        intent = "LLM-Call"
    elif t == "review":
        intent = "Cross-Model-Review"
    elif t == "pr_review":
        intent = f"PR-Review (Reviewer-Identity): {step.get('repo', '')}"
    elif t == "pr_merge":
        intent = f"PR mergen ({step.get('method', 'squash')}): {step.get('repo', '')}"
    elif t == "issue":
        intent = _issue_intent(step)
    elif t == "human_gate":
        intent = "Kritizitäts-Check (menschliche Freigabe bei kritisch)"
    elif t == "escalation":
        intent = "Eskalation an einen Menschen (ADR 0059): warten, dann fortsetzen"
    elif t == "report":
        intent = f"Report: {step.get('write', '(nur Log)')}"
    elif t == "notify":
        intent = "Benachrichtigung"
    elif t == "state":
        intent = f"State {step.get('action', '')} {step.get('key', '')}"
    elif t == "verify":
        intent = "Verify-Checks"
    else:
        intent = f"Step-Typ: {t}"
    if run_ctx:
        intent = _substitute(intent, run_ctx)
    return intent


def _approval_context(
    spec: dict,
    trigger: str,
    *,
    reason: str,
    step: dict | None = None,
    tier: int | None = None,
    run_ctx: dict | None = None,
    condition: str = "",
) -> ApprovalRequest:
    """Freigabe-Anfrage: was passiert, warum, welche Rechte, welches Issue/welche PR.

    Wird in den Run-Record geschrieben (record['approval']) und in Log/UI/Chat
    angezeigt, damit der Approver weiß, WAS freigegeben wird und WARUM
    (welcher Step, welche Agent-Rechte, welches auslösende Issue, welche PR, welches Tier).
    run_ctx löst {Variablen} im 'was'-Text auf (z. B. {issue_number} → 42).
    Das optionale Step-Feld `approval_reason` (Template, z. B. "{reasoning}" aus
    einem vorherigen llm-Step) liefert die fachliche Begründung ('warum')."""
    pr = _pr_link(spec, trigger, run_ctx)
    req = ApprovalRequest(
        reason=reason,
        condition=condition,
        issue=_issue_link(spec, trigger),
        pr=pr,
        pr_title=str((run_ctx or {}).get("pr_title") or "") if pr else "",
    )
    if step is not None:
        req = replace(
            req,
            step=step.get("name", ""),
            why=_approval_reason(step, run_ctx),
            what=_step_intent(step, run_ctx),
            permissions=_format_permissions(step.get("permissions")),
        )
    if tier is not None:
        req = replace(req, tier=tier, what=f"Tier {tier} — Loop endet erst nach Freigabe (irreversibel/Accountability)")
    return req


def _approval_condition(step: dict, ctx: dict) -> str | None:
    """Ob der Step VOR seiner Ausführung pausiert — und warum.

    None → kein Gate. "" → statisches `approval: true`. Sonst die erfüllte
    Bedingung aus `approval_when` (ADR 0017), z. B. "label = close":
    `approval_when: {var: wert | [werte]}` pausiert nur, wenn JEDE Variable
    (AND) einen der Werte (OR) hat. Fehlende Variable → keine Übereinstimmung."""
    if step.get("approval"):
        return ""
    cond = step.get("approval_when")
    if not cond:
        return None
    if not _cond_matches(cond, ctx):
        return None
    return ", ".join(f"{var} = {ctx[var]}" for var in cond)


def _approval_reason(step: dict, run_ctx: dict | None) -> str:
    """`approval_reason` des Steps mit aufgelösten {Variablen}; "" wenn nicht gesetzt.

    Fehlt eine referenzierte Variable im Run, ergibt das "" statt eines rohen
    "{reasoning}" im Freigabe-Kontext."""
    tmpl = step.get("approval_reason")
    run_ctx = run_ctx or {}
    if not isinstance(tmpl, str) or any(v not in run_ctx for v in _PLACEHOLDER_RE.findall(tmpl)):
        return ""
    return _cap(_substitute(tmpl, run_ctx).strip())


def _emit_approval(emit, approval: ApprovalRequest) -> None:
    """Approval-Kontext in das Run-Log schreiben (mehr Kontext als nur der Step-Name)."""
    for line in approval.lines():
        emit(f"  {line}")


def _persist_vars(record: dict, ctx: dict) -> None:
    """Run-Variablen in den Record persistieren (`record['vars']`).

    Resume ist stateless: nach einer Approval-Pause ist der In-Memory-ctx weg,
    der Record ist die EINZIGE Quelle für {Variablen} ({issue_number}, {label},
    LLM-Outputs, …). Nur JSON-serialisierbare Werte landen im Record; `log`
    (SessionLog) fällt dadurch raus. `last_agent_output` wird gecappt, damit
    der Record klein bleibt (einziger Konsument: review-Step)."""
    record["vars"] = {
        k: (_cap(str(v)) if k == "last_agent_output" else v)
        for k, v in ctx.items()
        if isinstance(v, (str, int, float, bool))
    }


@dataclass
class _Run:
    """Laufzeit-Bündel eines run_loop-Aufrufs (Record, Log, Emit, Pfade)."""

    spec: dict
    project: str
    trigger: str
    run_id: str
    run_dir: Path
    record: dict
    log: SessionLog
    emit: Callable[[str], None]
    ctx: dict = field(default_factory=dict)
    worktree: bool = True  # False: Scratch-Workdir statt Run-Worktree (ADR 0020)
    base: Path | None = None  # Workdir des Runs, sobald angelegt
    state_snapshot: dict = field(default_factory=dict)  # Agent-State-Stand beim Start (ADR 0028)
    aborted_by: int | None = None  # Index des Steps, der den Run abgebrochen hat (ADR 0035)
    abort_failure: Failure | None = None  # Klasse eines Run-Abbruchs außerhalb der Steps
    lease: WorktreeLease | None = None  # Heartbeat der Worktree-Lease, solange der Run lebt (ADR 0028)


# record["status"] bei gate-Abbruch je Step-Outcome
_ABORT_STATUS = {"fail": "fail", "timeout": "fail", "error": "error"}
# Step-Status für Steps hinter einem `done`-Step (Run vorzeitig erfolgreich beendet).
STEP_SKIPPED = StepStatus.SKIPPED.value


def _persist(project: str, run_id: str, record: dict) -> None:
    """Run-Record eines laufenden Runs über den Store schreiben (Checkpoint, Pause, Ende).

    Vorbedingung: On-Disk-Status ``running`` (Compare-and-Set) und erlaubter Übergang laut
    ``RUN_TRANSITIONS``. Hat ein Reaper/Stale-Check den Run inzwischen beendet, wirft der
    Store :class:`PreconditionError` — die Engine überschreibt dessen Ergebnis nicht."""
    STORE.write(project, run_id, record, expect=RunStatus.RUNNING)


def _pause_for_approval(rn: _Run, waiting_step: str, approval: ApprovalRequest, message: str) -> dict:
    """Run in `needs_approval` parken (gemeinsamer Pfad für Step-/Gate-/Tier-Approval)."""
    record = rn.record
    record["status"] = "needs_approval"
    record["waiting_step"] = waiting_step
    record["approval"] = approval.to_dict()
    _persist_vars(record, rn.ctx)
    _persist(rn.project, rn.run_id, record)
    rn.log.event("approval", step_name=waiting_step)
    rn.log.set_status("needs_approval")
    rn.emit(message)
    _emit_approval(rn.emit, approval)
    return record


def _approve_hint(rn: _Run, sname: str) -> str:
    return f"⏸ {sname} wartet auf Approval: `fabrik approve {rn.project} {rn.run_id}`"


def _init_record(
    spec: dict,
    project_name: str,
    loop: dict,
    loop_name: str,
    trigger: str,
    note: str,
    resume_id,
    run_id,
    run_dir,
    actor: str | None = None,
    attempt: int = 0,
    retry_of: str = "",
    chain_key: str = "",
) -> tuple[dict, str, int]:
    """Record laden (Resume) bzw. neu anlegen; liefert (record, run_id, start_i).

    ``attempt``/``retry_of``/``trigger_key`` (ADR 0036) werden nur bei neuen
    Records gesetzt; ein Resume trägt die Werte des ursprünglichen Runs
    bereits. ``trigger_key`` ist der kanonische Ketten-Schlüssel: bei
    Retry-Versuchen der Schlüssel der claimenden Kette (``chain_key``), sonst
    aus dem Trigger-String berechnet — das Run-Ende bucht die Kette über das
    Record-Feld, nicht über die (zwischenzeitlich geänderte) Spec."""
    start_i = 0
    if resume_id:
        record = get_run(resume_id, project_name)
        if not record:
            raise RuntimeError(f"Resume: Run {resume_id} nicht gefunden")
        if record["status"] != "needs_approval":
            raise RuntimeError(f"Resume: Run {record['id']} ist '{record['status']}', nicht needs_approval")
        run_id = record["id"]
        record.setdefault("started_by", None)
        record.setdefault("approved_by", None)
        record.setdefault("rejected_by", None)
        record["status"] = "running"
        record["resumed"] = True
        for i, s in enumerate(record["steps"]):
            if s["status"] != "pass":
                start_i = i
                break
        if record.get("waiting_step") == "tier-approval":
            start_i = len(loop["steps"])  # synthetischer Tier-Gate: alle echten Steps überspringen
    else:
        run_id = run_id or f"{config.now_id()}-{loop_name}"
        spec_hash = ""
        with contextlib.suppress(Exception):
            spec_hash = hashlib.sha256(Path(spec["_path"]).read_bytes()).hexdigest()[:12]
        record = {
            "id": run_id,
            "project": project_name,
            "loop": loop_name,
            "trigger": trigger,
            "note": note,
            "spec_hash": spec_hash,
            "started": config.utcnow().isoformat(),
            "status": "running",
            "steps": [],
            "started_by": actor,
            "approved_by": None,
            "rejected_by": None,
            "attempt": attempt,
            "retry_of": retry_of,
            "trigger_key": chain_key or retry_chains.trigger_key(spec, loop_name, trigger) or "",
        }
    # Neu: Datei darf fehlen oder ein queued-Platzhalter sein (den der Start überschreibt);
    # Resume: On-Disk-Status muss needs_approval sein (Compare-and-Set → genau ein Resume
    # gewinnt, ein gleichzeitiger Reject schließt ihn aus).
    if resume_id:
        STORE.write(project_name, run_id, record, expect=RunStatus.NEEDS_APPROVAL)
    else:
        STORE.write(project_name, run_id, record, allow_missing=True)
    return record, run_id, start_i


def _build_ctx(rn: _Run, loop_name: str, note: str, resume: bool, initial_vars: dict | None) -> dict:
    """Variablen-Kontext aufbauen (Worktree, Resume-Vars, Env, Trigger-Vars)."""
    spec = rn.spec
    if rn.worktree:
        if spec.get("repo"):
            # Lease vor dem Worktree: ein fremder Cleanup sieht ihn nie ohne Lease (ADR 0028)
            rn.lease = WorktreeLease(run_worktree_path(rn.project, rn.run_id))
            rn.lease.start()
        base = ensure_worktree(spec, rn.run_id)
        rn.base = base
        if spec.get("repo"):
            rn.state_snapshot = agent_state.seed(rn.project, base)
    else:
        # ADR 0020: kein Worktree → leere Run-Workdir, kein Projekt-Lock.
        base = config.SCRATCH_DIR / rn.run_id
        if base.exists():
            cleanup_error = _cleanup_scratch(rn.run_id, rn.emit)
            if cleanup_error:
                rn.record["scratch_cleanup_error"] = cleanup_error
                rn.log.event("warning", data={"message": cleanup_error})
        base.mkdir(parents=True, exist_ok=True)
        rn.base = base
    # Resume: Run-Variablen aus dem Record als Basis (stateless — der
    # In-Memory-ctx vor der Pause ist weg). Frisch abgeleitete Werte
    # (Base-Keys, env, workdir, branch, head) gewinnen bewusst dagegen.
    ctx: dict = {k: str(v) for k, v in (rn.record.get("vars") or {}).items()} if resume else {}
    rn.ctx = ctx  # früh binden: der finale Record-Write persistiert ctx auch bei Fehlern
    ctx.update(
        {
            "project": rn.project,
            "root": spec["root"],
            "spec_path": spec.get("_path", ""),  # Basis für spec-relative Pfade (prompt_file, ADR 0047)
            "workdir": str(base),
            "loop": loop_name,
            "trigger": rn.trigger,
            "note": note,
            "run_id": rn.run_id,
            # fabrik-Checkout: Prompts fremder Projekte rufen damit fabriks Helfer-Scripts
            # (z. B. scripts/pr-status.py) aus ihrem eigenen Worktree auf.
            "fabrik_home": str(config.FABRIK_HOME),
            "log": rn.log,
        }
    )
    ctx.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    for v in (spec.get("env") or {}).values():  # Spec-Env-Values für Masking registrieren
        sv = str(v).strip()
        if sv and len(sv) >= 8 and sv not in _runtime_secrets:
            _runtime_secrets.append(sv)
    ctx["root"] = str(spec["root"])
    if initial_vars:
        ctx.update({k: str(v) for k, v in initial_vars.items()})
    if spec.get("repo") and rn.worktree:
        info = _worktree_sync_check(spec, base)
        ctx["branch"] = info["branch"]
        ctx["head"] = info["head"]
        rn.emit(f"worktree: {base} @ {info['branch']} ({info['head'][:10]})")
    else:
        ctx["branch"] = "-"
        rn.emit(f"workdir: {base}")
    return ctx


def _cleanup_scratch(run_id: str, emit: Callable[[str], None]) -> str | None:
    """Scratch-Workdir entfernen und einen Cleanup-Fehler als Run-Log melden."""

    path = config.SCRATCH_DIR / run_id
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        message = f"⚠ Scratch-Cleanup fehlgeschlagen: {path}: {e}"
        emit(message)
        return message
    return None


def _merge_agent_state(rn: _Run) -> str | None:
    """Agent-State des Runs in den gemeinsamen Stand mergen; Fehler als Meldung (ADR 0028)."""
    if not rn.spec.get("repo") or rn.base is None:
        return None
    try:
        agent_state.merge_back(rn.project, rn.base, rn.state_snapshot)
    except (OSError, ValueError) as e:
        message = f"⚠ Agent-State-Merge fehlgeschlagen: {e}"
        rn.emit(message)
        return message
    return None


def _cleanup_worktree(project: str, run_id: str, emit: Callable[[str], None]) -> str | None:
    """Run-Worktree entfernen, Erfolg prüfen; Fehler/Rest als Meldung (ADR 0028)."""
    try:
        removed = cleanup_run_worktree(project, run_id)
    except (OSError, subprocess.SubprocessError) as e:
        message = f"⚠ Worktree-Cleanup fehlgeschlagen: {e}"
    else:
        if removed:
            return None
        message = f"⚠ Worktree-Cleanup unvollständig: {run_worktree_path(project, run_id)} besteht noch"
    emit(message)
    return message


def cleanup_rejected_run(project: str, run_id: str, emit: Callable[[str], None]) -> dict[str, str]:
    """Aufräumen nach Reject und liefert Cleanup-Fehler für den Run-Record."""
    errors: dict[str, str] = {}
    scratch_error = _cleanup_scratch(run_id, emit)
    if scratch_error:
        errors["scratch_cleanup_error"] = scratch_error
    worktree_error = _cleanup_worktree(project, run_id, emit)
    if worktree_error:
        errors["worktree_cleanup_error"] = worktree_error
    return errors


def reject_run(project: str, run: dict, actor: str | None, emit: Callable[[str], None]) -> dict:
    """Run am Approval-Gate ablehnen (CLI, Chat): atomar ``rejected`` setzen, Workdir aufräumen,
    Cleanup-Fehler nachtragen und die Retry-Kette buchen (abgelehnter Versuch → dismissed, ADR 0036).

    :class:`PreconditionError`, wenn der Run nicht mehr ``needs_approval`` ist (z. B. bereits
    fortgesetzt). Liefert den abgelehnten Record."""
    run_id = run["id"]
    run = STORE.reject(project, run_id, run, actor)
    errors = cleanup_rejected_run(project, run_id, emit)
    if errors:
        run = STORE.update_fields(project, run_id, errors)
    retry_chains.record_run_end(project, run)
    return run


def _finish_workdir(rn: _Run, *, paused: bool) -> None:
    """Run-Ende bzw. Pause: Workdir aufräumen (ADR 0020/0028).

    Scratch-Workdir: immer entfernen. Run-Worktree: Agent-State mergen (auch bei
    der Pause), entfernen außer bei `needs_approval` (Resume nutzt ihn weiter).
    Merge und Cleanup werden getrennt behandelt; Fehler landen im Record und
    Log, der Run-Status bleibt unberührt. Bei Merge-Fehler bleibt der Worktree
    stehen — er enthält den ungemergten Stand. Die Worktree-Lease wird immer
    zuletzt freigegeben (ADR 0028)."""
    record = rn.record
    errors: dict[str, str] = {}
    try:
        if not rn.worktree:
            errors["scratch_cleanup_error"] = _cleanup_scratch(rn.run_id, rn.emit) or ""
        else:
            errors["state_merge_error"] = _merge_agent_state(rn) or ""
            if not paused and not errors["state_merge_error"] and rn.spec.get("repo"):
                errors["worktree_cleanup_error"] = _cleanup_worktree(rn.project, rn.run_id, rn.emit) or ""
    finally:
        # Lease erst nach Merge und Cleanup freigeben: bis dahin arbeitet der Run noch im Worktree.
        # Auch bei Pause (geschützt über `needs_approval`) und bei Ausnahmen — sonst liefe der
        # Heartbeat weiter und der Worktree wäre bis Prozessende geschützt.
        if rn.lease is not None:
            rn.lease.release()
            rn.lease = None
    failed = {k: v for k, v in errors.items() if v}
    for key, message in failed.items():
        record[key] = message
        rn.log.event("warning", data={"message": message})
    if failed and paused:
        # Read-Modify-Write: nur die Cleanup-Felder auf den aktuellen On-Disk-Stand anwenden —
        # ein gleichzeitiger Approve (``approved_by``/``approval_queued``) wird nicht überschrieben.
        STORE.update_fields(rn.project, rn.run_id, failed)


def _set_step_record(record: dict, i: int, new: dict) -> dict:
    """Step-Record an Position i-1 setzen (ersetzen oder anhängen)."""
    if i - 1 < len(record["steps"]):
        record["steps"][i - 1] = new
    else:
        record["steps"].append(new)
    return new


def _step_pass(rn: _Run, step: dict, step_record: dict, i: int, res: dict, dur: float) -> None:
    """Erfolgreichen Step im Record/Log festhalten."""
    sname = step_record["name"]
    step_record.update(status="pass", duration_s=dur, summary=res.get("summary", ""))
    # Builder-Tracking für review-Steps (Cross-Model-Check, pr_review-Fix-Runden):
    # das letzte Agent-Modell + Backend + dessen Output als Review-Ziel.
    if step["type"] == "agent":
        usage = res.get("usage") or {}
        if usage.get("model"):
            rn.ctx["last_agent_model"] = usage["model"]
        if usage.get("provider"):
            rn.ctx["last_agent_backend"] = usage["provider"]
        # Variante (Reasoning-Aufwand) wie Modell/Backend weiterreichen; leer = Default des Modells.
        rn.ctx["last_agent_variant"] = str(step.get("variant") or "").strip()
        rn.ctx["last_agent_output"] = res.get("output", "")
    extra = {"usage": res["usage"]} if "usage" in res else {}
    rn.log.event("step_done", step_name=sname, step_type=step["type"], status="pass", duration_s=dur, **extra)
    rn.log.checkpoint(i - 1, sname, "pass", duration_s=dur, **extra)


def _step_fail(rn: _Run, step: dict, step_record: dict, res: dict, dur: float) -> None:
    """Fehlgeschlagenen Step (ok=False) im Record/Log festhalten."""
    sname = step_record["name"]
    step_record.update(status="fail", duration_s=dur, summary=res.get("summary", ""), output=res.get("output", ""))
    step_record.update(from_result(res).fields())
    extra = {"usage": res["usage"]} if "usage" in res else {}  # Verbrauch auch bei Fehlschlag (Kosten/Debugging)
    rn.log.event("step_done", step_name=sname, step_type=step["type"], status="fail", duration_s=dur, **extra)
    rn.emit(f"✖ {sname} fehlgeschlagen (exit {res.get('exit_code', '?')}) — {res.get('summary', '')}")
    out = (res.get("output") or "").strip()
    if out:
        # Output-Tail ins Log: ohne ihn ist "exit 1" kaum debugbar.
        rn.emit("  " + "\n  ".join(out.splitlines()[-10:]))


def _execute_step(rn: _Run, step: dict, i: int) -> str:
    """Einen Step ausführen und Record/Log/Emit aktualisieren.

    Liefert `pass|fail|timeout|error`, `waiting` wenn der Step zur Laufzeit
    Approval verlangt hat (Run ist dann bereits pausiert, Record persistiert),
    oder `done` wenn ein erfolgreicher Step `done: True` meldet (nichts mehr zu
    tun, z. B. `pr` ohne Commits — die restlichen Steps entfallen)."""
    record = rn.record
    sname = step.get("name", f"step-{i}")
    rn.emit(f"▶ {sname} [{step['type']}]")
    rn.log.event("step_start", step_name=sname, step_type=step["type"])
    # Beim Resume: waiting-Placeholder wiederverwenden, nicht doppeln. Name/Typ
    # aus der aktuellen Spec — sie kann sich während der Pause geändert haben (PR #302).
    if i - 1 < len(record["steps"]) and record["steps"][i - 1].get("status") == "waiting":
        step_record = record["steps"][i - 1]
    else:
        step_record = {}
        record["steps"].append(step_record)
    step_record.update(name=sname, type=step["type"], status="running")
    t0 = time.time()
    try:
        res = _dispatch_step(rn.ctx, effective_step(rn.spec, step), rn.emit)
        dur = round(time.time() - t0, 2)
        # Dynamisches Gate (ADR 0010): Step entscheidet zur Laufzeit,
        # dass menschliche Freigabe nötig ist (human_gate → kritisch).
        if res.get("needs_approval"):
            step_record.update(status="waiting")
            approval = _approval_context(rn.spec, rn.trigger, reason="gate", step=step, run_ctx=rn.ctx)
            approval = approval.merged(res.get("approval"))
            _pause_for_approval(rn, sname, approval, _approve_hint(rn, sname))
            return "waiting"
        # Strukturierte Zusatzdaten aus Step-Results in den Run-Record
        # (Tracing-Bausteine, z. B. LLM-Usage für Kosten-Exporter).
        for extra in ("usage", "verify", "review", "verdict"):
            if extra in res:
                step_record[extra] = res[extra]
        ok = res.get("ok", True)
        if ok:
            _step_pass(rn, step, step_record, i, res, dur)
        else:
            _step_fail(rn, step, step_record, res, dur)
            if step.get("gate", True):
                return "fail"
        rn.emit(f"✔ {sname} ({dur}s) {res.get('summary', '')}")
        if not ok:
            return "fail"
        return "done" if res.get("done") else "pass"
    except subprocess.TimeoutExpired:
        step_record.update(status="timeout", duration_s=round(time.time() - t0, 2), **Failure().fields())
        rn.log.event("step_done", step_name=sname, step_type=step["type"], status="timeout")
        rn.emit(f"✖ {sname} Timeout nach {step.get('timeout', '?')}s")
        return "timeout"
    except Exception as e:
        step_record.update(status="error", duration_s=round(time.time() - t0, 2), summary=str(e))
        step_record.update(classify_exception(e).fields())
        rn.log.event("step_done", step_name=sname, step_type=step["type"], status="error")
        rn.emit(f"✖ {sname} Fehler: {e}")
        # Traceback ins Log: "Fehler: <msg>" allein sagt nicht, WO es hakt.
        rn.emit(traceback.format_exc(limit=5))
        return "error"


def _tier_gate(rn: _Run, loop: dict) -> dict | None:
    """Tier-Gate: T2+ endet nie ohne Approval (synthetischer Step, resume-tauglich).

    Liefert den pausierten Record, oder None wenn kein Gate nötig ist."""
    pol = tier_policy(loop.get("tier"))
    gate_satisfied = rn.record.get("waiting_step") == "tier-approval" and rn.record.get("resumed")
    # Nur statisches `approval: true` ersetzt das Tier-Gate — ein
    # `approval_when` kann unerfüllt bleiben (ADR 0017).
    if not pol["requires_approval"] or gate_satisfied or any(s.get("approval") for s in loop["steps"]):
        return None
    rn.record["steps"].append(
        {
            "name": "tier-approval",
            "type": "approval",
            "status": "waiting",
            "summary": f"Tier {pol['tier']} — Freigabe benötigt",
        }
    )
    approval = _approval_context(rn.spec, rn.trigger, reason="tier", tier=pol["tier"], run_ctx=rn.ctx)
    msg = f"⏸ Tier {pol['tier']}: PAUSE — Freigabe erforderlich: `fabrik approve {rn.run_id} --project {rn.project}`"
    return _pause_for_approval(rn, "tier-approval", approval, msg)


def _skip_remaining(rn: _Run, loop: dict, done_i: int) -> None:
    """Steps nach Step done_i als `skipped` in den Record schreiben (vorzeitiges Run-Ende)."""
    by = loop["steps"][done_i - 1].get("name", f"step-{done_i}")
    for j, step in enumerate(loop["steps"][done_i:], done_i + 1):
        name = step.get("name", f"step-{j}")
        _set_step_record(
            rn.record,
            j,
            {
                "name": name,
                "type": step["type"],
                "status": STEP_SKIPPED,
                "summary": f"übersprungen — {by} hat den Run beendet",
            },
        )
        rn.log.event("step_done", step_name=name, step_type=step["type"], status=STEP_SKIPPED)
    if done_i < len(loop["steps"]):
        rn.emit(f"■ {by}: nichts mehr zu tun — {len(loop['steps']) - done_i} Step(s) übersprungen")


def _run_steps(rn: _Run, loop: dict, start_i: int, resuming: bool) -> dict | None:
    """Steps ab start_i abarbeiten. Liefert den pausierten Record oder None (Status gesetzt)."""
    record = rn.record
    any_failed = False
    for i, step in enumerate(loop["steps"], 1):
        if i <= start_i:
            continue
        sname = step.get("name", f"step-{i}")
        # Beim Resume wird der gepauste Step bewusst nochmal ausgeführt — ohne erneutes Gate.
        condition = None if resuming and i == start_i + 1 else _approval_condition(step, rn.ctx)
        if condition is not None:
            # Gate: Run pausiert, bis jemand approvet (UI/Chat/CLI)
            _set_step_record(record, i, {"name": sname, "type": step["type"], "status": "waiting"})
            approval = _approval_context(
                rn.spec, rn.trigger, reason="step", step=step, run_ctx=rn.ctx, condition=condition
            )
            return _pause_for_approval(rn, sname, approval, _approve_hint(rn, sname))
        outcome = _execute_step(rn, step, i)
        if outcome == "waiting":
            return record
        if outcome == "done":
            # Vorzeitiges, erfolgreiches Ende; das Tier-Gate gilt trotzdem (s. u.).
            _skip_remaining(rn, loop, i)
            break
        if outcome != "pass":
            any_failed = True
            # gate (Default true): Step-Fehler bricht den Run ab. gate: false →
            # Fehler wird notiert, der Loop läuft weiter, der Run endet als fail.
            if step.get("gate", True):
                record["status"] = _ABORT_STATUS[outcome]
                rn.aborted_by = i - 1
                return None
    if any_failed:
        record["status"] = "fail"  # mindestens ein gate:false-Step ist fehlgeschlagen
        return None
    paused = _tier_gate(rn, loop)
    if paused is None:
        record["status"] = "pass"
    return paused


def _classify_run(rn: _Run) -> None:
    """Fehlerklasse des gescheiterten Runs in den Record (ADR 0035): Abbruch außerhalb der Steps,
    sonst abbrechender bzw. erster gescheiterter Step. Erfolgreiche Runs bekommen kein Feld."""
    record = rn.record
    if record["status"] not in FAILED_RUN_STATUSES:
        return
    failure = rn.abort_failure or run_failure(record["steps"], rn.aborted_by)
    record.update(failure.fields())
    rn.emit(f"Fehlerklasse: {failure.kind.value}")


def _scope_note(project: str, loop_name: str, loop: dict, held_scope: str | None) -> list[str]:
    """Log-Notiz, wenn sich die Spec zwischen Claim und Run geändert hat (ADR 0020).

    Der Run richtet sich nach dem Spec-Snapshot. Wechselt der Loop dabei von
    worktree-frei auf Worktree, belegt er keinen ``max_parallel``-Slot —
    bewusst toleriert (kurzes Config-Fenster, höchstens ein Run über dem Limit)."""
    current = loop_lock_key(project, loop_name, loop)
    if held_scope is None or held_scope == current:
        return []
    return [f"Config geändert (Scope {held_scope!r} → {current!r}): Run folgt der aktuellen Spec"]


def run_loop(
    project_name: str,
    loop_name: str,
    trigger: str = "manual",
    note: str = "",
    resume_id: str | None = None,
    initial_vars: dict | None = None,
    run_id: str | None = None,
    held_scope: str | None = None,
    actor: str | None = None,
    attempt: int = 0,
    retry_of: str = "",
    chain_key: str = "",
) -> dict:
    """Führt einen Loop aus (synchron). Gibt den Run-Record zurück.

    actor: auslösender Benutzer → ``started_by`` im Record (Audit, ADR 0030).

    resume_id: Fortsetzung eines needs_approval-Runs (nach Approval).
    initial_vars: Trigger-Variablen (z. B. Issue-Titel/-Body — untrusted!).
    run_id: Vorgegebene Run-Id (Server: wird im POST-Response an die UI
    zurückgegeben, BEVOR der Worker den Record auf Disk schreibt — die UI
    kann den Run sofort per Id verfolgen, ohne auf die Datei zu warten).
    held_scope: Scope des Claims, den der Aufrufer genommen hat (`RunClaim.scope`);
    weicht er vom Spec-Stand dieses Runs ab, wird das im Run-Log vermerkt.
    attempt: 0-basierte Retry-Versuchszahl (ADR 0036); 0 = erster Run.
    retry_of: Run-Id des vorherigen Versuchs (ADR 0036); "" = kein Retry.
    chain_key: Schlüssel der Retry-Kette, deren Claim der Versuch hält; ""
    = aus Spec + Trigger berechnen."""
    spec = load_projects().get(project_name)
    if not spec:
        raise SetupError(f"Projekt unbekannt: {project_name}")
    if spec.get("_error"):
        raise SetupError(f"Spec defekt: {spec['_error']}")
    loop = spec["loops"].get(loop_name)
    if not loop:
        raise SetupError(f"Loop unbekannt: {loop_name} (verfügbar: {list(spec['loops'])})")

    with project_github(spec):  # GitHub-Identitäten des Projekts (github_env_file)
        return _run_loop(
            spec,
            loop,
            project_name,
            loop_name,
            trigger,
            note,
            resume_id,
            initial_vars,
            run_id,
            uses_worktree(loop),
            _scope_note(project_name, loop_name, loop, held_scope),
            actor,
            attempt,
            retry_of,
            chain_key,
        )


def _run_loop(
    spec: dict,
    loop: dict,
    project_name: str,
    loop_name: str,
    trigger: str,
    note: str,
    resume_id: str | None,
    initial_vars: dict | None,
    run_id: str | None,
    with_worktree: bool,
    notes: list[str],
    actor: str | None = None,
    attempt: int = 0,
    retry_of: str = "",
    chain_key: str = "",
) -> dict:
    """Rumpf von :func:`run_loop` mit bereits aufgelöstem Spec-Snapshot."""
    run_dir = runs_dir_for(project_name)
    record, run_id, start_i = _init_record(
        spec,
        project_name,
        loop,
        loop_name,
        trigger,
        note,
        resume_id,
        run_id,
        run_dir,
        actor,
        attempt,
        retry_of,
        chain_key,
    )
    log = SessionLog(run_id, project_name, loop_name, trigger, record.get("spec_hash", ""))
    log.start()  # INSERT OR IGNORE — beim Resume existiert die Session bereits

    def emit(msg: str):
        line = _redact(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}")
        with config._log_lock, open(run_dir / f"{run_id}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ctx ist vor dem try gebunden (leer), damit der finale Record-Write die
    # Variablen immer persistieren kann — auch wenn der Run vor dem ctx-Build fehlschlägt.
    rn = _Run(spec, project_name, trigger, run_id, run_dir, record, log, emit, worktree=with_worktree)
    for line in notes:
        emit(line)
    try:
        _build_ctx(rn, loop_name, note, bool(resume_id), initial_vars)
        paused = _run_steps(rn, loop, start_i, bool(resume_id))
        if paused is not None:
            _finish_workdir(rn, paused=True)
            return paused
    except Exception as e:
        record["status"] = "error"
        record["error"] = str(e)
        rn.abort_failure = classify_exception(e)
        emit(f"✖ Run abgebrochen: {e}")
        emit(traceback.format_exc(limit=3))

    # Variablen immer persistieren (nicht nur an Approval-Gates): der Record
    # ist die einzige Quelle für {Variablen} beim stateless Resume und für
    # Forensik — auch bei pass/error.
    _finish_workdir(rn, paused=False)
    _persist_vars(record, rn.ctx)
    _classify_run(rn)
    record["finished"] = config.utcnow().isoformat()
    try:
        _persist(project_name, run_id, record)
    except PreconditionError as e:
        # Ein Reaper/Stale-Check hat den Run inzwischen beendet (running → error) und dabei
        # Retry-Kette und Alerts bereits gebucht: dessen Record gilt, die Engine bucht nichts.
        emit(f"⚠ Run-Record nicht geschrieben, Run wurde extern beendet: {e}")
        current = get_run(run_id, project_name) or record
        log.finish(current["status"], **session_totals(run_id))
        emit(f"■ {str(current['status']).upper()} — {loop_name}")
        return current
    except RunStoreError as e:
        # Defekter On-Disk-Stand / I/O: Log-Finish, Retry-Kette und Alerts laufen trotzdem.
        emit(f"⚠ Run-Record nicht geschrieben: {e}")
    log.finish(record["status"], **session_totals(run_id))
    # Retry-Kette aktualisieren (ADR 0036): der Zustand steuert die Alert-Dämpfung
    # (run_failed wird unterdrückt, solange die Kette pending ist; run_exhausted
    # feuert genau einmal, wenn das Budget aufgebraucht ist). Best-effort: ein
    # Store-Fehler (volles Dateisystem) darf das Alert nicht überspringen.
    chain_info = retry_chains.on_run_finished_safe(spec, project_name, loop_name, record)
    sent = _notify(spec, record, retry_state=chain_info.get("state"))
    if sent:
        emit("⚠ Alerts gesendet: " + ", ".join(sent))
    emit(f"■ {record['status'].upper()} — {loop_name}")
    return record


def _dispatch_step(ctx: dict, step: dict, emit) -> dict:
    t = step["type"]
    try:
        step = connections.apply_to_step(step)  # benannte/Default-Verbindung (ADR 0052)
    except connections.InvalidConnection as e:
        return failed(str(e), FailureKind.ENVIRONMENT)
    if t == "shell":
        return _step_shell(ctx, step, emit)
    if t == "git":
        return _step_git(ctx, step)
    if t == "pr":
        return _step_pr(ctx, step)
    if t == "agent":
        return _step_agent(ctx, step, emit)
    if t == "review":
        return _step_review(ctx, step, emit)
    if t == "pr_review":
        return _step_pr_review(ctx, step, emit)
    if t == "pr_merge":
        return _step_pr_merge(ctx, step)
    if t == "issue":
        return _step_issue(ctx, step)
    if t == "human_gate":
        return _step_human_gate(ctx, step, emit)
    if t == "escalation":
        return _step_escalation(ctx, step, emit)
    if t == "report":
        text = _substitute(step.get("text", ""), ctx)
        w = step.get("write")
        if w:
            p = Path(_substitute(w, ctx))
            if not p.is_absolute():
                p = Path(ctx.get("root", ".")) / p
            p = p.resolve()
            root_res = Path(ctx.get("root", ".")).resolve()
            if root_res not in p.parents:
                return {
                    "ok": False,
                    "summary": f"BLOCKED: Report-Pfad {p} liegt außerhalb "
                    f"der Projekt-Root {root_res} (Path-Traversal-Schutz)",
                }
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text + "\n", encoding="utf-8")
            emit(f"ℹ Report geschrieben: {p}")
            return {"ok": True, "summary": f"Report: {p}"}
        emit(f"ℹ {text}")
        return {"ok": True, "summary": text}
    if t == "http":
        return _step_http(ctx, step, emit)
    if t == "llm":
        return _step_llm(ctx, step, emit)
    if t == "notify":
        return _step_notify(ctx, step, emit)
    if t == "state":
        return _step_state(ctx, step, emit)
    if t == "verify":
        return _step_verify(ctx, step, emit)
    if t == "alert_route":
        return _step_alert_route(ctx, step, emit)
    raise ValueError(f"Unbekannter Step-Typ: {t}")


# ---------------------------------------------------------------- Worker-Pool
class _WorkerPool:
    """Bounded pool of daemon worker threads (size = FABRIK_WORKERS).

    Daemon-Threads (nicht ThreadPoolExecutor): ein Ctrl+C beendet den Prozess
    sofort, ohne auf laufende Runs zu warten. Lokal: FABRIK_WORKERS=1 → alle
    Runs seriell auf einem einzigen Worker.
    """

    def __init__(self, size: int):
        self._size = max(1, int(size))
        self._queue: queue.Queue = queue.Queue()
        self._busy = 0  # Worker, die gerade eine Work ausführen (Schedule-View)
        self._busy_guard = threading.Lock()
        self._threads = [
            threading.Thread(target=self._run, daemon=True, name=f"fabrik-worker-{i}") for i in range(self._size)
        ]
        for t in self._threads:
            t.start()

    def _run(self):
        while True:
            work = self._queue.get()
            if work is None:
                break
            with self._busy_guard:
                self._busy += 1
            try:
                work()
            except Exception:
                traceback.print_exc()
            finally:
                with self._busy_guard:
                    self._busy -= 1

    def submit(self, work) -> None:
        self._queue.put(work)

    def shutdown(self) -> None:
        for _ in range(self._size):
            self._queue.put(None)

    @property
    def size(self) -> int:
        return self._size

    @property
    def busy(self) -> int:
        """Worker, die gerade arbeiten."""
        return self._busy

    @property
    def waiting(self) -> int:
        """Eingereihte, noch nicht gestartete Works (ungefähr, wie `Queue.qsize`)."""
        return self._queue.qsize()


_run_pool: _WorkerPool | None = None
_run_pool_guard = threading.Lock()


def _get_run_pool() -> _WorkerPool:
    global _run_pool
    with _run_pool_guard:
        if _run_pool is None:
            _run_pool = _WorkerPool(config.configured_workers())
        return _run_pool


_launcher: RunLauncher | None = None
_launcher_guard = threading.Lock()


def _get_launcher() -> RunLauncher:
    """Run-Launcher (Port) für das konfigurierte Substrat (FABRIK_LAUNCHER).

    Singleton. In-process (Default) delegiert an den bestehenden Worker-Pool
    (:func:`_get_run_pool`) und Daemon-Threads; die Scheduling-Policy bleibt im
    Core. Neue Substrate (k8s, ecs) hängen über :mod:`fabrik.launcher.registry`
    an, ohne diesen Code zu ändern."""
    global _launcher
    with _launcher_guard:
        if _launcher is None:
            # Späte Bindung (Lambda, keine Funktionsreferenz): `pool_factory()`/
            # `pool_or_none()` lösen den Pool zum Aufrufzeitpunkt aus dem
            # Modul-Globalen auf — so respektieren Tests, die `engine._get_run_pool`
            # bzw. `engine._run_pool` monkeypatchen, den injizierten Pool.
            _launcher = create_launcher(
                config.configured_launcher(),
                pool_factory=lambda: _get_run_pool(),
                pool_or_none=lambda: _run_pool,
            )
        return _launcher


# Eingereihte, noch nicht gestartete Trigger (project, loop, trigger) → Einreichungszeit
# (Schedule-View: Queue mit echten Einreichungszeitpunkten). Der Run-Record entsteht
# erst in work(); reconcile-Polls würden denselben Trigger sonst erneut einreihen,
# solange der Pool ausgelastet ist.
_queued_triggers: dict[tuple[str, str, str], float] = {}
_queued_guard = threading.Lock()

# Trigger, deren String ein einmaliges Ereignis identifiziert (Issue, Slack-Nachricht):
# existiert bereits ein Run-Record mit genau diesem Trigger, wird eine wartende Kopie verworfen.
_ONCE_TRIGGER_PREFIXES = ("on_issue:", "on_slack:")


def queued_trigger_snapshot() -> list[dict]:
    """Pool-Queue: eingereihte Trigger-Runs (Cron/Datei/Issue/PR), nach Einreichung sortiert.

    `enqueued_at` = Zeitstempel (epoch) des Einreichens — kein Record existiert
    noch, daher ist dies die einzige persistenzfreie Quelle für die Wartezeit."""
    with _queued_guard:
        return [
            {"project": p, "loop": ln, "trigger": tg, "enqueued_at": ts}
            for (p, ln, tg), ts in sorted(_queued_triggers.items(), key=lambda kv: kv[1])
        ]


def pool_status() -> LauncherStatus:
    """Substrat-Status des konfigurierten Run-Launchers (ADR 0057): Größe,
    belegte Worker (`busy`) + Queue-Länge.

    Delegiert an den Launcher — die In-Prozess-Variante meldet die
    konfigurierte Größe (FABRIK_WORKERS), solange der Pool noch nicht
    angelegt ist (nicht 0 — irreführend)."""
    return _get_launcher().status()


def _safe_run(
    name: str,
    lname: str,
    trigger: str,
    initial_vars: dict | None = None,
    done: threading.Event | None = None,
    *,
    on_claimed: Callable[[], None] | None = None,
    on_rejected: Callable[[], None] | None = None,
):
    """Trigger-Run im Worker-Pool starten; verwerfen statt warten, wenn kein Slot frei ist.

    `done` wird gesetzt, sobald der Trigger erledigt ist — gelaufen, verworfen oder (schon
    eingereiht) gar nicht erst eingereiht. Damit wartet der Scheduler auf `on_start`-Fires.
    Pro eingereihtem Aufruf läuft genau einer der optionalen Callbacks:
    `on_claimed` nach erfolgreichem Claim (vor dem Run), sonst `on_rejected`
    (Pause, bereits gelaufen, Limit/Lock, Fehler vor dem Claim). Ein schon
    eingereihter identischer Trigger wird ignoriert (kein Callback)."""
    # Launcher vor der Mutation von `_queued_triggers` auflösen: ein unbekanntes
    # Substrat (FABRIK_LAUNCHER) wirft, ohne den Trigger dauerhaft zu unterdrücken.
    launcher = _get_launcher()

    qkey = (name, lname, trigger)
    with _queued_guard:
        if qkey in _queued_triggers:
            if done is not None:
                done.set()
            return
        _queued_triggers[qkey] = time.time()

    def work():
        try:
            _work()
        finally:
            if done is not None:
                done.set()

    def _work():
        # Ab hier schützt der Claim (aktiver Run) bzw. der Record vor Duplikaten.
        with _queued_guard:
            _queued_triggers.pop(qkey, None)
        claim = None
        try:
            claim = _claim_trigger_run(name, lname, trigger, initial_vars)
        finally:
            _run_callback(on_claimed if claim is not None else on_rejected, name, lname)
        if claim is None:
            return
        try:
            run_loop(name, lname, trigger=trigger, initial_vars=initial_vars, held_scope=claim.scope)
        except Exception as e:
            print(f"[fabrik] Trigger-Run {name}/{lname} fehlgeschlagen: {e}")
        finally:
            claim.release()

    try:
        launcher.submit(work)
    except Exception:
        # Submit fehlgeschlagen (z. B. ungültige FABRIK_WORKERS beim Pool-Aufbau):
        # den Trigger nicht dauerhaft unterdrücken — Eintrag wieder entfernen.
        with _queued_guard:
            _queued_triggers.pop(qkey, None)
        raise


def _claim_trigger_run(name: str, lname: str, trigger: str, initial_vars: dict | None) -> RunClaim | None:
    """Ausführungszeit-Prüfungen eines Trigger-Runs; Claim oder None (= verworfen)."""
    # Pause/Disable zur Ausführungszeit prüfen (ADR 0019): Die Work kann
    # lange im Pool-Queue gesessen haben; der Scheduler prüft nur beim
    # Einreihen — sonst laufen nach einem Pause noch alle wartenden Trigger.
    state = pause.effective_state(name, lname)
    if state != pause.ACTIVE:
        print(f"[fabrik] Trigger-Run {name}/{lname} ({trigger}) verworfen: {state}")
        return None
    # Persistente Dedup zur Ausführungszeit (einmalige Ereignisse: on_issue,
    # on_slack): Die Work kann lange im Pool-Queue gesessen haben, während ein
    # früherer Run für dasselbe Ereignis bereits abgeschlossen und sein Record
    # geschrieben wurde. Die Poll-Zeit-Dedup reicht dann nicht — sonst laufen
    # alle queuingen Kopien nacheinander (Doppel-Runs, Issue #226, Okt 2026).
    # on_pr bleibt ausgenommen: Event-Modus feuert beabsichtigt auf PR-Updates erneut.
    if trigger.startswith(_ONCE_TRIGGER_PREFIXES) and trigger in loop_run_triggers(name, lname):
        return None
    # Über dem Limit (max_parallel), bei aktivem Run für Loop + Issue/PR oder
    # wartendem Vorrang-Claim wird der Trigger verworfen; der nächste
    # Poll/Cron-Tick versucht es erneut.
    return claim_run(name, lname, initial_vars)


def _run_callback(callback: Callable[[], None] | None, name: str, lname: str) -> None:
    """Trigger-Callback ausführen; Fehler nur mit Exception-Typ loggen (kein Abbruch)."""
    if callback is None:
        return
    try:
        callback()
    except Exception as e:
        print(f"[fabrik] Trigger-Status {name}/{lname} fehlgeschlagen: {type(e).__name__}")


def _write_queued_record(
    name: str, lname: str, run_id: str, trigger: str, note: str, initial_vars: dict | None, actor: str | None = None
):
    """Platzhalter-Record (status `queued`): UI zeigt den Wartezustand, und ein
    Server-Restart setzt den Run fort (requeue_queued_runs). run_loop
    überschreibt ihn beim Start."""
    record: dict[str, Any] = {
        "id": run_id,
        "project": name,
        "loop": lname,
        "trigger": trigger,
        "note": note,
        "started": config.utcnow().isoformat(),
        "status": "queued",
        "queued_vars": initial_vars or {},
        "steps": [],
        "started_by": actor,
        "approved_by": None,
        "rejected_by": None,
    }
    # Neu: Datei fehlt; Requeue nach Restart: bestehender queued-Platzhalter wird überschrieben.
    STORE.write(name, run_id, record, allow_missing=True)


def start_priority_run(
    name: str,
    lname: str,
    trigger: str,
    *,
    run_id: str,
    note: str = "",
    initial_vars: dict | None = None,
    actor: str | None = None,
) -> bool:
    """Manuellen Run (UI/API/Chat) in einem Daemon-Thread starten — nie verwerfen.

    Ist ein Slot frei, läuft er sofort. Sonst (Limit erreicht oder Run für
    Loop + Issue/PR aktiv) wird er als `queued` persistiert und wartet mit
    Vorrang vor Trigger-Runs (ADR 0015). Gibt True zurück, wenn eingereiht.
    RuntimeError, wenn der Thread nicht startet (Claim/Wartezähler sind dann
    wieder frei)."""
    queued = False

    def work(held: RunClaim) -> None:
        try:
            # Während des Wartens deaktiviert (ADR 0019) → nicht mehr starten.
            if blocked := pause.manual_block_reason(name, lname):
                print(f"[fabrik] Run {name}/{lname} verworfen: {blocked}")
                _fail_queued_record(name, run_id, blocked)
                return
            run_loop(
                name,
                lname,
                trigger=trigger,
                note=note,
                initial_vars=initial_vars,
                run_id=run_id,
                held_scope=held.scope,
                actor=actor,
            )
        except Exception as e:
            print(f"[fabrik] Run {name}/{lname} fehlgeschlagen: {e}")
            _fail_queued_record(name, run_id, str(e), classify_exception(e))

    def on_start_error() -> None:
        if queued:
            _fail_queued_record(name, run_id, "Worker-Start fehlgeschlagen")

    def on_queued() -> None:
        nonlocal queued
        queued = True
        _write_queued_record(name, lname, run_id, trigger, note, initial_vars, actor)

    spawn_priority_worker(name, lname, initial_vars, work=work, on_queued=on_queued, on_start_error=on_start_error)
    return queued


def spawn_priority_worker(
    project: str,
    loop: str,
    variables: dict | None,
    *,
    claim: RunClaim | None = None,
    work: Callable[[RunClaim], None],
    on_queued: Callable[[], None] | None = None,
    on_start_error: Callable[[], None] | None = None,
    exclude_run_id: str | None = None,
) -> bool:
    """Daemon-Thread für einen Run mit Vorrang vor Trigger-Runs (ADR 0015).

    Der Helfer claimt zunächst nicht blockierend. Ist kein Slot frei,
    registriert er synchron einen Vorrang-Wartenden und der Thread wartet
    blockierend auf den Slot. `work(held)` fängt eigene Fehler selbst; der
    Claim wird in jedem Fall danach freigegeben. Startet der Thread nicht,
    wird der Claim freigegeben bzw. der Wartezähler zurückgenommen,
    `on_start_error()` aufgerufen und der RuntimeError weitergereicht.
    `exclude_run_id` (Resume): der fortgesetzte Run blockiert seinen eigenen
    Claim nicht (ADR 0043). Gibt zurück, ob der Run eingereiht wurde."""
    scope = lock_key(project, loop)
    held = claim if claim is not None else claim_run(project, loop, variables, exclude_run_id=exclude_run_id)
    queued = held is None
    if queued:
        mark_priority_waiter(scope, +1)
        if on_queued:
            try:
                on_queued()
            except Exception:
                mark_priority_waiter(scope, -1)
                if on_start_error:
                    on_start_error()
                raise

    def target() -> None:
        acquired = held or claim_run(project, loop, variables, blocking=True, registered=True)
        assert acquired is not None  # blocking=True liefert immer einen Claim
        try:
            work(acquired)
        finally:
            acquired.release()

    try:
        _get_launcher().submit_priority(target)
    except RuntimeError:
        if held is not None:
            held.release()
        else:
            mark_priority_waiter(scope, -1)
        if on_start_error:
            on_start_error()
        raise
    return queued


def _fail_queued_record(name: str, run_id: str, error: str, failure: Failure | None = None) -> None:
    """Ein noch `queued` Record (run_loop scheiterte vor dem Schreiben, z. B.
    Loop unbekannt) wird `error` — sonst hinge er für immer in der UI."""
    rec = get_run(run_id, name)
    if rec and rec.get("status") == "queued":
        rec["status"] = "error"
        rec["error"] = error
        rec.update((failure or Failure()).fields())
        # Compare-and-Set: hat run_loop den Record inzwischen übernommen (running), bleibt er.
        with contextlib.suppress(PreconditionError):
            STORE.write(name, run_id, rec, expect=RunStatus.QUEUED)


def requeue_queued_runs() -> list[str]:
    """Server-Start: eingereihte manuelle Runs (status `queued`) erneut einreihen."""
    started = []
    for run in list_runs(limit=None):
        if run.get("status") != "queued":
            continue
        try:
            start_priority_run(
                run["project"],
                run["loop"],
                run.get("trigger", "manual"),
                run_id=run["id"],
                note=run.get("note", ""),
                initial_vars=run.get("queued_vars") or None,
                actor=run.get("started_by"),
            )
        except RuntimeError as e:
            print(f"[fabrik] Requeue {run['id']} fehlgeschlagen: {e}")
            continue
        started.append(run["id"])
    return started
