"""on_webhook: HMAC, Payload→Variablen, CORS, Tunnel-Host-Sperre (ADR 0027)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from test_server_api import fabrik_home, fabrik_url  # noqa: F401  (Fixtures)

from fabrik import server, specs, webhook

SECRET = "s3cret-value"
ORIGIN = "https://hooks.example.com"


@pytest.fixture
def hook(fabrik_home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HOOK_SECRET", SECRET)
    monkeypatch.setenv(webhook.ORIGIN_ENV, ORIGIN)
    (fabrik_home / "projects" / "uitest.fabrik.yaml").write_text(
        f"name: uitest\nroot: {fabrik_home}\nloops:\n  t:\n    triggers:\n"
        "      - {type: on_webhook, secret_env: HOOK_SECRET, map: {title: issue.title, n: issue.number}}\n"
        '    steps:\n      - {type: shell, name: a, cmd: "true"}\n'
    )
    started = []

    def fake_start(self, name, lname, note, initial_vars, trigger, actor=None):
        started.append((name, lname, initial_vars, trigger))
        return {"started": True, "run_id": "rid"}, 200

    monkeypatch.setattr(server.FabrikHandler, "_start_run", fake_start)
    return started


def _post(url, body: bytes, headers=None):
    req = urllib.request.Request(url, data=body, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def _signed(body: bytes, **extra):
    return {webhook.SIGNATURE_HEADER: webhook.sign(SECRET, body), **extra}


BODY = json.dumps({"issue": {"title": "Hi", "number": 7}}).encode()


def test_valid_signature_starts_run_with_mapped_vars(fabrik_url, hook):  # noqa: F811
    status, data, _ = _post(f"{fabrik_url}/api/webhook/uitest/t", BODY, _signed(BODY))
    assert status == 200 and data["run_id"] == "rid"
    assert hook == [("uitest", "t", {"title": "Hi", "n": "7"}, "on_webhook")]


@pytest.mark.parametrize("headers", [{}, {webhook.SIGNATURE_HEADER: "sha256=deadbeef"}, {webhook.SIGNATURE_HEADER: ""}])
def test_bad_or_missing_signature_401(fabrik_url, hook, headers):  # noqa: F811
    status, _, _ = _post(f"{fabrik_url}/api/webhook/uitest/t", BODY, headers)
    assert status == 401 and hook == []


def test_signature_for_other_body_401(fabrik_url, hook):  # noqa: F811
    status, _, _ = _post(f"{fabrik_url}/api/webhook/uitest/t", BODY + b" ", _signed(BODY))
    assert status == 401 and hook == []


@pytest.mark.parametrize("path", ["uitest/nope", "nope/t", "uitest", "uitest/t/x"])
def test_unknown_target_is_401(fabrik_url, hook, path):  # noqa: F811
    status, _, _ = _post(f"{fabrik_url}/api/webhook/{path}", BODY, _signed(BODY))
    assert status == 401 and hook == []


def test_unset_secret_env_401(fabrik_url, hook, monkeypatch):  # noqa: F811
    monkeypatch.delenv("HOOK_SECRET")
    status, _, _ = _post(f"{fabrik_url}/api/webhook/uitest/t", BODY, _signed(BODY))
    assert status == 401 and hook == []


def test_invalid_json_400_and_missing_field_422(fabrik_url, hook):  # noqa: F811
    bad = b"{nope"
    assert _post(f"{fabrik_url}/api/webhook/uitest/t", bad, _signed(bad))[0] == 400
    arr = b"[1]"
    assert _post(f"{fabrik_url}/api/webhook/uitest/t", arr, _signed(arr))[0] == 400
    miss = b'{"issue": {"title": "x"}}'
    assert _post(f"{fabrik_url}/api/webhook/uitest/t", miss, _signed(miss))[0] == 422
    assert hook == []


def test_cors_only_for_tunnel_origin(fabrik_url, hook):  # noqa: F811
    url = f"{fabrik_url}/api/webhook/uitest/t"
    _, _, h = _post(url, BODY, _signed(BODY, Origin=ORIGIN))
    assert h["Access-Control-Allow-Origin"] == ORIGIN
    _, _, h = _post(url, BODY, _signed(BODY, Origin="https://evil.example"))
    assert "Access-Control-Allow-Origin" not in h
    status, _, h = _post(url, BODY, {"Origin": "https://evil.example"})
    assert status == 401 and "Access-Control-Allow-Origin" not in h


def test_preflight(fabrik_url, hook):  # noqa: F811
    def opt(origin):
        req = urllib.request.Request(f"{fabrik_url}/api/webhook/uitest/t", method="OPTIONS", headers={"Origin": origin})
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers)

    status, h = opt(ORIGIN)
    assert status == 204 and h["Access-Control-Allow-Origin"] == ORIGIN
    assert webhook.SIGNATURE_HEADER in h["Access-Control-Allow-Headers"]
    assert "Access-Control-Allow-Origin" not in opt("https://evil.example")[1]


def test_tunnel_host_only_reaches_webhook(fabrik_url, hook):  # noqa: F811
    host = {"Host": "HOOKS.EXAMPLE.COM:443"}
    req = urllib.request.Request(f"{fabrik_url}/api/projects", headers=host)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 404
    assert _post(f"{fabrik_url}/api/runs", b"{}", host)[0] == 404
    assert _post(f"{fabrik_url}/api/webhook/uitest/t", BODY, {**_signed(BODY), **host})[0] == 200


def test_tunnel_host_blocks_options_for_non_webhook(fabrik_url, hook):  # noqa: F811
    req = urllib.request.Request(
        f"{fabrik_url}/api/projects",
        method="OPTIONS",
        headers={"Host": "HOOKS.EXAMPLE.COM:443"},
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 404


def test_no_cors_star_anywhere(fabrik_url, hook):  # noqa: F811
    req = urllib.request.Request(f"{fabrik_url}/api/health", headers={"Origin": ORIGIN})
    with urllib.request.urlopen(req) as r:
        assert "Access-Control-Allow-Origin" not in r.headers


# ---------------------------------------------------------------- Units
def test_verify_signature_unit():
    sig = webhook.sign("k", b"x")
    assert webhook.verify_signature("k", b"x", sig)
    assert not webhook.verify_signature("", b"x", sig)
    assert not webhook.verify_signature("k", b"x", "")
    assert not webhook.verify_signature("k", b"y", sig)


def test_map_payload_nested_list_and_non_string():
    body = b'{"a": {"b": [10, {"c": true}]}, "s": "x"}'
    out = webhook.map_payload(body, {"v1": "a.b.0", "v2": "a.b.1.c", "v3": "a.b", "v4": "s"})
    assert out == {"v1": "10", "v2": "true", "v3": '[10, {"c": true}]', "v4": "x"}
    assert webhook.map_payload(b"", {}) == {}


def test_find_trigger():
    spec = {"loops": {"l": {"triggers": [{"type": "manual"}, {"type": "on_webhook", "secret_env": "X"}]}}}
    assert webhook.find_trigger(spec, "l")["secret_env"] == "X"
    assert webhook.find_trigger(spec, "other") is None
    assert webhook.find_trigger(None, "l") is None


def test_spec_validation(tmp_path):
    base = {"root": str(tmp_path), "loops": {"l": {"steps": [{"type": "shell", "cmd": "true"}]}}}

    def check(trig):
        specs._validate_spec({**base, "loops": {"l": {**base["loops"]["l"], "triggers": [trig]}}})

    check({"type": "on_webhook", "secret_env": "X", "map": {"a": "b.c"}})
    with pytest.raises(ValueError, match="secret_env"):
        check({"type": "on_webhook"})
    with pytest.raises(ValueError, match="map"):
        check({"type": "on_webhook", "secret_env": "X", "map": ["a"]})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0), ("0", 0), ("42", 42), (str(webhook.MAX_BODY_BYTES), webhook.MAX_BODY_BYTES)],
)
def test_parse_content_length_ok(raw, expected):
    assert webhook.parse_content_length(raw) == expected


@pytest.mark.parametrize(
    ("raw", "status"), [("-1", 400), ("abc", 400), ("", 400), (str(webhook.MAX_BODY_BYTES + 1), 413)]
)
def test_parse_content_length_rejects(raw, status):
    with pytest.raises(webhook.WebhookError) as e:
        webhook.parse_content_length(raw)
    assert e.value.status == status


@pytest.mark.parametrize(("length", "status"), [("-5", 400), ("abc", 400), (str(webhook.MAX_BODY_BYTES + 1), 413)])
def test_webhook_bad_content_length(fabrik_url, hook, length, status):  # noqa: F811
    res = _post(
        f"{fabrik_url}/api/webhook/uitest/t", b"", {webhook.SIGNATURE_HEADER: "sha256=x", "Content-Length": length}
    )
    assert res[0] == status and hook == []


@pytest.mark.parametrize(
    "host", ["hooks.example.com.", "hooks.example.com.:443", "HOOKS.EXAMPLE.COM.", "hooks.example.com"]
)
def test_tunnel_matches_trailing_dot(hook, host):  # noqa: F811
    assert webhook.tunnel_matches(host)


@pytest.mark.parametrize("host", ["evil.example.com.", "hooks.example.com.:8443", "[::1]:443"])
def test_tunnel_matches_rejects_other_hosts(hook, host):  # noqa: F811
    assert not webhook.tunnel_matches(host)
