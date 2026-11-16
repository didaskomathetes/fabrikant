"""Schedule-API (Live-Server): GET /api/schedule, POST/DELETE /api/schedule/onetime."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

from fabrik import config, schedule


@pytest.fixture
def sched_home(tmp_path, monkeypatch):
    home = tmp_path / "fabrik_home"
    (home / "projects").mkdir(parents=True)
    (home / "runs").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    (home / "web").mkdir(parents=True)
    (home / "web" / "index.html").write_text("<html>fabrik</html>")
    (home / "projects" / "sched.fabrik.yaml").write_text(
        "name: sched\nroot: /tmp\ndescription: d\nrepo:\n  branch: main\nmax_parallel: 1\nloops:\n"
        "  cronloop:\n"
        "    description: cron-loop\n"
        '    triggers:\n      - {type: cron, cron: "*/10 * * * *"}\n'
        '    steps:\n      - {type: shell, name: a, cmd: "true"}\n'
        "  manualloop:\n"
        "    description: manual-loop\n"
        "    triggers:\n      - {type: manual}\n"
        '    steps:\n      - {type: shell, name: a, cmd: "true"}\n'
        "  slowloop:\n"
        "    description: slow-loop (Queue-Test, eigener Scope ohne Worktree)\n"
        "    worktree: false\n"
        "    triggers:\n      - {type: manual}\n"
        '    steps:\n      - {type: shell, name: a, cmd: "sleep 30"}\n'
    )
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    monkeypatch.setattr(config, "WEB_DIR", home / "web")
    return home


@pytest.fixture
def sched_url(sched_home, clean_token, live_server):
    return live_server()


def _req(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test_get_schedule_shape(sched_url):
    status, body = _req(f"{sched_url}/api/schedule")
    assert status == 200
    assert set(body) == {"now", "loops", "onetime", "queue"}
    assert body["onetime"] == []
    assert len(body["loops"]) == 1  # nur der cron-Loop
    # Queue: leer (keine Runs); Pool-Größe ist Modul-Global (Suite-Zustand erlaubt)
    queue = body["queue"]
    assert set(queue) == {"pool", "projects", "running", "queued", "approval", "priority_waiters"}
    assert set(queue["pool"]) == {"workers", "busy", "queue"}
    assert queue["projects"] == []
    assert queue["running"] == [] and queue["queued"] == [] and queue["approval"] == []
    assert isinstance(queue["priority_waiters"], dict)
    loop = body["loops"][0]
    assert (loop["project"], loop["loop"], loop["cron"]) == ("sched", "cronloop", "*/10 * * * *")
    assert loop["effective_state"] == "active"
    assert loop["onetime"] is None
    assert loop["last"] is None
    # Nächste Fire-Zeiten: > now, 10er-Minuten, aufsteigend
    now = datetime.fromisoformat(body["now"])
    assert 0 < len(loop["next"]) <= 5
    for iso in loop["next"]:
        t = datetime.fromisoformat(iso)
        assert t > now and t.minute % 10 == 0
    assert loop["next"] == sorted(loop["next"])


def test_onetime_post_and_delete_roundtrip(sched_url):
    # Ansetzen (Uhrzeit heute — parse_when rollt zurück, falls schon vorbei)
    at = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=37)
    status, body = _req(
        f"{sched_url}/api/schedule/onetime",
        "POST",
        {"project": "sched", "loop": "cronloop", "when": at.strftime("%H:%M"), "note": "test"},
    )
    assert status == 200, body
    entry = body["onetime"]
    assert entry["project"] == "sched" and entry["loop"] == "cronloop" and entry["note"] == "test"
    assert datetime.fromisoformat(entry["at"]) > datetime.now()

    # GET /api/schedule zeigt den Eintrag (global + am Loop)
    status, body = _req(f"{sched_url}/api/schedule")
    assert status == 200
    assert body["onetime"] == [entry]
    assert body["loops"][0]["onetime"] == entry

    # Ersetzen (dieselbe Projekt/Loop → nur ein Eintrag)
    at2 = at + timedelta(minutes=10)
    status, body = _req(
        f"{sched_url}/api/schedule/onetime",
        "POST",
        {"project": "sched", "loop": "cronloop", "when": at2.strftime("%H:%M")},
    )
    assert status == 200
    assert len(schedule.list_onetimes()) == 1

    # Entfernen
    status, body = _req(
        f"{sched_url}/api/schedule/onetime",
        "DELETE",
        {"project": "sched", "loop": "cronloop"},
    )
    assert status == 200
    assert schedule.list_onetimes() == []
    status, body = _req(
        f"{sched_url}/api/schedule/onetime",
        "DELETE",
        {"project": "sched", "loop": "cronloop"},
    )
    assert status == 404


def test_onetime_post_validation(sched_url):
    cases = [
        ({"project": "sched", "loop": "cronloop", "when": "garbage"}, 400),
        ({"project": "sched", "loop": "cronloop", "when": "2020-01-01T00:00"}, 400),
        ({"project": "ghost", "loop": "cronloop", "when": "15:05"}, 404),
        ({"project": "sched", "loop": "ghost", "when": "15:05"}, 404),
        ({"project": "sched", "loop": "manualloop", "when": ""}, 400),
        ({"loop": "cronloop", "when": "15:05"}, 400),
    ]
    for payload, expected in cases:
        status, body = _req(f"{sched_url}/api/schedule/onetime", "POST", payload)
        assert status == expected, (payload, status, body)
        assert "error" in body


def test_queue_shows_running_and_waiting(sched_url):
    """Queue-Sektion: laufender Run (Elapsed) + zweiter Run wartet (slot/Scope).

    slowloop = `worktree: false` → eigener Scope `sched::slowloop`, kein
    Worktree nötig. Run 1 läuft 30 s (sleep); Run 2 blockiert auf dem Claim
    desselben Scopes → queued-Record mit `est: slot`."""
    status, body = _req(f"{sched_url}/api/runs", "POST", {"project": "sched", "loop": "slowloop"})
    assert status in (200, 202), body

    # Run 1 wird als running sichtbar (Record entsteht nach Claim)
    deadline = time.time() + 10
    running = []
    while time.time() < deadline:
        _, body = _req(f"{sched_url}/api/schedule")
        running = body["queue"]["running"]
        if running:
            break
        time.sleep(0.2)
    assert running, "slowloop-Run wurde nicht als running sichtbar"
    assert running[0]["project"] == "sched" and running[0]["loop"] == "slowloop"
    assert running[0]["trigger"] == "manual"
    assert running[0]["elapsed_s"] >= 0 and running[0]["started"]

    # Run 2 (gleicher Scope) → wartet als Vorrang-Claim (queued-Record sofort)
    status, body = _req(f"{sched_url}/api/runs", "POST", {"project": "sched", "loop": "slowloop", "note": "wartend"})
    assert status in (200, 202), body
    _, body = _req(f"{sched_url}/api/schedule")
    queue = body["queue"]
    waiting = [e for e in queue["queued"] if e["loop"] == "slowloop"]
    assert waiting, f"wartender Run nicht in Queue: {queue}"
    assert waiting[0]["est"] == "slot"
    assert waiting[0]["queued_at"]
    assert queue["priority_waiters"].get("sched::slowloop", 0) >= 1
    # Slot-Kontext: Projekt im Snapshot, eigener Scope belegt keinen Projekt-Slot
    assert {p["name"] for p in queue["projects"]} == {"sched"}
    proj = queue["projects"][0]
    assert proj["slots_used"] == 0 and proj["slots_limit"] == 1


def test_pool_queue_est_accounts_for_busy_workers(sched_url, monkeypatch):
    """Pool-Queue: `now` nur, solange belegte Worker + FIFO-Position < Pool-Größe."""
    from fabrik import server

    monkeypatch.setattr(server, "pool_status", lambda: {"workers": 2, "busy": 1, "queue": 2})
    monkeypatch.setattr(
        server,
        "queued_trigger_snapshot",
        lambda: [
            {"project": "sched", "loop": "cronloop", "trigger": "cron", "enqueued_at": 100.0},
            {"project": "sched", "loop": "manualloop", "trigger": "file", "enqueued_at": 200.0},
        ],
    )
    _, body = _req(f"{sched_url}/api/schedule")
    est = {e["loop"]: e["est"] for e in body["queue"]["queued"]}
    assert est == {"cronloop": "now", "manualloop": "worker"}
