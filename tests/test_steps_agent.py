"""Steps: agent (Backend-Dispatcher) — Statuspfade + Timeout-Diagnose.

Regression (Issue #50): ein Agent-Timeout durfte nicht nur "timeout nach
1800s" melden, sondern muss den letzten bekannten Text + die genutzten Tools
zeigen, damit man sieht, worauf die Session gewartet hat.
"""

import json

import pytest

from fabrikant import secrets
from fabrikant.runmodel import FailureKind
from fabrikant.steps import agent as agent_step


class _FakeBackend:
    """AgentBackend-Fake: poll() liefert eine feste Status-Folge (letzter
    Eintrag wird wiederholt, solange der Loop weiterpollt). `transcript`
    liefert eine feste Message-Liste (für den Konversations-Log-Test)."""

    name = "fake"

    def __init__(
        self,
        polls,
        transcript=None,
        *,
        healthy: bool = True,
        diagnosis: str | None = None,
        unhealthy: FailureKind = FailureKind.ENVIRONMENT,
    ):
        self._polls = list(polls)
        self._transcript = list(transcript or [])
        self.cancelled = False
        self._healthy, self._diagnosis, self._unhealthy = healthy, diagnosis, unhealthy

    def health(self) -> bool:
        return self._healthy

    def diagnose(self) -> str | None:
        return self._diagnosis

    def unhealthy_kind(self) -> FailureKind:
        return self._unhealthy

    def create_task(self, workdir, title, permissions=None):
        return "ses_fake"

    def send_prompt(self, sid, prompt, workdir):
        pass

    def poll(self, sid, workdir):
        if len(self._polls) > 1:
            return self._polls.pop(0)
        return self._polls[0]

    def transcript(self, sid, workdir):
        return self._transcript

    def cancel(self, sid, workdir):
        self.cancelled = True


def _run(monkeypatch, polls, timeout=1, transcript=None):
    fake = _FakeBackend(polls, transcript=transcript)
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    monkeypatch.setattr(agent_step.time, "sleep", lambda s: None)
    # time.time() steuert die Loop-Dauer: die ersten zwei Aufrufe (t0 + erste
    # While-Prüfung) liefern 0 → Loop startet; danach 1000 → sofort Timeout.
    calls = {"n": 0}

    def _fake_time():
        calls["n"] += 1
        return 0.0 if calls["n"] <= 2 else 1000.0

    monkeypatch.setattr(agent_step.time, "time", _fake_time)
    ctx = {"project": "p", "run_id": "r1", "workdir": "/tmp/wd"}
    step = {
        "name": "agent",
        "backend": "opencode",
        "prompt": "hallo",
        "endpoint": "http://e",
        "model": "ollama/m",
        "timeout": timeout,
    }
    res = agent_step._step_agent(ctx, step, lambda m: None)
    return res, fake


def test_agent_timeout_summary_includes_last_text_and_tools(monkeypatch):
    running = {
        "status": "running",
        "text": "ich arbeite gerade an der PR …",
        "tokens": {"input": 10, "output": 2},
        "cost": 0.0,
        "tools": ["shell", "read"],
    }
    res, fake = _run(monkeypatch, [running])
    assert res["ok"] is False
    assert "timeout" in res["summary"]
    assert "ses_fake" in res["summary"]
    # Diagnose: letzter Text + Tools sind im Summary enthalten.
    assert "letzter Text: ich arbeite gerade an der PR" in res["summary"]
    assert "Tools: shell, read" in res["summary"]
    assert fake.cancelled is True


def test_agent_timeout_without_text_or_tools_is_plain(monkeypatch):
    running = {"status": "running", "text": "", "tokens": {}, "cost": 0, "tools": []}
    res, _ = _run(monkeypatch, [running])
    assert res["ok"] is False
    assert "timeout" in res["summary"]
    assert "letzter Text" not in res["summary"]
    assert "Tools:" not in res["summary"]


