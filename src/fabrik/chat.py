"""Chat (UI + API): Befehle + optionaler LLM-Copilot."""

from __future__ import annotations

import contextlib
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from . import auth, config, pause
from . import llm as llm_registry
from .approval import ApprovalRequest
from .auth import Principal
from .engine import reject_run, run_loop, spawn_priority_worker, start_priority_run
from .i18n import t
from .runs import RunClaim, get_run, list_runs
from .runstore import STORE, PreconditionError
from .specs import _validate_spec, load_projects, load_spec, spec_path
from .steps import _notify

_CHAT_MAX_TOKENS = 8000
# Config hint for the copilot (details: docstring of `llm`). CLI providers
# need their name in FABRIK_LLM_PROVIDERS — `_CLI` alone is not enough.
_CHAT_TIMEOUT_S = 180

_pending_specs: dict[str, tuple[str, Path]] = {}  # projekt -> (yaml-text, pfad)
_pending_guard = threading.Lock()

# In-flight chat requests: req_id -> {"cancel": Event}
# Makes it possible to cancel a running chat request (UI button / POST /api/chat/cancel).
# The cancel takes effect via the server handler's wait loop (the worker thread is
# no longer awaited); the LLM call runs to completion in the background (daemon).
_inflight: dict[str, dict] = {}
_inflight_guard = threading.Lock()


def _register_chat(req_id: str) -> threading.Event:
    """Idempotent: if the req_id already exists, the existing event is returned."""
    with _inflight_guard:
        slot = _inflight.get(req_id)
        if slot is None:
            slot = {"cancel": threading.Event()}
            _inflight[req_id] = slot
        return slot["cancel"]


def _cancel_chat(req_id: str) -> bool:
    """Sets the cancel flag. True if the req_id was known."""
    with _inflight_guard:
        slot = _inflight.get(req_id)
    if not slot:
        return False
    slot["cancel"].set()
    return True


def _finish_chat(req_id: str) -> None:
    with _inflight_guard:
        _inflight.pop(req_id, None)


def _chat_start_run(name: str, lname: str, note: str, actor: str | None = None) -> str:
    """Start a chat run; queue it if the project is busy (priority over trigger runs, ADR 0015)."""
    blocked = pause.manual_block_reason(name, lname)
    if blocked:
        return f"⊘ {blocked}"
    run_id = f"{config.now_id()}-{lname}"
    try:
        queued = start_priority_run(name, lname, "chat", run_id=run_id, note=note, actor=actor)
    except RuntimeError as e:
        return t("chat.worker_start_failed", name=name, loop=lname, error=e)
    if queued:
        return t("chat.queued", name=name, loop=lname, run_id=run_id)
    return t("chat.started", name=name, loop=lname, run_id=run_id)


_STATE_VERBS = ("pause ", "pausiere ", "resume ", "fortsetzen ", "disable ", "deaktiviere ")
_VERB_STATE = {
    "pause": pause.PAUSED,
    "pausiere": pause.PAUSED,
    "resume": pause.ACTIVE,
    "fortsetzen": pause.ACTIVE,
    "disable": pause.DISABLED,
    "deaktiviere": pause.DISABLED,
}


def _chat_set_state(parts: list[str], actor: str | None = None) -> str:
    """Chat-Befehl `pause|resume|disable <projekt> [loop]` (ADR 0019)."""
    if len(parts) < 2:
        return t("chat.state_format")
    loop = parts[2] if len(parts) > 2 else None
    try:
        pause.set_state(parts[1], loop, _VERB_STATE[parts[0].lower()], actor=f"chat:{actor}" if actor else "chat")
    except ValueError as e:
        return f"✖ {e}"
    scope = f"{parts[1]}/{loop}" if loop else parts[1]
    return f"✔ {scope}: {_VERB_STATE[parts[0].lower()]}"


def _approval_detail(ctx: dict | None) -> str:
    """Approval context as chat line(s): what happens, which permissions, which issue.

    Empty if the run has no structured context (older runs)."""
    lines = ApprovalRequest.from_dict(ctx).lines()
    return ("\n" + "\n".join(lines)) if lines else ""


# Run ids whose resume worker has already been started in this process (running
# or waiting for a run slot) — prevents duplicate workers on
# repeated approve and on requeue after restart (ADR 0015).
_resume_workers: set[str] = set()
_resume_guard = threading.Lock()


