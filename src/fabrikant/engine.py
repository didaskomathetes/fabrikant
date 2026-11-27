"""Run engine: run_loop (gate/approval/resume), step dispatch, worker pool.

Stateless per run: all runtime data in the record (runs/<project>/<id>.json),
no in-memory session state. This is the P2 prerequisite (stateless workers).
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

from . import agent_state, config, connections, hosting, pause, retry_chains
from .approval import ApprovalRequest
from .context import _PLACEHOLDER_RE, _cap, _cond_matches, _substitute, tier_policy
from .excerpt import failure_excerpt
from .failures import Failure, FailureKind, SetupError, classify_exception, failed, from_result, run_failure
from .gitops import (
    WorktreeLease,
    _worktree_sync_check,
    cleanup_run_worktree,
    ensure_worktree,
    project_github,
    run_worktree_path,
)
from .i18n import t as _t
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
from .steps import registry as step_registry
from .steps.extra_context import effective_step


def _trigger_link(spec: dict, trigger: str, kind: str, path: str) -> str:
    """GitHub link to the trigger object (``on_<kind>:#N``, optional ``@sha``) if the repo is known."""
    m = re.match(rf"on_{kind}:#(\d+)", trigger or "")
    repo = spec.get("github")
    if not m or not repo:
        return ""
    return f"https://github.com/{repo}/{path}/{m.group(1)}"


def _issue_link(spec: dict, trigger: str) -> str:
    """GitHub link to the trigger issue (on_issue:#N), if the repo is known.

    Only for on_issue triggers; otherwise empty (no issue context). The link gives
    the approver/debugging a direct path to the origin of the run."""
    return _trigger_link(spec, trigger, "issue", "issues")


def _pr_link(spec: dict, trigger: str, run_ctx: dict | None) -> str:
    """Link to the run's PR: `pr_url` from the run variables (on_pr scheduler,
    pr step), otherwise from the on_pr trigger + spec repo (older runs without `pr_url`)."""
    url = str((run_ctx or {}).get("pr_url") or "")
    return url or _trigger_link(spec, trigger, "pr", "pull")


def _format_permissions(perms: list[dict | str] | None) -> list[str]:
    """Permission rules (step.permissions) as a readable list, e.g. 'edit * → allow'.

    Shows the approver WHICH permissions the agent step gets (containment)."""
    out = []
    for p in perms or []:
        if isinstance(p, dict):
            out.append(f"{p.get('permission', '?')} {p.get('pattern', '*')} → {p.get('action', '?')}")
        else:
            out.append(str(p))
    return out


def _as_list(v: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Spec value as a list (``labels: x`` and ``labels: [x, y]`` are both allowed)."""
    if v in (None, ""):
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _issue_intent(step: dict) -> str:
    """Issue step concretely: number + label diff, so the approver sees what changes
    (e.g. ``Issue #42: +ready for development −needs approval``)."""
    head = f"Issue #{step['number']}" if step.get("number") else f"Issue {step.get('repo', '')}"
    action = step.get("action", "")
    if action != "label":
        return f"{head}: {action}"
    diff = [f"+{lbl}" for lbl in _as_list(step.get("labels"))]
    diff += [f"−{lbl}" for lbl in _as_list(step.get("remove_labels"))]
    return f"{head}: {' '.join(diff) or 'labels unchanged'}"


def _step_intent(step: dict, run_ctx: dict | None = None) -> str:
    """Short description of what the step will do (approval context: 'what').

    Derived deterministically from the spec — no LLM, no untrusted content.
    With run_ctx, {variables} (e.g. {issue_number}, {label}) are resolved to the
    runtime values so the approver sees the actual command.
    Unknown placeholders stay unchanged (safe: no crash, no wrong value)."""
    t = step.get("type")
    if t == "shell":
        cmd = " ".join(str(step.get("cmd", "")).split())
        intent = f"Shell-Befehl: {cmd}"
    elif t == "agent":
        with contextlib.suppress(connections.InvalidConnection):
            step = connections.apply_to_step(step, with_secret=False)  # display only: no decrypting
        backend = (step.get("backend") or os.environ.get("FABRIKANT_AGENT_BACKEND", "")).strip()
        model = (step.get("model") or os.environ.get("FABRIKANT_AGENT_MODEL", "")).strip()
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
        intent = f"Git: {', '.join(parts) or 'no-op'}"
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
        intent = _t("engine.criticality_check_human_approval_if")
    elif t == "escalation":
        intent = "Escalation to a human (ADR 0059): wait, then resume"
    elif t == "report":
        intent = f"Report: {step.get('write', '(log only)')}"
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
    """Approval request: what happens, why, which permissions, which issue/PR.

    Written into the run record (record['approval']) and shown in log/UI/chat
    so the approver knows WHAT is approved and WHY
    (which step, which agent permissions, which triggering issue, which PR, which tier).
    run_ctx resolves {variables} in the 'what' text (e.g. {issue_number} → 42).
    The optional step field `approval_reason` (template, e.g. "{reasoning}" from
    a previous llm step) provides the domain rationale ('why')."""
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
        req = replace(req, tier=tier, what=_t("approval.tier_what", tier=tier))
    return req


def _approval_condition(step: dict, ctx: dict) -> str | None:
    """Whether the step pauses BEFORE its execution — and why.

    None → no gate. "" → static `approval: true`. Otherwise the satisfied
    condition from `approval_when` (ADR 0017), e.g. "label = close":
    `approval_when: {var: value | [values]}` pauses only if EVERY variable
    (AND) has one of the values (OR). Missing variable → no match."""
    if step.get("approval"):
        return ""
    cond = step.get("approval_when")
    if not cond:
        return None
    if not _cond_matches(cond, ctx):
        return None
    return ", ".join(f"{var} = {ctx[var]}" for var in cond)


def _approval_reason(step: dict, run_ctx: dict | None) -> str:
    """The step's `approval_reason` with resolved {variables}; "" if not set.

    If a referenced variable is missing in the run, the result is "" instead of a raw
    "{reasoning}" in the approval context."""
    tmpl = step.get("approval_reason")
    run_ctx = run_ctx or {}
    if not isinstance(tmpl, str) or any(v not in run_ctx for v in _PLACEHOLDER_RE.findall(tmpl)):
        return ""
    return _cap(_substitute(tmpl, run_ctx).strip())


def _emit_approval(emit, approval: ApprovalRequest) -> None:
    """Write the approval context to the run log (more context than just the step name)."""
    for line in approval.lines():
        emit(f"  {line}")


def _persist_vars(record: dict, ctx: dict) -> None:
    """Persist run variables into the record (`record['vars']`).

    Resume is stateless: after an approval pause the in-memory ctx is gone,
    the record is the ONLY source for {variables} ({issue_number}, {label},
    LLM outputs, …). Only JSON-serializable values land in the record; `log`
    (SessionLog) thereby drops out. `last_agent_output` is capped so
    the record stays small (only consumer: review step)."""
    record["vars"] = {
        k: (_cap(str(v)) if k == "last_agent_output" else v)
        for k, v in ctx.items()
        if isinstance(v, (str, int, float, bool))
    }


@dataclass
class _Run:
    """Runtime bundle of a run_loop call (record, log, emit, paths)."""

    spec: dict
    project: str
    trigger: str
    run_id: str
    run_dir: Path
    record: dict
    log: SessionLog
    emit: Callable[[str], None]
    ctx: dict = field(default_factory=dict)
    worktree: bool = True  # False: scratch workdir instead of run worktree (ADR 0020)
    base: Path | None = None  # Workdir des Runs, sobald angelegt
    state_snapshot: dict = field(default_factory=dict)  # agent state snapshot at start (ADR 0028)
    aborted_by: int | None = None  # Index of the step that aborted the run (ADR 0035)
    abort_failure: Failure | None = None  # Class of a run abort outside the steps
    lease: WorktreeLease | None = None  # heartbeat of the worktree lease while the run is alive (ADR 0028)


# record["status"] on gate abort, per step outcome
_ABORT_STATUS = {"fail": "fail", "timeout": "fail", "error": "error"}
# Step status for steps behind a `done` step (run ended successfully early).
STEP_SKIPPED = StepStatus.SKIPPED.value


def _persist(project: str, run_id: str, record: dict) -> None:
    """Write the run record of a running run via the store (checkpoint, pause, end).

    Precondition: on-disk status ``running`` (compare-and-set) and allowed transition per
    ``RUN_TRANSITIONS``. If a reaper/stale check has meanwhile ended the run, the
    store raises :class:`PreconditionError` — the engine does not overwrite its result."""
    STORE.write(project, run_id, record, expect=RunStatus.RUNNING)


def _pause_for_approval(rn: _Run, waiting_step: str, approval: ApprovalRequest, message: str) -> dict:
    """Park the run in `needs_approval` (shared path for step/gate/tier approval)."""
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
    return _t("engine.waiting_for_approval_fabrikant_approve", sname=sname, v2=rn.project, v3=rn.run_id)


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
    replay_of: str = "",
) -> tuple[dict, str, int]:
    """Load the record (resume) or create a new one; returns (record, run_id, start_i).

    ``attempt``/``retry_of``/``trigger_key`` (ADR 0036) are set only on new
    records; a resume already carries the values of the original run.
    ``trigger_key`` is the canonical chain key: for retry attempts the key of
    the claiming chain (``chain_key``), otherwise computed from the trigger string
    — the run end books the chain via the record field, not via the (meanwhile
    changed) spec.
    ``replay_of`` (ADR 0061) links a replay run to the original."""
    start_i = 0
    if resume_id:
        record = get_run(resume_id, project_name)
        if not record:
            raise RuntimeError(_t("engine.resume_run_not_found", resume_id=resume_id))
        if record["status"] != "needs_approval":
            raise RuntimeError(_t("engine.resume_run_is_not_needs", v1=record["id"], v2=record["status"]))
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
            start_i = len(loop["steps"])  # synthetic tier gate: skip all real steps
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
            "replay_of": replay_of,
            "trigger_key": chain_key or retry_chains.trigger_key(spec, loop_name, trigger) or "",
        }
    # New: the file may be missing or a queued placeholder (which the start overwrites);
    # resume: on-disk status must be needs_approval (compare-and-set → exactly one resume
    # wins, a concurrent reject rules it out).
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
            # Lease before the worktree: a foreign cleanup never sees it without a lease (ADR 0028)
            rn.lease = WorktreeLease(run_worktree_path(rn.project, rn.run_id))
            rn.lease.start()
        base = ensure_worktree(spec, rn.run_id)
        rn.base = base
        if spec.get("repo"):
            rn.state_snapshot = agent_state.seed(rn.project, base)
    else:
        # ADR 0020: no worktree → empty run workdir, no project lock.
        base = config.SCRATCH_DIR / rn.run_id
        if base.exists():
            cleanup_error = _cleanup_scratch(rn.run_id, rn.emit)
            if cleanup_error:
                rn.record["scratch_cleanup_error"] = cleanup_error
                rn.log.event("warning", data={"message": cleanup_error})
        base.mkdir(parents=True, exist_ok=True)
        rn.base = base
    # Resume: run variables from the record as base (stateless — the
    # in-memory ctx from before the pause is gone). Freshly derived values
    # (base keys, env, workdir, branch, head) deliberately win over it.
    ctx: dict = {k: str(v) for k, v in (rn.record.get("vars") or {}).items()} if resume else {}
    rn.ctx = ctx  # bind early: the final record write persists ctx even on errors
    ctx.update(
        {
            "project": rn.project,
            "root": spec["root"],
            "spec_path": spec.get("_path", ""),  # Base for spec-relative paths (prompt_file, ADR 0047)
            "workdir": str(base),
            "loop": loop_name,
            "trigger": rn.trigger,
            "note": note,
            "run_id": rn.run_id,
            # fabrikant checkout: prompts of other projects use it to call fabrikant's helper scripts
            # (e.g. scripts/pr-status.py) from their own worktree.
            "fabrikant_home": str(config.FABRIKANT_HOME),
            "log": rn.log,
        }
    )
    ctx.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    for v in (spec.get("env") or {}).values():  # Register spec env values for masking
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
    """Remove the scratch workdir and report a cleanup error as run log."""

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
    """Merge the run's agent state into the shared state; error as message (ADR 0028)."""
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
    """Remove the run worktree, check success; error/remainder as message (ADR 0028)."""
    try:
        removed = cleanup_run_worktree(project, run_id)
    except (OSError, subprocess.SubprocessError) as e:
        message = f"⚠ Worktree-Cleanup fehlgeschlagen: {e}"
    else:
        if removed:
            return None
        message = _t("engine.worktree_cleanup_incomplete_still_exists", v1=run_worktree_path(project, run_id))
    emit(message)
    return message


def cleanup_rejected_run(project: str, run_id: str, emit: Callable[[str], None]) -> dict[str, str]:
    """Clean up after reject and return the cleanup error for the run record."""
    errors: dict[str, str] = {}
    scratch_error = _cleanup_scratch(run_id, emit)
    if scratch_error:
        errors["scratch_cleanup_error"] = scratch_error
    worktree_error = _cleanup_worktree(project, run_id, emit)
    if worktree_error:
        errors["worktree_cleanup_error"] = worktree_error
    return errors


def reject_run(project: str, run: dict, actor: str | None, emit: Callable[[str], None]) -> dict:
    """Reject a run at the approval gate (CLI, chat): atomically set ``rejected``, clean up the workdir,
    add cleanup errors and book the retry chain (rejected attempt → dismissed, ADR 0036).

    :class:`PreconditionError` if the run is no longer ``needs_approval`` (e.g. already
    resumed). Returns the rejected record."""
    run_id = run["id"]
    run = STORE.reject(project, run_id, run, actor)
    errors = cleanup_rejected_run(project, run_id, emit)
    if errors:
        run = STORE.update_fields(project, run_id, errors)
    retry_chains.record_run_end(project, run)
    return run


def _finish_workdir(rn: _Run, *, paused: bool) -> None:
    """Run end or pause: clean up the workdir (ADR 0020/0028).

    Scratch workdir: always remove. Run worktree: merge agent state (also on
    pause), remove except on `needs_approval` (resume keeps using it).
    Merge and cleanup are handled separately; errors land in the record and
    log, the run status stays untouched. On a merge error the worktree
    is kept — it contains the unmerged state. The worktree lease is always
    released last (ADR 0028)."""
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
        # Release the lease only after merge and cleanup: until then the run still works in the worktree.
        # Also on pause (protected via `needs_approval`) and on exceptions — otherwise the
        # heartbeat would keep running and the worktree would be protected until process end.
        if rn.lease is not None:
            rn.lease.release()
            rn.lease = None
    failed = {k: v for k, v in errors.items() if v}
    for key, message in failed.items():
        record[key] = message
        rn.log.event("warning", data={"message": message})
    if failed and paused:
        # Read-modify-write: apply only the cleanup fields to the current on-disk state —
        # a concurrent approve (``approved_by``/``approval_queued``) is not overwritten.
        STORE.update_fields(rn.project, rn.run_id, failed)


def _set_step_record(record: dict, i: int, new: dict) -> dict:
    """Set the step record at position i-1 (replace or append)."""
    if i - 1 < len(record["steps"]):
        record["steps"][i - 1] = new
    else:
        record["steps"].append(new)
    return new


def _step_pass(rn: _Run, step: dict, step_record: dict, i: int, res: dict, dur: float) -> None:
    """Erfolgreichen Step im Record/Log festhalten."""
    sname = step_record["name"]
    step_record.update(status="pass", duration_s=dur, summary=res.get("summary", ""))
    # Builder tracking for review steps (cross-model check, pr_review fix rounds):
    # the last agent model + backend + its output as review target.
    if step["type"] == "agent":
        usage = res.get("usage") or {}
        if usage.get("model"):
            rn.ctx["last_agent_model"] = usage["model"]
        if usage.get("provider"):
            rn.ctx["last_agent_backend"] = usage["provider"]
        # Pass on the variant (reasoning effort) like model/backend; empty = model default.
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
    extra = {"usage": res["usage"]} if "usage" in res else {}  # Usage also on failure (cost/debugging)
    rn.log.event("step_done", step_name=sname, step_type=step["type"], status="fail", duration_s=dur, **extra)
    rn.emit(f"✖ {sname} fehlgeschlagen (exit {res.get('exit_code', '?')}) — {res.get('summary', '')}")
    out = (res.get("output") or "").strip()
    if out:
        # Failure-aware excerpt into the log (markers + tail, cuts are stated): a plain tail showed only
        # package lists instead of the cause for test steps. The full output is in the record.
        rn.emit("  " + "\n  ".join(failure_excerpt(out).render().splitlines()))


def _execute_step(rn: _Run, step: dict, i: int) -> str:
    """Execute a step and update record/log/emit.

    Returns `pass|fail|timeout|error`, `waiting` if the step demanded
    approval at runtime (the run is then already paused, record persisted),
    or `done` if a successful step reports `done: True` (nothing left to
    do, e.g. `pr` without commits — the remaining steps are dropped)."""
    record = rn.record
    sname = step.get("name", f"step-{i}")
    rn.emit(f"▶ {sname} [{step['type']}]")
    rn.log.event("step_start", step_name=sname, step_type=step["type"])
    # On resume: reuse the waiting placeholder, do not duplicate. Name/type
    # from the current spec — it may have changed during the pause (PR #302).
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
        # Dynamic gate (ADR 0010): the step decides at runtime
        # that human approval is required (human_gate → critical).
        if res.get("needs_approval"):
            step_record.update(status="waiting")
            approval = _approval_context(rn.spec, rn.trigger, reason="gate", step=step, run_ctx=rn.ctx)
            approval = approval.merged(res.get("approval"))
            _pause_for_approval(rn, sname, approval, _approve_hint(rn, sname))
            return "waiting"
        # Structured additional data from step results into the run record
        # (tracing building blocks, e.g. LLM usage for cost exporters).
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
        rn.emit(f"✖ {sname} timed out after {step.get('timeout', '?')}s")
        return "timeout"
    except Exception as e:
        step_record.update(status="error", duration_s=round(time.time() - t0, 2), summary=str(e))
        step_record.update(classify_exception(e).fields())
        rn.log.event("step_done", step_name=sname, step_type=step["type"], status="error")
        rn.emit(_t("engine.error", sname=sname, e=e))
        # Traceback into the log: "Error: <msg>" alone does not say WHERE it hangs.
        rn.emit(traceback.format_exc(limit=5))
        return "error"


def _tier_gate(rn: _Run, loop: dict) -> dict | None:
    """Tier gate: T2+ never ends without approval (synthetic step, resumable).

    Returns the paused record, or None if no gate is needed."""
    pol = tier_policy(loop.get("tier"))
    gate_satisfied = rn.record.get("waiting_step") == "tier-approval" and rn.record.get("resumed")
    # Only a static `approval: true` replaces the tier gate — an
    # `approval_when` may remain unmet (ADR 0017).
    if not pol["requires_approval"] or gate_satisfied or any(s.get("approval") for s in loop["steps"]):
        return None
    rn.record["steps"].append(
        {
            "name": "tier-approval",
            "type": "approval",
            "status": "waiting",
            "summary": _t("engine.tier_approval_required", v1=pol["tier"]),
        }
    )
    approval = _approval_context(rn.spec, rn.trigger, reason="tier", tier=pol["tier"], run_ctx=rn.ctx)
    msg = _t("engine.tier_pause_approval_required_fabrikant", v1=pol["tier"], v2=rn.run_id, v3=rn.project)
    return _pause_for_approval(rn, "tier-approval", approval, msg)


def _skip_remaining(rn: _Run, loop: dict, done_i: int) -> None:
    """Write the steps after step done_i as `skipped` into the record (early run end)."""
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
                "summary": _t("engine.skipped_ended_the_run", by=by),
            },
        )
        rn.log.event("step_done", step_name=name, step_type=step["type"], status=STEP_SKIPPED)
    if done_i < len(loop["steps"]):
        rn.emit(_t("engine.nothing_left_to_do_step", by=by, v2=len(loop["steps"]) - done_i))


