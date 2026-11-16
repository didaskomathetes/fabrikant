"""Pause/Disable: Zustand, Präzedenz, Persistenz, until, Scheduler-Skip, API/CLI/Chat."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from helpers import not_none

from fabrik import chat, cli, config, pause, scheduler
from fabrik.scheduler import Scheduler


@pytest.fixture
def proj(projects_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    root = tmp_path / "root"
    root.mkdir()
    (projects_dir / "p.fabrik.yaml").write_text(
        f'name: p\nroot: {root}\nloops:\n  l:\n    triggers:\n      - {{type: cron, cron: "* * * * *"}}\n'
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )
    return "p"


def _future(h=1):
    return (config.utcnow() + timedelta(hours=h)).isoformat()


def test_default_active(proj):
    assert pause.effective_state("p", "l") == "active"
    assert not pause.trigger_blocked("p", "l")
    assert pause.manual_block_reason("p", "l") is None


def test_paused_blocks_triggers_not_manual(proj):
    pause.set_state("p", "l", "paused", actor="t")
    assert pause.trigger_blocked("p", "l")
    assert pause.manual_block_reason("p", "l") is None


def test_disabled_blocks_manual(proj):
    pause.set_state("p", "l", "disabled", actor="t")
    assert pause.trigger_blocked("p", "l")
    assert "deaktiviert" in not_none(pause.manual_block_reason("p", "l"))


def test_project_beats_loop(proj):
    pause.set_state("p", "l", "disabled", actor="t")
    pause.set_state("p", None, "paused", actor="t")
    assert pause.effective_state("p", "l") == "paused"
    pause.set_state("p", None, "disabled", actor="t")
    pause.set_state("p", "l", "active", actor="t")
    assert pause.effective_state("p", "l") == "disabled"
    assert pause.manual_block_reason("p", "l")


def test_resume_removes_entry(proj):
    pause.set_state("p", None, "paused", actor="t")
    pause.set_state("p", None, "active", actor="t")
    assert json.loads(pause._states_path().read_text()) == {}


def test_persists_and_survives_spec_change(proj, projects_dir):
    pause.set_state("p", "l", "paused", until=_future(), actor="t")
    spec = projects_dir / "p.fabrik.yaml"
    spec.write_text(spec.read_text() + "\n# edit\n")
    st = pause.get_state("p", "l")  # frischer Read von Disk = "Restart"
    assert st["state"] == "paused" and st["until"]


def test_until_expiry_and_auto_resume_logged(proj):
    pause.set_state("p", "l", "paused", until=_future(), actor="t")
    data = json.loads(pause._states_path().read_text())
    data["p/l"]["until"] = (config.utcnow() - timedelta(seconds=1)).isoformat()
    pause._states_path().write_text(json.dumps(data))
    assert pause.get_state("p", "l")["state"] == "active"  # lazy
    assert pause.resume_expired() == ["p/l"]
    assert pause.resume_expired() == []
    last = json.loads(pause._audit_path().read_text().splitlines()[-1])
    assert last["actor"] == "auto-resume" and last["from"] == "paused" and last["to"] == "active"


def test_audit_log(proj):
    pause.set_state("p", "l", "paused", until=_future(), actor="alice")
    pause.set_state("p", "l", "disabled", actor="bob")
    recs = [json.loads(x) for x in pause._audit_path().read_text().splitlines()]
    assert [(r["actor"], r["from"], r["to"]) for r in recs] == [
        ("alice", "active", "paused"),
        ("bob", "paused", "disabled"),
    ]
    assert recs[0]["until"] and recs[0]["loop"] == "l" and recs[0]["ts"]


@pytest.mark.parametrize(
    ("loop", "state", "until"),
    [
        ("nope", "paused", None),
        ("l", "bogus", None),
        ("l", "active", _future()),
        ("l", "paused", "garbage"),
        ("l", "paused", "2000-01-01T00:00:00"),
    ],
    # Feste IDs: _future() im ID würde je Collection variieren (bricht pytest-xdist).
    ids=["unknown-loop", "bad-state", "active-with-until", "bad-until", "past-until"],
)
def test_set_state_validation(proj, loop, state, until):
    with pytest.raises(ValueError):
        pause.set_state("p", loop, state, until=until)


def test_unknown_project(proj):
    with pytest.raises(ValueError, match="unbekannt"):
        pause.set_state("zzz", None, "paused")


def test_naive_until_is_utc(proj):
    naive = (config.utcnow() + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert pause.parse_until(naive).endswith("+00:00")
    assert pause.parse_until(None) is None


def test_broken_state_file_is_empty(proj):
    pause._states_path().parent.mkdir(parents=True)
    pause._states_path().write_text("[1]")
    assert pause._load() == {}
    pause._states_path().write_text("{nope")
    assert pause._load() == {}


def test_scheduler_skips_paused(proj, monkeypatch):
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    s = Scheduler.__new__(Scheduler)
    s.__init__()
    pause.set_state("p", "l", "paused", actor="t")
    s.tick()
    assert fired == []
    pause.set_state("p", "l", "active", actor="t")
    s.tick()
    assert fired == ["cron"]


def test_scheduler_skips_when_project_disabled(proj, monkeypatch):
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    s = Scheduler()
    pause.set_state("p", None, "disabled", actor="t")
    s.tick()
    assert fired == []


def test_scheduler_resumes_after_until(proj, monkeypatch):
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    pause.set_state("p", None, "paused", until=_future(), actor="t")
    data = json.loads(pause._states_path().read_text())
    data["p"]["until"] = (config.utcnow() - timedelta(seconds=1)).isoformat()
    pause._states_path().write_text(json.dumps(data))
    Scheduler().tick()
    assert fired == ["cron"]


def test_chat_commands(proj):
    assert "✔" in chat._chat("pause p l")
    assert pause.effective_state("p", "l") == "paused"
    assert "✔" in chat._chat("disable p")
    assert pause.effective_state("p", "l") == "disabled"
    assert chat._chat("lauf p l").startswith("⊘")
    assert "✔" in chat._chat("resume p")
    assert chat._chat("pause nope").startswith("✖")
    assert chat._chat_set_state(["pause"]).startswith("Format")


def test_chat_run_blocked_when_disabled(proj):
    pause.set_state("p", "l", "disabled", actor="t")
    assert chat._chat("lauf p l").startswith("⊘")


def test_cli_state_and_run_block(proj, capsys):
    cli.main(["pause", "p", "l", "--until", _future()])
    assert pause.get_state("p", "l")["state"] == "paused"
    cli.main(["resume", "p", "l"])
    assert pause.get_state("p", "l")["state"] == "active"
    cli.main(["disable", "p"])
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "p", "l"])
    assert e.value.code == 1
    with pytest.raises(SystemExit):
        cli.main(["pause", "nope"])
    assert "deaktiviert" in capsys.readouterr().out


@pytest.mark.parametrize("until", [123, "kaputt", ["x"]])
def test_corrupt_until_stays_paused(proj, until):
    pause._states_path().parent.mkdir(parents=True)
    pause._states_path().write_text(json.dumps({"p": {"state": "paused", "until": until}}))
    assert pause.resume_expired() == []
    assert pause.get_state("p")["state"] == "paused"


def test_naive_persisted_until_is_utc(proj):
    past = (config.utcnow() - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    pause._states_path().parent.mkdir(parents=True)
    pause._states_path().write_text(json.dumps({"p": {"state": "paused", "until": past}}))
    assert pause.resume_expired() == ["p"]
