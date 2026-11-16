"""Backends: opencode serve (HTTP-API, 1.x und 2.x — Dual-Support)."""

import base64
import json
import urllib.error
import urllib.request

import pytest
from helpers import http_error

from fabrik.backends.opencode import OpenCodeBackend, _default_permissions, _ServeApi

# Test-only Dummy-Credentials (kein echtes Secret) — zentral gepflegt.
TEST_AUTH_USER = "fabrik"
TEST_AUTH_PASS = "geheim"


# ---------------------------------------------------------------- fakes
class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode() if isinstance(body, (dict, list)) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeUrlopen:
    """urlopen-Fake: Routes (method, url-prefix) → (status, body) | Exception."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        data = json.loads(req.data.decode()) if req.data else None
        self.calls.append({"method": req.get_method(), "url": url, "headers": dict(req.headers), "data": data})
        for method, prefix, resp in self.routes:
            if req.get_method() == method and prefix in url:
                if isinstance(resp, Exception):
                    raise resp
                return _FakeResp(*resp)
        raise AssertionError(f"unroutet: {req.get_method()} {url}")


def _install(monkeypatch, routes):
    fake = _FakeUrlopen(routes)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def _v1(routes):
    """1.x-Server: /api/health → healthy, /api/info → 404."""
    return [("GET", "/api/info", (404, {})), ("GET", "/api/health", (200, {"healthy": True})), *routes]


def _v2(routes):
    """2.x-Server: /api/info → version, /api/health → 404."""
    return [("GET", "/api/health", (404, {})), ("GET", "/api/info", (200, {"version": "2.0.32"})), *routes]


def _be(monkeypatch, routes, **kw):
    _install(monkeypatch, routes)
    return OpenCodeBackend("http://srv:1", "ollama/llama3.2:3b", **kw)


# ---------------------------------------------------------------- _ServeApi v1
def test_v1_health_true(monkeypatch):
    api = _ServeApi("http://srv:1", "1", None, "opencode")
    _install(monkeypatch, [("GET", "/api/health", (200, {"healthy": True}))])
    assert api.health() is True


def test_v1_health_false_on_unhealthy_or_error(monkeypatch):
    api = _ServeApi("http://srv:1", "1", None, "opencode")
    _install(monkeypatch, [("GET", "/api/health", (200, {"healthy": False}))])
    assert api.health() is False
    _install(monkeypatch, [("GET", "/api/health", (500, {}))])
    assert api.health() is False
    _install(monkeypatch, [("GET", "/api/health", ConnectionRefusedError("down"))])
    assert api.health() is False


def test_v1_create_session_uses_directory_param_and_permission_field(monkeypatch):
    api = _ServeApi("http://srv:1", "1", None, "opencode")
    fake = _install(monkeypatch, [("POST", "/session", (200, {"id": "ses_1"}))])
    sid = api.create_session("/tmp/wd", "t", {"providerID": "ollama", "id": "m"}, _default_permissions())
    assert sid == "ses_1"
    c = fake.calls[0]
    assert c["url"] == "http://srv:1/session?directory=/tmp/wd"
    assert c["data"]["permission"] == _default_permissions()
    assert "permissions" not in c["data"]
    assert "Authorization" not in c["headers"]


def test_v1_create_session_raises_without_id(monkeypatch):
    api = _ServeApi("http://srv:1", "1", None, "opencode")
    _install(monkeypatch, [("POST", "/session", (200, {}))])
    with pytest.raises(RuntimeError, match="ohne id"):
        api.create_session("/tmp/wd", "t", {}, [])


def test_v1_send_prompt_uses_prompt_async_parts(monkeypatch):
    api = _ServeApi("http://srv:1", "1", None, "opencode")
    fake = _install(monkeypatch, [("POST", "/session/s1/prompt_async", (204, {}))])
    api.send_prompt("s1", "hallo", "/tmp/wd")
    c = fake.calls[0]
    assert c["url"] == "http://srv:1/session/s1/prompt_async?directory=/tmp/wd"
    assert c["data"] == {"parts": [{"type": "text", "text": "hallo"}]}


def test_v1_send_prompt_raises_on_error_status(monkeypatch):
    api = _ServeApi("http://srv:1", "1", None, "opencode")
    _install(monkeypatch, [("POST", "/session/s1/prompt_async", (500, {}))])
    with pytest.raises(RuntimeError, match="HTTP 500"):
        api.send_prompt("s1", "x", "/tmp/wd")


def test_v1_fetch_messages_returns_bare_list(monkeypatch):
    api = _ServeApi("http://srv:1", "1", None, "opencode")
    msgs = [{"info": {"role": "assistant"}, "parts": []}]
    _install(monkeypatch, [("GET", "/session/s1/message", (200, msgs))])
    assert api.fetch_messages("s1", "/tmp/wd") == msgs


def test_v1_abort_posts_abort_with_directory(monkeypatch):
    api = _ServeApi("http://srv:1", "1", None, "opencode")
    fake = _install(monkeypatch, [("POST", "/session/s1/abort", (200, {}))])
    api.abort("s1", "/tmp/wd")
    assert fake.calls[0]["url"] == "http://srv:1/session/s1/abort?directory=/tmp/wd"


# ---------------------------------------------------------------- _ServeApi v2
def test_v2_health_true_on_info(monkeypatch):
    api = _ServeApi("http://srv:1", "2", "pw", "opencode")
    _install(monkeypatch, [("GET", "/api/info", (200, {"version": "2.0.32"}))])
    assert api.health() is True


def test_v2_create_session_maps_rules_location_and_unwraps_data(monkeypatch):
    api = _ServeApi("http://srv:1", "2", None, "opencode")
    fake = _install(monkeypatch, [("POST", "/api/session", (200, {"data": {"id": "ses_2"}}))])
    sid = api.create_session("/tmp/wd", "t", {"providerID": "ollama", "id": "m"}, _default_permissions())
    assert sid == "ses_2"
    c = fake.calls[0]
    assert c["url"] == "http://srv:1/api/session"  # kein ?directory=
    assert c["data"]["location"] == {"directory": "/tmp/wd"}
    assert c["data"]["permissions"] == [
        {"action": "edit", "resource": "*", "effect": "allow"},
        {"action": "bash", "resource": "*", "effect": "allow"},
    ]
    assert "permission" not in c["data"]


def test_v2_create_session_raises_without_id(monkeypatch):
    api = _ServeApi("http://srv:1", "2", None, "opencode")
    _install(monkeypatch, [("POST", "/api/session", (200, {"data": {}}))])
    with pytest.raises(RuntimeError, match="ohne id"):
        api.create_session("/tmp/wd", "t", {}, [])


def test_v2_send_prompt_uses_text_body(monkeypatch):
    api = _ServeApi("http://srv:1", "2", None, "opencode")
    fake = _install(monkeypatch, [("POST", "/api/session/s2/prompt", (200, {"data": {}}))])
    api.send_prompt("s2", "hallo", "/tmp/wd")
    c = fake.calls[0]
    assert c["url"] == "http://srv:1/api/session/s2/prompt"
    assert c["data"] == {"text": "hallo"}


def test_v2_send_prompt_raises_on_error_status(monkeypatch):
    api = _ServeApi("http://srv:1", "2", None, "opencode")
    _install(monkeypatch, [("POST", "/api/session/s2/prompt", (400, {}))])
    with pytest.raises(RuntimeError, match="HTTP 400"):
        api.send_prompt("s2", "x", "/tmp/wd")


def test_v2_fetch_messages_follows_cursor_and_normalizes(monkeypatch):
    api = _ServeApi("http://srv:1", "2", None, "opencode")
    # 2.x-Seiten sind NEUESTE-ZUERST: page1 (ohne Cursor) = neueste Batch,
    # page2 (via cursor) = ältere Batch. created-Werte entsprechend.
    page1 = {
        "data": [
            {"type": "user", "payload": {"text": "hi"}},  # wird gefiltert
            {
                "type": "assistant",
                "time": {"created": 200},
                "finish": "tool-calls",
                "tokens": {"input": 5, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                "cost": 0,
                "content": [
                    {"type": "tool", "name": "bash", "state": {"status": "completed", "input": {}}},
                ],
            },
        ],
        "cursor": {"previous": None, "next": "c2"},
    }
    page2 = {
        "data": [
            {
                "type": "assistant",
                "time": {"created": 100},
                "finish": "stop",
                "tokens": {"input": 7, "output": 3, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                "cost": 0.01,
                "content": [{"type": "text", "text": "fertig"}],
            }
        ],
        "cursor": {"previous": "c2", "next": None},
    }
    fake = _install(
        monkeypatch,
        [
            ("GET", "/api/session/s2/message?cursor=c2", (200, page2)),
            ("GET", "/api/session/s2/message", (200, page1)),
        ],
    )
    msgs = api.fetch_messages("s2", "/tmp/wd")
    assert len(msgs) == 2  # user-Message gefiltert
    # Umgekehrt → älteste-zuerst: page2 (created=100) zuerst, page1 (200) zuletzt.
    assert msgs[0]["info"]["finish"] == "stop"
    assert msgs[0]["parts"][0] == {"type": "text", "text": "fertig"}
    assert msgs[1]["info"]["finish"] == "tool-calls"
    tool = msgs[1]["parts"][0]
    assert tool["type"] == "tool"
    assert json.loads(tool["text"])["name"] == "bash"
    assert tool["state"]["status"] == "completed"
    assert len(fake.calls) == 2  # Cursor gefolgt


def test_v2_abort_posts_interrupt(monkeypatch):
    api = _ServeApi("http://srv:1", "2", None, "opencode")
    fake = _install(monkeypatch, [("POST", "/api/session/s2/interrupt", (200, {"interrupted": True}))])
    api.abort("s2", "/tmp/wd")
    assert fake.calls[0]["url"] == "http://srv:1/api/session/s2/interrupt"


def test_v2_sends_basic_auth_header(monkeypatch):
    api = _ServeApi("http://srv:1", "2", TEST_AUTH_PASS, TEST_AUTH_USER)
    fake = _install(monkeypatch, [("GET", "/api/info", (200, {"version": "2"}))])
    api.health()
    expected = "Basic " + base64.b64encode(f"{TEST_AUTH_USER}:{TEST_AUTH_PASS}".encode()).decode()
    assert fake.calls[0]["headers"].get("Authorization") == expected


# ---------------------------------------------------------------- _normalize_v2
def test_normalize_v2_skips_non_assistant():
    assert _ServeApi._normalize_v2({"type": "user"}) is None
    assert _ServeApi._normalize_v2({"type": "system"}) is None


def test_normalize_v2_maps_flat_message_to_envelope():
    m = {
        "type": "assistant",
        "time": {"created": 42},
        "finish": "stop",
        "tokens": {"input": 1, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "cost": 0.5,
        "error": {"type": "x", "message": "boom"},
        "content": [
            {"type": "text", "text": "hallo"},
            {"type": "reasoning", "text": "denken"},  # wird ignoriert
            {"type": "tool", "name": "write", "state": {"status": "error", "input": {}}},
        ],
    }
    n = _ServeApi._normalize_v2(m)
    assert n["info"] == {
        "role": "assistant",
        "tokens": m["tokens"],
        "cost": 0.5,
        "finish": "stop",
        "error": {"type": "x", "message": "boom"},
        "time": {"created": 42},
    }
    assert [p["type"] for p in n["parts"]] == ["text", "tool"]
    assert n["parts"][1]["state"]["status"] == "error"
    assert json.loads(n["parts"][1]["text"])["name"] == "write"


# ---------------------------------------------------------------- Versions-Erkennung
def test_detect_v2_server(monkeypatch):
    be = _be(monkeypatch, _v2([]))
    assert be._detect().version == "2"
    assert be.health() is True


def test_detect_v1_server(monkeypatch):
    be = _be(monkeypatch, _v1([]))
    assert be._detect().version == "1"
    assert be.health() is True


def test_detect_unreachable_is_none_and_health_false(monkeypatch):
    be = _be(
        monkeypatch,
        [("GET", "/api/info", ConnectionRefusedError("down")), ("GET", "/api/health", ConnectionRefusedError("down"))],
    )
    assert be._detect() is None
    assert be.health() is False


def test_detect_caches_result(monkeypatch):
    be = _be(monkeypatch, _v2([]))
    first = be._detect()
    assert be._detect() is first


def test_require_api_raises_with_password_hint(monkeypatch):
    be = _be(monkeypatch, [("GET", "/api/info", (401, {})), ("GET", "/api/health", (404, {}))])
    with pytest.raises(RuntimeError, match="FABRIK_AGENT_PASSWORD"):
        be.create_task("/tmp/wd", "t")


def test_v2_without_password_is_unhealthy(monkeypatch):
    # 2.x-Server mit (auto-generiertem) Passwort, fabrik kennt es nicht → 401
    be = _be(monkeypatch, [("GET", "/api/info", (401, {})), ("GET", "/api/health", (404, {}))])
    assert be.health() is False


# ---------------------------------------------------------------- diagnose
def test_diagnose_healthy_is_none(monkeypatch):
    be = _be(monkeypatch, _v2([]))
    assert be.diagnose() is None


def test_diagnose_unreachable_mentions_default_port(monkeypatch):
    # Regression (Port-Mismatch 2026-10-18): Server lief auf 4096, Config
    # zeigte auf 8945 — die Diagnose muss den Default-Port nennen.
    be = _be(
        monkeypatch,
        [("GET", "/api/info", ConnectionRefusedError("down")), ("GET", "/api/health", ConnectionRefusedError("down"))],
    )
    d = be.diagnose()
    assert d is not None
    assert "http://srv:1" in d
    assert "4096" in d


def test_diagnose_401_mentions_password(monkeypatch):
    # Echter urlopen wirft bei 401 HTTPError (der Fake tut das hier gleich).
    be = _be(
        monkeypatch,
        [
            ("GET", "/api/info", http_error(401, "http://srv:1/api/info", "Unauthorized")),
            ("GET", "/api/health", http_error(404, "http://srv:1/api/health", "Not Found")),
        ],
    )
    d = be.diagnose()
    assert d is not None
    assert "401" in d
    assert "FABRIK_AGENT_PASSWORD" in d


# ---------------------------------------------------------------- create() / Model
def test_create_reads_env_password_and_username(monkeypatch):
    monkeypatch.setenv("FABRIK_AGENT_PASSWORD", "pw-env")
    monkeypatch.setenv("FABRIK_AGENT_USERNAME", "user-env")
    be = OpenCodeBackend.create({}, "http://e", "m", 900)
    assert be.password == "pw-env"
    assert be.username == "user-env"


def test_create_step_overrides_env(monkeypatch):
    monkeypatch.setenv("FABRIK_AGENT_PASSWORD", "pw-env")
    be = OpenCodeBackend.create({"password": "pw-step"}, "http://e", "m", 900)
    assert be.password == "pw-step"


def test_create_without_password_is_none(monkeypatch):
    monkeypatch.delenv("FABRIK_AGENT_PASSWORD", raising=False)
    be = OpenCodeBackend.create({}, "http://e", "m", 900)
    assert be.password is None
    assert be.username == "opencode"


def test_variant_is_part_of_model_ref():
    be = OpenCodeBackend.create({"variant": " medium "}, "http://e", "llama/m", 900)
    assert be.model == {"providerID": "llama", "id": "m", "variant": "medium"}


@pytest.mark.parametrize("step", [{}, {"variant": ""}, {"variant": None}])
def test_no_variant_keeps_model_ref_unchanged(step):
    assert "variant" not in OpenCodeBackend.create(step, "http://e", "llama/m", 900).model


@pytest.mark.parametrize(
    "model,expected",
    [
        ("ollama/llama3.2:3b", {"providerID": "ollama", "id": "llama3.2:3b"}),
        ("anthropic/claude-x", {"providerID": "anthropic", "id": "claude-x"}),
        ("llama3.2:3b", {"providerID": "ollama", "id": "llama3.2:3b"}),  # Default-Provider
    ],
)
def test_model_parsing(model, expected):
    assert OpenCodeBackend("http://e", model).model == expected


# ---------------------------------------------------------------- create_task
def test_create_task_v1_default_permissions(monkeypatch):
    be = _be(monkeypatch, _v1([("POST", "/session", (200, {"id": "ses_1"}))]))
    assert be.create_task("/tmp/wd", "t") == "ses_1"


def test_create_task_v2_custom_permissions(monkeypatch):
    be = _be(monkeypatch, _v2([("POST", "/api/session", (200, {"data": {"id": "ses_2"}}))]))
    perms = [{"permission": "edit", "pattern": "src/*", "action": "allow"}]
    assert be.create_task("/tmp/wd", "t", perms) == "ses_2"


# ---------------------------------------------------------------- poll (gemeinsame Logik)
def _v1_msg(role="assistant", finish=None, parts=None, tokens=None, cost: float = 0, error=None, created=None):
    info = {"role": role, "tokens": tokens or {}, "cost": cost, "finish": finish, "error": error}
    if created is not None:
        info["time"] = {"created": created}
    return {"info": info, "parts": parts or []}


def _poll_v1(monkeypatch, msgs, **kw):
    be = _be(monkeypatch, _v1([("GET", "/session/s1/message", (200, msgs))]), **kw)
    return be.poll("s1", "/tmp/wd")


def test_poll_no_messages_is_running(monkeypatch):
    r = _poll_v1(monkeypatch, [])
    assert r["status"] == "running"
    assert r["text"] == ""
    assert r["tokens"] == {"input": 0, "output": 0}
    assert r["tools"] == []


def test_poll_running_while_finish_none(monkeypatch):
    r = _poll_v1(monkeypatch, [_v1_msg(finish=None, parts=[{"type": "text", "text": "denke…"}], created=1e12)])
    assert r["status"] == "running"
    assert r["text"] == "denke…"


def test_poll_stale_when_no_parts_and_old(monkeypatch):
    old = 1_000_000_000  # weit in der Vergangenheit
    r = _poll_v1(monkeypatch, [_v1_msg(finish=None, parts=[], created=old)], stale_after=1)
    assert r["status"] == "failed"
    assert "inaktiv" in r["text"]
    # Hängendes Modell = transient → Agent-Step darf die Session neu starten (ADR 0035).
    assert r["retryable"] is True


def test_poll_running_on_tool_calls_finish(monkeypatch):
    r = _poll_v1(monkeypatch, [_v1_msg(finish="tool-calls", parts=[], created=1e12)])
    assert r["status"] == "running"


def test_poll_done_with_final_text(monkeypatch):
    r = _poll_v1(monkeypatch, [_v1_msg(finish="stop", parts=[{"type": "text", "text": "alles klar"}])])
    assert r["status"] == "done"
    assert r["text"] == "alles klar"


def test_poll_done_with_completed_tool(monkeypatch):
    parts = [{"type": "tool", "text": json.dumps({"name": "write"}), "state": {"status": "completed"}}]
    r = _poll_v1(monkeypatch, [_v1_msg(finish="stop", parts=parts)])
    assert r["status"] == "done"
    assert r["tools"] == ["write"]


def test_poll_failed_on_tool_error_with_text(monkeypatch):
    parts = [{"type": "tool", "text": json.dumps({"name": "bash"}), "state": {"status": "error"}}]
    r = _poll_v1(monkeypatch, [_v1_msg(finish="stop", parts=parts + [{"type": "text", "text": "hat geklappt"}])])
    assert r["status"] == "failed"
    assert "unzuverlaessig" in r["text"]


def test_poll_failed_on_empty_finished_message(monkeypatch):
    r = _poll_v1(monkeypatch, [_v1_msg(finish="stop", parts=[])])
    assert r["status"] == "failed"
    assert "Substanz" in r["text"]


def test_poll_failed_on_message_error(monkeypatch):
    r = _poll_v1(monkeypatch, [_v1_msg(finish="stop", parts=[], error={"message": "LLM 500"})])
    assert r["status"] == "failed"
    assert "LLM 500" in r["text"]


def test_poll_tool_call_json_as_text_is_no_substance(monkeypatch):
    tc = json.dumps({"name": "write", "parameters": {"path": "x"}})
    r = _poll_v1(monkeypatch, [_v1_msg(finish="stop", parts=[{"type": "text", "text": tc}])])
    assert r["status"] == "failed"


def test_poll_aggregates_tokens_cost_tools_across_messages(monkeypatch):
    msgs = [
        _v1_msg(
            finish="tool-calls",
            parts=[{"type": "tool", "text": json.dumps({"name": "read"}), "state": {"status": "completed"}}],
            tokens={"input": 10, "output": 2},
            cost=0.01,
        ),
        _v1_msg(finish="stop", parts=[{"type": "text", "text": "done"}], tokens={"input": 20, "output": 4}, cost=0.02),
    ]
    r = _poll_v1(monkeypatch, msgs)
    assert r["status"] == "done"
    assert r["tokens"] == {"input": 30, "output": 6}
    assert r["cost"] == pytest.approx(0.03)
    assert r["tools"] == ["read"]


def test_poll_v2_normalized_messages_done(monkeypatch):
    # 2.x-Flach-Message (via Cursor-Page) → done über normalisierte Envelope
    be = _be(
        monkeypatch,
        _v2(
            [
                (
                    "GET",
                    "/api/session/s2/message",
                    (
                        200,
                        {
                            "data": [
                                {
                                    "type": "assistant",
                                    "time": {"created": 1},
                                    "finish": "stop",
                                    "tokens": {
                                        "input": 3,
                                        "output": 1,
                                        "reasoning": 0,
                                        "cache": {"read": 0, "write": 0},
                                    },
                                    "cost": 0,
                                    "content": [{"type": "text", "text": "v2 fertig"}],
                                }
                            ],
                            "cursor": {"previous": None, "next": None},
                        },
                    ),
                )
            ]
        ),
    )
    r = be.poll("s2", "/tmp/wd")
    assert r["status"] == "done"
    assert r["text"] == "v2 fertig"
    assert r["tokens"] == {"input": 3, "output": 1}


def test_poll_v2_newest_first_api_order_is_done_not_running(monkeypatch):
    # Regression (Issue #50): 2.x liefert die Messages neueste-zuerst. Ohne
    # Umkehr in fetch_messages() würde poll() die ÄLTESTE Assistant-Message
    # (finish=tool-calls) als "letzte" lesen → ewig "running" → Step-Timeout,
    # obwohl die Session längst mit finish=stop + Final-Text fertig war.
    be = _be(
        monkeypatch,
        _v2(
            [
                (
                    "GET",
                    "/api/session/s2/message",
                    (
                        200,
                        {
                            # Seite ist neueste-zuerst sortiert:
                            "data": [
                                {
                                    "type": "assistant",
                                    "time": {"created": 200},
                                    "finish": "stop",
                                    "tokens": {
                                        "input": 3,
                                        "output": 1,
                                        "reasoning": 0,
                                        "cache": {"read": 0, "write": 0},
                                    },
                                    "cost": 0,
                                    "content": [{"type": "text", "text": "nichts zu tun — keine neuen Kommentare"}],
                                },
                                {
                                    "type": "assistant",
                                    "time": {"created": 100},
                                    "finish": "tool-calls",
                                    "tokens": {
                                        "input": 2,
                                        "output": 1,
                                        "reasoning": 0,
                                        "cache": {"read": 0, "write": 0},
                                    },
                                    "cost": 0,
                                    "content": [
                                        {"type": "tool", "name": "shell", "state": {"status": "completed", "input": {}}}
                                    ],
                                },
                            ],
                            "cursor": {"previous": None, "next": None},
                        },
                    ),
                )
            ]
        ),
    )
    r = be.poll("s2", "/tmp/wd")
    assert r["status"] == "done"
    assert r["text"] == "nichts zu tun — keine neuen Kommentare"
    assert r["tools"] == ["shell"]


# ---------------------------------------------------------------- cancel
def test_cancel_v2_posts_interrupt(monkeypatch):
    be = _be(monkeypatch, _v2([("POST", "/api/session/s2/interrupt", (200, {}))]))
    be.cancel("s2", "/tmp/wd")  # keine Exception


def test_cancel_suppresses_errors(monkeypatch):
    be = _be(monkeypatch, _v2([("POST", "/api/session/s2/interrupt", ConnectionRefusedError("down"))]))
    be.cancel("s2", "/tmp/wd")  # keine Exception


# ---------------------------------------------------------------- Fehlerklasse (ADR 0035)
def test_unhealthy_kind_unreachable_is_transient(monkeypatch):
    err = urllib.error.URLError("refused")
    be = _be(monkeypatch, [("GET", "/api/info", err), ("GET", "/api/health", err)])
    assert be.health() is False
    assert be.unhealthy_kind().value == "transient"


def test_unhealthy_kind_auth_is_environment(monkeypatch):
    err = http_error(401, "http://srv:1/api/info", "Unauthorized")
    be = _be(monkeypatch, [("GET", "/api/info", err), ("GET", "/api/health", err)])
    assert be.health() is False
    assert be.unhealthy_kind().value == "environment"
