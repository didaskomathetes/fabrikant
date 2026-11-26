"""Step: agent (pluggables Backend, z. B. opencode; Legacy-CLI via cmd)."""

from __future__ import annotations

import contextlib
import json
import os
import time

from .. import config, sandbox
from ..backends import AgentBackend, create_backend, known_backends
from ..backends._oneshot import OneShotCliBackend
from ..billing import Billing
from ..billing import for_step as billing_for_step
from ..context import _cap, _substitute, step_workdir
from ..failures import ConfigError, Failure, classify_agent_output, describe_exit, failed, from_result
from ..gitops import cleared_tokens, github_env, subprocess_env, token_overrides
from ..i18n import t as _t
from ..runmodel import FailureKind
from ..secrets import _redact
from ..spec_sources import resolve_relative
from ..tokens import TokenCounts
from .extra_context import render_extra_context
from .shell import _step_shell


def _step_agent(ctx: dict, step: dict, emit) -> dict:
    """Agent step: pluggable backend (e.g. opencode) or legacy CLI (cmd).

    Backend choice: step['backend'] > FABRIKANT_AGENT_BACKEND.
    opencode: endpoint/model from step or FABRIKANT_AGENT_ENDPOINT / FABRIKANT_AGENT_MODEL.
    Prompt: step['prompt'] (inline) or step['prompt_file'] (file, relative to the spec,
    fallback fabrikant root — ADR 0047; {vars} are substituted).
    """
    prompt = (step.get("prompt") or "").strip()
    if not prompt and step.get("prompt_file"):
        pf = resolve_relative(step["prompt_file"], ctx.get("spec_path"))
        if not pf.is_file():
            return failed(_t("steps.agent.prompt_file_missing", pf=pf), FailureKind.ENVIRONMENT)
        prompt = pf.read_text(encoding="utf-8").strip()
    if not prompt:
        return failed(_t("steps.agent.agent_step_prompt_or_prompt"), FailureKind.ENVIRONMENT)
    prompt = _substitute(prompt, ctx) + render_extra_context(ctx, step, emit)
    backend_name = (step.get("backend") or os.environ.get("FABRIKANT_AGENT_BACKEND", "")).strip()
    if backend_name:
        return _step_agent_backend(ctx, step, emit, backend_name, prompt)
    # Legacy: Agent-CLI via cmd (z.B. claude -p {prompt_file})
    cmd = (step.get("cmd") or "").strip()
    if not cmd:
        emit(_t("steps.agent.agent_step_without_cmd_backend"))
        return {"ok": True, "summary": "skipped (no agent backend)"}
    prompt_file = config.RUNS_DIR / ctx["project"] / f"{ctx['run_id']}-{step.get('name', 'agent')}.prompt.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt)
    ctx2 = dict(ctx, prompt_file=str(prompt_file))
    return _step_shell(ctx2, {**step, "cmd": _substitute(cmd, ctx2)}, emit)


def _emit_agent_prompt(emit, prompt: str) -> None:
    """Agent prompt (input) into the run log — capped so large prompts do not
    blow up the log (full text stays in the session / prompt file)."""
    p = prompt.strip()
    if not p:
        return
    cap = config.AGENT_LOG_PROMPT_CAP
    truncated = len(p) > cap
    emit(f"  agent prompt ({len(p)} characters):")
    for line in p[:cap].splitlines():
        emit(f"    {line}")
    if truncated:
        emit(f"    … (truncated, {len(p) - cap} more characters)")


def _emit_agent_message(emit, m: dict) -> None:
    """Emit an assistant message to the run log: text (truncated) + tool calls."""
    for part in m.get("parts", []):
        t = part.get("type")
        if t == "text":
            txt = (part.get("text") or "").strip()
            if txt:
                cap = config.AGENT_LOG_TEXT_CAP
                emit(f"  agent sagt: {txt[:cap]}{'…' if len(txt) > cap else ''}")
        elif t == "tool":
            name = "tool"
            with contextlib.suppress(Exception):
                name = json.loads(part.get("text", "{}")).get("name", "tool")
            status = (part.get("state") or {}).get("status", "?")
            emit(f"  agent tool: {name} [{status}]")


