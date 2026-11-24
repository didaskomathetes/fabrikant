"""LLM-Provider-Registry: Env-Parsing, aktiver Provider, Persistenz, Step-Auflösung."""

import http.server
import os
import threading

import pytest

from fabrikant import llm


@pytest.fixture
def clean_llm_env(monkeypatch, tmp_path):
    """Isoliert alle FABRIKANT_LLM_*-Env-Vars + die Persistenz-Datei pro Test."""
    for k in list(os.environ):
        if k.startswith("FABRIKANT_LLM"):
            monkeypatch.delenv(k, raising=False)
    state_file = tmp_path / "active_llm"
    monkeypatch.setattr(llm, "_ACTIVE_FILE", state_file)
    llm.reset_cache()
    yield
    llm.reset_cache()


# ---------------------------------------------------------------- list_providers
def test_list_providers_named(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "ollama,llama-cpp")
    monkeypatch.setenv("FABRIKANT_LLM_OLLAMA_URL", "http://o/v1")
    monkeypatch.setenv("FABRIKANT_LLM_OLLAMA_MODEL", "m1")
    monkeypatch.setenv("FABRIKANT_LLM_LLAMA_CPP_URL", "http://l/v1")
    monkeypatch.setenv("FABRIKANT_LLM_LLAMA_CPP_MODEL", "m2")
    names = [p["name"] for p in llm.list_providers()]
    assert names == ["ollama", "llama-cpp"]
    # Bindestrich im Namen → Unterstrich im Env-Präfix
    assert llm.get_provider("llama-cpp")["url"] == "http://l/v1"


def test_list_providers_legacy_default(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_URL", "http://x/v1")
    monkeypatch.setenv("FABRIKANT_LLM_MODEL", "legacy")
    providers = llm.list_providers()
    assert [p["name"] for p in providers] == ["default"]
    assert providers[0]["model"] == "legacy"


def test_list_providers_named_wins_over_legacy(clean_llm_env, monkeypatch):
    # 'default' als Named-Provider + Legacy → nur ein Eintrag
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "default")
    monkeypatch.setenv("FABRIKANT_LLM_DEFAULT_URL", "http://n/v1")
    monkeypatch.setenv("FABRIKANT_LLM_DEFAULT_MODEL", "named")
    monkeypatch.setenv("FABRIKANT_LLM_URL", "http://legacy/v1")
    monkeypatch.setenv("FABRIKANT_LLM_MODEL", "legacy")
    providers = llm.list_providers()
    assert [p["name"] for p in providers] == ["default"]
    assert providers[0]["url"] == "http://n/v1"


def test_list_providers_incomplete_skipped(clean_llm_env, monkeypatch):
    # Provider ohne URL oder ohne Modell wird nicht gelistet
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "a,b")
    monkeypatch.setenv("FABRIKANT_LLM_A_URL", "http://a/v1")  # kein Modell
    monkeypatch.setenv("FABRIKANT_LLM_B_MODEL", "m")  # keine URL
    assert llm.list_providers() == []


def test_list_providers_key_included(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "k")
    monkeypatch.setenv("FABRIKANT_LLM_K_URL", "http://k/v1")
    monkeypatch.setenv("FABRIKANT_LLM_K_MODEL", "m")
    monkeypatch.setenv("FABRIKANT_LLM_K_KEY", "sekret123")
    assert llm.get_provider("k")["key"] == "sekret123"


# ---------------------------------------------------------------- active provider
def test_default_name_first_named(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "a,b")
    monkeypatch.setenv("FABRIKANT_LLM_A_URL", "http://a")
    monkeypatch.setenv("FABRIKANT_LLM_A_MODEL", "m")
    monkeypatch.setenv("FABRIKANT_LLM_B_URL", "http://b")
    monkeypatch.setenv("FABRIKANT_LLM_B_MODEL", "m")
    assert llm.default_name() == "a"


def test_default_name_none_when_empty(clean_llm_env):
    assert llm.default_name() is None
    assert llm.active_provider() is None


def test_set_active_persists(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "a,b")
    monkeypatch.setenv("FABRIKANT_LLM_A_URL", "http://a")
    monkeypatch.setenv("FABRIKANT_LLM_A_MODEL", "m")
    monkeypatch.setenv("FABRIKANT_LLM_B_URL", "http://b")
    monkeypatch.setenv("FABRIKANT_LLM_B_MODEL", "m")
    llm.set_active("b")
    assert llm.active_name() == "b"
    # Persistenz überlebt Cache-Reset (simulierter Neustart)
    llm.reset_cache()
    assert llm.active_name() == "b"


def test_set_active_unknown_raises(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "a")
    monkeypatch.setenv("FABRIKANT_LLM_A_URL", "http://a")
    monkeypatch.setenv("FABRIKANT_LLM_A_MODEL", "m")
    with pytest.raises(ValueError, match="unbekannt"):
        llm.set_active("zzz")