def test_agent_done_returns_output_and_usage(monkeypatch):
    done = {
        "status": "done",
        "text": "fertig",
        "tokens": {"input": 5, "output": 3},
        "cost": 0.01,
        "tools": ["bash"],
    }
    res, _ = _run(monkeypatch, [done])
    assert res["ok"] is True
    assert res["output"] == "fertig"
    assert res["agent_session"] == "ses_fake"
    assert res["usage"]["total_tokens"] == 8
    assert res["usage"]["cost_usd"] == 0.01


def test_agent_failed_returns_error_text(monkeypatch):
    failed = {"status": "failed", "text": "LLM 500", "tokens": {}, "cost": 0, "tools": []}
    res, fake = _run(monkeypatch, [failed])
    assert res["ok"] is False
    assert "LLM 500" in res["summary"]
    assert fake.cancelled is True


# ---------------------------------------------------------------- Health-Gate
def _run_unhealthy(monkeypatch, diagnose):
    fake = _FakeBackend(
        [{"status": "running", "text": "", "tokens": {}, "cost": 0, "tools": []}],
        healthy=False,
        diagnosis=diagnose,
        unhealthy=FailureKind.TRANSIENT,
    )
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    ctx = {"project": "p", "run_id": "r1", "workdir": "/tmp/wd"}
    step = {
        "name": "agent",
        "backend": "opencode",
        "prompt": "hallo",
        "endpoint": "http://e",
        "model": "m",
        "timeout": 10,
    }
    return agent_step._step_agent(ctx, step, lambda m: None)


def test_agent_health_gate_reports_backend_diagnose(monkeypatch):
    # Regression (Port-Mismatch 2026-10-18): unhealthy-Backend muss die Ursache
    # nennen (diagnose()), nicht nur "unhealthy: <endpoint>".
    res = _run_unhealthy(monkeypatch, "opencode serve nicht erreichbar: http://e (Default-Port 4096)")
    assert res["ok"] is False
    assert res["summary"] == "opencode serve nicht erreichbar: http://e (Default-Port 4096)"


def test_agent_health_gate_fallback_without_diagnose(monkeypatch):
    res = _run_unhealthy(monkeypatch, None)
    assert res["ok"] is False
    assert res["summary"] == "opencode unhealthy: http://e"


# ---------------------------------------------------------------- Retry (transiente Fehler)
class _RetryFakeBackend:
    """Backend-Fake für Retry-Tests: `create_task` zählt die Versuche, `poll`
    liefert pro Versuch die nächste Status-Folge aus `sequences`."""

    def __init__(self, sequences):
        self._sequences = [list(s) for s in sequences]
        self.attempt = 0
        self.cancelled = 0

    def health(self):
        return True

    def diagnose(self):
        return None

    def create_task(self, workdir, title, permissions=None):
        self.attempt += 1
        return f"ses_{self.attempt}"

    def send_prompt(self, sid, prompt, workdir):
        pass

    def poll(self, sid, workdir):
        seq = self._sequences[self.attempt - 1]
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0]

    def transcript(self, sid, workdir):
        return []

    def cancel(self, sid, workdir):
        self.cancelled += 1


def _run_retry(monkeypatch, fake, step_extra=None):
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    monkeypatch.setattr(agent_step.time, "sleep", lambda s: None)
    monkeypatch.setattr(agent_step.time, "time", lambda: 0.0)  # kein Timeout
    ctx = {"project": "p", "run_id": "r1", "workdir": "/tmp/wd"}
    step = {
        "name": "agent",
        "backend": "opencode",
        "prompt": "hallo",
        "endpoint": "http://e",
        "model": "m",
        "timeout": 100,
    }
    step.update(step_extra or {})
    emits: list[str] = []
    res = agent_step._step_agent(ctx, step, emits.append)
    return res, fake, emits


def test_agent_retries_transient_failure_then_succeeds(monkeypatch):
    """Transienter Fehler (retryable) → Session wird neu gestartet, 2. Versuch
    erfolgreich → Run passt (statt den Run beim ersten API-Ausfall zu killen)."""
    failed = {"status": "failed", "retryable": True, "text": "API-Ausfall", "tokens": {}, "cost": 0, "tools": []}
    done = {"status": "done", "text": "fertig", "tokens": {"input": 5, "output": 3}, "cost": 0.0, "tools": []}
    res, fake, emits = _run_retry(monkeypatch, _RetryFakeBackend([[failed], [done]]))
    assert res["ok"] is True
    assert res["output"] == "fertig"
    assert fake.attempt == 2  # 2 Versuche
    assert "Retry" in "\n".join(emits)


