"""Alert-Intake + Incident-Roundtrip (ADR 0045): Parser, Auth, Routing, E2E ohne externe Dienste."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import pytest
from test_server_api import fabrikant_home, fabrikant_url  # noqa: F401  (Fixtures)

from fabrikant import alerts, config, gitops, runs, server, specs, webhook
from fabrikant import llm as llm_registry
from fabrikant.engine import run_loop

SECRET = "alert-secret-value"


# ------------------------------------------------------------------ Parser
SENTRY = {"data": {"event": {"title": "KeyError: x", "message": "boom", "level": "error", "project": "api"}}}
CW_ALARM = {
    "AlarmName": "high-5xx",
    "NewStateValue": "ALARM",
    "NewStateReason": "Threshold crossed",
    "Trigger": {"Namespace": "AWS/ApplicationELB"},
}


def test_parse_sentry_modern_and_legacy():
    a = alerts.parse_alert("sentry", json.dumps(SENTRY).encode())
    assert (a.source, a.title, a.service, a.level) == ("sentry", "KeyError: x", "api", "error")
    legacy = alerts.parse_alert("sentry", b'{"message": "m", "project": "p", "culprit": "c", "level": "fatal"}')
    assert (legacy.title, legacy.message, legacy.service) == ("m", "c", "p")


@pytest.mark.parametrize(
    ("payload", "service"),
    [
        # Issue-Alert: data.event.project ist die numerische ID, data.issue.project trägt den Slug
        ({"data": {"event": {"title": "t", "project": 42}, "issue": {"project": {"slug": "api", "id": "42"}}}}, "api"),
        ({"data": {"event": {"title": "t", "project": 42}}}, "42"),
        ({"message": "t", "project_slug": "web", "project": "Web App"}, "web"),
        ({"message": "t", "project_name": "Legacy"}, "Legacy"),
        ({"message": "t", "project": True}, ""),
    ],
)
def test_parse_sentry_service(payload, service):
    assert alerts.parse_alert("sentry", json.dumps(payload).encode()).service == service


def test_parse_sentry_without_title_is_422():
    with pytest.raises(webhook.WebhookError) as e:
        alerts.parse_alert("sentry", b'{"data": {}}')
    assert e.value.status == 422


def test_parse_cloudwatch_direct_and_sns_envelope():
    direct = alerts.parse_alert("cloudwatch", json.dumps(CW_ALARM).encode())
    assert direct and (direct.title, direct.service, direct.level) == ("high-5xx", "AWS/ApplicationELB", "ALARM")
    sns = alerts.parse_alert(
        "cloudwatch", json.dumps({"Type": "Notification", "Message": json.dumps(CW_ALARM)}).encode()
    )
    assert sns == direct


def test_parse_cloudwatch_ok_state_is_ignored():
    assert alerts.parse_alert("cloudwatch", json.dumps({**CW_ALARM, "NewStateValue": "OK"}).encode()) is None


@pytest.mark.parametrize(
    ("body", "status"),
    [(b"nope", 400), (b"[1]", 400), (b"{}", 422), (b'{"Message": "x"}', 422), (b'{"Message": "[1]"}', 422)],
)
def test_parse_cloudwatch_errors(body, status):
    with pytest.raises(webhook.WebhookError) as e:
        alerts.parse_alert("cloudwatch", body)
    assert e.value.status == status


def test_alert_vars_and_fingerprint_are_stable():
    a = alerts.Alert(source="sentry", title="t", message="m" * 5000)
    assert a.fingerprint == alerts.Alert(source="sentry", title="t").fingerprint
    assert a.fingerprint != alerts.Alert(source="sentry", title="t2").fingerprint
    v = a.to_vars()
    assert len(v["alert_message"]) == 2000
    assert set(v) >= {"alert_source", "alert_title", "alert_fingerprint"}


# ---------------------------------------------------------------- Routing
def test_route_is_deterministic_with_defined_fallback():
    routes = {"bug": "hotfix"}
    assert alerts.route("bug", routes) == "hotfix"
    assert alerts.route("unknown", routes) == alerts.ROUTE_NONE
    assert alerts.route("unknown", routes, "manual-triage") == "manual-triage"
    assert alerts.route("bug", None) == alerts.ROUTE_NONE


# -------------------------------------------------------------------- Auth
TRIG = {"type": "on_alert", "source": "sentry", "secret_env": "ALERT_SECRET"}


def test_authenticate_accepts_fabrikant_and_sentry_signature(monkeypatch):
    monkeypatch.setenv("ALERT_SECRET", SECRET)
    body = b"{}"
    assert alerts.authenticate(TRIG, body, webhook.sign(SECRET, body)) is TRIG
    raw = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert alerts.authenticate(TRIG, body, "", raw) is TRIG
    # falsche Fabrikant-Signatur, aber gültige Sentry-Signatur → akzeptiert
    assert alerts.authenticate(TRIG, body, "sha256=bad", raw) is TRIG


@pytest.mark.parametrize("trigger", [None, {**TRIG, "secret_env": "UNSET_ENV"}, {**TRIG, "source": "cloudwatch"}])
def test_authenticate_rejects(monkeypatch, trigger):
    monkeypatch.setenv("ALERT_SECRET", SECRET)
    raw = hmac.new(SECRET.encode(), b"{}", hashlib.sha256).hexdigest()
    with pytest.raises(webhook.WebhookError) as e:
        alerts.authenticate(trigger, b"{}", "", raw)
    assert e.value.status == 401


def test_authenticate_rejects_wrong_signature(monkeypatch):
    monkeypatch.setenv("ALERT_SECRET", SECRET)
    with pytest.raises(webhook.WebhookError):
        alerts.authenticate(TRIG, b"{}", webhook.sign("other", b"{}"))
    with pytest.raises(webhook.WebhookError):
        alerts.authenticate(TRIG, b"{}", "", "deadbeef")


@pytest.mark.parametrize(
    ("step", "n_errs"),
    [
        ({"routes": {"bug": "hotfix"}}, 0),
        ({"routes": {"bug": "hotfix", "noise": "none"}, "fallback": "manual"}, 0),
        ({"routes": {"bug": "missing"}}, 1),
        ({"routes": {"bug": "triage"}}, 1),
        ({"routes": {"bug": "hotfix"}, "fallback": "nope"}, 1),
        ({"routes": ["bug"]}, 1),
        ({}, 1),
        ({"routes": {"bug": 1}}, 1),
        ({"routes": {"bug": "hotfix"}, "fallback": 5}, 1),
    ],
)
def test_validate_route_step(step, n_errs):
    # Der Step liegt im Loop `triage` der Spec (so wird er auch von `_validate_spec` geprüft).
    full = {"type": "alert_route", **step}
    spec = {"loops": {"triage": {"steps": [full]}, "hotfix": {}, "manual": {}}}
    assert len(alerts.validate_route_step(step, spec, "w")) == n_errs


def test_validate_route_step_rejects_two_loop_cycle():
    spec = {
        "loops": {
            "triage": {"steps": [{"type": "alert_route", "routes": {"bug": "hotfix"}}]},
            "hotfix": {"steps": [{"type": "alert_route", "routes": {"bug": "triage"}}]},
        }
    }
    errs = alerts.validate_route_step({"routes": {"bug": "hotfix"}}, spec, "w")
    assert any("Route-Zyklus" in e and "triage" in e and "hotfix" in e for e in errs)


def test_validate_route_step_rejects_cycle_via_fallback():
    spec = {
        "loops": {
            "triage": {"steps": [{"type": "alert_route", "routes": {"bug": "hotfix"}}]},
            "hotfix": {"steps": [{"type": "alert_route", "routes": {}, "fallback": "triage"}]},
        }
    }
    errs = alerts.validate_route_step({"routes": {"bug": "hotfix"}}, spec, "w")
    assert any("Route-Zyklus" in e for e in errs)


def test_validate_trigger():
    assert alerts.validate_trigger(TRIG, "w") == []
    assert len(alerts.validate_trigger({"type": "on_alert"}, "w")) == 2


# ----------------------------------------------------------- alert_route Step
def test_alert_route_none_for_unmapped_category():
    from fabrikant.steps.alert_route import _step_alert_route

    ctx = {"category": "noise", "loop": "triage"}
    res = _step_alert_route(ctx, {"routes": {"bug": "hotfix"}}, lambda m: None)
    assert res["ok"] and ctx["route"] == "none"


def test_alert_route_rejects_self_route():
    from fabrikant.steps.alert_route import _step_alert_route

    res = _step_alert_route({"category": "bug", "loop": "triage"}, {"routes": {"bug": "triage"}}, lambda m: None)
    assert not res["ok"]


# ---------------------------------------------------------------------- E2E
SPEC = """\
name: incident
root: {repo}
repo: {{branch: main}}
loops:
  triage:
    tier: 1
    worktree: false
    triggers:
      - {{type: on_alert, source: sentry, secret_env: ALERT_SECRET}}
    steps:
      - name: triage
        type: llm
        prompt: "Klassifiziere den Alert {{alert_title}}"
        untrusted: [alert_title, alert_message]
        schema:
          category: {{type: string, enum: [bug, infra, noise, unknown], default: unknown}}
      - name: route
        type: alert_route
        routes: {{bug: hotfix}}
  hotfix:
    tier: 1
    steps:
      - {{name: fix, type: shell, cmd: "echo fixed > fix.txt"}}
      - name: verify
        type: verify
        checks:
          - {{name: fix-present, type: shell, cmd: "grep -q fixed fix.txt"}}
      - {{name: pr, type: pr, repo: o/r, head: "hotfix/{{alert_fingerprint}}", title: "hotfix: {{alert_title}}"}}
