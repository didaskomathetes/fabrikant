"""Chat-Abbruch: In-Flight-Registry + cancellable LLM-Read."""

import threading

import pytest
from helpers import not_none

from fabrik import chat, llm


@pytest.fixture
def clean_llm(monkeypatch, tmp_path):
    """Isoliert LLM-Env + Persistenz; konfiguriert einen Dummy-Provider."""
    import os

    for k in list(os.environ):
        if k.startswith("FABRIK_LLM"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_ACTIVE_FILE", tmp_path / "active_llm")
    monkeypatch.setenv("FABRIK_LLM_PROVIDERS", "dummy")
    monkeypatch.setenv("FABRIK_LLM_DUMMY_URL", "http://127.0.0.1:1/v1/chat/completions")
    monkeypatch.setenv("FABRIK_LLM_DUMMY_MODEL", "m")
    llm.reset_cache()
    llm.set_active("dummy")
    yield
    llm.reset_cache()


def test_register_is_idempotent():
    ev1 = chat._register_chat("r1")
    ev2 = chat._register_chat("r1")
    assert ev1 is ev2  # gleiches Event, nicht neu angelegt
    chat._finish_chat("r1")


def test_cancel_known_sets_event():
    ev = chat._register_chat("r2")
    assert not ev.is_set()
    assert chat._cancel_chat("r2") is True
    assert ev.is_set()
    chat._finish_chat("r2")


def test_cancel_unknown_returns_false():
    assert chat._cancel_chat("ghost") is False


def test_finish_removes_entry():
    chat._register_chat("r3")
    chat._finish_chat("r3")
    assert chat._cancel_chat("r3") is False  # weg


def test_llm_chat_presets_cancel_returns_aborted(clean_llm):
    ev = threading.Event()
    ev.set()  # vor dem Call abgebrochen → kein Netzwerk-Call
    assert chat._llm_chat("hi", ev, "r4") == "⊘ abgebrochen"


def test_llm_chat_no_cancel_no_event(clean_llm, monkeypatch):
    # Ohne Cancel-Event: urlopen wird aufgerufen (hier auf unerreichen Port → Fehler-String)
    res = chat._llm_chat("hi", None, "r5")
    assert res is not None
    assert res.startswith("✖")  # Verbindung fehlgeschlagen, aber kein 'abgebrochen'


def _fake_urlopen(payload):
    import json as _json

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps(payload).encode()

    return lambda req, timeout=None: _R()


def test_llm_chat_empty_content_returns_warning(clean_llm, monkeypatch):
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}),
    )
    res = not_none(chat._llm_chat("hi", None, "r6"))
    assert res.startswith("⚠")
    assert "leere Antwort" in res


def test_llm_chat_null_content_returns_warning(clean_llm, monkeypatch):
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen({"choices": [{"message": {"content": None}, "finish_reason": "length"}]}),
    )
    res = not_none(chat._llm_chat("hi", None, "r7"))
    assert res.startswith("⚠")
    assert "length" in res


def test_llm_chat_missing_choices_returns_warning(clean_llm, monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_urlopen({"choices": []}))
    res = chat._llm_chat("hi", None, "r8")
    assert res.startswith("⚠")


def test_llm_chat_truncated_yaml_warns_and_not_registered(clean_llm, monkeypatch):
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen(
            {
                "choices": [
                    {
                        "message": {"content": "Hier die Spec:\n```yaml\nname: trunc\nloops:\n  a:"},
                        "finish_reason": "length",
                    }
                ]
            }
        ),
    )
    res = not_none(chat._llm_chat("hi", None, "r9"))
    assert "abgeschnitten" in res
    assert "trunc" not in chat._pending_specs


def test_llm_chat_valid_proposal_registers_and_applies(clean_llm, monkeypatch, tmp_path, projects_dir):
    root = tmp_path / "root"
    root.mkdir()
    spec_yaml = f"name: prop\nroot: {root}\nloops:\n  lint:\n    steps:\n      - type: shell\n        cmd: echo hi\n"
    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        _fake_urlopen(
            {"choices": [{"message": {"content": f"Gerne:\n```yaml\n{spec_yaml}```"}, "finish_reason": "stop"}]}
        ),
    )
    res = not_none(chat._llm_chat("ändere die spec", None, "r10"))
    assert "Vorschlag validiert" in res
    assert "prop" in chat._pending_specs
    out = chat._chat("apply prop")
    assert "aktualisiert" in out
    assert (projects_dir / "prop.fabrik.yaml").read_text().startswith("name: prop")
    assert "prop" not in chat._pending_specs
