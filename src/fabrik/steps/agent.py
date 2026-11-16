"""Step: agent (pluggables Backend, z. B. opencode; Legacy-CLI via cmd)."""

from __future__ import annotations

import contextlib
import json
import os
import time

from .. import config, sandbox
from ..backends import AgentBackend, create_backend, known_backends
from ..backends._oneshot import OneShotCliBackend
from ..context import _cap, _substitute, step_workdir
from ..failures import ConfigError, Failure, classify_agent_output, describe_exit, failed, from_result
from ..gitops import cleared_tokens, github_env, subprocess_env, token_overrides
from ..runmodel import FailureKind
from ..secrets import _redact
from ..spec_sources import resolve_relative
from .extra_context import render_extra_context
from .shell import _step_shell


def _step_agent(ctx: dict, step: dict, emit) -> dict:
    """Agent-Schritt: pluggables Backend (z.B. opencode) oder Legacy-CLI (cmd).

    Backend-Wahl: step['backend'] > FABRIK_AGENT_BACKEND.
    opencode: endpoint/model aus step oder FABRIK_AGENT_ENDPOINT / FABRIK_AGENT_MODEL.
    Prompt: step['prompt'] (inline) oder step['prompt_file'] (Datei, relativ zur Spec,
    Fallback fabrik-Root — ADR 0047; {vars} werden substituiert).
    """
    prompt = (step.get("prompt") or "").strip()
    if not prompt and step.get("prompt_file"):
        pf = resolve_relative(step["prompt_file"], ctx.get("spec_path"))
        if not pf.is_file():
            return failed(f"prompt_file fehlt: {pf}", FailureKind.ENVIRONMENT)
        prompt = pf.read_text(encoding="utf-8").strip()
    if not prompt:
        return failed("agent-step: prompt oder prompt_file fehlt", FailureKind.ENVIRONMENT)
    prompt = _substitute(prompt, ctx) + render_extra_context(ctx, step, emit)
    backend_name = (step.get("backend") or os.environ.get("FABRIK_AGENT_BACKEND", "")).strip()
    if backend_name:
        return _step_agent_backend(ctx, step, emit, backend_name, prompt)
    # Legacy: Agent-CLI via cmd (z.B. claude -p {prompt_file})
    cmd = (step.get("cmd") or "").strip()
    if not cmd:
        emit("  ⚠ agent-step ohne cmd/backend — übersprungen (keine Agent-CLI konfiguriert)")
        return {"ok": True, "summary": "skipped (no agent backend)"}
    prompt_file = config.RUNS_DIR / ctx["project"] / f"{ctx['run_id']}-{step.get('name', 'agent')}.prompt.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt)
    ctx2 = dict(ctx, prompt_file=str(prompt_file))
    return _step_shell(ctx2, {**step, "cmd": _substitute(cmd, ctx2)}, emit)


def _emit_agent_prompt(emit, prompt: str) -> None:
    """Agent-Prompt (Input) ins Run-Log — gekappt, damit große Prompts das
    Log nicht sprengen (Volltext bleibt in der Session / prompt-Datei)."""
    p = prompt.strip()
    if not p:
        return
    cap = config.AGENT_LOG_PROMPT_CAP
    truncated = len(p) > cap
    emit(f"  agent prompt ({len(p)} Zeichen):")
    for line in p[:cap].splitlines():
        emit(f"    {line}")
    if truncated:
        emit(f"    … (gekappt, {len(p) - cap} Zeichen mehr)")