"""


@pytest.fixture
def incident(fabrikant_home, tmp_path, monkeypatch):  # noqa: F811
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c1"], check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "HEAD:refs/heads/main"], check=True)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    monkeypatch.setenv("ALERT_SECRET", SECRET)
    (fabrikant_home / "projects" / "incident.fabrikant.yaml").write_text(SPEC.format(repo=repo))
    prs: list[dict] = []

    def fake_gh(method, url, token, payload=None):
        if method == "POST" and url.endswith("/pulls"):
            assert payload is not None
            prs.append(payload)
            return {"number": 7, "html_url": "https://github.com/o/r/pull/7"}
        return []

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", fake_gh)
    monkeypatch.setattr(llm_registry, "active_provider", lambda: {"name": "fake", "model": "m"})
    answer = {"text": json.dumps({"category": "bug"})}
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    monkeypatch.setattr(
        llm_registry, "complete", lambda *a, **k: {"text": answer["text"], "usage": usage, "finish_reason": "stop"}
    )

    def run_sync(self, name, lname, note, initial_vars, trigger, actor=None):
        rec = run_loop(name, lname, trigger=trigger, note=note, initial_vars=initial_vars, actor=actor)
        return {"started": True, "run_id": rec["id"]}, 200

    monkeypatch.setattr(server.FabrikantHandler, "_start_run", run_sync)
    return {"prs": prs, "answer": answer}


def _post(url, body: dict, headers=None):
    raw = json.dumps(body).encode()
    req = urllib.request.Request(url, data=raw, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _signed(body: dict):
    return {webhook.SIGNATURE_HEADER: webhook.sign(SECRET, json.dumps(body).encode())}


def _runs(loop):
    return [r for r in runs.list_runs() if r.get("project") == "incident" and r.get("loop") == loop]


def test_spec_with_on_alert_is_valid(incident):
    assert "_error" not in specs.load_projects()["incident"]


def test_spec_with_unknown_route_target_is_invalid(incident):
    path = config.PROJECTS_DIR / "incident.fabrikant.yaml"
    path.write_text(path.read_text().replace("routes: {bug: hotfix}", "routes: {bug: hotfx}"))
    err = specs.load_projects()["incident"].get("_error") or ""
    assert "unbekannter Loop 'hotfx'" in err


def test_intake_accepts_sentry_signature_header_case_insensitive(incident, fabrikant_url):  # noqa: F811
    """HTTP-Header sind case-insensitiv; Proxies/HTTP/2 (Tunnel) liefern sie kleingeschrieben."""
    body = json.dumps(SENTRY).encode()
    raw = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    url = urllib.parse.urlsplit(fabrikant_url)
    conn = http.client.HTTPConnection(url.hostname, url.port)
    try:  # http.client sendet Header-Namen unverändert
        conn.request("POST", "/api/alert/incident/triage", body, {"sentry-hook-signature": raw})
        resp = conn.getresponse()
        assert resp.status == 200 and json.loads(resp.read())["started"]
    finally:
        conn.close()
    assert _runs("triage")


def test_e2e_alert_to_triage_to_hotfix_pr(incident, fabrikant_url):  # noqa: F811
    status, resp = _post(f"{fabrikant_url}/api/alert/incident/triage", SENTRY, _signed(SENTRY))
    assert status == 200 and resp["started"]
    (triage,) = _runs("triage")
    (hotfix,) = _runs("hotfix")
    assert triage["status"] == "pass" and hotfix["status"] == "pass"
    assert triage["vars"]["route"] == "hotfix" and triage["vars"]["hotfix_run_id"] == hotfix["id"]
    assert hotfix["trigger"] == f"alert:{triage['id']}"
    assert hotfix["vars"]["alert_title"] == "KeyError: x"
    assert [s["name"] for s in hotfix["steps"]] == ["fix", "verify", "pr"]
    assert len(incident["prs"]) == 1 and incident["prs"][0]["title"] == "hotfix: KeyError: x"


def test_e2e_unknown_alert_gets_no_hotfix(incident, fabrikant_url):  # noqa: F811
    incident["answer"]["text"] = json.dumps({"category": "noise"})
    status, _ = _post(f"{fabrikant_url}/api/alert/incident/triage", SENTRY, _signed(SENTRY))
    assert status == 200
    (triage,) = _runs("triage")
    assert triage["status"] == "pass" and triage["vars"]["route"] == "none"
    assert not _runs("hotfix") and not incident["prs"]


def test_e2e_failed_verify_blocks_pr(incident, fabrikant_url, monkeypatch):  # noqa: F811
    spec = (config.PROJECTS_DIR / "incident.fabrikant.yaml").read_text().replace("grep -q fixed fix.txt", "false")
    (config.PROJECTS_DIR / "incident.fabrikant.yaml").write_text(spec)
    _post(f"{fabrikant_url}/api/alert/incident/triage", SENTRY, _signed(SENTRY))
    assert _runs("hotfix")[0]["status"] == "fail"
    assert _runs("triage")[0]["status"] == "fail"
    assert not incident["prs"]


def test_intake_rejects_unsigned_and_unknown(incident, fabrikant_url):  # noqa: F811
    assert _post(f"{fabrikant_url}/api/alert/incident/triage", SENTRY)[0] == 401
    assert _post(f"{fabrikant_url}/api/alert/incident/nope", SENTRY, _signed(SENTRY))[0] == 401
    assert _post(f"{fabrikant_url}/api/alert/incident", SENTRY, _signed(SENTRY))[0] == 401
    assert not _runs("triage")


def test_intake_422_on_bad_payload(incident, fabrikant_url):  # noqa: F811
    assert _post(f"{fabrikant_url}/api/alert/incident/triage", {"x": 1}, _signed({"x": 1}))[0] == 422


def test_intake_cloudwatch_ok_state_ignored(incident, fabrikant_url):  # noqa: F811
    spec = (config.PROJECTS_DIR / "incident.fabrikant.yaml").read_text().replace("source: sentry", "source: cloudwatch")
    (config.PROJECTS_DIR / "incident.fabrikant.yaml").write_text(spec)
    body = {**CW_ALARM, "NewStateValue": "OK"}
    status, resp = _post(f"{fabrikant_url}/api/alert/incident/triage", body, _signed(body))
    assert status == 200 and resp["ignored"] and not _runs("triage")