def test_agent_retries_exhausted_returns_failure(monkeypatch):
    """Alle Versuche transient fehlgeschlagen → nach max_retries Fehler (kein
    endloser Retry-Loop)."""
    failed = {"status": "failed", "retryable": True, "text": "API-Ausfall", "tokens": {}, "cost": 0, "tools": []}
    res, fake, _ = _run_retry(monkeypatch, _RetryFakeBackend([[failed]] * 3), {"retries": 2})
    assert res["ok"] is False
    assert "API-Ausfall" in res["summary"]
    assert fake.attempt == 3  # 1 + 2 Retries


def test_agent_non_retryable_failure_does_not_retry(monkeypatch):
    """Nicht-transienter Fehler (kein retryable-Flag) → sofort Fehler, kein
    Retry (z. B. Config-Fehler neu zu starten bringt nichts)."""
    failed = {"status": "failed", "text": "LLM 500", "tokens": {}, "cost": 0, "tools": []}
    done = {"status": "done", "text": "fertig", "tokens": {}, "cost": 0, "tools": []}
    res, fake, _ = _run_retry(monkeypatch, _RetryFakeBackend([[failed], [done]]))
    assert res["ok"] is False
    assert "LLM 500" in res["summary"]
    assert fake.attempt == 1  # kein Retry


def test_agent_context_exhausted_is_not_retried(monkeypatch):
    """Context window full: even a `retryable` session is not restarted — the same prompt fails again."""
    failed = {"status": "failed", "retryable": True, "text": "Prompt is too long", "tokens": {}, "cost": 0, "tools": []}
    res, fake, emits = _run_retry(monkeypatch, _RetryFakeBackend([[failed]] * 3), {"retries": 2})
    assert res["ok"] is False and res["failure_kind"] == "outcome"
    assert fake.attempt == 1
    assert any("Kontextfenster erschöpft" in line for line in emits)