def _emit_new_agent_messages(emit, msgs: list[dict], emitted_n: int, flush: bool = False) -> int:
    """Emit new, COMPLETED assistant messages into the run log.

    The last message is held as "in progress" (finish=None/
    "tool-calls") until a follow-up message arrives OR `flush=True`
    (session end) — otherwise a still-growing tool part would be only half
    emitted. Returns the new count of emitted messages.
    """
    complete_n = len(msgs)
    if not flush and msgs:
        fin = (msgs[-1].get("info") or {}).get("finish")
        if fin is None or fin == "tool-calls":
            complete_n = len(msgs) - 1
    for m in msgs[emitted_n:complete_n]:
        _emit_agent_message(emit, m)
    return complete_n


def _record_agent_messages(lg, msgs: list[dict], recorded_n: int, step_name: str, flush: bool = False) -> int:
    """Persist every completed backend message, including full tool payloads."""
    complete_n = len(msgs)
    if not flush and msgs:
        fin = (msgs[-1].get("info") or {}).get("finish")
        if fin is None or fin == "tool-calls":
            complete_n -= 1
    for message in msgs[recorded_n:complete_n]:
        for part in message.get("parts", []):
            kind = part.get("type")
            if kind == "text":
                text = part.get("text") or ""
                if text:
                    lg.event("message", step_name=step_name, step_type="agent", text=text)
            elif kind == "tool":
                payload = dict(part)
                raw = payload.get("text")
                if isinstance(raw, str):
                    with contextlib.suppress(ValueError, TypeError):
                        payload["call"] = json.loads(raw)
                lg.event("tool_call", step_name=step_name, step_type="agent", tool=payload)
    return complete_n


def _session_failure(last: dict) -> Failure:
    """Class of a `failed` session (ADR 0035): the backend's statement, otherwise `retryable` → transient,
    otherwise text fallback for the agent CLI output."""
    reported = from_result(last)
    if reported.kind is not FailureKind.UNKNOWN:
        return reported
    if last.get("retryable"):
        return Failure(FailureKind.TRANSIENT)
    return classify_agent_output(str(last.get("text") or ""))


def _session_failed(last: dict, sid: str, usage: dict, emit) -> dict:
    """Step result of a failed session. Exit code (with signal name) into the summary, the end
    of stderr (one-shot CLIs: `stderr_tail`) redacted into the run log and as `output` into the record —
    on a crash the cause is there, not in the truncated `text`."""
    code = last.get("exit_code")
    exit_note = f" ({describe_exit(code)})" if isinstance(code, int) else ""
    extra: dict = {"exit_code": code} if isinstance(code, int) else {}
    if tail := _redact(str(last.get("stderr_tail") or "")):
        emit("  agent stderr (Ende):")
        for line in tail.splitlines():
            emit(f"    {line}")
        extra["output"] = _cap(tail)
    return failed(
        f"agent session fehlgeschlagen{exit_note}: {str(last.get('text', ''))[:120]} (session {sid})",
        _session_failure(last),
        usage=usage,
        **extra,
    )


class _Progress:
    """Progress of a session (new messages/tools/text) for the timeout class (ADR 0035 no. 2)."""

    def __init__(self, started: float):
        self.started = started
        self.last_change = started
        self.seen_any = False
        self._sig: tuple | None = None

    def observe(self, messages: list[dict], last: dict) -> None:
        sig = (len(messages), len(last.get("tools") or []), last.get("text") or "")
        if sig == self._sig:
            return
        # First state: the transcript already contains the sent prompt — progress only if the agent
        # has already delivered tools/text; otherwise a session without any answer would be "working".
        progressed = self._sig is not None or any(sig[1:])
        self._sig = sig
        if progressed:
            self.last_change = time.time()
            self.seen_any = True

    def timeout_kind(self, now: float, window: int, observable: bool) -> FailureKind:
        """Timeout without progress in the last `window` (s) or without any progress → transient (hanging
        session); agent was still working → outcome. Without observable progress (one-shot CLI without
        transcript) → unknown. `window` is at most the runtime since start — otherwise a session that
        never delivered anything would be "still working" at `timeout < window`."""
        if not self.seen_any:
            return FailureKind.TRANSIENT if observable else FailureKind.UNKNOWN
        stalled = now - self.last_change >= min(window, now - self.started)
        return FailureKind.TRANSIENT if stalled else FailureKind.OUTCOME