def _run_steps(rn: _Run, loop: dict, start_i: int, resuming: bool) -> dict | None:
    """Process steps from start_i. Returns the paused record or None (status set)."""
    record = rn.record
    any_failed = False
    for i, step in enumerate(loop["steps"], 1):
        if i <= start_i:
            continue
        sname = step.get("name", f"step-{i}")
        # On resume the paused step is deliberately executed again — without another gate.
        condition = None if resuming and i == start_i + 1 else _approval_condition(step, rn.ctx)
        if condition is not None:
            # Gate: run pauses until someone approves (UI/chat/CLI)
            _set_step_record(record, i, {"name": sname, "type": step["type"], "status": "waiting"})
            approval = _approval_context(
                rn.spec, rn.trigger, reason="step", step=step, run_ctx=rn.ctx, condition=condition
            )
            return _pause_for_approval(rn, sname, approval, _approve_hint(rn, sname))
        outcome = _execute_step(rn, step, i)
        if outcome == "waiting":
            return record
        if outcome == "done":
            # Early, successful end; the tier gate still applies (see below).
            _skip_remaining(rn, loop, i)
            break
        if outcome != "pass":
            any_failed = True
            # gate (default true): a step error aborts the run. gate: false →
            # the error is noted, the loop continues, the run ends as fail.
            if step.get("gate", True):
                record["status"] = _ABORT_STATUS[outcome]
                rn.aborted_by = i - 1
                return None
    if any_failed:
        record["status"] = "fail"  # at least one gate:false step failed
        return None
    paused = _tier_gate(rn, loop)
    if paused is None:
        record["status"] = "pass"
    return paused


