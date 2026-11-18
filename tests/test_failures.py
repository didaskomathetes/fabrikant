"""Fehlerklassifikation (ADR 0035): failures.py — Klassen je Quelle, Text-Fallback, Run-Auswahl.

Die Fehlerbilder in den Text-Tests stammen aus echten Run-Records unter `runs/` (2026-10).
"""

from __future__ import annotations

import subprocess
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest
from helpers import http_error

from fabrik.failures import (
    ConfigError,
    Failure,
    GitError,
    SetupError,
    StepFailure,
    classify_agent_output,
    classify_exception,
    classify_exit_code,
    classify_git_output,
    classify_http,
    describe_exit,
    failed,
    from_result,
    is_signal_exit,
    parse_kind,
    parse_retry_after,
    run_failure,
)
from fabrik.runmodel import FailureKind as K

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- Failure
def test_retry_after_only_kept_for_transient():
    assert Failure(K.TRANSIENT, NOW).retry_after == NOW
    assert Failure(K.ENVIRONMENT, NOW).retry_after is None
    assert Failure(K.OUTCOME, NOW).fields() == {"failure_kind": "outcome"}


def test_fields_serialize_retry_after_as_utc_iso():
    local = NOW.astimezone(datetime.now().astimezone().tzinfo)
    assert Failure(K.TRANSIENT, local).fields() == {
        "failure_kind": "transient",
        "retry_after": "2026-10-26T12:00:00+00:00",
    }


def test_failed_builds_step_result():
    res = failed("exit 1: rot", K.OUTCOME, exit_code=1, output="x")
    assert res == {"ok": False, "summary": "exit 1: rot", "exit_code": 1, "output": "x", "failure_kind": "outcome"}
    assert failed("s", Failure(K.TRANSIENT, NOW))["retry_after"] == NOW.isoformat()


# ---------------------------------------------------------------- Exit-Codes / HTTP
@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (127, K.ENVIRONMENT),
        (126, K.ENVIRONMENT),
        (75, K.TRANSIENT),  # sysexits EX_TEMPFAIL
        (78, K.ENVIRONMENT),  # sysexits EX_CONFIG
        (1, K.UNKNOWN),
        (2, K.UNKNOWN),
        (None, K.UNKNOWN),
    ],
)
def test_exit_codes(code, kind):
    # Real: "exit 127: scripts/sonar-scan.sh: line 81: pysonar: command not found" (31× sonar-scan)
    assert classify_exit_code(code) is kind


@pytest.mark.parametrize(
    ("code", "signal"),
    [
        (-9, True),
        (-15, True),
        (137, True),
        (143, True),
        (192, True),
        (1, False),
        (127, False),
        (128, False),
        (193, False),
    ],
)
def test_is_signal_exit(code, signal):
    assert is_signal_exit(code) is signal


@pytest.mark.parametrize(
    ("code", "text"),
    [
        (0, "exit 0"),
        (1, "exit 1"),
        (128, "exit 128"),
        (-11, "signal SIGSEGV"),
        (139, "exit 139 (SIGSEGV)"),
        (134, "exit 134 (SIGABRT)"),
        (191, "exit 191 (signal 63)"),  # Echtzeit-Signal ohne eigenen Namen im Enum
        (-63, "signal signal 63"),
        (193, "exit 193"),
    ],
)
def test_describe_exit(code, text):
    assert describe_exit(code) == text


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (429, K.TRANSIENT),
        (500, K.TRANSIENT),
        (502, K.TRANSIENT),
        (504, K.TRANSIENT),  # Real: "LLM-Call fehlgeschlagen: HTTP Error 504: Gateway Time-out"
        (501, K.UNKNOWN),  # Not Implemented heilt kein Retry
        (505, K.UNKNOWN),
        (408, K.TRANSIENT),
        (401, K.ENVIRONMENT),
        (403, K.ENVIRONMENT),
        (404, K.UNKNOWN),
        (422, K.UNKNOWN),  # Real: pr-Step "HTTP Error 422: Unprocessable Entity"
    ],
)
def test_http_status(status, kind):
    assert classify_http(status, now=NOW).kind is kind


def test_http_retry_after_seconds_and_date():
    assert classify_http(429, {"Retry-After": "120"}, now=NOW).retry_after == NOW + timedelta(seconds=120)
    date = classify_http(503, {"Retry-After": "Mon, 26 Oct 2026 13:00:00 GMT"}, now=NOW)
    assert date.retry_after == datetime(2026, 10, 26, 13, 0, tzinfo=UTC)
    assert classify_http(503, {"Retry-After": "irgendwann"}, now=NOW).retry_after is None


def test_github_5xx_ignores_rate_limit_reset_while_quota_left():
    """GitHub sendet X-RateLimit-* bei jeder Antwort — ein 502 ist kein Rate-Limit, also kein Reset-Zeitpunkt."""
    reset = str(int((NOW + timedelta(minutes=50)).timestamp()))
    f = classify_http(502, {"X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": reset}, now=NOW)
    assert f.kind is K.TRANSIENT
    assert f.retry_after is None


