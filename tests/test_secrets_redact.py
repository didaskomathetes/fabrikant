"""Secret-Masking: _redact_record deckt auch nested verify-Output ab.

Regressionstest für das Review-Finding: verify[].output war ein ungefilterter
Datapfad in die Run-JSON (per /api/run/{id} im UI lesbar — darf kein Token tragen).
"""

from __future__ import annotations

import pytest

from fabrik import secrets

SECRET = "sk-SECRET-XYZ999"


@pytest.fixture
def secret_env(monkeypatch):
    monkeypatch.setenv("FABRIK_TEST_SECRET", SECRET)
    yield


def test_verify_output_is_redacted(secret_env):
    rec = {
        "id": "t",
        "steps": [
            {
                "name": "v",
                "type": "verify",
                "status": "fail",
                "summary": f"verify FAILED: bad: {SECRET} leaked",
                "verify": [
                    {"name": "good", "ok": True, "output": "hello"},
                    {"name": "bad", "ok": False, "output": f"shell: echo $T => {SECRET} leaked"},
                ],
            }
        ],
    }
    out = secrets._redact_record(rec)
    assert SECRET not in str(out)
    assert "[REDACTED]" in out["steps"][0]["verify"][1]["output"]
    assert "[REDACTED]" in out["steps"][0]["summary"]
    # saubere Checks bleiben unangetastet
    assert out["steps"][0]["verify"][0]["output"] == "hello"


def test_verify_without_key_is_noop(secret_env):
    rec = {"id": "t", "steps": [{"name": "s", "type": "shell", "summary": "ok"}]}
    out = secrets._redact_record(rec)
    assert out["steps"][0]["summary"] == "ok"


def test_usage_key_survives_redaction(secret_env):
    """usage (Tokens/Model/Provider) trägt kein Secret-Material — bleibt intakt."""
    rec = {
        "steps": [
            {
                "name": "l",
                "type": "llm",
                "usage": {"model": "m", "provider": "p", "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
        ]
    }
    out = secrets._redact_record(rec)
    assert out["steps"][0]["usage"]["total_tokens"] == 3


def test_approval_context_is_redacted(secret_env):
    """Approval-Kontext (was/Rechte) ist ein eigener Datapfad in die UI-lesbare JSON."""
    rec = {
        "id": "t",
        "approval": {
            "reason": "step",
            "step": "deploy",
            "what": f"Shell-Befehl: deploy --token {SECRET}",
            "permissions": ["bash * → allow", f"env {SECRET}"],
            "issue": "https://github.com/o/r/issues/1",
            "tier": 2,
        },
    }
    out = secrets._redact_record(rec)
    assert SECRET not in str(out)
    assert "[REDACTED]" in out["approval"]["what"]
    assert "[REDACTED]" in out["approval"]["permissions"][1]
    # saubere Werte bleiben unangetastet
    assert out["approval"]["permissions"][0] == "bash * → allow"
    assert out["approval"]["issue"] == "https://github.com/o/r/issues/1"
    assert out["approval"]["tier"] == 2


def test_run_vars_are_redacted(secret_env):
    """Run-Variablen (record['vars']) sind ein eigener Datapfad in die UI-lesbare JSON."""
    rec = {
        "id": "t",
        "vars": {
            "issue_number": "42",
            "token": f"Bearer {SECRET}",
            "label": "ready to develop",
        },
    }
    out = secrets._redact_record(rec)
    assert SECRET not in str(out)
    assert "[REDACTED]" in out["vars"]["token"]
    # saubere Werte bleiben unangetastet
    assert out["vars"]["issue_number"] == "42"
    assert out["vars"]["label"] == "ready to develop"