def _emit_agent_message(emit, m: dict) -> None:
    """Eine Assistant-Message ins Run-Log: Text (gekappt) + Tool-Calls."""
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
    """Neue, ABGESCHLOSSENE Assistant-Messages ins Run-Log emittieren.

    Die letzte Message wird als "in Progress" gehalten (finish=None/
    "tool-calls"), bis eine Folge-Message kommt ODER `flush=True`
    (Session-Ende) — sonst würde ein noch wachsendes Tool-Teil nur halb
    emittiert. Liefert die neue Anzahl emittierter Messages.
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
    """Klasse einer `failed`-Session (ADR 0035): Angabe des Backends, sonst `retryable` → transient,
    sonst Text-Fallback für die Agent-CLI-Ausgabe."""
    reported = from_result(last)
    if reported.kind is not FailureKind.UNKNOWN:
        return reported
    if last.get("retryable"):
        return Failure(FailureKind.TRANSIENT)
    return classify_agent_output(str(last.get("text") or ""))


def _session_failed(last: dict, sid: str, usage: dict, emit) -> dict:
    """Step-Result einer gescheiterten Session. Exit-Code (mit Signalname) in die Summary, das Ende
    von stderr (One-Shot-CLIs: `stderr_tail`) redigiert ins Run-Log und als `output` in den Record —
    bei einem Absturz steht die Ursache dort, nicht im gekürzten `text`."""
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
    """Fortschritt einer Session (neue Messages/Tools/Text) für die Timeout-Klasse (ADR 0035 Nr. 2)."""

    def __init__(self, started: float):
        self.started = started
        self.last_change = started
        self.seen_any = False
        self._sig: tuple | None = None

    def observe(self, messages: list[dict], last: dict) -> None:
        sig = (len(messages), len(last.get("tools") or []), last.get("text") or "")
        if sig == self._sig:
            return
        # Erster Stand: das Transcript enthält schon den gesendeten Prompt — Fortschritt nur, wenn der Agent
        # bereits Tools/Text geliefert hat; sonst wäre eine Session ohne jede Antwort „am Arbeiten“.
        progressed = self._sig is not None or any(sig[1:])
        self._sig = sig
        if progressed:
            self.last_change = time.time()
            self.seen_any = True

    def timeout_kind(self, now: float, window: int, observable: bool) -> FailureKind:
        """Timeout ohne Fortschritt im letzten `window` (s) bzw. ganz ohne Fortschritt → transient (hängende
        Session); Agent arbeitete noch → outcome. Ohne beobachtbaren Fortschritt (One-Shot-CLI ohne
        Transcript) → unknown. `window` ist höchstens die Laufzeit seit Start — sonst wäre eine Session, die
        nie etwas geliefert hat, bei `timeout < window` „noch am Arbeiten“."""
        if not self.seen_any:
            return FailureKind.TRANSIENT if observable else FailureKind.UNKNOWN
        stalled = now - self.last_change >= min(window, now - self.started)
        return FailureKind.TRANSIENT if stalled else FailureKind.OUTCOME


def _apply_github_identity(be: AgentBackend, step: dict) -> str | None:
    """`github_identity: dev|reviewer` → `gh` im Agent-Prozess läuft als diese Identity (ADR 0010).

    Nur One-Shot-CLI-Backends: `opencode serve` ist ein fremder Prozess, dessen Env
    der Step nicht setzen kann. Ohne Identity bekommen One-Shot-Agents auf dem Host die
    Projekt-Einstellungen (Token-Datei, `commit_via` für `python -m fabrik.push`). In der
    Sandbox (ADR 0054) dagegen KEIN Token: ohne `github_identity` sind alle Token-Variablen leer —
    sonst trüge jeder Container still das Builder-Token des Projekts (GitHub ist über den
    Egress-Proxy erreichbar). Liefert eine Fehlermeldung oder None."""
    role = str(step.get("github_identity") or "").strip()
    if not role:
        if isinstance(be, OneShotCliBackend) and sandbox.sandbox_enabled():
            be.extra_env = cleared_tokens()
        elif isinstance(be, OneShotCliBackend):
            # Geerbte globale Tokens überlagern (leer = `gh` ignoriert sie), sonst läuft `gh` als falsche Identität.
            be.extra_env = {**token_overrides(), **subprocess_env()}
        return None
    if not isinstance(be, OneShotCliBackend):
        return f"github_identity: nur für CLI-Backends (claude_code, codex, copilot; opencode in der Sandbox), nicht {be.name}"
    try:
        be.extra_env = github_env(role)
    except (RuntimeError, ValueError) as e:
        return f"github_identity {role!r}: {e}"
    return None


def _agent_usage(last: dict, backend_name: str, model: str, carry: dict | None = None) -> dict:
    """Usage-Dict (Tokens, Kosten) aus dem letzten Poll-Ergebnis.

    ``carry`` = Verbrauch früherer, neu gestarteter Versuche — auch fehlgeschlagene
    und abgebrochene Sessions kosten Tokens und gehören in die Summen."""
    tok = last.get("tokens") or {}
    carry = carry or {}
    t_in = int(tok.get("input", 0) or 0) + int(carry.get("prompt_tokens", 0))
    t_out = int(tok.get("output", 0) or 0) + int(carry.get("completion_tokens", 0))
    usage: dict = {
        "model": model or "-",
        "provider": backend_name,
        "prompt_tokens": t_in,
        "completion_tokens": t_out,
        "total_tokens": t_in + t_out,
    }
    cost = float(last.get("cost") or 0) + float(carry.get("cost_usd", 0.0))
    if cost:
        usage["cost_usd"] = cost
    return usage


def _record_cost(lg, step_name: str, usage: dict, last: dict | None = None) -> None:
    """``cost``-Event ins Session-Log (Telemetrie, nicht Summand — siehe sessionlog).

    ``last``: Poll-Ergebnis; ein ``premium_requests``-Zähler (copilot) wandert mit ins Event."""
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
    """Agent-Step über ein pluggables Backend (opencode, copilot, …).

    Robustheit gegen transiente Backend-Fehler (ADR 0012):
    - **Transport-Fehler** während des Pollings (`be.poll()`/`be.transcript()`
      werfen, z. B. opencode serve down/Restart) killen den Run NICHT — der
      Step pollt mit Backoff weiter, bis das Step-Timeout erreicht ist. Erst
      dann wird der anhaltende Fehler gemeldet.
    - **Status-Fehler** (`poll()` → `failed` + `retryable`) starten die Session
      neu (`step.retries`, Default 2 → max. 3 Versuche, Backoff 5s, 15s, …).
    """
    stale_after = int(str(step.get("stale_after") or os.environ.get("FABRIK_AGENT_STALE_AFTER", 900)))
    if backend_name not in known_backends():
        raise ConfigError(
            f"agent-step: unbekanntes Backend '{backend_name}' (verfügbar: {', '.join(known_backends())})"
        )
    endpoint = (step.get("endpoint") or os.environ.get("FABRIK_AGENT_ENDPOINT", "")).strip()
    model = (step.get("model") or os.environ.get("FABRIK_AGENT_MODEL", "")).strip()
    if backend_name == "opencode":
        if not endpoint or not model:
            return failed(
                "opencode-Backend: endpoint/model fehlen "
                "(step.endpoint/model oder FABRIK_AGENT_ENDPOINT + FABRIK_AGENT_MODEL)",
                FailureKind.ENVIRONMENT,
            )
    elif backend_name not in ("codex", "copilot") and not model:
        # claude_code: endpoint ist irrelevant (CLI-Backend), nur model nötig
        # (codex/copilot: model optional — die CLI hat ein Default-Modell)
        return failed(
            f"{backend_name}-Backend: model fehlt (step.model oder FABRIK_AGENT_MODEL)", FailureKind.ENVIRONMENT
        )
    be: AgentBackend = create_backend(backend_name, step, endpoint, model, stale_after)
    identity_err = _apply_github_identity(be, step)
    if identity_err:
        return failed(identity_err, FailureKind.ENVIRONMENT)
    if not be.health():
        # diagnose() nennt die Ursache (falscher Port, 401/Passwort, CLI nicht
        # im PATH) — statt nur "unhealthy" zu melden.
        return failed(be.diagnose() or f"{backend_name} unhealthy: {endpoint}", be.unhealthy_kind())
    workdir = step_workdir(ctx, step)
    timeout = int(step.get("timeout", 1800))
    # Transiente Backend-Fehler (z. B. copilot: Model-Call stirbt ohne finale
    # Antwort → API-Ausfall) werden neu gestartet, statt den Run zu killen.
    # `step.retries` = Anzahl der NEUSTARTS nach dem ersten Versuch (Default 2
    # → max. 3 Versuche). Backoff zwischen den Versuchen: 5s, 15s, …
    max_retries = max(0, int(step.get("retries", 2)))
    lg = ctx.get("log")
    sname = step.get("name", "agent")
    sid = None
    carry: dict = {}  # Verbrauch verworfener Versuche
    try:
        for attempt in range(max_retries + 1):
            t0 = time.time()
            sid = be.create_task(workdir, f"fabrik-{ctx['run_id']}-{sname}", step.get("permissions"))
            emit(
                f"  agent({backend_name}): session {sid} model={model or '-'} cwd={workdir}"
                + (f" [Versuch {attempt + 1}/{max_retries + 1}]" if max_retries else "")
            )
            be.send_prompt(sid, prompt, workdir)
            if attempt == 0:
                _emit_agent_prompt(emit, prompt)  # Prompt nur 1× loggen
                if lg is not None:  # Volltext fürs Transkript (UI), redigiert im SessionLog
                    lg.event("prompt", step_name=sname, step_type="agent", text=prompt)
            last: dict = {}
            emitted_n = 0
            recorded_n = 0
            fallback_tool_n = 0
            last_err = ""  # letzter Backend-/Transport-Fehler (leer = gesund)
            err_streak = 0  # aufeinanderfolgende Poll-Fehler (für Backoff)
            progress = _Progress(started=t0)
            while time.time() < t0 + timeout:
                try:
                    last = be.poll(sid, workdir)
                    # Konversation live ins Run-Log (neue abgeschlossene Messages).
                    messages = be.transcript(sid, workdir)
                    progress.observe(messages, last)
                    emitted_n = _emit_new_agent_messages(emit, messages, emitted_n)
                    if lg is not None:
                        recorded_n = _record_agent_messages(lg, messages, recorded_n, sname)
                        tools = last.get("tools") or []
                        if not messages:
                            for tool in tools[fallback_tool_n:]:
                                lg.event("tool_call", step_name=sname, step_type="agent", tool=tool)
                        # Auch fortschreiben, wenn die Messages die Tools schon tragen:
                        # sonst loggt ein späterer leerer Transcript-Poll alles erneut.
                        fallback_tool_n = max(fallback_tool_n, len(tools))
                except Exception as e:
                    # Transienter Backend-/Transport-Fehler (z. B. opencode serve
                    # down/Restart): den Run NICHT killen, sondern innerhalb des
                    # Step-Timeouts weiterpollen (Backoff). Erst wenn das Timeout
                    # erreicht ist, wird der Fehler gemeldet (ADR 0012).
                    err_streak += 1
                    last_err = str(e)
                    emit(f"  ⚠ agent({backend_name}): Backend-Fehler ({str(e)[:80]}) — Retry (Fehler #{err_streak})")
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
                    # Transienter Fehler + Versuche übrig → neu starten.
                    if last.get("retryable") and attempt < max_retries:
                        wait = 5 * (attempt + 1)
                        emit(
                            f"  ⚠ agent({backend_name}): transienter Fehler "
                            f"({str(last.get('text', ''))[:80]}) — Retry in {wait}s "
                            f"(Versuch {attempt + 2}/{max_retries + 1})"
                        )
                        carry = _agent_usage(last, backend_name, model, carry)
                        time.sleep(wait)
                        break  # → nächste for-Iteration (neue Session)
                    usage = _agent_usage(last, backend_name, model, carry)
                    _record_cost(lg, sname, usage, last)
                    return _session_failed(last, sid, usage, emit)
                time.sleep(15)
            else:
                # Timeout (while-Condition false) — kein Retry: eine hängende
                # Session neu zu starten würde nur die Wartezeit verdoppeln.
                be.cancel(sid, workdir)
                # Rest der Konversation ins Log (letzte Message war "in Progress").
                # suppress: im Timeout-Fall ist das Backend ggf. weg — ein
                # Transcript-Fehler darf den (ohnehin fehlgeschlagenen) Step
                # nicht noch zusätzlich crashen.
                with contextlib.suppress(Exception):
                    messages = be.transcript(sid, workdir)
                    _emit_new_agent_messages(emit, messages, emitted_n, flush=True)
                    if lg is not None:
                        _record_agent_messages(lg, messages, recorded_n, sname, flush=True)
                # Timeout-Output brauchbar machen: letzter bekannter Text + Tools,
                # damit man sieht, WORAUF die Session gewartet hat (statt nur
                # "timeout nach 1800s"). Bei anhaltendem Backend-Fehler die
                # Ursache nennen (ADR 0012).
                usage = _agent_usage(last, backend_name, model, carry)
                _record_cost(lg, sname, usage, last)
                if last_err:
                    return failed(
                        f"agent timeout nach {timeout}s (session {sid}) · Backend-Fehler: {last_err[:120]}",
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
                    f"agent timeout nach {timeout}s (session {sid}){extra}",
                    progress.timeout_kind(time.time(), stale_after, observable=not isinstance(be, OneShotCliBackend)),
                    usage=usage,
                )
            if last["status"] != "done":
                continue  # Retry-Pfad (transienter Fehler) → nächste for-Iteration
            # done: Rest der Konversation ins Log (letzte Message war "in Progress").
            messages = be.transcript(sid, workdir)
            _emit_new_agent_messages(emit, messages, emitted_n, flush=True)
            if lg is not None:
                _record_agent_messages(lg, messages, recorded_n, sname, flush=True)
            # Finale Antwort ins Run-Log: One-Shot-Backends (copilot/codex) haben
            # kein Transcript — ohne diese Zeile ist ein 'pass'-Run, der nichts
            # getan hat, im Log nicht erkennbar (Regression: review-pr 2026-10-18).
            final_txt = (last.get("text") or "").strip()
            if lg is not None and final_txt and not messages:
                lg.event("message", step_name=sname, step_type="agent", text=final_txt)
            if final_txt:
                cap = config.AGENT_LOG_TEXT_CAP
                emit(f"  agent final: {final_txt[:cap]}{'…' if len(final_txt) > cap else ''}")
            dur = round(time.time() - t0, 1)
            tools = last.get("tools") or []
            usage = _agent_usage(last, backend_name, model, carry)
            t_in, t_out = usage["prompt_tokens"], usage["completion_tokens"]
            premium = last.get("premium_requests")
            summary = (
                f"{dur}s · {t_in} in / {t_out} out tokens"
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
        # for-Loop ohne return beendet = unerreichbar (jeder Pfad bricht ab).
        raise RuntimeError(f"agent({backend_name}): Retry-Loop ohne Ergebnis (session {sid})")
    except Exception:
        if sid:  # Orphan-Schutz: Exception mid-Run bricht die Session ab (H3)
            try:
                be.cancel(sid, workdir)
                emit(f"  ⚠ agent session {sid} nach Exception abgebrochen")
            except Exception:
                pass
        raise