def _classify_run(rn: _Run) -> None:
    """Failure class of the failed run into the record (ADR 0035): abort outside the steps,
    otherwise aborting or first failed step. Successful runs get no field."""
    record = rn.record
    if record["status"] not in FAILED_RUN_STATUSES:
        return
    failure = rn.abort_failure or run_failure(record["steps"], rn.aborted_by)
    record.update(failure.fields())
    rn.emit(f"Fehlerklasse: {failure.kind.value}")


def _scope_note(project: str, loop_name: str, loop: dict, held_scope: str | None) -> list[str]:
    """Log note if the spec changed between claim and run (ADR 0020).

    The run follows the spec snapshot. If the loop switches from
    worktree-free to worktree meanwhile, it occupies no ``max_parallel`` slot —
    deliberately tolerated (short config window, at most one run over the limit)."""
    current = loop_lock_key(project, loop_name, loop)
    if held_scope is None or held_scope == current:
        return []
    return [_t("engine.config_changed_scope_run_follows", held_scope=held_scope, current=current)]


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
    replay_of: str = "",
) -> dict:
    """Runs a loop (synchronously). Returns the run record.

    actor: triggering user → ``started_by`` in the record (audit, ADR 0030).

    resume_id: continuation of a needs_approval run (after approval).
    initial_vars: trigger variables (e.g. issue title/body — untrusted!).
    run_id: predefined run id (server: returned to the UI in the POST response
    BEFORE the worker writes the record to disk — the UI
    can follow the run by id immediately without waiting for the file).
    held_scope: scope of the claim the caller took (`RunClaim.scope`);
    if it deviates from this run's spec state, it is noted in the run log.
    attempt: 0-based retry attempt number (ADR 0036); 0 = first run.
    retry_of: run id of the previous attempt (ADR 0036); "" = no retry.
    chain_key: key of the retry chain whose claim the attempt holds; ""
    = compute from spec + trigger.
    replay_of: run id of the original if this is a replay (ADR 0061);
    "" = no replay."""
    spec = load_projects().get(project_name)
    if not spec:
        raise SetupError(_t("engine.unknown_project", project_name=project_name))
    if spec.get("_error"):
        raise SetupError(f"Spec defekt: {spec['_error']}")
    loop = spec["loops"].get(loop_name)
    if not loop:
        raise SetupError(_t("engine.unknown_loop_available", loop_name=loop_name, v2=list(spec["loops"])))

    with project_github(spec), hosting.spec_context(spec):  # GitHub identities + hosting provider (ADR 0062)
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
            replay_of,
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
    replay_of: str = "",
) -> dict:
    """Body of :func:`run_loop` with an already resolved spec snapshot."""
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
        replay_of,
    )
    log = SessionLog(run_id, project_name, loop_name, trigger, record.get("spec_hash", ""))
    log.start()  # INSERT OR IGNORE — on resume the session already exists

    def emit(msg: str):
        line = _redact(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}")
        with config._log_lock, open(run_dir / f"{run_id}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ctx is bound before the try (empty) so the final record write can
    # always persist the variables — even if the run fails before the ctx build.
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
        emit(_t("engine.run_aborted", e=e))
        emit(traceback.format_exc(limit=3))

    # Always persist variables (not only at approval gates): the record
    # is the only source for {variables} on stateless resume and for
    # forensics — also on pass/error.
    _finish_workdir(rn, paused=False)
    _persist_vars(record, rn.ctx)
    _classify_run(rn)
    record["finished"] = config.utcnow().isoformat()
    try:
        _persist(project_name, run_id, record)
    except PreconditionError as e:
        # A reaper/stale check has meanwhile ended the run (running → error) and already
        # booked the retry chain and alerts: its record applies, the engine books nothing.
        emit(_t("engine.run_record_not_written_run", e=e))
        current = get_run(run_id, project_name) or record
        log.finish(current["status"], **session_totals(run_id))
        emit(f"■ {str(current['status']).upper()} — {loop_name}")
        return current
    except RunStoreError as e:
        # Corrupt on-disk state / I/O error: log finish, retry chain and alerts still run.
        emit(_t("engine.run_record_not_written", e=e))
    log.finish(record["status"], **session_totals(run_id))
    # Update the retry chain (ADR 0036): the state controls alert damping
    # (run_failed is suppressed while the chain is pending; run_exhausted
    # fires exactly once when the budget is used up). Best effort: a
    # store error (full file system) must not skip the alert.
    chain_info = retry_chains.on_run_finished_safe(spec, project_name, loop_name, record)
    sent = _notify(spec, record, retry_state=chain_info.get("state"))
    if sent:
        emit("⚠ Alerts gesendet: " + ", ".join(sent))
    emit(f"■ {record['status'].upper()} — {loop_name}")
    return record


def _builtin_handlers() -> dict[str, Callable[[dict, dict, Callable[[str], None]], dict]]:
    """Built-in step type → handler with the uniform ``(ctx, step, emit)`` signature.

    Resolved per call from this module's globals (tests patch ``engine._step_*``). `report` is inline
    in :func:`_dispatch_step`; third-party types live in :mod:`fabrikant.steps.registry`."""
    return {
        "shell": _step_shell,
        "git": lambda ctx, step, _emit: _step_git(ctx, step),
        "pr": lambda ctx, step, _emit: _step_pr(ctx, step),
        "agent": _step_agent,
        "review": _step_review,
        "pr_review": _step_pr_review,
        "pr_merge": lambda ctx, step, _emit: _step_pr_merge(ctx, step),
        "issue": lambda ctx, step, _emit: _step_issue(ctx, step),
        "human_gate": _step_human_gate,
        "escalation": _step_escalation,
        "http": _step_http,
        "llm": _step_llm,
        "notify": _step_notify,
        "state": _step_state,
        "verify": _step_verify,
        "alert_route": _step_alert_route,
    }


def _dispatch_step(ctx: dict, step: dict, emit) -> dict:
    t = step["type"]
    try:
        step = connections.apply_to_step(step)  # benannte/Default-Verbindung (ADR 0052)
    except connections.InvalidConnection as e:
        return failed(str(e), FailureKind.ENVIRONMENT)
    builtin = _builtin_handlers().get(t)
    if builtin is not None:
        return builtin(ctx, step, emit)
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
                    "summary": _t("engine.blocked_report_path_is_outside", p=p, root_res=root_res),
                }
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text + "\n", encoding="utf-8")
            emit(f"ℹ Report geschrieben: {p}")
            return {"ok": True, "summary": f"Report: {p}"}
        emit(f"ℹ {text}")
        return {"ok": True, "summary": text}
    plugin = step_registry.get_step(t)
    if plugin is not None:
        return plugin.handler(ctx, step, emit)
    raise ValueError(f"Unbekannter Step-Typ: {t}")


