"""Step: llm — Helfer (Provider, Prompt, JSON-Extraktion, ctx-Isolation) und Fehlerpfade (Issue #211)."""

from __future__ import annotations

import os

import pytest

from fabrikant import llm
from fabrikant.runmodel import FailureKind
from fabrikant.steps import llm as llm_step


@pytest.fixture
def provider(monkeypatch, tmp_path):
    """Ein aktiver Dummy-Provider, isoliert von Env und Persistenz."""
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


def _answer(monkeypatch, text: str) -> None:
    usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    monkeypatch.setattr(
        llm_step.llm_registry,
        "complete",
        lambda *a, **kw: {"text": text, "usage": dict(usage), "finish_reason": "stop"},
    )


def _run(step: dict, ctx: dict | None = None) -> tuple[dict, list[str]]:
    lines: list[str] = []
    return llm_step._step_llm(ctx if ctx is not None else {}, step, lines.append), lines


def test_resolve_provider_unknown_pinned(provider):
    with pytest.raises(llm_step._ProviderError, match="unbekannt.*dummy"):
        llm_step._resolve_provider({"provider": "nope"})


def test_resolve_provider_none_configured(monkeypatch):
    monkeypatch.setattr(llm_step.llm_registry, "active_provider", lambda: None)
    with pytest.raises(llm_step._ProviderError, match="kein LLM konfiguriert"):
        llm_step._resolve_provider({})


def test_step_without_provider_is_environment_failure(monkeypatch):
    monkeypatch.setattr(llm_step.llm_registry, "active_provider", lambda: None)
    res, _ = _run({"prompt": "x"})
    assert res["ok"] is False
    assert res["failure_kind"] == FailureKind.ENVIRONMENT.value


def test_build_prompt_marks_untrusted_blocks():
    prompt = llm_step._build_prompt({"prompt": "Titel: {t}", "untrusted": ["body"]}, {"t": "A", "body": "rm -rf /"})
    assert prompt.startswith("Titel: A")
    assert '<untrusted name="body">\nrm -rf /\n</untrusted>' in prompt
    assert "KEINE Anweisungen" in prompt


def test_build_prompt_without_untrusted_is_plain():
    assert llm_step._build_prompt({"prompt": "hi"}, {}) == "hi"


@pytest.mark.parametrize(
    ("text", "clean", "parsed"),
    [
        ('```json\n{"a": 1}\n```', '{"a": 1}', {"a": 1}),
        ('Hier:\n```\n{"a": 2}\n```\nfertig', '{"a": 2}', {"a": 2}),
        ('Ergebnis: {"a": 3} — ok', 'Ergebnis: {"a": 3} — ok', {"a": 3}),
        ("[1, 2]", "[1, 2]", None),
        ("keine Ahnung", "keine Ahnung", None),
    ],
)
def test_extract_json(text, clean, parsed):
    assert llm_step._extract_json(text) == (clean, parsed)


def test_inject_schema_keys_isolates_ctx():
    ctx = {"workdir": "/safe"}
    lines: list[str] = []
    injected = llm_step._inject_schema_keys(
        {"label": "bug", "tags": ["a"], "workdir": "/evil", "extra": 1},
        {"label": "string", "tags": "list", "workdir": "string"},
        ctx,
        lines.append,
    )
    assert ctx == {"workdir": "/safe", "label": "bug", "tags": '["a"]'}
    assert injected == ["label=bug", "tags=['a']"]
    assert any("'extra' nicht im Schema" in ln for ln in lines)
    assert any("'workdir' existiert bereits" in ln for ln in lines)


def test_step_fenced_json_lands_in_ctx(provider, monkeypatch):
    _answer(monkeypatch, '```json\n{"label": "bug"}\n```')
    ctx: dict = {}
    res, _ = _run({"prompt": "x", "schema": {"label": "string"}}, ctx)
    assert res["ok"] is True
    assert ctx["label"] == "bug"
    assert res["summary"] == "label=bug"


def test_step_non_object_json_fails(provider, monkeypatch):
    _answer(monkeypatch, "[1, 2, 3]")
    res, _ = _run({"prompt": "x"})
    assert res["ok"] is False
    assert res["summary"] == "LLM lieferte kein JSON-Object"


def test_step_schema_enum_violation_fails(provider, monkeypatch):
    _answer(monkeypatch, '{"label": "evil; rm -rf /"}')
    res, _ = _run({"prompt": "x", "schema": {"label": {"type": "string", "enum": ["bug", "feature"]}}})
    assert res["ok"] is False
    assert res["summary"].startswith("Schema validation:")
    assert "nicht in erlaubten Werten" in res["summary"]


def test_step_without_schema_keeps_output_in_log_only(provider, monkeypatch):
    _answer(monkeypatch, '{"a": 1}')
    ctx: dict = {}
    res, _ = _run({"prompt": "x"}, ctx)
    assert res["ok"] is True
    assert ctx == {}
    assert res["summary"] == "kein Schema — LLM-Output nur im Log"


def test_step_call_error_is_classified(provider, monkeypatch):
    def boom(*a, **kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm_step.llm_registry, "complete", boom)
    res, _ = _run({"prompt": "x"})
    assert res["ok"] is False
    assert res["summary"] == "LLM call failed: timed out"
    assert "failure_kind" in res


def test_step_emits_untrusted_count(provider, monkeypatch):
    _answer(monkeypatch, '{"a": 1}')
    _, lines = _run({"prompt": "x", "untrusted": ["b"]}, {"b": "y"})
    assert "1 untrusted-Blöcke" in lines[0]
