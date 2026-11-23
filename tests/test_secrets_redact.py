"""Secret-Masking: _redact_record deckt auch nested verify-Output ab.

Regressionstest für das Review-Finding: verify[].output war ein ungefilterter
Datapfad in die Run-JSON (per /api/run/{id} im UI lesbar — darf kein Token tragen).
"""

from __future__ import annotations

import pytest

from fabrikant import secrets

SECRET = "sk-SECRET-XYZ999"


@pytest.fixture
def secret_env(monkeypatch):
    monkeypatch.setenv("FABRIKANT_TEST_SECRET", SECRET)
    monkeypatch.setattr(secrets, "_env_secrets", [])
    secrets.refresh_env_secrets()
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


def test_secrets_does_not_iterate_environ(monkeypatch):
    """Hotpath darf os.environ nicht iterieren (Issue #13: Segfault-Hazard)."""

    def boom(*_a, **_k):
        raise AssertionError("os.environ iteriert")

    monkeypatch.setattr(secrets.os.environ.__class__, "items", boom)
    monkeypatch.setattr(secrets, "_env_secrets", [SECRET])
    assert SECRET in secrets._secrets()
    assert secrets._redact(f"x {SECRET} y") == "x [REDACTED] y"


def test_refresh_env_secrets_picks_up_new_value(monkeypatch):
    monkeypatch.setattr(secrets, "_env_secrets", [])
    monkeypatch.setenv("FABRIKANT_TEST_SECRET", SECRET)
    assert SECRET not in secrets._secrets()
    secrets.refresh_env_secrets()
    assert SECRET in secrets._secrets()


def test_refresh_ignores_short_and_non_secret_names(monkeypatch):
    monkeypatch.setattr(secrets, "_env_secrets", [])
    monkeypatch.setenv("FABRIKANT_SHORT_TOKEN", "abc")
    monkeypatch.setenv("FABRIKANT_PLAIN", SECRET)
    secrets.refresh_env_secrets()
    assert "abc" not in secrets._secrets()
    assert SECRET not in secrets._secrets()


def test_concurrent_redact_and_env_mutation(monkeypatch):
    """Redaction parallel zu setenv-Schreibern: kein Crash, Ergebnis stabil."""
    import threading

    monkeypatch.setattr(secrets, "_env_secrets", [SECRET])
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        i = 0
        while not stop.is_set():
            monkeypatch.setenv(f"FABRIKANT_RACE_{i % 50}", str(i))
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(2000):
            try:
                assert secrets._redact(f"a {SECRET}") == "a [REDACTED]"
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
                break
    finally:
        stop.set()
        t.join()
    assert not errors


@pytest.mark.parametrize("key", ["max_tokens", "num_tokens", "completion_tokens", "token_budget", "tokens_used"])
def test_token_counts_are_not_masked(key):
    assert secrets.redact_secret_keys({key: 5}) == {key: 5}


@pytest.mark.parametrize("key", ["token", "api_token", "access_token", "tokens", "GITHUB_TOKEN", "password"])
def test_token_secrets_are_still_masked(key):
    assert secrets.redact_secret_keys({key: "x"}) == {key: secrets.MASKED_VALUE}


def test_contains_masked_value_finds_nested_mask():
    mask = secrets.MASKED_VALUE
    assert secrets.contains_masked_value({"a": [{"b": mask}]})
    assert not secrets.contains_masked_value({"a": [{"b": "real"}], "n": 1})


def test_redact_secret_keys_masks_whole_value_any_type():
    # A secret-looking key masks its ENTIRE value (any type), so a secret held as a
    # list or nested dict is not returned verbatim.
    out = secrets.redact_secret_keys(
        {
            "api_keys": ["hunter2", "x"],
            "credentials": {"user": "u", "password": "pw"},
            "token": 7,
            "name": "n",
            "nested": {"ok": 1, "secret": ["leak"]},
        }
    )
    assert out["api_keys"] == secrets.MASKED_VALUE
    assert out["credentials"] == secrets.MASKED_VALUE
    assert out["token"] == secrets.MASKED_VALUE
    assert out["name"] == "n"
    assert out["nested"] == {"ok": 1, "secret": secrets.MASKED_VALUE}
