"""Server-API: do_GET / do_POST Endpunkte + Origin/Token-Auth (Live-Server)."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from fabrik import auth, config, connections, engine, secrets
from fabrik.server import FabrikHandler

RUN_ID = "20260101T000000Z-t"
APPROVAL_RUN_ID = "20260102T000000Z-t-appr"


@pytest.fixture
def fabrik_home(tmp_path, monkeypatch):
    home = tmp_path / "fabrik_home"
    (home / "projects").mkdir(parents=True)
    (home / "runs" / "uitest").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    (home / "web").mkdir(parents=True)
    (home / "web" / "index.html").write_text("<html>fabrik</html>")
    (home / "projects" / "uitest.fabrik.yaml").write_text(
        f"name: uitest\nroot: {home}\ndescription: d\nrepo:\n  branch: main\nloops:\n"
        "  t:\n    description: loop-t\n    triggers:\n      - {type: manual}\n"
        '    steps:\n      - {type: shell, name: a, cmd: "true"}\n'
    )
    (home / "runs" / "uitest" / f"{RUN_ID}.json").write_text(
        json.dumps(
            {"id": RUN_ID, "project": "uitest", "loop": "t", "status": "pass", "started": "2024-01-01T00:00:00Z"}
        )
    )
    (home / "runs" / "uitest" / f"{RUN_ID}.log").write_text("logline\n", encoding="utf-8")
    (home / "runs" / "uitest" / f"{APPROVAL_RUN_ID}.json").write_text(
        json.dumps(
            {
                "id": APPROVAL_RUN_ID,
                "project": "uitest",
                "loop": "t",
                "status": "needs_approval",
                "started": "2024-01-02T00:00:00Z",
                "approval": {"reason": "approval", "what": "Shell-Befehl: true", "tier": 2},
            }
        )
    )
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    monkeypatch.setattr(config, "WEB_DIR", home / "web")
    return home


@pytest.fixture
def fabrik_url(fabrik_home, clean_token, live_server):
    return live_server()


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def _get(url):
    return _req(url, "GET")


# ---------------------------------------------------------------- GET
def test_get_projects(fabrik_url):
    status, body, _ = _get(f"{fabrik_url}/api/projects")
    assert status == 200
    data = json.loads(body)
    assert data[0]["name"] == "uitest"
    assert data[0]["loops"][0]["name"] == "t"


def test_get_projects_includes_pending_approvals(fabrik_url):
    """Der Projekt-Payload enthält offene Freigaben (kompakt, neueste zuerst)."""
    status, body, _ = _get(f"{fabrik_url}/api/projects")
    assert status == 200
    data = json.loads(body)
    appr = data[0]["pending_approvals"]
    assert len(appr) == 1
    assert appr[0]["id"] == APPROVAL_RUN_ID
    assert appr[0]["loop"] == "t"
    assert appr[0]["approval"]["what"] == "Shell-Befehl: true"
    # Kompakte Projektion: kein volles Record (Steps etc. kommen per /api/run/<id>).
    assert "steps" not in appr[0] and "trigger" not in appr[0]


def test_get_projects_shelved_list(fabrik_url, fabrik_home):
    """ADR 0036: Der Projekt-Payload enthält erschöpfte Ketten („Abgelegt“) mit Klasse,
    letztem Fehler und Versuchszahl des letzten Runs; ``pending``-Ketten stehen nicht
    in der Liste (die sind am Run sichtbar)."""
    (fabrik_home / "projects" / "uitest.fabrik.yaml").write_text(
        "name: uitest\n"
        f"root: {fabrik_home}\n"
        "description: d\n"
        "github: owner/repo\n"
        "loops:\n"
        "  t:\n"
        "    description: loop-t\n"
        "    triggers:\n"
        "      - {type: on_issue, repo: owner/repo}\n"
        "    retry: {max: 3}\n"
        "    steps:\n"
        '      - {type: shell, name: a, cmd: "true"}\n'
    )
    rid = "20260103T000000Z-t"
    (fabrik_home / "runs" / "uitest" / f"{rid}.json").write_text(
        json.dumps(
            {
                "id": rid,
                "project": "uitest",
                "loop": "t",
                "trigger": "on_issue:#42",
                "trigger_key": "issue:owner/repo#42",
                "status": "fail",
                "failure_kind": "transient",
                "error": "network timeout",
                "attempt": 2,
                "started": "2024-01-03T00:00:00Z",
            }
        )
    )
    chains = {
        "uitest::t::issue:owner/repo#42": {
            "project": "uitest",
            "loop": "t",
            "trigger_key": "issue:owner/repo#42",
            "state": "exhausted",
            "next_attempt_at": "",
            "last_run_id": rid,
            "claimed_run_id": "",
            "created": "2024-01-01T00:00:00Z",
            "updated": "2024-01-03T00:00:00Z",
        },
        "uitest::t::issue:owner/repo#43": {
            "project": "uitest",
            "loop": "t",
            "trigger_key": "issue:owner/repo#43",
            "state": "pending",
            "next_attempt_at": "2026-01-03T00:05:00+00:00",
            "last_run_id": "",
            "claimed_run_id": "",
            "created": "2024-01-01T00:00:00Z",
            "updated": "2024-01-03T00:00:00Z",
        },
    }
    (fabrik_home / "state" / "retry-chains.json").write_text(json.dumps(chains))
    status, body, _ = _get(f"{fabrik_url}/api/projects")
    assert status == 200
    shelved = json.loads(body)[0]["shelved"]
    assert len(shelved) == 1  # nur exhausted, pending fehlt
    assert shelved[0]["trigger_key"] == "issue:owner/repo#42"
    assert shelved[0]["state"] == "exhausted"
    assert shelved[0]["last_run_id"] == rid
    assert shelved[0]["failure_kind"] == "transient"
    assert shelved[0]["error"] == "network timeout"
    assert shelved[0]["attempt"] == 2


def test_get_runs(fabrik_url):
    status, body, _ = _get(f"{fabrik_url}/api/runs?project=uitest")
    assert status == 200
    ids = [r["id"] for r in json.loads(body)]
    assert RUN_ID in ids and APPROVAL_RUN_ID in ids


def test_get_run(fabrik_url):
    status, body, _ = _get(f"{fabrik_url}/api/run/{RUN_ID}")
    assert status == 200
    assert json.loads(body)["id"] == RUN_ID


def test_get_run_missing(fabrik_url):
    status, _, _ = _get(f"{fabrik_url}/api/run/keiner")
    assert status == 404


def test_get_run_retry_info_only_for_active_chain_current_attempt(fabrik_url, fabrik_home):
    """ADR 0036: Ketten-Info nur für den aktuellen Versuch (last/claimed) einer
    aktiven Kette — nicht für ältere Versuche desselben Schlüssels oder
    beendete Ketten."""
    (fabrik_home / "projects" / "uitest.fabrik.yaml").write_text(
        "name: uitest\n"
        f"root: {fabrik_home}\n"
        "description: d\n"
        "github: owner/repo\n"
        "loops:\n"
        "  t:\n"
        "    description: loop-t\n"
        "    triggers:\n"
        "      - {type: on_issue, repo: owner/repo}\n"
        "    retry: {max: 3, backoff: [5m]}\n"
        "    steps:\n"
        '      - {type: shell, name: a, cmd: "true"}\n'
    )
    rid = "20260103T000000Z-t"
    (fabrik_home / "runs" / "uitest" / f"{rid}.json").write_text(
        json.dumps(
            {
                "id": rid,
                "project": "uitest",
                "loop": "t",
                "trigger": "on_issue:#42",
                "trigger_key": "issue:owner/repo#42",
                "status": "fail",
                "started": "2024-01-03T00:00:00Z",
            }
        )
    )
    chain_file = fabrik_home / "state" / "retry-chains.json"
    chain_file.write_text(
        json.dumps(
            {
                "uitest::t::issue:owner/repo#42": {
                    "project": "uitest",
                    "loop": "t",
                    "trigger_key": "issue:owner/repo#42",
                    "state": "pending",
                    "next_attempt_at": "2026-01-03T00:05:00+00:00",
                    "last_run_id": rid,
                    "claimed_run_id": "",
                    "created": "2024-01-03T00:00:00Z",
                    "updated": "2024-01-03T00:00:00Z",
                }
            }
        )
    )
    status, body, _ = _get(f"{fabrik_url}/api/run/{rid}")
    assert status == 200
    assert json.loads(body)["retry"] == {
        "state": "pending",
        "next_attempt_at": "2026-01-03T00:05:00+00:00",
        "max": 3,
    }

    # Älterer Versuch desselben Schlüssels: keine Ketten-Info.
    old_id = "20260102T000000Z-t"
    (fabrik_home / "runs" / "uitest" / f"{old_id}.json").write_text(
        json.dumps(
            {
                "id": old_id,
                "project": "uitest",
                "loop": "t",
                "trigger": "on_issue:#42",
                "trigger_key": "issue:owner/repo#42",
                "status": "fail",
                "started": "2024-01-02T00:00:00Z",
            }
        )
    )
    status, body, _ = _get(f"{fabrik_url}/api/run/{old_id}")
    assert status == 200
    assert "retry" not in json.loads(body)

    # Beendete Kette: keine Ketten-Info.
    chains = json.loads(chain_file.read_text())
    chains["uitest::t::issue:owner/repo#42"]["state"] = "resolved"
    chain_file.write_text(json.dumps(chains))
    status, body, _ = _get(f"{fabrik_url}/api/run/{rid}")
    assert status == 200
    assert "retry" not in json.loads(body)


def test_get_health(fabrik_url):
    status, body, _ = _get(f"{fabrik_url}/api/health")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_get_metrics(fabrik_url):
    status, body, headers = _get(f"{fabrik_url}/api/metrics")
    assert status == 200
    assert "text/plain" in headers.get("Content-Type", "")
    # Prometheus-Textformat; die Reaper-KPI ist module-basiert registriert,
    # daher steht immer mindestens ihr HELP/TYPE-Block im Export.
    assert "# TYPE fabrik_reaped_runs_total counter" in body


def test_get_metrics_nonempty_export(fabrik_url):
    """Integration: ein inkrementierter Counter erscheint als Sample im Export."""
    from fabrik import metrics

    before = metrics.FABRIK_REAPED_RUNS_TOTAL.value(reason="stale")
    metrics.FABRIK_REAPED_RUNS_TOTAL.inc(reason="stale")
    status, body, _ = _get(f"{fabrik_url}/api/metrics")
    assert status == 200
    assert f'fabrik_reaped_runs_total{{reason="stale"}} {before + 1.0}' in body


def test_get_llm(fabrik_url):
    status, body, _ = _get(f"{fabrik_url}/api/llm")
    assert status == 200
    assert "providers" in json.loads(body)


def test_get_spec_yaml(fabrik_url):
    status, body, headers = _get(f"{fabrik_url}/api/projects/uitest/spec.yaml")
    assert status == 200
    assert "name: uitest" in body
    assert headers.get("Content-Type", "").startswith("text/yaml")


def test_get_spec_yaml_missing(fabrik_url):
    status, _, _ = _get(f"{fabrik_url}/api/projects/keiner/spec.yaml")
    assert status == 404


def test_get_spec(fabrik_url):
    status, body, _ = _get(f"{fabrik_url}/api/projects/uitest/spec")
    assert status == 200
    assert json.loads(body)["name"] == "uitest"


def test_get_spec_missing(fabrik_url):
    status, _, _ = _get(f"{fabrik_url}/api/projects/keiner/spec")
    assert status == 404


def test_get_spec_raw(fabrik_url):
    """?raw=1 → rohe (nicht expandierte) Spec + _hash (Draft-Quelle für den Graph-Editor)."""
    status, body, _ = _get(f"{fabrik_url}/api/projects/uitest/spec?raw=1")
    assert status == 200
    data = json.loads(body)
    assert data["name"] == "uitest"
    assert len(data["_hash"]) == 64


def test_get_spec_raw_missing(fabrik_url):
    status, _, _ = _get(f"{fabrik_url}/api/projects/keiner/spec?raw=1")
    assert status == 404


def test_get_template_params(fabrik_url, fabrik_home, monkeypatch):
    """Template-params-Block für den UI-Graph-Editor (with:-Editor)."""
    tdir = fabrik_home / "user_templates"
    tdir.mkdir()
    (tdir / "dev.loop.yaml").write_text(
        'params:\n  issue_label: "ready"\n  branch: null\nloop:\n  steps:\n    - {type: shell, name: a, cmd: "true"}\n'
    )
    monkeypatch.setattr(config, "USER_TEMPLATES_DIR", tdir)
    status, body, _ = _get(f"{fabrik_url}/api/templates/dev/params")
    assert status == 200
    data = json.loads(body)
    assert data["name"] == "dev"
    assert data["params"] == {"issue_label": "ready", "branch": None}


def test_get_template_params_project_local(fabrik_url, fabrik_home):
    """?project=<name> → projektlokale <spec-ordner>/templates wie bei der Expansion."""
    tdir = fabrik_home / "projects" / "templates"
    tdir.mkdir()
    (tdir / "local.loop.yaml").write_text(
        'params:\n  a: 1\nloop:\n  steps:\n    - {type: shell, name: a, cmd: "true"}\n'
    )
    status, body, _ = _get(f"{fabrik_url}/api/templates/local/params?project=uitest")
    assert status == 200
    assert json.loads(body)["params"] == {"a": 1}
    # Ohne Projekt-Kontext ist das projektlokale Template nicht sichtbar.
    status, _, _ = _get(f"{fabrik_url}/api/templates/local/params")
    assert status == 404


def test_get_template_params_unknown(fabrik_url):
    status, _, _ = _get(f"{fabrik_url}/api/templates/keiner/params")
    assert status == 404


def test_get_template_params_unknown_project(fabrik_url):
    """Unbekanntes/Traversal-Projekt → 404 (kein Lesen außerhalb der Spec-Quellen)."""
    for proj in ("nope", "../../../tmp/x"):
        status, _, _ = _get(f"{fabrik_url}/api/templates/dev/params?project={proj}")
        assert status == 404


def test_get_template_params_malformed_yaml(fabrik_url, fabrik_home):
    """Kaputtes Template-YAML → JSON-404 statt abgebrochener Verbindung."""
    tdir = fabrik_home / "projects" / "templates"
    tdir.mkdir(exist_ok=True)
    (tdir / "kaputt.loop.yaml").write_text("params: [unclosed\n")
    status, body, _ = _get(f"{fabrik_url}/api/templates/kaputt/params?project=uitest")
    assert status == 404
    assert "error" in json.loads(body)


def test_get_branches(fabrik_url):
    status, body, _ = _get(f"{fabrik_url}/api/projects/uitest/branches")
    assert status == 200
    assert "branches" in json.loads(body)


def test_get_branches_unknown(fabrik_url):
    status, _, _ = _get(f"{fabrik_url}/api/projects/keiner/branches")
    assert status == 404


def test_get_static_index(fabrik_url):
    status, body, _ = _get(f"{fabrik_url}/")
    assert status == 200
    assert "fabrik" in body


def test_get_static_missing(fabrik_url):
    status, _, _ = _get(f"{fabrik_url}/nope.html")
    assert status == 404


# ---------------------------------------------------------------- POST
def test_post_runs_starts(fabrik_url, monkeypatch):

    started = []
    done = threading.Event()

    def fake_run_loop(*a, **k):
        started.append(1)
        done.set()
        return {}

    # done.wait(): Worker-Thread muss vor dem Test-Ende fertig sein — sonst
    # rückt monkeypatch den echten run_loop zurück, und der Worker würde ihn
    # (mit project-Lock) aufrufen → Race/Hang im Folge-Test.
    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    status, body, _ = _req(f"{fabrik_url}/api/runs", "POST", {"project": "uitest", "loop": "t"})
    assert status == 200
    assert json.loads(body)["started"] is True
    assert done.wait(timeout=5)
    assert started == [1]


def test_post_runs_returns_run_id(fabrik_url, monkeypatch):
    """Der Response enthält die Run-Id, und der Worker bekommt sie — die UI
    kann den neuen Run sofort per /api/run/<id> verfolgen (Start-Race: der
    Record existiert beim Response noch nicht auf Disk)."""

    calls = []
    done = threading.Event()

    def fake_run_loop(*a, **k):
        calls.append(k)
        done.set()
        return {}

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    status, body, _ = _req(f"{fabrik_url}/api/runs", "POST", {"project": "uitest", "loop": "t"})
    assert status == 200
    data = json.loads(body)
    assert data["run_id"].endswith("-t")
    assert done.wait(timeout=5)
    assert calls[0]["run_id"] == data["run_id"]


def test_post_runs_with_vars(fabrik_url, monkeypatch):
    """vars (K=V) landen als initial_vars im run_loop-Call."""

    calls = []
    done = threading.Event()

    def fake_run_loop(*a, **k):
        calls.append(k)
        done.set()
        return {}

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    status, body, _ = _req(
        f"{fabrik_url}/api/runs", "POST", {"project": "uitest", "loop": "t", "vars": {"issue_number": "42"}}
    )
    assert status == 200
    assert json.loads(body)["started"] is True
    assert done.wait(timeout=5)
    assert calls[0]["initial_vars"] == {"issue_number": "42"}


def test_post_runs_vars_not_dict(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/runs", "POST", {"project": "uitest", "loop": "t", "vars": "issue_number=42"})
    assert status == 400


def test_post_runs_bad_json(fabrik_url):
    req = urllib.request.Request(f"{fabrik_url}/api/runs", data=b"{bad", method="POST")
    try:
        urllib.request.urlopen(req)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400


def test_post_runs_missing_fields(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/runs", "POST", {"project": "uitest"})
    assert status == 400


def test_post_runs_project_busy_queues(fabrik_url, monkeypatch):
    """Projekt am Limit → 202 + queued-Record statt 409; Start nach Slot-Freigabe (ADR 0015/0028)."""
    from fabrik import runs

    done = threading.Event()
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: done.set())
    monkeypatch.setattr(runs, "max_parallel_for", lambda _n: 1)
    claim = runs.claim_run("uitest", "other")
    assert claim is not None
    try:
        status, body, _ = _req(f"{fabrik_url}/api/runs", "POST", {"project": "uitest", "loop": "t"})
        data = json.loads(body)
        assert status == 202
        assert data["queued"] is True and data["started"] is False
        assert runs.get_run(data["run_id"], "uitest")["status"] == "queued"
        assert not done.wait(0.2)
    finally:
        claim.release()
    assert done.wait(5)


def test_post_chat(fabrik_url, monkeypatch):
    import fabrik.server as srv

    monkeypatch.setattr(srv, "_chat", lambda msg, proj="", req_id="", who=None: "antwort")
    status, body, _ = _req(f"{fabrik_url}/api/chat", "POST", {"message": "hallo"})
    assert status == 200
    assert json.loads(body)["reply"] == "antwort"


def test_post_chat_missing_message(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/chat", "POST", {})
    assert status == 400


def test_post_chat_cancel(fabrik_url):
    status, body, _ = _req(f"{fabrik_url}/api/chat/cancel", "POST", {"req_id": "xyz"})
    assert status == 200
    assert json.loads(body)["ok"] is False


def test_post_chat_cancel_missing(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/chat/cancel", "POST", {})
    assert status == 400


def test_post_approve(fabrik_url, monkeypatch):
    import fabrik.server as srv

    monkeypatch.setattr(srv, "_chat_resume", lambda proj, rid, approve, actor=None: ("ok-resume", True))
    status, body, _ = _req(f"{fabrik_url}/api/approve", "POST", {"project": "uitest", "run_id": RUN_ID})
    assert status == 200
    data = json.loads(body)
    assert data["reply"] == "ok-resume"
    assert data["resumed"] is True


def test_post_approve_lock_busy(fabrik_url, monkeypatch):
    """Lock belegt → resumed=False, UI soll nicht pollen, sondern die Begründung zeigen."""
    import fabrik.server as srv

    monkeypatch.setattr(srv, "_chat_resume", lambda proj, rid, approve, actor=None: ("⚠ uitest läuft bereits.", False))
    status, body, _ = _req(f"{fabrik_url}/api/approve", "POST", {"project": "uitest", "run_id": RUN_ID})
    assert status == 200
    assert json.loads(body)["resumed"] is False


def test_post_reject_missing(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/reject", "POST", {"project": "uitest"})
    assert status == 400


def test_post_reject(fabrik_url, monkeypatch):
    import fabrik.server as srv

    monkeypatch.setattr(srv, "_chat_resume", lambda proj, rid, approve, actor=None: ("⊘ abgelehnt", False))
    status, body, _ = _req(f"{fabrik_url}/api/reject", "POST", {"project": "uitest", "run_id": RUN_ID})
    assert status == 200
    data = json.loads(body)
    assert data["reply"] == "⊘ abgelehnt"
    assert data["resumed"] is False


def test_post_llm_missing_provider(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/llm", "POST", {})
    assert status == 400


def test_post_branch(fabrik_url, monkeypatch):
    import fabrik.branches as br

    saved = []
    monkeypatch.setattr(br, "save_override", lambda name, branch: saved.append((name, branch)))
    monkeypatch.setattr(br, "list_branches", lambda root: [])
    status, body, _ = _req(f"{fabrik_url}/api/projects/uitest/branch", "POST", {"branch": "dev"})
    assert status == 200
    assert saved == [("uitest", "dev")]


def test_post_branch_unknown_project(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/projects/keiner/branch", "POST", {"branch": "dev"})
    assert status == 404


def test_post_branch_no_repo(fabrik_url, monkeypatch):
    import fabrik.branches as br

    # Projekt ohne repo → 400
    monkeypatch.setattr(br, "list_branches", lambda root: [])
    # spec ohne repo: neues Projekt schreiben
    from fabrik import config as cfg

    pdir = cfg.PROJECTS_DIR
    (pdir / "norepo.fabrik.yaml").write_text(
        f'name: norepo\nroot: {pdir.parent}\nloops:\n  t:\n    steps:\n      - {{type: shell, cmd: "true"}}\n'
    )
    status, _, _ = _req(f"{fabrik_url}/api/projects/norepo/branch", "POST", {"branch": "dev"})
    assert status == 400


def test_post_unknown_route(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/keiner-da", "POST", {})
    assert status == 404


# ---------------------------------------------------------------- Auth
def test_post_origin_rejected(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/chat", "POST", {"message": "x"}, headers={"Origin": "http://evil.example"})
    assert status == 403


def test_post_token_required(fabrik_url, monkeypatch):
    monkeypatch.setenv("FABRIK_TOKEN", "sekret")
    status, _, _ = _req(f"{fabrik_url}/api/chat", "POST", {"message": "x"})
    assert status == 403
    status, body, _ = _req(
        f"{fabrik_url}/api/chat", "POST", {"message": "x"}, headers={"Authorization": "Bearer sekret"}
    )
    assert status == 200


# ---------------------------------------------------------------- Pause/Disable (ADR 0019)
def test_post_state_roundtrip(fabrik_url):
    status, body, _ = _req(f"{fabrik_url}/api/projects/uitest/state", "POST", {"state": "paused", "loop": "t"})
    assert status == 200 and json.loads(body)["state"] == "paused"
    _, body, _ = _get(f"{fabrik_url}/api/projects")
    loop = json.loads(body)[0]["loops"][0]
    assert loop["state"]["state"] == "paused" and loop["effective_state"] == "paused"
    status, _, _ = _req(f"{fabrik_url}/api/projects/uitest/state", "POST", {"state": "active", "loop": "t"})
    assert status == 200


def test_post_state_errors(fabrik_url):
    url = f"{fabrik_url}/api/projects/uitest/state"
    assert _req(url, "POST", {"state": "bogus"})[0] == 400
    assert _req(f"{fabrik_url}/api/projects/nope/state", "POST", {"state": "paused"})[0] == 404
    assert _req(url, "POST", {"state": "paused", "loop": "zzz"})[0] == 404
    req = urllib.request.Request(url, data=b"{x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400


@pytest.mark.parametrize("raw", [b"null", b"[]", b"\xff"])
def test_post_state_non_object_body_returns_400(fabrik_url, raw):
    status, body = _post(f"{fabrik_url}/api/projects/uitest/state", raw)
    assert status == 400
    assert json.loads(body)["error"] == "ungültiges JSON"


@pytest.mark.parametrize("length", ["abc", "-1"])
def test_post_state_invalid_content_length_returns_400(fabrik_url, length):
    conn = http.client.HTTPConnection(fabrik_url.removeprefix("http://"), timeout=5)
    conn.putrequest("POST", "/api/projects/uitest/state")
    conn.putheader("Content-Length", length)
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 400
    conn.close()


def test_post_state_requires_token(fabrik_url, monkeypatch):
    monkeypatch.setenv("FABRIK_TOKEN", "s3cret")
    assert _req(f"{fabrik_url}/api/projects/uitest/state", "POST", {"state": "paused"})[0] == 403


def test_post_runs_blocked_when_disabled(fabrik_url):
    _req(f"{fabrik_url}/api/projects/uitest/state", "POST", {"state": "disabled"})
    status, body, _ = _req(f"{fabrik_url}/api/runs", "POST", {"project": "uitest", "loop": "t"})
    assert status == 409 and "deaktiviert" in body


# ---------------------------------------------------------------- Helfer
def _post(url, raw: bytes):
    req = urllib.request.Request(url, data=raw, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_read_json_invalid_returns_400(fabrik_url):
    status, body = _post(f"{fabrik_url}/api/chat/cancel", b"{kaputt")
    assert status == 400
    assert json.loads(body)["error"] == "ungültiges JSON"


@pytest.mark.parametrize("raw", [b"null", b"[]", b'"text"'])
def test_read_json_non_object_returns_400(fabrik_url, raw):
    status, body = _post(f"{fabrik_url}/api/chat/cancel", raw)
    assert status == 400
    assert json.loads(body)["error"] == "ungültiges JSON"


def test_read_json_invalid_utf8_returns_400(fabrik_url):
    status, body = _post(f"{fabrik_url}/api/chat/cancel", b"\xff")
    assert status == 400
    assert json.loads(body)["error"] == "ungültiges JSON"


def test_read_json_empty_body_is_empty_object(fabrik_url):
    status, body = _post(f"{fabrik_url}/api/chat/cancel", b"")
    assert status == 400
    assert json.loads(body)["error"] == "req_id nötig"  # {} geparst, nicht JSON-Fehler


@pytest.mark.parametrize("length", ["abc", "-1"])
def test_read_json_invalid_content_length_returns_400(fabrik_url, length):
    conn = http.client.HTTPConnection(fabrik_url.removeprefix("http://"), timeout=5)
    conn.putrequest("POST", "/api/chat/cancel")
    conn.putheader("Content-Length", length)
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 400
    assert json.loads(resp.read())["error"] == "ungültiges JSON"
    conn.close()


def test_read_json_valid(fabrik_url):
    status, body = _post(f"{fabrik_url}/api/chat/cancel", b'{"req_id": "x"}')
    assert status == 200
    assert json.loads(body)["ok"] is False


def test_query_params_decodes_url_encoding():
    h = FabrikHandler.__new__(FabrikHandler)
    h.path = "/api/runs?project=my%20proj&limit=5&flag=&x=a%3Db"
    assert h._query_params() == {"project": "my proj", "limit": "5", "flag": "", "x": "a=b"}


def test_query_params_without_query():
    h = FabrikHandler.__new__(FabrikHandler)
    h.path = "/api/runs"
    assert h._query_params() == {}


def test_retry_now_passes_bare_actor(fabrik_url, monkeypatch):
    """started_by trägt den Namen wie bei POST /api/runs — kein ``user:``/``api:``-Präfix (ADR 0030)."""
    from fabrik import retry_chains

    seen = {}

    def fake_retry_now(project, loop, key, actor=None):
        seen["actor"] = actor
        return "rid"

    monkeypatch.setattr(retry_chains, "retry_now", fake_retry_now)
    status, body, _ = _req(
        f"{fabrik_url}/api/retry-chains/retry-now",
        "POST",
        {"project": "uitest", "loop": "t", "trigger_key": "cron:* * * * *"},
    )
    assert status == 200 and json.loads(body)["run_id"] == "rid"
    assert seen["actor"] is None  # ohne Auth: kein Name, nicht "api:<ip>"


def test_corrupt_chain_store_keeps_dashboard_up(fabrik_url, fabrik_home):
    (fabrik_home / "state" / "retry-chains.json").write_text("{abgeschnitten")
    status, _, _ = _get(f"{fabrik_url}/api/projects")
    assert status == 200


# ---------------------------------------------------------------- PUT /api/projects/<name>/spec
def test_put_spec_happy_path(fabrik_url, fabrik_home):
    """PUT speichert Loops + Top-Level-Felder; Kommentare überleben; GET sieht die Änderung."""
    spec_file = fabrik_home / "projects" / "uitest.fabrik.yaml"
    spec_file.write_text("# Header-Kommentar\n" + spec_file.read_text())
    body = {
        "description": "neu",
        "max_parallel": 2,
        "loops": {
            "t": {
                "description": "loop-t",
                "triggers": [{"type": "manual"}],
                "steps": [{"type": "shell", "name": "a2", "cmd": "true"}],
            }
        },
    }
    status, resp, _ = _req(f"{fabrik_url}/api/projects/uitest/spec", "PUT", body)
    assert status == 200
    data = json.loads(resp)
    assert data["ok"] is True
    assert data["spec"]["description"] == "neu"
    assert data["spec"]["max_parallel"] == 2
    text = spec_file.read_text()
    assert "# Header-Kommentar" in text  # Kommentar-Erhaltung (ruamel Round-Trip)
    assert "name: a2" in text
    status, resp, _ = _get(f"{fabrik_url}/api/projects/uitest/spec")
    assert status == 200
    assert json.loads(resp)["loops"]["t"]["steps"][0]["name"] == "a2"


def test_put_spec_base_hash_conflict_409(fabrik_url, fabrik_home):
    """Stale base_hash (externale Änderung) → 409, Datei bleibt unangetastet."""
    from fabrik.specs import spec_hash

    spec_file = fabrik_home / "projects" / "uitest.fabrik.yaml"
    before = spec_file.read_text()
    status, resp, _ = _req(f"{fabrik_url}/api/projects/uitest/spec", "PUT", {"description": "x", "base_hash": "0" * 64})
    assert status == 409
    assert "extern" in json.loads(resp)["error"]
    assert spec_file.read_text() == before
    # Korrekter aktueller Hash → Save geht durch.
    status, resp, _ = _req(
        f"{fabrik_url}/api/projects/uitest/spec", "PUT", {"description": "x", "base_hash": spec_hash(spec_file)}
    )
    assert status == 200
    assert json.loads(resp)["spec"]["_hash"] == spec_hash(spec_file)


def test_put_spec_invalid_returns_400_and_keeps_file(fabrik_url, fabrik_home):
    """Validierungsfehler → 400 mit Meldung, Datei bleibt unangetastet."""
    spec_file = fabrik_home / "projects" / "uitest.fabrik.yaml"
    before = spec_file.read_text()
    status, resp, _ = _req(
        f"{fabrik_url}/api/projects/uitest/spec", "PUT", {"loops": {"t": {"steps": [{"type": "shell"}]}}}
    )
    assert status == 400
    assert "shell ohne cmd" in json.loads(resp)["error"]
    assert spec_file.read_text() == before


def test_put_spec_unknown_project_returns_404(fabrik_url):
    status, _, _ = _req(f"{fabrik_url}/api/projects/keiner/spec", "PUT", {"description": "x"})
    assert status == 404


def test_put_spec_unknown_field_returns_400(fabrik_url):
    status, resp, _ = _req(f"{fabrik_url}/api/projects/uitest/spec", "PUT", {"name": "x"})
    assert status == 400
    assert "unbekannte Felder" in json.loads(resp)["error"]


# ---------------------------------------------------------------- Connections (ADR 0052)
CONN_BODY = {
    "kind": "agent",
    "driver": "opencode",
    "endpoint": "http://127.0.0.1:4096",
    "model": "llama/q",
    "secret": "pw-secret-123",
}


def _put_conn(url, name, body):
    return _req(f"{url}/api/connections/{name}", "PUT", body, {"Content-Type": "application/json"})


def test_connections_put_get_delete_roundtrip_hides_secret(fabrik_url):
    status, body, _ = _put_conn(fabrik_url, "oc", CONN_BODY)
    assert status == 200, body
    status, body, _ = _get(f"{fabrik_url}/api/connections")
    data = json.loads(body)
    assert status == 200 and "pw-secret-123" not in body
    oc = next(c for c in data["connections"] if c["name"] == "oc")
    assert oc["secret_set"] is True and oc["source"] == "db"
    assert "opencode" in data["drivers"]["agent"] and "http" in data["drivers"]["llm"]
    assert data["min_masked_secret_len"] == secrets.MIN_SECRET_LEN  # UI-Hinweis für kurze Secrets
    status, _, _ = _req(f"{fabrik_url}/api/connections/oc", "DELETE")
    assert status == 200
    status, _, _ = _req(f"{fabrik_url}/api/connections/oc", "DELETE")
    assert status == 404


def test_connections_put_without_secret_keeps_it_and_clear_removes_it(fabrik_url):
    _put_conn(fabrik_url, "oc", CONN_BODY)
    _put_conn(fabrik_url, "oc", {k: v for k, v in CONN_BODY.items() if k != "secret"} | {"model": "p/other"})
    assert next(c for c in connections.list_connections() if c.name == "oc").secret == "pw-secret-123"
    _put_conn(fabrik_url, "oc", {k: v for k, v in CONN_BODY.items() if k != "secret"} | {"clear_secret": True})
    assert next(c for c in connections.list_connections() if c.name == "oc").secret == ""


def test_connections_put_invalid_is_400(fabrik_url):
    status, body, _ = _put_conn(fabrik_url, "oc", {**CONN_BODY, "driver": "nope"})
    assert status == 400 and "unbekannt" in json.loads(body)["error"]


def test_connections_writes_need_admin_reads_need_viewer():
    assert auth.required_role("GET", "/api/connections") == auth.VIEWER
    assert auth.required_role("PUT", "/api/connections/x") == auth.ADMIN
    assert auth.required_role("DELETE", "/api/connections/x") == auth.ADMIN
