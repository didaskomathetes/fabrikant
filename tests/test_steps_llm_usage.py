"""Step: llm — Usage-Recording (Tokens/Model/Provider im Result).

Tracing-Baustein: der Run-Record trägt pro LLM-Call strukturierte Usage-Daten,
damit ein späterer Exporter (OTel/Langfuse) Kosten ausrechnen kann, ohne
fabrikant-Semantik zu forken.
"""

from __future__ import annotations

import json

import pytest

from fabrikant import llm, run_loop
from fabrikant.steps import llm as llm_step


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
    return {"root": str(tmp_path), "workdir": str(tmp_path), "project": "u", "run_id": "ru"}


def test_usage_recorded_in_result(clean_llm, monkeypatch, tmp_path):
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen(
            {
                "choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        ),
    )
    ctx = _ctx(tmp_path)
    res = llm_step._step_llm(ctx, {"prompt": "x", "schema": {"a": "integer"}}, lambda _m: None)
    assert res["ok"] is True
    assert res["usage"] == {
        "model": "test-model",
        "provider": "dummy",
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "billing": "unknown",
    }
    assert ctx["a"] == 1  # Schema-Injection weiterhin unverändert


def test_usage_missing_defaults_to_zero(clean_llm, monkeypatch, tmp_path):
    # Provider ohne usage-Block (z. B. trimmte lokale Kompat-Server) → keine Exception
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen({"choices": [{"message": {"content": '{"a": 2}'}, "finish_reason": "stop"}]}),
    )
    ctx = _ctx(tmp_path)
    res = llm_step._step_llm(ctx, {"prompt": "x", "schema": {"a": "integer"}}, lambda _m: None)
    assert res["ok"] is True
    assert res["usage"]["total_tokens"] == 0
    assert res["usage"]["provider"] == "dummy"