def test_github_429_with_exhausted_quota_uses_reset():
    reset = int((NOW + timedelta(minutes=5)).timestamp())
    f = classify_http(429, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}, now=NOW)
    assert f.retry_after == NOW + timedelta(minutes=5)


def test_github_rate_limited_403_is_transient_with_reset():
    reset = int((NOW + timedelta(minutes=10)).timestamp())
    f = classify_http(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}, now=NOW)
    assert f.kind is K.TRANSIENT
    assert f.retry_after == NOW + timedelta(minutes=10)


# ---------------------------------------------------------------- Exceptions
@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (http_error(503), K.TRANSIENT),
        (http_error(401), K.ENVIRONMENT),
        (urllib.error.URLError("Name or service not known"), K.TRANSIENT),
        (ConnectionResetError(), K.TRANSIENT),
        (TimeoutError(), K.TRANSIENT),
        (TimeoutError(), K.TRANSIENT),
        # Real: "[Errno 2] No such file or directory: '…/.fabrik/worktrees/fabrik/…'"
        (FileNotFoundError(2, "No such file or directory"), K.ENVIRONMENT),
        (PermissionError(13, "Permission denied"), K.ENVIRONMENT),
        (subprocess.TimeoutExpired("sh", 600), K.UNKNOWN),
        (ValueError("flush of closed file"), K.UNKNOWN),  # Real: agent/error
        (ConfigError("pr-step: 'repo: owner/name' fehlt"), K.ENVIRONMENT),
        (SetupError("Subpfad x existiert nicht"), K.ENVIRONMENT),
        (StepFailure("x", K.OUTCOME), K.OUTCOME),
    ],
)
def test_classify_exception(exc, kind):
    assert classify_exception(exc).kind is kind


def test_http_exception_carries_retry_after():
    f = classify_exception(http_error(429, headers={"Retry-After": "30"}))
    assert f.kind is K.TRANSIENT
    assert f.retry_after is not None


def test_typed_errors_keep_their_base_types():
    """Bestehende Aufrufer fangen ValueError/RuntimeError — die typisierten Fehler bleiben kompatibel."""
    assert isinstance(ConfigError("x"), ValueError)
    assert isinstance(SetupError("x"), RuntimeError)
    assert isinstance(GitError("x", ""), RuntimeError)


# ---------------------------------------------------------------- git-Fallback
@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        # Real (4×): git fetch Netzfehler
        (
            "fatal: unable to access 'https://github.com/didaskomathetes/quark-playground.git/': "
            "Could not resolve host: github.com",
            K.TRANSIENT,
        ),
        ("error: RPC failed; curl 56 GnuTLS recv error\nfatal: early EOF", K.TRANSIENT),
        ("fatal: unable to access '…': The requested URL returned error: 502", K.TRANSIENT),
        # Real: abgelaufenes/falsches Token
        (
            "remote: Invalid username or token. Password authentication is not supported for Git operations.\n"
            "fatal: Authentication failed for 'https://github.com/didaskomathetes/quark-playground.git/'",
            K.ENVIRONMENT,
        ),
        ("fatal: unable to access '…': The requested URL returned error: 403", K.ENVIRONMENT),
        # TLS/Proxy-Konfiguration: dauerhaft, braucht einen Menschen
        ("fatal: unable to access '…': SSL certificate problem: self-signed certificate", K.ENVIRONMENT),
        ("fatal: unable to access '…': Could not resolve proxy: proxy.local", K.ENVIRONMENT),
        # "unable to access" allein (Präfix jedes https-Fehlers) entscheidet nichts
        ("fatal: unable to access '…': something unexpected", K.UNKNOWN),
        # Real: Branch existiert remote nicht — weder Netz noch Auth
        ("fatal: couldn't find remote ref refs/heads/opencode-2x-dual-support", K.UNKNOWN),
        ("", K.UNKNOWN),
    ],
)
def test_git_output(stderr, kind):
    assert classify_git_output(stderr) is kind
    assert GitError("git fetch fehlgeschlagen", stderr).failure.kind is kind