def _append_run_log(project: str, run_id: str, message: str) -> None:
    """Append a message to a run's file log."""
    line = f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}"
    with config._log_lock, open(config.RUNS_DIR / project / f"{run_id}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _revoke_approval(project: str, run_id: str) -> None:
    """Revoke the approval of a run that was not resumed (best effort): if the run
    has meanwhile been decided (resumed/rejected), it stays unchanged."""
    with contextlib.suppress(PreconditionError):
        STORE.revoke_approval(project, run_id)


def _start_resume_worker(project: str, run: dict) -> bool:
    """Starts the resume worker for an approved run.

    The worker blocks as needed (with priority over new
    trigger runs, ADR 0015) until a slot is free or an active run for
    loop + issue/PR (dedup) ends, then checks again whether the run is still
    needs_approval (a reject while waiting wins) and the loop has not
    been disabled in the meantime (ADR 0019 — the run then stays
    needs_approval and can be approved again after resume). False
    if a worker for the run already exists or the thread start
    fails."""
    run_id = run["id"]
    with _resume_guard:
        if run_id in _resume_workers:
            return False
        _resume_workers.add(run_id)

    def discard() -> None:
        with _resume_guard:
            _resume_workers.discard(run_id)

    def work(held: RunClaim) -> None:
        try:
            # Disabled while waiting (ADR 0019) → do not resume;
            # the run stays needs_approval, the approval is revoked
            # (otherwise requeue_approved_runs would schedule it again on every start)
            # and discard() allows approving again.
            if blocked := pause.manual_block_reason(project, run["loop"]):
                print(f"[fabrik] Resume {run_id} verworfen: {blocked}")
                _revoke_approval(project, run_id)
                return
            current = get_run(run_id, project)
            if current and current["status"] == "needs_approval":
                run_loop(project, run["loop"], trigger="approve", resume_id=run_id, held_scope=held.scope)
        except Exception as e:
            print(f"[fabrik] Resume {run_id} fehlgeschlagen: {e}")
        finally:
            discard()

    def on_queued() -> None:
        if run.get("project"):
            STORE.mark_approval_queued(run["project"], run["id"], run, run.get("approved_by"))

    try:
        spawn_priority_worker(
            project,
            run["loop"],
            run.get("vars"),
            work=work,
            on_queued=on_queued,
            on_start_error=discard,
            exclude_run_id=run_id,
        )
    except RuntimeError:
        return False
    return True


def _chat_resume(project: str, run_id: str, approve: bool, actor: str | None = None) -> tuple[str, bool]:
    """Approve/reject a needs_approval run.

    Returns (reply, resumed): resumed=True if a resume worker for the
    run exists — NOT that it has already written "running" to disk.
    If the project is at its limit (max_parallel), the approval is persisted as
    `approval_queued` and the worker waits for a slot (ADR 0015);
    the run stays needs_approval until then. False = thread start error.
    actor: user name for the audit (``approved_by``/``rejected_by`` in the run record)."""
    run = get_run(run_id, project)
    if not run:
        return t("chat.run_not_found", run_id=run_id), False
    if run["status"] != "needs_approval":
        return t("chat.not_needs_approval", run_id=run["id"], status=run["status"]), False
    run.setdefault("started_by", None)
    run.setdefault("approved_by", None)
    run.setdefault("rejected_by", None)
    detail = _approval_detail(run.get("approval") or {})
    if not approve:
        # Atomic rejection (rules out a concurrent resume, ADR 0034) incl. cleanup
        # and retry-chain booking.
        try:
            run = reject_run(project, run, actor, lambda msg: _append_run_log(project, run_id, msg))
        except PreconditionError as e:
            return t("chat.cannot_reject", run_id=run["id"], error=e), False
        return t("chat.rejected", run_id=run["id"], detail=detail), False
    with _resume_guard:
        already = run_id in _resume_workers
    if already:
        return t("chat.already_approved", run_id=run["id"], project=project), True
    if actor:
        # Also with a waiting approval: it is accepted (approval_queued) — do not lose the audit.
        try:
            STORE.approve(project, run_id, run, actor)
        except PreconditionError as e:
            return t("chat.cannot_approve", run_id=run["id"], error=e), False
    try:
        started = _start_resume_worker(project, run)
    except PreconditionError as e:
        # mark_approval_queued failed (e.g. reject while waiting) — the run is
        # already decided, nothing to do.
        return t("chat.resume_error", run_id=run["id"], error=e), False
    if not started:
        if run.get("approval_queued") or actor:
            # No worker started: revoke approval audit/queue marker (run stays subject to approval).
            _revoke_approval(project, run_id)
        return t("chat.thread_start_failed", run_id=run["id"]), False
    if run.get("approval_queued"):
        return t("chat.approval_saved", run_id=run["id"], project=project, detail=detail), True
    return t("chat.approval_resuming", run_id=run["id"], step=run.get("waiting_step", "?"), detail=detail), True


def requeue_approved_runs() -> list[str]:
    """Starts resume workers for approved runs that are still waiting (server start).

    Without this, an approval that was still waiting for a run slot at restart
    would be lost (the run would hang in needs_approval). Returns the run ids."""
    started = []
    for run in list_runs(limit=None):
        queued = run.get("status") == "needs_approval" and run.get("approval_queued")
        if queued and _start_resume_worker(run["project"], run):
            started.append(run["id"])
    return started


def _chat_alert_test(project: str | None) -> str:
    projects = load_projects()
    sent_all = []
    for name, spec in projects.items():
        if spec.get("_error"):
            continue
        if project and name != project:
            continue
        if not (spec.get("notify") or {}):
            continue
        rec = {
            "project": name,
            "loop": "alert-test",
            "status": "fail",
            "id": f"test-{config.now_id()}",
            "trigger": "chat-test",
        }
        sent_all += [f"{name}:{s}" for s in _notify(spec, rec)]
    if not sent_all:
        return t("chat.alert_no_notify")
    return t("chat.alert_sent", sent=", ".join(sent_all))


def _llm_chat(message: str, cancel_event: threading.Event | None = None, req_id: str = "") -> str | None:
    """OpenAI-compatible endpoint (active provider). Without config → None.

    ``cancel_event``: if set → the running LLM read is aborted
    (connection closed) and '⊘ cancelled' is returned."""
    prov = llm_registry.active_provider()
    if not prov:
        return None
    specs = load_projects()
    spec_text = "\n\n".join(
        f"### {p['name']}\n```yaml\n{Path(p['_path']).read_text()}\n```" for p in specs.values() if not p.get("_error")
    )
    fails = []
    for name in specs:
        for r in list_runs(name, limit=5):
            if r["status"] in ("fail", "error"):
                logf = config.RUNS_DIR / name / f"{r['id']}.log"
                tail = "\n".join(logf.read_text(encoding="utf-8").splitlines()[-25:]) if logf.is_file() else ""
                fails.append(f"### {r['id']} ({r['loop']})\n{tail}")
                if len(fails) >= 2:
                    break
    sys_prompt = (
        f"{t('chat.sys_prompt')}\n\n{t('chat.sys_specs_header')}\n{spec_text}\n\n"
        f"{t('chat.sys_fails_header')}\n" + ("\n".join(fails) if fails else t("chat.sys_no_fails"))
    )
    if cancel_event is not None and cancel_event.is_set():
        return t("chat.cancelled")
    try:
        res = llm_registry.complete(
            prov,
            sys_prompt,
            message,
            # 8000: Qwen3 thinking tokens + complete spec (YAML block) must fit,
            # otherwise the proposal is truncated and not applicable.
            max_tokens=_CHAT_MAX_TOKENS,
            timeout=_CHAT_TIMEOUT_S,
            cancel_event=cancel_event,
        )
    except Exception as e:
        if cancel_event is not None and cancel_event.is_set():
            return t("chat.cancelled")
        return t("chat.llm_failed", error=e)
    if not res["text"]:
        return t("chat.llm_empty", reason=res["finish_reason"])
    text = res["text"].strip()
    # Spec-Vorschlag? → validieren + pfeilen
    m = re.search(r"```ya?ml\n(.*?)```", text, re.S)
    if m:
        try:
            import yaml

            data2 = yaml.safe_load(m.group(1))
            if isinstance(data2, dict) and data2.get("name"):
                name = data2["name"]
                p = spec_path(name)
                if not Path(data2.get("root", "")).is_dir():
                    return t("chat.proposal_missing_root", name=name, root=data2.get("root"), text=text)
                _validate_spec(data2)
                with _pending_guard:
                    _pending_specs[name] = (m.group(1).strip(), p)
                text += t("chat.proposal_valid", name=name, path=p)
        except Exception as e:
            text += t("chat.proposal_invalid", error=e)
    elif re.search(r"```ya?ml", text):
        # Opening fence without closing one → answer was truncated
        # (max_tokens/context) → no applicable proposal.
        text += t("chat.proposal_truncated")
    return text


def _chat(message: str, project_hint: str = "", req_id: str = "", who: Principal | None = None) -> str:
    m = message.strip()
    low = m.lower()
    parts = m.split()
    actor = who.name if who else None
    projects = load_projects()

    if low in ("help", "hilfe", "?"):
        return t("chat.help")
    if low in ("list", "liste", "projekte"):
        lines = []
        for name, spec in projects.items():
            if spec.get("_error"):
                lines.append(f"✖ {name}: {spec['_error']}")
                continue
            loops = ", ".join(spec["loops"])
            lines.append(f"● {name} — {loops}")
        return "\n".join(lines) or t("chat.no_projects")
    if low.startswith(("show ", "spec ")):
        name = parts[1] if len(parts) > 1 else project_hint
        p = spec_path(name, projects)
        if not p.is_file():
            return t("chat.no_project", name=name, available=", ".join(projects))
        return f"```yaml\n{p.read_text()}\n```"
    if low.startswith(("lauf ", "run ")):
        if len(parts) < 3:
            return t("chat.run_format")
        name, lname = parts[1], parts[2]
        if name not in projects:
            return t("chat.unknown_project", name=name)
        if lname not in (projects[name].get("loops") or {}):
            return t("chat.unknown_loop", loop=lname, available=", ".join(projects[name]["loops"]))
        return _chat_start_run(name, lname, " ".join(parts[3:]), actor=actor)
    if low in ("runs",) or low.startswith("runs "):
        proj = parts[1] if len(parts) > 1 else None
        lines = [f"{r['id']}  {r['project']}/{r['loop']}  {r['status']}" for r in list_runs(proj, 10)]
        return "\n".join(lines) or t("chat.no_runs")
    if low.startswith(("log ", "logs ")):
        if len(parts) < 2:
            return t("chat.log_format")
        rid = parts[1]
        f = None
        if config.RUNS_DIR.is_dir():
            for d in config.RUNS_DIR.iterdir():
                if (d / f"{rid}.log").is_file():
                    f = d / f"{rid}.log"
                    break
        if not f:
            return t("chat.log_not_found", run_id=rid)
        return "```\n" + "\n".join(f.read_text(encoding="utf-8").splitlines()[-40:]) + "\n```"
    if low.startswith(("approve ", "freigeben ")):
        rid = parts[1] if len(parts) > 1 else ""
        run_rec = get_run(rid, None)
        reply, _ = _chat_resume(run_rec["project"] if run_rec else project_hint, rid, True, actor=actor)
        return reply
    if low.startswith(("reject ", "ablehnen ")):
        rid = parts[1] if len(parts) > 1 else ""
        run_rec = get_run(rid, None)
        reply, _ = _chat_resume(run_rec["project"] if run_rec else project_hint, rid, False, actor=actor)
        return reply
    if low.startswith(_STATE_VERBS):
        return _chat_set_state(parts, actor=actor)
    if low.startswith("alert test"):
        return _chat_alert_test(parts[2] if len(parts) > 2 else None)
    if low.startswith("apply "):
        if who and not who.allows(auth.ADMIN):
            return t("chat.admin_required")
        name = parts[1]
        with _pending_guard:
            pend = _pending_specs.get(name)
        if not pend:
            return t("chat.no_proposal", name=name)
        text, p = pend
        p.write_text(text + "\n")
        _validate_spec(load_spec(p))
        with _pending_guard:
            _pending_specs.pop(name, None)
        return t("chat.spec_updated", path=p)
    # otherwise: LLM (cancellable via the in-flight registry)
    if not req_id:
        req_id = config.now_id()
    cancel_event = _register_chat(req_id)
    try:
        ans = _llm_chat(message, cancel_event, req_id)
    finally:
        _finish_chat(req_id)
    if ans is None:
        return t("chat.no_llm", hint=t("chat.no_llm_hint"))
    return ans