def test_malformed_usage_does_not_fail_call(clean_llm, monkeypatch, tmp_path):
    """Malformierter usage-Block (Drittanbieter-API) dreht einen erfolgreichen
    Call NICHT in einen Failure — Zero-Fallback pro Feld (gültige Felder bleiben),
    Schema-Injection bleibt stehen."""
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen(
            {
                "choices": [{"message": {"content": '{"a": 3}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": "zwölf", "completion_tokens": 7, "total_tokens": 18},
            }
        ),
    )
    ctx = _ctx(tmp_path)
    res = llm_step._step_llm(ctx, {"prompt": "x", "schema": {"a": "integer"}}, lambda _m: None)
    assert res["ok"] is True
    assert res["usage"]["prompt_tokens"] == 0
    assert res["usage"]["total_tokens"] == 18
    assert ctx["a"] == 3


def test_usage_with_pinned_provider(clean_llm, monkeypatch, tmp_path):
    """expliziter `provider:`-Key → usage.provider trägt den pinnten Namen."""
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen(
            {
                "choices": [{"message": {"content": '{"a": 4}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ),
    )
    ctx = _ctx(tmp_path)
    res = llm_step._step_llm(ctx, {"prompt": "x", "provider": "dummy", "schema": {"a": "integer"}}, lambda _m: None)
    assert res["ok"] is True
    assert res["usage"]["provider"] == "dummy"


# ---------------------------------------------------------------- Engine-Level
def test_usage_lands_in_run_record(clean_llm, monkeypatch, runs_dir, projects_dir, tmp_path):
    """Der Run-Record (nicht nur das Step-Result) trägt die Usage-Daten."""
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen(
            {
                "choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
        ),
    )
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "tusage.fabrikant.yaml").write_text(
        f"name: tusage\nroot: {root}\nloops:\n  t:\n    steps:\n"
        "      - type: llm\n        name: ask\n        prompt: hallo\n"
        "        schema:\n          a: integer\n"
    )
    rec = run_loop("tusage", "t")
    llm_rec = [s for s in rec["steps"] if s["type"] == "llm"][0]
    assert llm_rec["status"] == "pass"
    assert llm_rec["usage"]["total_tokens"] == 8
    assert llm_rec["usage"]["model"] == "test-model"


def test_verify_lands_in_run_record(runs_dir, projects_dir, tmp_path):
    """Pro-Check-Output landet im Run-Record (Key 'verify'), nicht nur im Result."""
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "tverify.fabrikant.yaml").write_text(
        f"name: tverify\nroot: {root}\nloops:\n  t:\n    steps:\n"
        "      - type: verify\n        name: check\n        checks:\n"
        '          - type: shell\n            name: ok\n            cmd: "true"\n'
    )
    rec = run_loop("tverify", "t")
    v = [s for s in rec["steps"] if s["type"] == "verify"][0]
    assert v["status"] == "pass"
    assert len(v["verify"]) == 1
    assert v["verify"][0]["ok"] is True


def _fake_urlopen_seq(payloads, budgets):
    """Liefert ``payloads`` nacheinander; protokolliert max_tokens je Request."""
    it = iter(payloads)

    def _open(req, timeout=None):
        budgets.append(json.loads(req.data)["max_tokens"])
        return _fake_urlopen(next(it))(req, timeout)

    return _open


def test_truncated_answer_retries_with_larger_budget(clean_llm, monkeypatch, tmp_path):
    """Reasoning-Modell verbraucht max_tokens fürs Denken → leerer Content,
    finish_reason=length → ein Retry mit vervielfachtem Budget; Usage summiert."""
    budgets: list[int] = []
    usage = {"prompt_tokens": 10, "completion_tokens": 300, "total_tokens": 310}
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen_seq(
            [
                {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": usage},
                {"choices": [{"message": {"content": '{"a": 5}'}, "finish_reason": "stop"}], "usage": usage},
            ],
            budgets,
        ),
    )
    ctx = _ctx(tmp_path)
    res = llm_step._step_llm(ctx, {"prompt": "x", "schema": {"a": "integer"}, "max_tokens": 300}, lambda _m: None)
    assert res["ok"] is True
    assert budgets == [300, 300 * llm_step._TRUNCATION_RETRY_FACTOR]
    assert res["usage"]["total_tokens"] == 620
    assert ctx["a"] == 5


def test_truncated_twice_reports_truncation(clean_llm, monkeypatch, tmp_path):
    budgets: list[int] = []
    cut = {"choices": [{"message": {"content": '{"a": '}, "finish_reason": "length"}]}
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_urlopen_seq([cut, cut], budgets))
    res = llm_step._step_llm(_ctx(tmp_path), {"prompt": "x", "max_tokens": 10}, lambda _m: None)
    assert res["ok"] is False
    assert "abgeschnitten" in res["summary"]
    assert len(budgets) == 2


def test_null_content_is_no_json_not_crash(clean_llm, monkeypatch, tmp_path):
    """content: null (manche Server bei reinem Reasoning) → sauberer Fehler, kein AttributeError."""
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen({"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}),
    )
    res = llm_step._step_llm(_ctx(tmp_path), {"prompt": "x"}, lambda _m: None)
    assert res["ok"] is False
    assert res["summary"] == "LLM lieferte kein JSON-Object"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('Hier: {"a": 1} fertig.', {"a": 1}),
        ('{"a": {"b": "}"}}', {"a": {"b": "}"}}),
        # Selbstkorrektur (refine/dependencies, issue#282): das letzte Object gewinnt.
        ('{"a": "#1, #2, #3, #4"}\n\nMoment, höchstens 3. Korrigiert:\n\n{"a": "#1, #2, #3"}', {"a": "#1, #2, #3"}),
        ('{kaputt} danach {"a": 2}', {"a": 2}),
        ("[1, 2]", None),
        ("kein json", None),
        ('{"a": ', None),
        ("", None),
    ],
)
def test_extract_json_object(text, expected):
    assert llm_step._extract_json_object(text) == expected


def test_self_correcting_answer_uses_last_object(clean_llm, monkeypatch, tmp_path):
    content = '{"a": 1}\n\nMoment, falsch. Hier die korrigierte Antwort:\n\n{"a": 2}'
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}),
    )
    ctx = _ctx(tmp_path)
    res = llm_step._step_llm(ctx, {"prompt": "x", "schema": {"a": "integer"}}, lambda _m: None)
    assert res["ok"] is True
    assert ctx["a"] == 2