def test_active_falls_back_to_default(clean_llm_env, monkeypatch):
    # Persistierte Datei zeigt auf nicht mehr existierenden Provider → Default
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "a")
    monkeypatch.setenv("FABRIKANT_LLM_A_URL", "http://a")
    monkeypatch.setenv("FABRIKANT_LLM_A_MODEL", "m")
    llm._ACTIVE_FILE.write_text("gone\n")
    assert llm.active_name() == "a"


# ---------------------------------------------------------------- step resolution
def _step_ctx():
    return {"project": "p", "root": "/tmp"}


def test_step_uses_active_provider(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "a")
    monkeypatch.setenv("FABRIKANT_LLM_A_URL", "http://a/v1")
    monkeypatch.setenv("FABRIKANT_LLM_A_MODEL", "m")
    llm.set_active("a")
    prov = llm.active_provider()
    assert prov["url"] == "http://a/v1" and prov["model"] == "m"


def test_step_pins_provider(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "a,b")
    monkeypatch.setenv("FABRIKANT_LLM_A_URL", "http://a")
    monkeypatch.setenv("FABRIKANT_LLM_A_MODEL", "ma")
    monkeypatch.setenv("FABRIKANT_LLM_B_URL", "http://b")
    monkeypatch.setenv("FABRIKANT_LLM_B_MODEL", "mb")
    llm.set_active("a")
    # Step pinnt 'b' → gewinnt über aktiven 'a'
    assert llm.get_provider("b")["model"] == "mb"


def test_step_model_override(clean_llm_env, monkeypatch):
    monkeypatch.setenv("FABRIKANT_LLM_PROVIDERS", "a")
    monkeypatch.setenv("FABRIKANT_LLM_A_URL", "http://a")
    monkeypatch.setenv("FABRIKANT_LLM_A_MODEL", "ma")
    step = {"provider": "a", "model": "override"}
    prov = llm.get_provider(step["provider"])
    assert (step.get("model") or prov["model"]) == "override"


@pytest.fixture
def chat_endpoint():
    """Echter lokaler HTTP-Endpunkt (kein Patchen von urllib): ``serve(body)`` → URL;
    ``seen["headers"]`` hält die Request-Header des letzten Calls."""
    seen: dict = {}
    servers = []

    def serve(body: bytes) -> str:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen["headers"] = dict(self.headers.items())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"

    serve.seen = seen
    yield serve
    for srv in servers:
        srv.shutdown()
        srv.server_close()


_HI = b'{"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}'


def test_complete_sets_bearer_header(chat_endpoint):
    """Regression: Authorization-Header trägt den echten Key (kein Literal-Platzhalter)."""
    url = chat_endpoint(_HI)
    out = llm.complete({"url": url, "model": "m", "key": "sekret123"}, "sys", "user")
    assert out["text"] == "hi"
    assert chat_endpoint.seen["headers"]["Authorization"] == "Bearer sekret123"


def test_complete_without_key_sends_no_authorization(chat_endpoint):
    url = chat_endpoint(_HI)
    llm.complete({"url": url, "model": "m", "key": ""}, "sys", "user")
    assert "Authorization" not in chat_endpoint.seen["headers"]


def test_complete_tolerates_overflowing_usage(chat_endpoint):
    """`1e999` parst zu inf → int() wirft OverflowError; der Call bleibt erfolgreich."""
    url = chat_endpoint(b'{"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 1e999}}')
    out = llm.complete({"url": url, "model": "m", "key": ""}, "sys", "user")
    assert out["text"] == "hi"
    assert out["usage"]["total_tokens"] == 0


_ZERO = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}


@pytest.mark.parametrize(
    "raw",
    [None, "x", 5, [1], {"prompt_tokens": "abc"}, {"total_tokens": float("inf")}, {"total_tokens": "nan"}],
)
def test_usage_malformed_yields_zeros(raw):
    assert llm._usage(raw) == _ZERO


def test_usage_valid():
    assert llm._usage({"prompt_tokens": 2, "total_tokens": "5"}) == {
        "prompt_tokens": 2,
        "completion_tokens": 0,
        "total_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_usage_truncates_fractions_regardless_of_type():
    """Zahl und String zählen gleich — die Serialisierung des Anbieters ist egal."""
    assert llm._usage({"prompt_tokens": 5.5, "total_tokens": "5.5"}) == {
        "prompt_tokens": 5,
        "completion_tokens": 0,
        "total_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_usage_keeps_valid_fields_when_one_is_malformed():
    raw = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": "n/a"}
    assert llm._usage(raw) == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_usage_reports_cached_prompt_tokens():
    """OpenAI: gecachte Tokens stehen in prompt_tokens_details und sind Teil von prompt_tokens."""
    raw = {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 100},
    }
    assert llm._usage(raw)["cache_read_tokens"] == 100
    assert llm._usage({**raw, "prompt_tokens_details": "kaputt"})["cache_read_tokens"] == 0
