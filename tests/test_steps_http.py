"""Step: http — _step_http (urllib, method/headers/body/expect/emit)."""

from __future__ import annotations

import pytest
from helpers import http_error

from fabrik.steps import http


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode()
        self.headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _install(monkeypatch, resp=None, exc=None):
    def _fake(req, timeout=None):
        if exc is not None:
            raise exc
        return resp

    monkeypatch.setattr(http.urllib.request, "urlopen", _fake)


def _ctx():
    return {"project": "p", "loop": "l", "run_id": "r1"}


def test_http_get_ok(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(200, "hello"))
    res = http._step_http(_ctx(), {"url": "http://x"}, lambda m: None)
    assert res["ok"] is True
    assert "200" in res["summary"]


def test_http_emit_vars(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(200, "body-content"))
    ctx = _ctx()
    res = http._step_http(ctx, {"url": "http://x", "emit": {"b": "body", "s": "status"}}, lambda m: None)
    assert res["ok"] is True
    assert ctx["b"] == "body-content"
    assert ctx["s"] == "200"


def test_http_emit_unknown_type(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(200, "x"))
    with pytest.raises(ValueError, match="unbekannter Typ"):
        http._step_http(_ctx(), {"url": "http://x", "emit": {"v": "bogus"}}, lambda m: None)


def test_http_status_mismatch(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(500, "err"))
    res = http._step_http(_ctx(), {"url": "http://x", "expect": {"status": 200}}, lambda m: None)
    assert res["ok"] is False
    assert "500" in res["summary"]


def test_http_httperror(monkeypatch):
    err = http_error(404, "http://x", "not found", body=b"nope")
    _install(monkeypatch, exc=err)
    res = http._step_http(_ctx(), {"url": "http://x", "expect": {"status": 404}}, lambda m: None)
    assert res["ok"] is True
    assert "404" in res["summary"]


def test_http_post_with_body(monkeypatch):
    seen = {}

    def _fake(req, timeout=None):
        seen["data"] = req.data
        seen["method"] = req.get_method()
        return _FakeResp(200, "ok")

    monkeypatch.setattr(http.urllib.request, "urlopen", _fake)
    res = http._step_http(_ctx(), {"url": "http://x", "method": "post", "body": "payload"}, lambda m: None)
    assert res["ok"] is True
    assert seen["method"] == "POST"
    assert seen["data"] == b"payload"


def test_http_substitution(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(200, "ok"))
    res = http._step_http(_ctx(), {"url": "http://x/{project}", "headers": {"H": "{loop}"}}, lambda m: None)
    assert res["ok"] is True


@pytest.mark.parametrize(("status", "kind"), [(503, "transient"), (401, "environment"), (404, "unknown")])
def test_http_status_mismatch_kind(monkeypatch, status, kind):
    """Fehlerklasse aus dem HTTP-Status (ADR 0035)."""
    _install(monkeypatch, exc=http_error(status, "http://x", "e"))
    res = http._step_http(_ctx(), {"url": "http://x"}, lambda m: None)
    assert res["failure_kind"] == kind