def _apply_github_identity(be: AgentBackend, step: dict) -> str | None:
    """`github_identity: dev|reviewer` → `gh` in the agent process runs as this identity (ADR 0010).

    One-shot CLI backends only: `opencode serve` is a foreign process whose env
    the step cannot set. Without an identity, one-shot agents on the host still get the
    project settings (token file, `commit_via` for `python -m fabrikant.push`). In the
    sandbox (ADR 0054), by contrast, NO token: without `github_identity` all token variables are empty —
    otherwise every container would silently carry the project's builder token (GitHub is reachable
    via the egress proxy). Returns an error message or None."""
    role = str(step.get("github_identity") or "").strip()
    if not role:
        if isinstance(be, OneShotCliBackend) and sandbox.sandbox_enabled():
            be.extra_env = cleared_tokens()
        elif isinstance(be, OneShotCliBackend):
            # Override inherited global tokens (empty = `gh` ignores them), otherwise `gh` runs as the wrong identity.
            be.extra_env = {**token_overrides(), **subprocess_env()}
        return None
    if not isinstance(be, OneShotCliBackend):
        return _t("steps.agent.github_identity_only_for_cli", v1=be.name)
    try:
        be.extra_env = github_env(role)
    except (RuntimeError, ValueError) as e:
        return f"github_identity {role!r}: {e}"
    return None


def _detected_billing(be: AgentBackend) -> Billing:
    """Billing mode the backend derives from its auth (optional part of the port)."""
    detect = getattr(be, "billing", None)
    detected = detect() if callable(detect) else None
    return detected if isinstance(detected, Billing) else Billing.UNKNOWN


def _agent_usage(
    last: dict, backend_name: str, model: str, carry: dict | None = None, billing: Billing = Billing.UNKNOWN
) -> dict:
    """Usage dict (tokens, costs) from the last poll result.

    ``carry`` = consumption of earlier, restarted attempts — failed
    and aborted sessions also cost tokens and belong in the sums."""
    carry = carry or {}
    tokens = TokenCounts.from_poll(last.get("tokens")) + TokenCounts.from_usage(carry)
    usage: dict = {
        "model": model or "-",
        "provider": backend_name,
        **tokens.usage_fields(),
        "billing": billing.value,
    }
    cost = float(last.get("cost") or 0) + float(carry.get("cost_usd", 0.0))
    if cost:
        usage["cost_usd"] = cost
    return usage


def _record_cost(lg, step_name: str, usage: dict, last: dict | None = None) -> None:
    """``cost`` event into the session log (telemetry, not a summand — see sessionlog).

    ``last``: poll result; a ``premium_requests`` counter (copilot) goes into the event too."""
    if lg is not None:
        extra = {}
        if (premium := (last or {}).get("premium_requests")) is not None:
            extra["premium_requests"] = premium
        lg.event(
            "cost",
            step_name=step_name,
            step_type="agent",
            tokens_in=usage["prompt_tokens"],
            tokens_out=usage["completion_tokens"],
            cost_usd=usage.get("cost_usd", 0.0),
            **extra,
        )