# ---------------------------------------------------------------- Transport-Fehler (ADR 0012)
class _FlakyBackend:
    """Backend-Fake für Transport-Fehler-Tests: `poll()` liefert pro Aufruf den
    nächsten Eintrag aus `outcomes` — ein `dict` (Status) ODER eine `Exception`
    (wird geworfen, z. B. Connection-Refused). Letzter Eintrag wird wiederholt.
    Zählt Poll- und Cancel-Aufrufe."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.polls = 0
        self.cancelled = 0

    def health(self):
        return True

    def diagnose(self):
        return None

    def create_task(self, workdir, title, permissions=None):
        return "ses_flaky"

    def send_prompt(self, sid, prompt, workdir):
        pass

    def poll(self, sid, workdir):
        self.polls += 1
        out = self._outcomes[0] if len(self._outcomes) > 1 else self._outcomes[-1]
        if len(self._outcomes) > 1:
            self._outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def transcript(self, sid, workdir):
        return []

    def cancel(self, sid, workdir):
        self.cancelled += 1


def _run_flaky(monkeypatch, fake, timeout=100, timed_out=False):
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    monkeypatch.setattr(agent_step.time, "sleep", lambda s: None)
    if timed_out:
        # Wie in _run: erste zwei time.time()-Aufrufe 0.0 (Loop startet),
        # danach 1000.0 → sofort Timeout.
        calls = {"n": 0}

        def _fake_time():
            calls["n"] += 1
            return 0.0 if calls["n"] <= 2 else 1000.0

        monkeypatch.setattr(agent_step.time, "time", _fake_time)
    else:
        monkeypatch.setattr(agent_step.time, "time", lambda: 0.0)  # kein Timeout
    ctx = {"project": "p", "run_id": "r1", "workdir": "/tmp/wd"}
    step = {
        "name": "agent",
        "backend": "opencode",
        "prompt": "hallo",
        "endpoint": "http://e",
        "model": "m",
        "timeout": timeout,
    }
    emits: list[str] = []
    res = agent_step._step_agent(ctx, step, emits.append)
    return res, fake, emits


def test_agent_transient_transport_error_then_success(monkeypatch):
    """Transport-Fehler (poll() wirft) → Step pollt weiter (kein Run-Kill);
    nächster Poll erfolgreich → Step passt (ADR 0012)."""
    done = {"status": "done", "text": "fertig", "tokens": {"input": 5, "output": 3}, "cost": 0.0, "tools": []}
    res, fake, emits = _run_flaky(monkeypatch, _FlakyBackend([ConnectionRefusedError("conn refused"), done]))
    assert res["ok"] is True
    assert res["output"] == "fertig"
    assert fake.polls >= 2  # Fehler-Poll + Erfolgs-Poll
    assert "Backend-Fehler" in "\n".join(emits)
    assert fake.cancelled == 0  # kein Cancel bei transientem Fehler


def test_agent_persistent_transport_error_times_out(monkeypatch):
    """Anhaltender Transport-Fehler bis zum Step-Timeout → Fehler mit
    Backend-Ursache im Summary (statt nur 'timeout nach Xs')."""
    res, fake, _ = _run_flaky(
        monkeypatch,
        _FlakyBackend([ConnectionRefusedError("conn refused")]),
        timeout=1,
        timed_out=True,
    )
    assert res["ok"] is False
    assert "timeout" in res["summary"]
    assert "Backend-Fehler" in res["summary"]
    assert "conn refused" in res["summary"]
    assert fake.cancelled == 1  # Cancel erst beim Timeout


def test_agent_transport_error_does_not_cancel_prematurely(monkeypatch):
    """Mehrere Transport-Fehler in Folge → Session wird NICHT vorzeitig
    gecancelt (erst beim Timeout); Step erholt sich, wenn das Backend zurückkommt."""
    done = {"status": "done", "text": "ok", "tokens": {}, "cost": 0, "tools": []}
    res, fake, _ = _run_flaky(
        monkeypatch,
        _FlakyBackend([ConnectionRefusedError("a"), ConnectionRefusedError("b"), done]),
    )
    assert res["ok"] is True
    assert fake.cancelled == 0
    assert fake.polls == 3


# ---------------------------------------------------------------- Konversations-Log
def _msg(finish, *parts):
    return {"info": {"role": "assistant", "finish": finish}, "parts": list(parts)}


def test_agent_conversation_written_to_log(monkeypatch):
    """Prompt (Input) + Assistant-Text + Tool-Calls (Konversation) landen im Run-Log.

    Die letzte, noch in Progress befindliche Message (finish=tool-calls) wird
    erst beim Flush (Session-Ende) emittiert — nicht zu früh halb fertig."""
    transcript = [
        _msg("stop", {"type": "text", "text": "Ich schaue mir den Code an."}),
        _msg(
            "tool-calls",
            {"type": "tool", "text": json.dumps({"name": "bash"}), "state": {"status": "completed"}},
        ),
        # letzte Message: in Progress → wird erst beim Flush emittiert
        _msg("tool-calls", {"type": "text", "text": "Fertig, ich habe den Bug gefixt."}),
    ]
    done = {
        "status": "done",
        "text": "Fertig, ich habe den Bug gefixt.",
        "tokens": {"input": 5, "output": 3},
        "cost": 0.0,
        "tools": ["bash"],
    }
    emits: list[str] = []
    fake = _FakeBackend([done], transcript=transcript)
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    monkeypatch.setattr(agent_step.time, "sleep", lambda s: None)
    monkeypatch.setattr(agent_step.time, "time", lambda: 0.0)  # kein Timeout
    ctx = {"project": "p", "run_id": "r1", "workdir": "/tmp/wd"}
    step = {
        "name": "agent",
        "backend": "opencode",
        "prompt": "Fixe den Bug in foo.py",
        "endpoint": "http://e",
        "model": "ollama/m",
        "timeout": 100,
    }
    res = agent_step._step_agent(ctx, step, emits.append)
    assert res["ok"] is True
    joined = "\n".join(emits)
    # Input: Prompt im Log
    assert "agent prompt" in joined
    assert "Fixe den Bug in foo.py" in joined
    # Konversation: Assistant-Text + Tool-Call
    assert "agent sagt: Ich schaue mir den Code an." in joined
    assert "agent tool: bash [completed]" in joined
    # Output: Final-Text (war die in-Progress-Message, kommt per Flush)
    assert "agent sagt: Fertig, ich habe den Bug gefixt." in joined


def test_agent_prompt_is_capped_in_log(monkeypatch):
    """Sehr lange Prompts werden im Log gekappt (kein Log-Sprengen)."""
    long_prompt = "x" * 5000
    done = {"status": "done", "text": "ok", "tokens": {}, "cost": 0, "tools": []}
    emits: list[str] = []
    fake = _FakeBackend([done])
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    monkeypatch.setattr(agent_step.time, "sleep", lambda s: None)
    monkeypatch.setattr(agent_step.time, "time", lambda: 0.0)
    ctx = {"project": "p", "run_id": "r1", "workdir": "/tmp/wd"}
    step = {
        "name": "agent",
        "backend": "opencode",
        "prompt": long_prompt,
        "endpoint": "http://e",
        "model": "ollama/m",
        "timeout": 100,
    }
    agent_step._step_agent(ctx, step, emits.append)
    joined = "\n".join(emits)
    assert "agent prompt (5000 Zeichen):" in joined
    assert "gekappt" in joined
    # Der volle Prompt (5000 x) darf nicht komplett im Log stehen.
    assert "x" * 2500 not in joined


def test_agent_in_progress_last_message_held_back_until_flush(monkeypatch):
    """Eine in-Progress-letzte Message (finish=tool-calls) wird NICHT im
    Live-Tick emittiert, sondern erst beim Flush — sonst halb fertig."""
    # Nur EINE Message, in Progress. poll() meldet "running" (kein done),
    # dann Timeout → Flush emittiert die Message.
    transcript = [_msg("tool-calls", {"type": "text", "text": "arbeite gerade …"})]
    running = {"status": "running", "text": "arbeite gerade …", "tokens": {}, "cost": 0, "tools": []}
    emits: list[str] = []
    fake = _FakeBackend([running], transcript=transcript)
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    monkeypatch.setattr(agent_step.time, "sleep", lambda s: None)
    # Wie in _run: erste zwei time.time()-Aufrufe 0.0 (Loop startet), dann 1000 → Timeout.
    calls = {"n": 0}

    def _fake_time():
        calls["n"] += 1
        return 0.0 if calls["n"] <= 2 else 1000.0

    monkeypatch.setattr(agent_step.time, "time", _fake_time)
    ctx = {"project": "p", "run_id": "r1", "workdir": "/tmp/wd"}
    step = {
        "name": "agent",
        "backend": "opencode",
        "prompt": "hallo",
        "endpoint": "http://e",
        "model": "ollama/m",
        "timeout": 1,
    }
    res = agent_step._step_agent(ctx, step, emits.append)
    assert res["ok"] is False  # timeout
    joined = "\n".join(emits)
    # Die in-Progress-Message wurde (per Flush) emittiert …
    assert "agent sagt: arbeite gerade …" in joined
    # … aber genau EINMAL (nicht im Live-Tick UND beim Flush).
    assert joined.count("agent sagt: arbeite gerade …") == 1


# ---------------------------------------------------------------- github_identity (ADR 0010)
class _FakeCli(agent_step.OneShotCliBackend):
    name = "fakecli"

    def health(self):
        return False  # Step endet nach dem Identity-Setup am Health-Gate

    def diagnose(self):
        return "stop"


def _identity_step(role):
    return {"name": "a", "backend": "claude_code", "model": "m", "prompt": "p", "github_identity": role}


def test_github_identity_sets_role_token_on_cli_backend(monkeypatch):
    fake = _FakeCli()
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    monkeypatch.setenv("GITHUB_REVIEWER_TOKEN", "qa-token")
    res = agent_step._step_agent({"project": "p", "run_id": "r", "workdir": "/tmp"}, _identity_step("reviewer"), print)
    assert res["summary"] == "stop"
    assert fake.extra_env["GH_TOKEN"] == fake.extra_env["GITHUB_TOKEN"] == "qa-token"


def test_github_identity_missing_token_fails_step(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: _FakeCli())
    monkeypatch.delenv("GITHUB_REVIEWER_TOKEN", raising=False)
    monkeypatch.setenv("FABRIKANT_ENV_FILE", str(tmp_path / "none.env"))
    res = agent_step._step_agent({"project": "p", "run_id": "r", "workdir": "/tmp"}, _identity_step("reviewer"), print)
    assert res["ok"] is False and "GITHUB_REVIEWER_TOKEN" in res["summary"]


def test_github_identity_rejected_for_server_backend(monkeypatch):
    fake = _FakeBackend([{"status": "done"}])
    fake.name = "opencode"
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    step = {**_identity_step("dev"), "backend": "opencode", "endpoint": "http://e", "model": "o/m"}
    res = agent_step._step_agent({"project": "p", "run_id": "r", "workdir": "/tmp"}, step, print)
    assert res["ok"] is False and "nur für CLI-Backends" in res["summary"]


# ---------------------------------------------------------------- Fehlerklasse (ADR 0035)
def test_agent_timeout_while_working_is_outcome(monkeypatch):
    """Timeout, während der Agent noch Fortschritt zeigte → outcome (passt nicht ins Zeitbudget)."""
    working = {"status": "running", "text": "arbeite …", "tokens": {}, "cost": 0, "tools": ["bash"]}
    res, _ = _run(monkeypatch, [working])
    assert res["failure_kind"] == "outcome"


def test_agent_timeout_without_progress_is_transient(monkeypatch):
    """Session bis zuletzt ohne Fortschritt (hängende Session) → transient."""
    idle = {"status": "running", "text": "", "tokens": {}, "cost": 0, "tools": []}
    res, _ = _run(monkeypatch, [idle])
    assert res["failure_kind"] == "transient"


def test_agent_timeout_with_backend_error_is_transient(monkeypatch):
    res, _, _ = _run_flaky(monkeypatch, _FlakyBackend([ConnectionRefusedError("x")]), timeout=1, timed_out=True)
    assert res["failure_kind"] == "transient"


def test_agent_failed_session_without_hint_uses_text_fallback(monkeypatch):
    failed = {"status": "failed", "text": "Error: 429 Too Many Requests", "tokens": {}, "cost": 0, "tools": []}
    res, _ = _run(monkeypatch, [failed])
    assert res["failure_kind"] == "transient"
    unknown = {"status": "failed", "text": "LLM lieferte Unsinn", "tokens": {}, "cost": 0, "tools": []}
    res, _ = _run(monkeypatch, [unknown])
    assert res["failure_kind"] == "unknown"


def test_agent_retries_exhausted_is_transient(monkeypatch):
    failed = {"status": "failed", "retryable": True, "text": "API-Ausfall", "tokens": {}, "cost": 0, "tools": []}
    res, _, _ = _run_retry(monkeypatch, _RetryFakeBackend([[failed]] * 3), {"retries": 2})
    assert res["failure_kind"] == "transient"


def test_agent_backend_reported_kind_wins(monkeypatch):
    reset = "2026-10-26T14:00:00+00:00"
    failed = {
        "status": "failed",
        "text": "Limit",
        "failure_kind": "transient",
        "retry_after": reset,
        "tokens": {},
        "cost": 0,
        "tools": [],
    }
    res, _ = _run(monkeypatch, [failed])
    assert (res["failure_kind"], res["retry_after"]) == ("transient", reset)


@pytest.mark.parametrize("kind", [FailureKind.TRANSIENT, FailureKind.ENVIRONMENT])
def test_agent_health_gate_uses_backend_kind(monkeypatch, kind):
    fake = _FakeBackend(
        [{"status": "running", "text": "", "tokens": {}, "cost": 0, "tools": []}],
        healthy=False,
        diagnosis="weg",
        unhealthy=kind,
    )
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    step = {"name": "a", "backend": "opencode", "prompt": "p", "endpoint": "http://e", "model": "m"}
    res = agent_step._step_agent({"project": "p", "run_id": "r", "workdir": "/tmp"}, step, lambda m: None)
    assert res["failure_kind"] == kind.value


def test_agent_config_errors_are_environment(monkeypatch):
    ctx = {"project": "p", "run_id": "r", "workdir": "/tmp"}
    assert agent_step._step_agent(ctx, {"backend": "opencode"}, lambda m: None)["failure_kind"] == "environment"
    res = agent_step._step_agent(ctx, {"backend": "opencode", "prompt": "p"}, lambda m: None)
    assert res["failure_kind"] == "environment"  # endpoint/model fehlen
    with pytest.raises(ValueError):  # ConfigError bleibt ein ValueError
        agent_step._step_agent(ctx, {"backend": "gibtsnicht", "prompt": "p"}, lambda m: None)


def test_opencode_sandbox_mode_needs_no_endpoint(monkeypatch):
    """Sandbox (ADR 0054): Config aus der LLM-Verbindung — opencode braucht nur das Modell,
    kein endpoint (wird ignoriert). Ohne Sandbox bleibt das endpoint Pflicht."""
    fake = _FakeBackend([{"status": "done", "text": "ok", "tokens": {}, "cost": 0, "tools": []}])
    monkeypatch.setattr(agent_step, "create_backend", lambda *a, **k: fake)
    monkeypatch.setattr(agent_step.time, "sleep", lambda s: None)
    ctx = {"project": "p", "run_id": "r", "workdir": "/tmp"}
    step = {"name": "a", "backend": "opencode", "prompt": "p", "model": "lan/qwen"}
    monkeypatch.setenv("FABRIKANT_SANDBOX", "1")
    assert agent_step._step_agent(ctx, step, lambda m: None)["ok"] is True
    monkeypatch.setenv("FABRIKANT_SANDBOX", "0")
    res = agent_step._step_agent(ctx, step, lambda m: None)
    assert res["ok"] is False and "endpoint/model fehlen" in res["summary"]


def test_progress_one_shot_without_signal_is_unknown():
    p = agent_step._Progress(started=0.0)
    p.observe([], {"text": "", "tools": []})
    assert p.timeout_kind(now=5000.0, window=900, observable=False) is FailureKind.UNKNOWN
    assert p.timeout_kind(now=5000.0, window=900, observable=True) is FailureKind.TRANSIENT


def test_progress_no_signal_with_short_timeout_is_transient():
    """timeout < window: eine Session, die nie etwas geliefert hat, hängt — nicht „noch am Arbeiten“."""
    p = agent_step._Progress(started=0.0)
    p.observe([], {"text": "", "tools": []})
    assert p.timeout_kind(now=600.0, window=900, observable=True) is FailureKind.TRANSIENT


def test_progress_first_snapshot_is_baseline_not_progress():
    """Das Transcript enthält beim ersten Poll schon den gesendeten Prompt: das ist kein Fortschritt."""
    prompt = [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "p"}]}]
    p = agent_step._Progress(started=0.0)
    for _ in range(3):  # Session antwortet nie
        p.observe(prompt, {"text": "", "tools": []})
    assert not p.seen_any
    assert p.timeout_kind(now=600.0, window=900, observable=True) is FailureKind.TRANSIENT
    p.observe([*prompt, {"info": {"role": "assistant"}, "parts": []}], {"text": "arbeite", "tools": []})
    assert p.seen_any


def test_progress_window_capped_by_runtime():
    p = agent_step._Progress(started=0.0)
    p.seen_any = True
    p.last_change = 0.0  # Fortschritt nur ganz am Anfang, dann 600 s Stille bei timeout 600 < window
    assert p.timeout_kind(now=600.0, window=900, observable=True) is FailureKind.TRANSIENT
    p.last_change = 550.0  # kurz vor dem Timeout noch gearbeitet
    assert p.timeout_kind(now=600.0, window=900, observable=True) is FailureKind.OUTCOME


def test_agent_timeout_reports_usage(monkeypatch):
    """Auch ein Timeout hat Tokens/Kosten verbraucht — sie landen im Step-Result."""
    running = {"status": "running", "text": "x", "tokens": {"input": 100, "output": 20}, "cost": 0.5, "tools": []}
    res, _ = _run(monkeypatch, [running])
    assert res["ok"] is False
    assert res["usage"]["prompt_tokens"] == 100
    assert res["usage"]["completion_tokens"] == 20
    assert res["usage"]["total_tokens"] == 120
    assert res["usage"]["cost_usd"] == 0.5


def test_agent_failed_reports_usage(monkeypatch):
    failed = {"status": "failed", "text": "LLM 500", "tokens": {"input": 7, "output": 1}, "cost": 0.1, "tools": []}
    res, _, _ = _run_retry(monkeypatch, _RetryFakeBackend([[failed]]))
    assert res["ok"] is False
    assert res["usage"]["total_tokens"] == 8


def test_agent_retry_accumulates_usage_of_discarded_attempts(monkeypatch):
    """Verbrauch eines fehlgeschlagenen, neu gestarteten Versuchs geht nicht verloren."""
    failed = {"status": "failed", "retryable": True, "text": "x", "tokens": {"input": 10, "output": 2}, "cost": 0.2}
    done = {"status": "done", "text": "ok", "tokens": {"input": 5, "output": 3}, "cost": 0.3, "tools": []}
    res, _, _ = _run_retry(monkeypatch, _RetryFakeBackend([[failed], [done]]))
    assert res["ok"] is True
    assert res["usage"]["prompt_tokens"] == 15
    assert res["usage"]["completion_tokens"] == 5
    assert res["usage"]["cost_usd"] == pytest.approx(0.5)


def test_session_failed_reports_exit_and_stderr_tail(monkeypatch):
    """Abgestürzte One-Shot-CLI: Exit-Code mit Signalname in der Summary, das stderr-Ende redigiert
    als `output` und im Run-Log — dort steht die Ursache, nicht im gekürzten `text`."""
    secret = "super-secret-value-123"
    monkeypatch.setattr(secrets, "_env_secrets", [secret])
    lines: list[str] = []
    last = {
        "status": "failed",
        "text": "=" * 60 + "\nBun v1.4.4",
        "exit_code": 134,
        "stderr_tail": f"token {secret}\npanic(main thread): Segmentation fault at address 0x10",
    }
    res = agent_step._session_failed(last, "claude_code-abc", {"model": "m"}, lines.append)
    assert res["ok"] is False
    assert "(exit 134 (SIGABRT))" in res["summary"]
    assert res["exit_code"] == 134
    assert res["output"].endswith("Segmentation fault at address 0x10")
    assert secret not in res["output"] and "[REDACTED]" in res["output"]
    assert "    panic(main thread): Segmentation fault at address 0x10" in lines
    assert not any(secret in line for line in lines)


def test_session_failed_without_exit_info_keeps_old_shape():
    """Backends ohne Exit-Infos (opencode serve): Summary wie bisher, kein output/exit_code."""
    lines: list[str] = []
    res = agent_step._session_failed({"status": "failed", "text": "boom"}, "s1", {}, lines.append)
    assert res["summary"] == "agent session fehlgeschlagen: boom (session s1)"
    assert "exit_code" not in res and "output" not in res
    assert lines == []


@pytest.mark.parametrize("field", ["text", "stderr_tail"])
def test_session_failed_context_exhausted_warns_as_outcome(field):
    """Context window full: warning in the log, class `outcome` (no retry)."""
    lines: list[str] = []
    last = {"status": "failed", "text": "", field: "API Error: 400 Prompt is too long"}
    res = agent_step._session_failed(last, "s1", {}, lines.append)
    assert res["failure_kind"] == "outcome"
    assert any("Kontextfenster erschöpft (session s1)" in line for line in lines)


def test_session_failed_other_errors_do_not_warn_about_context():
    lines: list[str] = []
    res = agent_step._session_failed({"status": "failed", "text": "boom"}, "s1", {}, lines.append)
    assert res["failure_kind"] == "unknown"
    assert not any("Kontextfenster" in line for line in lines)