# ---------------------------------------------------------------- Worker-Pool
class _WorkerPool:
    """Bounded pool of daemon worker threads (size = FABRIKANT_WORKERS).

    Daemon threads (not ThreadPoolExecutor): a Ctrl+C ends the process
    immediately without waiting for running runs. Locally: FABRIKANT_WORKERS=1 → all
    runs serially on a single worker.
    """

    def __init__(self, size: int):
        self._size = max(1, int(size))
        self._queue: queue.Queue = queue.Queue()
        self._busy = 0  # Workers currently executing a work item (schedule view)
        self._busy_guard = threading.Lock()
        self._threads = [
            threading.Thread(target=self._run, daemon=True, name=f"fabrikant-worker-{i}") for i in range(self._size)
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
        """Workers that are currently busy."""
        return self._busy

    @property
    def waiting(self) -> int:
        """Queued, not yet started work items (approximately, like `Queue.qsize`)."""
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
    """Run launcher (port) for the configured substrate (FABRIKANT_LAUNCHER).

    Singleton. In-process (default) delegates to the existing worker pool
    (:func:`_get_run_pool`) and daemon threads; the scheduling policy stays in the
    core. New substrates (k8s, ecs) attach via :mod:`fabrikant.launcher.registry`
    without changing this code."""
    global _launcher
    with _launcher_guard:
        if _launcher is None:
            # Late binding (lambda, no function reference): `pool_factory()`/
            # `pool_or_none()` resolve the pool at call time from the
            # module global — so tests that monkeypatch `engine._get_run_pool`
            # or `engine._run_pool` respect the injected pool.
            _launcher = create_launcher(
                config.configured_launcher(),
                pool_factory=lambda: _get_run_pool(),
                pool_or_none=lambda: _run_pool,
            )
        return _launcher


# Queued, not yet started triggers (project, loop, trigger) → submission time
# (schedule view: queue with real submission times). The run record is only created
# in work(); reconcile polls would otherwise queue the same trigger again
# while the pool is busy.
_queued_triggers: dict[tuple[str, str, str], float] = {}
_queued_guard = threading.Lock()

# Triggers whose string identifies a one-off event (issue, Slack message):
# if a run record with exactly this trigger already exists, a waiting copy is discarded.
_ONCE_TRIGGER_PREFIXES = ("on_issue:", "on_slack:")


def _is_once_trigger(trigger: str) -> bool:
    """Does the trigger string identify a one-off event (execution-time dedup)?

    Besides on_issue/on_slack also the on_pr reconcile keys ``on_pr:#N@<sha>``
    (`reconcile: head`, ADR 0023) and ``on_pr:#N@u<stamp>`` (`reconcile: updated`): the
    scheduler only produces an ``@`` key in these modes and checks it against all run
    records. The plain ``on_pr:#N`` stays repeatable — event mode fires again on updates."""
    return trigger.startswith(_ONCE_TRIGGER_PREFIXES) or (trigger.startswith("on_pr:#") and "@" in trigger)


def queued_trigger_snapshot() -> list[dict]:
    """Pool queue: queued trigger runs (cron/file/issue/PR), sorted by submission.

    `enqueued_at` = timestamp (epoch) of submission — no record exists
    yet, so this is the only persistence-free source for the wait time."""
    with _queued_guard:
        return [
            {"project": p, "loop": ln, "trigger": tg, "enqueued_at": ts}
            for (p, ln, tg), ts in sorted(_queued_triggers.items(), key=lambda kv: kv[1])
        ]


def pool_status() -> LauncherStatus:
    """Substrate status of the configured run launcher (ADR 0057): size,
    busy workers (`busy`) + queue length.

    Delegates to the launcher — the in-process variant reports the
    configured size (FABRIKANT_WORKERS) as long as the pool is not yet
    created (not 0 — misleading)."""
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
    """Start a trigger run in the worker pool; discard instead of waiting if no slot is free.

    `done` is set as soon as the trigger is handled — ran, discarded or (already
    queued) not queued at all. The scheduler thereby waits for `on_start` fires.
    Per queued call exactly one of the optional callbacks runs:
    `on_claimed` after a successful claim (before the run), otherwise `on_rejected`
    (pause, already ran, limit/lock, error before the claim). An identical trigger
    that is already queued is ignored (no callback)."""
    # Resolve the launcher before mutating `_queued_triggers`: an unknown
    # substrate (FABRIKANT_LAUNCHER) raises without permanently suppressing the trigger.
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
        # From here on the claim (active run) or the record protects against duplicates.
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
            print(f"[fabrikant] Trigger-Run {name}/{lname} fehlgeschlagen: {e}")
        finally:
            claim.release()

    try:
        launcher.submit(work)
    except Exception:
        # Submit failed (e.g. invalid FABRIKANT_WORKERS when building the pool):
        # do not suppress the trigger permanently — remove the entry again.
        with _queued_guard:
            _queued_triggers.pop(qkey, None)
        raise


def _claim_trigger_run(name: str, lname: str, trigger: str, initial_vars: dict | None) -> RunClaim | None:
    """Execution-time checks of a trigger run; claim or None (= discarded)."""
    # Check pause/disable at execution time (ADR 0019): the work item may have
    # sat in the pool queue for a long time; the scheduler only checks when
    # queueing — otherwise all waiting triggers would still run after a pause.
    state = pause.effective_state(name, lname)
    if state != pause.ACTIVE:
        print(f"[fabrikant] Trigger-Run {name}/{lname} ({trigger}) verworfen: {state}")
        return None
    # Persistent dedup at execution time (one-off events: on_issue,
    # on_slack, on_pr reconcile keys `@<sha>`/`@u<stamp>`): the work item may have sat in the
    # pool queue for a long time while an earlier run for the same event had already finished
    # and its record been written. The poll-time dedup is then not enough — otherwise
    # all queued copies run one after another (double runs, issue #226, Oct 2026).
    # Plain `on_pr:#N` stays excluded: event mode intentionally fires again on PR updates.
    # Checked while holding the claim: a copy of the same trigger has the same claim key, so an
    # earlier run is either finished (record visible) or still active (claim refused). Checking
    # before the claim would leave a gap — check, earlier run finishes and releases, claim.
    # Over the limit (max_parallel), with an active run for loop + issue/PR or
    # a waiting priority claim, the trigger is discarded; the next
    # poll/cron tick tries again.
    claim = claim_run(name, lname, initial_vars)
    if claim is None or not _is_once_trigger(trigger):
        return claim
    try:
        seen = trigger in loop_run_triggers(name, lname)
    except BaseException:
        claim.release()
        raise
    if seen:
        claim.release()
        return None
    return claim


def _run_callback(callback: Callable[[], None] | None, name: str, lname: str) -> None:
    """Run a trigger callback; log errors with exception type only (no abort)."""
    if callback is None:
        return
    try:
        callback()
    except Exception as e:
        print(f"[fabrikant] Trigger-Status {name}/{lname} fehlgeschlagen: {type(e).__name__}")


def _write_queued_record(
    name: str, lname: str, run_id: str, trigger: str, note: str, initial_vars: dict | None, actor: str | None = None
):
    """Placeholder record (status `queued`): the UI shows the waiting state, and a
    server restart resumes the run (requeue_queued_runs). run_loop
    overwrites it at start."""
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
    # New: file missing; requeue after restart: an existing queued placeholder is overwritten.
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
    """Start a manual run (UI/API/chat) in a daemon thread — never discard.

    If a slot is free, it runs immediately. Otherwise (limit reached or run for
    loop + issue/PR active) it is persisted as `queued` and waits with
    priority over trigger runs (ADR 0015). Returns True if queued.
    RuntimeError if the thread does not start (claim/wait counter are then
    free again)."""
    queued = False

    def work(held: RunClaim) -> None:
        try:
            # Disabled while waiting (ADR 0019) → do not start any more.
            if blocked := pause.manual_block_reason(name, lname):
                print(f"[fabrikant] Run {name}/{lname} verworfen: {blocked}")
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
            print(f"[fabrikant] Run {name}/{lname} fehlgeschlagen: {e}")
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
    """Daemon thread for a run with priority over trigger runs (ADR 0015).

    The helper first claims non-blockingly. If no slot is free,
    it synchronously registers a priority waiter and the thread waits
    blockingly for the slot. `work(held)` catches its own errors; the
    claim is released afterwards in any case. If the thread does not start,
    the claim is released or the wait counter taken back,
    `on_start_error()` is called and the RuntimeError is passed on.
    `exclude_run_id` (resume): the resumed run does not block its own
    claim (ADR 0043). Returns whether the run was queued."""
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
        assert acquired is not None  # blocking=True always returns a claim
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
    """A still `queued` record (run_loop failed before writing, e.g.
    loop unknown) becomes `error` — otherwise it would hang in the UI forever."""
    rec = get_run(run_id, name)
    if rec and rec.get("status") == "queued":
        rec["status"] = "error"
        rec["error"] = error
        rec.update((failure or Failure()).fields())
        # Compare-and-set: if run_loop has meanwhile taken over the record (running), it stays.
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
            print(f"[fabrikant] Requeue {run['id']} fehlgeschlagen: {e}")
            continue
        started.append(run["id"])
    return started