def _step_agent_backend(ctx: dict, step: dict, emit, backend_name: str, prompt: str) -> dict:
    """Agent step via a pluggable backend (opencode, copilot, …).

    Robustness against transient backend errors (ADR 0012):
    - **Transport errors** during polling (`be.poll()`/`be.transcript()`
      raise, e.g. opencode serve down/restart) do NOT kill the run — the
      step keeps polling with backoff until the step timeout is reached. Only
      then is the persistent error reported.
    - **Status errors** (`poll()` → `failed` + `retryable`) restart the session
      (`step.retries`, default 2 → max. 3 attempts, backoff 5s, 15s, …).
    """
    stale_after = int(str(step.get("stale_after") or os.environ.get("FABRIKANT_AGENT_STALE_AFTER", 900)))
    if backend_name not in known_backends():
        raise ConfigError(
            _t(
                "steps.agent.agent_step_unknown_backend_available",
                backend_name=backend_name,
                v2=", ".join(known_backends()),
            )
        )
    endpoint = (step.get("endpoint") or os.environ.get("FABRIKANT_AGENT_ENDPOINT", "")).strip()
    model = (step.get("model") or os.environ.get("FABRIKANT_AGENT_MODEL", "")).strip()
    if backend_name == "opencode":
        # In the sandbox (ADR 0054) the config comes from the LLM connection — the endpoint
        # is ignored, only the model (<connection>/<model>) is needed.
        if not model or (not endpoint and not sandbox.sandbox_enabled()):
            return failed(
                _t("steps.agent.opencode_backend_endpoint_model_missing"),
                FailureKind.ENVIRONMENT,
            )
    elif backend_name not in ("codex", "copilot") and not model:
        # claude_code: endpoint is irrelevant (CLI backend), only model needed
        # (codex/copilot: model optional — the CLI has a default model)
        return failed(
            _t("steps.agent.backend_model_missing_step_model", backend_name=backend_name), FailureKind.ENVIRONMENT
        )
    be: AgentBackend = create_backend(backend_name, step, endpoint, model, stale_after)
    identity_err = _apply_github_identity(be, step)
    # Billing mode: spec `billing:` takes precedence over detection from the auth (backends without detection: unknown).
    billing = billing_for_step(step, _detected_billing(be))
    if identity_err:
        return failed(identity_err, FailureKind.ENVIRONMENT)
    if not be.health():
        # diagnose() names the cause (wrong port, 401/password, CLI not
        # in PATH) — instead of just reporting "unhealthy".
        return failed(be.diagnose() or f"{backend_name} unhealthy: {endpoint}", be.unhealthy_kind())
    workdir = step_workdir(ctx, step)
    timeout = int(step.get("timeout", 1800))
    # Transient backend errors (e.g. copilot: model call dies without final
    # answer → API outage) are restarted instead of killing the run.
    # `step.retries` = number of RESTARTS after the first attempt (default 2
    # → max. 3 attempts). Backoff between attempts: 5s, 15s, …
    max_retries = max(0, int(step.get("retries", 2)))
    lg = ctx.get("log")
    sname = step.get("name", "agent")
    sid = None
    carry: dict = {}  # Verbrauch verworfener Versuche
    try:
        for attempt in range(max_retries + 1):
            t0 = time.time()
            sid = be.create_task(workdir, f"fabrikant-{ctx['run_id']}-{sname}", step.get("permissions"))
            emit(
                f"  agent({backend_name}): session {sid} model={model or '-'} cwd={workdir}"
                + (f" [Versuch {attempt + 1}/{max_retries + 1}]" if max_retries else "")
            )
            be.send_prompt(sid, prompt, workdir)
            if attempt == 0:
                _emit_agent_prompt(emit, prompt)  # log the prompt only once
                if lg is not None:  # Full text for the transcript (UI), redacted in the SessionLog
                    lg.event("prompt", step_name=sname, step_type="agent", text=prompt)
            last: dict = {}
            emitted_n = 0
            recorded_n = 0
            fallback_tool_n = 0
            last_err = ""  # letzter Backend-/Transport-Fehler (leer = gesund)
            err_streak = 0  # consecutive poll errors (for backoff)
            progress = _Progress(started=t0)
            while time.time() < t0 + timeout:
                try:
                    last = be.poll(sid, workdir)
                    # Stream the conversation to the run log (newly completed messages).
                    messages = be.transcript(sid, workdir)
                    progress.observe(messages, last)
                    emitted_n = _emit_new_agent_messages(emit, messages, emitted_n)
                    if lg is not None:
                        recorded_n = _record_agent_messages(lg, messages, recorded_n, sname)
                        tools = last.get("tools") or []
                        if not messages:
                            for tool in tools[fallback_tool_n:]:
                                lg.event("tool_call", step_name=sname, step_type="agent", tool=tool)
                        # Also advance if the messages already carry the tools:
                        # otherwise a later empty transcript poll logs everything again.
                        fallback_tool_n = max(fallback_tool_n, len(tools))
                except Exception as e:
                    # Transient backend/transport error (e.g. opencode serve
                    # down/restart): do NOT kill the run, keep polling within the
                    # step timeout (backoff). Only when the timeout
                    # is reached is the error reported (ADR 0012).
                    err_streak += 1
                    last_err = str(e)
                    emit(
                        _t(
                            "steps.agent.agent_backend_error_retry_error",
                            backend_name=backend_name,
                            v2=str(e)[:80],
                            err_streak=err_streak,
                        )
                    )
                    time.sleep(min(30, 5 * err_streak))
                    continue
                err_streak = 0
                last_err = ""
                if last["status"] == "done":
                    break
                if last["status"] == "failed":
                    be.cancel(sid, workdir)
                    messages = be.transcript(sid, workdir)
                    _emit_new_agent_messages(emit, messages, emitted_n, flush=True)
                    if lg is not None:
                        _record_agent_messages(lg, messages, recorded_n, sname, flush=True)
                    # Transient error + attempts left → restart.
                    if last.get("retryable") and attempt < max_retries:
                        wait = 5 * (attempt + 1)
                        emit(
                            _t(
                                "steps.agent.agent_transient_error_retry_in",
                                backend_name=backend_name,
                                v2=str(last.get("text", ""))[:80],
                                wait=wait,
                                v4=attempt + 2,
                                v5=max_retries + 1,
                            )
                        )
                        carry = _agent_usage(last, backend_name, model, carry, billing)
                        time.sleep(wait)
                        break  # → next for iteration (new session)
                    usage = _agent_usage(last, backend_name, model, carry, billing)
                    _record_cost(lg, sname, usage, last)
                    return _session_failed(last, sid, usage, emit)
                time.sleep(15)
            else:
                # Timeout (while condition false) — no retry: restarting a hanging
                # session would only double the wait time.
                be.cancel(sid, workdir)
                # Rest of the conversation into the log (last message was "in progress").
                # suppress: in the timeout case the backend may be gone — a
                # transcript error must not additionally crash the (already failed)
                # step.
                with contextlib.suppress(Exception):
                    messages = be.transcript(sid, workdir)
                    _emit_new_agent_messages(emit, messages, emitted_n, flush=True)
                    if lg is not None:
                        _record_agent_messages(lg, messages, recorded_n, sname, flush=True)
                # Make the timeout output usable: last known text + tools,
                # so one sees WHAT the session was waiting for (instead of just
                # "timeout after 1800s"). On a persistent backend error name
                # the cause (ADR 0012).

                usage = _agent_usage(last, backend_name, model, carry, billing)
                _record_cost(lg, sname, usage, last)
                if last_err:
                    return failed(
                        _t("steps.agent.agent_timeout_after_s_session", timeout=timeout, sid=sid, v3=last_err[:120]),
                        FailureKind.TRANSIENT,
                        usage=usage,
                    )
                last_txt = (last.get("text") or "").strip()
                tools = last.get("tools") or []
                extra = ""
                if last_txt:
                    extra += f" · letzter Text: {last_txt[:160]}"
                if tools:
                    extra += f" · Tools: {', '.join(tools[:8])}"
                return failed(
                    f"agent timeout after {timeout}s (session {sid}){extra}",
                    progress.timeout_kind(time.time(), stale_after, observable=not isinstance(be, OneShotCliBackend)),
                    usage=usage,
                )
            if last["status"] != "done":
                continue  # Retry path (transient error) → next for iteration
            # done: flush the rest of the conversation to the log (last message was "in progress").
            messages = be.transcript(sid, workdir)
            _emit_new_agent_messages(emit, messages, emitted_n, flush=True)
            if lg is not None:
                _record_agent_messages(lg, messages, recorded_n, sname, flush=True)
            # Final answer into the run log: one-shot backends (copilot/codex) have
            # no transcript — without this line a 'pass' run that did nothing
            # is not recognizable in the log (regression: review-pr 2026-10-18).
            final_txt = (last.get("text") or "").strip()
            if lg is not None and final_txt and not messages:
                lg.event("message", step_name=sname, step_type="agent", text=final_txt)
            if final_txt:
                cap = config.AGENT_LOG_TEXT_CAP
                emit(f"  agent final: {final_txt[:cap]}{'…' if len(final_txt) > cap else ''}")
            dur = round(time.time() - t0, 1)
            tools = last.get("tools") or []
            usage = _agent_usage(last, backend_name, model, carry, billing)
            premium = last.get("premium_requests")
            cost = usage.get("cost_usd")
            summary = (
                f"{dur}s · {TokenCounts.from_usage(usage).summary()}"
                + (f" · ${cost:.2f}" if cost else "")
                + (f" · {premium} premium requests" if premium is not None else "")
                + (f" · tools: {', '.join(tools[:5])}" if tools else "")
            )
            _record_cost(lg, sname, usage, last)
            return {
                "ok": True,
                "summary": summary,
                "output": _cap(last.get("text", "")),
                "agent_session": sid,
                "usage": usage,
            }
        # for loop ended without return = unreachable (every path breaks off).
        raise RuntimeError(_t("steps.agent.agent_retry_loop_without_result", backend_name=backend_name, sid=sid))
    except Exception:
        if sid:  # orphan guard: an exception mid-run aborts the session (H3)
            try:
                be.cancel(sid, workdir)
                emit(_t("steps.agent.agent_session_aborted_after_exception", sid=sid))
            except Exception:
                pass
        raise