# ---------------------------------------------------------------- Agent-CLI-Fallback
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        # Real: opencode provider.transport
        (
            "{'type': 'provider.transport', 'message': 'Connection lost while reading the response: "
            "ECONNRESET: The socket connection",
            K.TRANSIENT,
        ),
        ("API Error: 529 overloaded_error", K.TRANSIENT),
        ("Error: 429 Too Many Requests", K.TRANSIENT),
        ("API Error: 500 Internal error", K.TRANSIENT),
        ('API Error: 401 {"type":"error"}', K.ENVIRONMENT),
        # Zahlen ohne HTTP-Kontext (z. B. im Agent-Ergebnis von claude_code) sind kein Status-Code
        ("Fixed 401 lint findings in 502 files", K.UNKNOWN),
        ("You exceeded your current quota: insufficient_quota", K.ENVIRONMENT),
        ("You exceeded your current quota, please check your plan and billing details.", K.ENVIRONMENT),
        # Limits ohne angekündigten Reset brauchen einen Menschen, mit Reset heilen sie von selbst
        ("Claude AI usage limit reached", K.ENVIRONMENT),
        ("Claude usage limit reached. Your limit will reset at 5pm (Europe/Berlin).", K.TRANSIENT),
        ("5-hour limit reached ∙ resets 3pm", K.TRANSIENT),
        ("Quota exceeded for quota metric 'Generate Content API requests per minute'", K.TRANSIENT),
        ("429 RESOURCE_EXHAUSTED: Quota exceeded for metric requests per day", K.TRANSIENT),
        ("Quota exceeded. Upgrade your plan.", K.ENVIRONMENT),
        ("Error: 401 Unauthorized", K.ENVIRONMENT),
        # Fachwörter im Agent-Text sind kein Fehlerbild
        ("Refactored billing.py and the unauthorized-access handler", K.UNKNOWN),
        ("Your credit balance is too low to access the Anthropic API", K.ENVIRONMENT),
        # Real: copilot mit nicht verfügbarem Modell
        ('Error: Model "gpt-5-mini" from --model flag is not available.', K.ENVIRONMENT),
        # Real, ungeklärt (Issue #289: erst untersuchen) → bewusst nicht transient
        ('{"duration_api_ms":0,"stop_reason":"stop_sequence","session_id":"0862d802"', K.UNKNOWN),
        ('{"type":"session.mcp_server_status_changed","data":{"serverName":"github-mcp-server"}}', K.UNKNOWN),
        ("{'type': 'provider.internal', 'message': 'Context size has been exceeded.'", K.UNKNOWN),
        ("", K.UNKNOWN),
    ],
)
def test_agent_output(text, kind):
    assert classify_agent_output(text).kind is kind


def test_claude_usage_limit_carries_reset_time():
    reset = int((NOW + timedelta(hours=2)).timestamp())
    f = classify_agent_output(f"Claude AI usage limit reached|{reset}")
    assert f.kind is K.TRANSIENT
    assert f.retry_after == NOW + timedelta(hours=2)


# ---------------------------------------------------------------- Lesen
def test_parse_kind_defaults_to_unknown():
    assert parse_kind(None) is K.UNKNOWN
    assert parse_kind("") is K.UNKNOWN
    assert parse_kind("bogus") is K.UNKNOWN
    assert parse_kind("transient") is K.TRANSIENT


def test_parse_retry_after():
    assert parse_retry_after("2026-10-26T12:00:00+00:00") == NOW
    assert parse_retry_after("2026-10-26T12:00:00") == NOW  # ohne Offset = UTC
    assert parse_retry_after("kein datum") is None
    assert parse_retry_after(None) is None


def test_from_result():
    assert from_result({"ok": False}).kind is K.UNKNOWN
    res = {"ok": False, "failure_kind": "transient", "retry_after": NOW.isoformat()}
    assert from_result(res) == Failure(K.TRANSIENT, NOW)


# ---------------------------------------------------------------- Run-Klasse
def _step(status, kind=None, retry_after=None):
    s = {"name": "s", "status": status}
    if kind:
        s["failure_kind"] = kind
    if retry_after:
        s["retry_after"] = retry_after
    return s


def test_run_takes_aborting_step():
    steps = [_step("pass"), _step("fail", "outcome"), _step("error", "environment")]
    assert run_failure(steps, aborted_by=2).kind is K.ENVIRONMENT


def test_run_without_abort_takes_first_failed_step():
    """Nur gate:false-Steps gescheitert → Klasse des ersten gescheiterten Steps."""
    steps = [_step("fail", "outcome"), _step("pass"), _step("timeout", "unknown")]
    assert run_failure(steps, aborted_by=None).kind is K.OUTCOME


def test_run_without_failed_step_is_unknown():
    assert run_failure([_step("pass")], aborted_by=None) == Failure(K.UNKNOWN)


def test_run_legacy_step_without_kind_is_unknown():
    assert run_failure([_step("fail")], aborted_by=0).kind is K.UNKNOWN


def test_run_retry_after_is_latest_value():
    early, late = "2026-10-26T12:00:00+00:00", "2026-10-26T14:00:00+00:00"
    steps = [_step("fail", "transient", early), _step("fail", "transient", late)]
    assert run_failure(steps, aborted_by=1).retry_after == datetime(2026, 10, 26, 14, 0, tzinfo=UTC)


def test_run_retry_after_dropped_unless_transient():
    steps = [_step("fail", "transient", "2026-10-26T12:00:00+00:00"), _step("error", "environment")]
    assert run_failure(steps, aborted_by=1) == Failure(K.ENVIRONMENT)
