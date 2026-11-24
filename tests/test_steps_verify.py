"""Steps: verify (Check-Liste-Semantik, Issue #13).

Läuft über den echten Engine-Dispatch, damit Sub-Checks (shell) real ausgeführt werden.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from fabrikant import llm, run_loop
from fabrikant.engine import _dispatch_step


@pytest.fixture
def clean_llm(monkeypatch, tmp_path):
    """Isoliert LLM-Env + Persistenz; konfiguriert einen Dummy-Provider."""
    import os

    for k in list(os.environ):
        if k.startswith("FABRIKANT_LLM"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_ACTIVE_FILE", tmp_path / "active_llm")
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "dummy")
    monkeypatch.setenv("FABRIKANT_LLM_DUMMY_URL", "http://127.0.0.1:1/v1/chat/completions")
    monkeypatch.setenv("FABRIKANT_LLM_DUMMY_MODEL", "test-model")
    llm.reset_cache()
    llm.set_active("dummy")
    yield
    llm.reset_cache()


def _fake_urlopen(payload):
    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return lambda req, timeout=None: _R()


def _ctx(tmp_path):
    return {"root": str(tmp_path), "workdir": str(tmp_path), "project": "v", "run_id": "r"}


def test_all_green_passes(tmp_path):
    ctx = _ctx(tmp_path)
    res = _dispatch_step(
        ctx,
        {
            "type": "verify",
            "checks": [
                {"name": "a", "type": "shell", "cmd": "echo hi"},
                {"name": "b", "type": "shell", "cmd": "true"},
            ],
        },
        lambda _m: None,
    )
    assert res["ok"] is True
    assert len(res["verify"]) == 2
    assert all(c["ok"] for c in res["verify"])
    assert "pass" in res["summary"]


def test_one_red_fails_with_name(tmp_path):
    ctx = _ctx(tmp_path)
    res = _dispatch_step(
        ctx,
        {
            "type": "verify",
            "checks": [
                {"name": "good", "type": "shell", "cmd": "true"},
                {"name": "bad", "type": "shell", "cmd": "echo oops >&2; exit 3"},
                {"name": "never-checked-fail-fast-off", "type": "shell", "cmd": "true"},
            ],
        },
        lambda _m: None,
    )
    assert res["ok"] is False
    assert "bad" in res["summary"]
    # alle Checks wurden durchlaufen (Checkliste, kein Fail-Fast)
    assert len(res["verify"]) == 3
    by_name = {c["name"]: c for c in res["verify"]}
    assert by_name["good"]["ok"] is True
    assert by_name["bad"]["ok"] is False
    assert by_name["never-checked-fail-fast-off"]["ok"] is True


def test_check_output_in_record(tmp_path):
    ctx = _ctx(tmp_path)
    res = _dispatch_step(
        ctx,
        {
            "type": "verify",
            "checks": [{"name": "c", "type": "shell", "cmd": "echo MARKER-XYZ"}],
        },
        lambda _m: None,
    )
    assert res["ok"] is True
    assert "MARKER-XYZ" in res["verify"][0]["output"]


def test_empty_list_vacuous_pass(tmp_path):
    ctx = _ctx(tmp_path)
    res = _dispatch_step(ctx, {"type": "verify", "checks": []}, lambda _m: None)
    assert res["ok"] is True
    assert res["verify"] == []
    assert "leere Liste" in res["summary"]


def test_missing_checks_vacuous_pass(tmp_path):
    ctx = _ctx(tmp_path)
    res = _dispatch_step(ctx, {"type": "verify"}, lambda _m: None)
    assert res["ok"] is True
    assert res["verify"] == []


def test_invalid_check_def_fails(tmp_path):
    ctx = _ctx(tmp_path)
    res = _dispatch_step(
        ctx,
        {"type": "verify", "checks": [{"name": "no-type", "cmd": "true"}]},
        lambda _m: None,
    )
    assert res["ok"] is False
    assert res["verify"][0]["ok"] is False
    assert "type" in res["verify"][0]["output"]


# ---------------------------------------------------------------- Engine-Level
def test_failed_verify_lands_in_run_record(runs_dir, projects_dir, tmp_path):
    """Auch bei status=fail muss der 'verify'-Key mit Pro-Check-Daten im Record stehen."""
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "tverifyfail.fabrikant.yaml").write_text(
        f"name: tverifyfail\nroot: {root}\nloops:\n  t:\n    steps:\n"
        "      - type: verify\n        name: check\n        checks:\n"
        '          - type: shell\n            name: bad\n            cmd: "exit 2"\n'
    )
    rec = run_loop("tverifyfail", "t")
    v = [s for s in rec["steps"] if s["type"] == "verify"][0]
    assert v["status"] == "fail"
    assert v["verify"][0]["ok"] is False


def test_verify_with_llm_subcheck(clean_llm, monkeypatch, runs_dir, projects_dir, tmp_path):
    """llm-Sub-Check (der spätere LLM-Judge-Pfad) läuft über verify korrekt durch."""
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen({"choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}]}),
    )
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "tverifyllm.fabrikant.yaml").write_text(
        f"name: tverifyllm\nroot: {root}\nloops:\n  t:\n    steps:\n"
        "      - type: verify\n        name: check\n        checks:\n"
        "          - type: llm\n            name: judge\n            prompt: ok?\n"
        "            schema:\n              a: integer\n"
    )
    rec = run_loop("tverifyllm", "t")
    v = [s for s in rec["steps"] if s["type"] == "verify"][0]
    assert v["status"] == "pass"
    assert v["verify"][0]["ok"] is True
    assert v["verify"][0]["name"] == "judge"


# ---------------------------------------------------------------- Fehlerklasse (ADR 0035)
def test_red_check_without_cause_is_outcome(tmp_path):
    step = {"checks": [{"name": "ac1", "type": "shell", "cmd": "exit 1"}]}
    assert _dispatch_step(_ctx(tmp_path), step | {"type": "verify"}, lambda m: None)["failure_kind"] == "outcome"


def test_red_check_with_known_cause_keeps_it(tmp_path):
    step = {"type": "verify", "checks": [{"name": "ac1", "type": "shell", "cmd": "gibt-es-nicht-xyz"}]}
    assert _dispatch_step(_ctx(tmp_path), step, lambda m: None)["failure_kind"] == "environment"


def test_invalid_check_is_environment(tmp_path):
    step = {"type": "verify", "checks": [{"name": "kaputt"}]}
    assert _dispatch_step(_ctx(tmp_path), step, lambda m: None)["failure_kind"] == "environment"


def test_unclassified_exception_stays_unknown(tmp_path, monkeypatch):
    """Eine Exception ohne Klasse (Timeout, Bug) ist kein unerfülltes Kriterium (ADR 0035 Nr. 4)."""

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired("sh", 1)

    monkeypatch.setattr("fabrikant.engine._step_shell", boom)
    step = {"type": "verify", "checks": [{"name": "hängt", "type": "shell", "cmd": "sleep 9"}]}
    assert _dispatch_step(_ctx(tmp_path), step, lambda m: None)["failure_kind"] == "unknown"


def test_interrupted_check_is_classified(tmp_path, monkeypatch):
    """Jede FailureKind hat einen Rang — ein abgebrochener Check lässt verify nicht selbst scheitern."""
    from fabrikant.failures import failed
    from fabrikant.runmodel import FailureKind
    from fabrikant.steps import verify

    assert set(verify._KIND_PRIORITY) == set(FailureKind)
    monkeypatch.setattr(
        "fabrikant.engine._step_shell", lambda *_a, **_k: failed("abgebrochen", FailureKind.INTERRUPTED)
    )
    checks = [{"name": "ac1", "type": "shell", "cmd": "exit 1"}]
    res = _dispatch_step(_ctx(tmp_path), {"type": "verify", "checks": checks}, lambda m: None)
    assert res["failure_kind"] == "interrupted"


def test_environment_check_outranks_outcome(tmp_path):
    """Rote Kriterien verdecken nicht, dass ein Check gar nicht laufen konnte (braucht einen Menschen)."""
    checks = [
        {"name": "ac1", "type": "shell", "cmd": "exit 1"},
        {"name": "ac2", "type": "shell", "cmd": "gibt-es-nicht-xyz"},
    ]
    res = _dispatch_step(_ctx(tmp_path), {"type": "verify", "checks": checks}, lambda m: None)
    assert res["failure_kind"] == "environment"
