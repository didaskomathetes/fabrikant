"""Tests für die pattern-basierte Redaction (secrets.redact_text / redact_log_tail)."""

from __future__ import annotations

import logging

import pytest

from fabrik import secrets


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ("Authorization: Bearer abcDEF123456.tokenvalue", "abcDEF123456.tokenvalue"),
        ("aws key AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
        ("key sk-abcdefghijklmnopqrstuvwx", "sk-abcdefghijklmnopqrstuvwx"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
        ("api_key=hunter2hunter2", "hunter2hunter2"),
        ("key=supersecret", "supersecret"),
        ('token="abc,def;ghi"', "abc,def;ghi"),
        ('{"access_token": "zzz-secret-zzz"}', "zzz-secret-zzz"),
        ("PASSWORD: s3cret", "s3cret"),
    ],
)
def test_redact_patterns(line: str, secret: str) -> None:
    out = secrets.redact_text(line)
    assert secret not in out
    assert "[REDACTED]" in out


def test_redact_private_key_block() -> None:
    text = "before\\n-----BEGIN RSA PRIVATE KEY-----\\nMIIabc\\ndef\\n-----END RSA PRIVATE KEY-----\\nafter".replace(
        "\\n", "\n"
    )
    out = secrets.redact_text(text)
    assert "MIIabc" not in out
    assert out.startswith("before") and out.endswith("after")


def test_redact_private_key_truncated() -> None:
    out = secrets.redact_text("x\n-----BEGIN PRIVATE KEY-----\nMIIabc")
    assert "MIIabc" not in out


def test_redact_keeps_harmless_text() -> None:
    text = "step build failed: exit 1 (tests/test_x.py)"
    assert secrets.redact_text(text) == text


def test_log_tail_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets.config, "ALERT_LOG_REDACTION", False)
    assert secrets.redact_log_tail("token=abc12345") == "token=abc12345"


def test_log_tail_failsafe(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def _boom(_: str) -> str:
        raise ValueError("kaputt")

    monkeypatch.setattr(secrets, "redact_text", _boom)
    with caplog.at_level(logging.WARNING, logger="fabrik.secrets"):
        assert secrets.redact_log_tail("token=abc12345") == "token=abc12345"
    assert "unredigiert" in caplog.text
