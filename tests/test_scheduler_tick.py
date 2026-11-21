"""Scheduler: tick() (Trigger-Dispatch), _check_issue, _check_stale_runs."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fabrik import gitops, retry_chains, scheduler
from fabrik.scheduler import Scheduler


class _FakeSched(Scheduler):
    """Echter Scheduler, Thread nie gestartet: Tests rufen tick()/_check_* direkt auf."""


class _FakePool:
    def __init__(self, submitted):
        self._submitted = submitted

    def submit(self, fn):
        self._submitted.append(fn)
        return None


# ---------------------------------------------------------------- tick()
def test_tick_dispatches_cron(projects_dir, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "p.fabrik.yaml").write_text(
        f'name: p\nroot: {root}\nloops:\n  l:\n    triggers:\n      - {{type: cron, cron: "* * * * *"}}\n'
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    s = _FakeSched()
    s.tick()
    assert len(fired) == 1
    assert fired[0] == "cron"


def test_tick_onetime_store_error_does_not_block_cron(projects_dir, tmp_path, monkeypatch):
    """Ein defekter One-time-Store darf Cron-Trigger nicht für jeden Tick stoppen."""
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "p.fabrik.yaml").write_text(
        f'name: p\nroot: {root}\nloops:\n  l:\n    triggers:\n      - {{type: cron, cron: "* * * * *"}}\n'
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))

    def broken_store(now):
        raise OSError("disk full")

    monkeypatch.setattr(scheduler.schedule, "pop_due", broken_store)
    _FakeSched().tick()
    assert fired == ["cron"]


def test_tick_cron_no_double_fire_same_minute(projects_dir, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "p.fabrik.yaml").write_text(
        f'name: p\nroot: {root}\nloops:\n  l:\n    triggers:\n      - {{type: cron, cron: "* * * * *"}}\n'
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    s = _FakeSched()
    s.tick()
    s.tick()  # gleiche Minute → kein zweites Fire
    assert len(fired) == 1


def _cron_spec(projects_dir, tmp_path, trigger: str) -> None:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (projects_dir / "p.fabrik.yaml").write_text(
        f"name: p\nroot: {root}\nloops:\n  l:\n    triggers:\n      - {trigger}\n"
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )


def _tick_at(monkeypatch, s, minute: int) -> None:
    """Ein Tick zur (lokalen und UTC-)Uhrzeit 10:<minute>."""
    t = datetime(2026, 11, 3, 10, minute)

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return t

    monkeypatch.setattr(scheduler, "datetime", _Fixed)
    monkeypatch.setattr(scheduler.config, "utcnow", lambda: t.replace(tzinfo=UTC))
    s.tick()


def test_tick_cron_on_start_fires_once_outside_schedule(projects_dir, tmp_path, monkeypatch):
    """Regression (ai-lab 2026-11-03): Server startet 14:26 mit abgelaufenem Token; token-refresh
    feuert erst 14:30 — gleichzeitig mit relay-dispatch, der noch das alte Token las (401)."""
    _cron_spec(projects_dir, tmp_path, '{type: cron, cron: "*/30 * * * *", on_start: true}')
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append((n, ln, t)))
    s = _FakeSched()
    _tick_at(monkeypatch, s, 26)  # Start außerhalb des Schedules → feuert sofort
    _tick_at(monkeypatch, s, 27)  # nur einmal pro Prozess
    _tick_at(monkeypatch, s, 30)  # danach normaler Schedule
    assert fired == [("p", "l", "cron"), ("p", "l", "cron")]


def test_tick_cron_on_start_no_double_fire_when_start_matches_schedule(projects_dir, tmp_path, monkeypatch):
    _cron_spec(projects_dir, tmp_path, '{type: cron, cron: "*/30 * * * *", on_start: true}')
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    s = _FakeSched()
    _tick_at(monkeypatch, s, 30)
    _tick_at(monkeypatch, s, 30)
    assert fired == ["cron"]


def test_tick_cron_without_on_start_waits_for_schedule(projects_dir, tmp_path, monkeypatch):
    _cron_spec(projects_dir, tmp_path, '{type: cron, cron: "*/30 * * * *"}')
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    _tick_at(monkeypatch, _FakeSched(), 26)
    assert fired == []


def test_tick_cron_on_start_fires_on_first_tick_after_resume(projects_dir, tmp_path, monkeypatch):
    """Beim Start pausiert → on_start holt das Fire beim ersten aktiven Tick nach."""
    _cron_spec(projects_dir, tmp_path, '{type: cron, cron: "*/30 * * * *", on_start: true}')
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    paused = {"v": True}
    monkeypatch.setattr(scheduler.pause, "trigger_blocked", lambda n, ln: paused["v"])
    s = _FakeSched()
    _tick_at(monkeypatch, s, 26)
    assert fired == []
    paused["v"] = False
    _tick_at(monkeypatch, s, 27)
    assert fired == ["cron"]


def _start_gate_spec(projects_dir, tmp_path) -> None:
    """token-refresh (on_start) + relay-dispatch (Cron jede Minute) + PR-Poll in einem Projekt."""
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    step = '    steps:\n      - {type: shell, cmd: "true"}\n'
    (projects_dir / "p.fabrik.yaml").write_text(
        f"name: p\nroot: {root}\nloops:\n"
        '  dispatch:\n    triggers:\n      - {type: cron, cron: "* * * * *"}\n'
        + step
        + "  review:\n    triggers:\n      - {type: on_pr, repo: o/r}\n"
        + step
        + '  refresh:\n    triggers:\n      - {type: cron, cron: "*/30 * * * *", on_start: true}\n'
        + step
    )


def _gated_sched(monkeypatch):
    """Scheduler, dessen Runs nur aufgezeichnet werden; liefert (s, fired, polled, start_done)."""
    fired: list[str] = []
    start_done: list[threading.Event] = []

    def fake_safe_run(n, ln, t, initial_vars=None, done=None):
        fired.append(ln)
        if done is not None:
            start_done.append(done)

    monkeypatch.setattr(scheduler, "_safe_run", fake_safe_run)
    s = _FakeSched()
    polled: list[str] = []
    monkeypatch.setattr(s, "_check_pr", lambda n, ln, spec, trig: polled.append(ln))
    return s, fired, polled, start_done


def test_tick_on_start_runs_before_other_triggers_and_gates_them(projects_dir, tmp_path, monkeypatch):
    """Regression (ai-lab 2026-11-09): Start-Tick pollte PRs parallel zum token-refresh mit dem
    abgelaufenen Token (HTTP 401). Jetzt: erst on_start, übrige Trigger warten, bis er fertig ist."""
    _start_gate_spec(projects_dir, tmp_path)
    s, fired, polled, start_done = _gated_sched(monkeypatch)
    _tick_at(monkeypatch, s, 26)
    assert (fired, polled) == (["refresh"], [])  # Refresh läuft (Spec-Reihenfolge egal) → Rest wartet
    _tick_at(monkeypatch, s, 26)
    assert (fired, polled) == (["refresh"], [])
    start_done[0].set()
    _tick_at(monkeypatch, s, 26)  # dieselbe Minute: aufgeschobener Cron feuert nach
    assert (fired, polled) == (["refresh", "dispatch"], ["review"])


def test_tick_start_gate_releases_after_max_wait(projects_dir, tmp_path, monkeypatch):
    """Hängt der Start-Run (z. B. hinter belegten Workern), blockiert er die übrigen Trigger nicht ewig."""
    _start_gate_spec(projects_dir, tmp_path)
    s, fired, polled, _ = _gated_sched(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock["t"])
    _tick_at(monkeypatch, s, 26)
    assert polled == []
    clock["t"] += scheduler.START_GATE_MAX_S
    _tick_at(monkeypatch, s, 26)
    assert (fired, polled) == (["refresh", "dispatch"], ["review"])


def test_tick_start_gate_is_per_project(projects_dir, tmp_path, monkeypatch):
    """Ein laufender on_start-Run eines Projekts hält die Trigger anderer Projekte nicht auf."""
    _start_gate_spec(projects_dir, tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (projects_dir / "q.fabrik.yaml").write_text(
        f"name: q\nroot: {other}\nloops:\n  poll:\n    triggers:\n      - {{type: on_pr, repo: o/q}}\n"
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )
    s, _, polled, _ = _gated_sched(monkeypatch)
    _tick_at(monkeypatch, s, 26)
    assert polled == ["poll"]


def test_tick_cron_skipped_when_retry_chain_pending_starts_exactly_one_run(projects_dir, tmp_path, monkeypatch):
    """Regression (#290, ADR 0036 Entscheidung 2): fällige pending-Kette → der Cron-Tick
    wird übersprungen und die Kette startet den Versuch — genau ein Run, nie zwei."""
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "p.fabrik.yaml").write_text(
        f'name: p\nroot: {root}\nloops:\n  l:\n    triggers:\n      - {{type: cron, cron: "* * * * *"}}\n'
        "    retry: {max: 3, backoff: [30s, 40s]}\n"  # Backoff < Intervall (sonst Spec-Fehler)
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )
    now = datetime.now(UTC)
    store = retry_chains.ChainStore(path=tmp_path / "chains.json")
    store.create("p", "l", "cron:* * * * *", next_attempt_at=now - timedelta(minutes=1), last_run_id="r1", now=now)
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    monkeypatch.setattr(retry_chains, "get_run", lambda rid, proj: None)
    fired, submitted = [], []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    monkeypatch.setattr("fabrik.engine._get_run_pool", lambda: _FakePool(submitted))
    s = _FakeSched()
    s.tick()
    assert fired == []  # Cron-Tick übersprungen (Kette pending)
    assert len(submitted) == 1  # genau der Kette-Versuch
    assert store.get("p", "l", "cron:* * * * *").is_claimed


def test_tick_skips_error_spec(projects_dir, monkeypatch):
    (projects_dir / "bad.fabrik.yaml").write_text("name: bad\nroot: /nonexistent\nloops: {}\n")
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    s = _FakeSched()
    s.tick()  # darf nicht crashen
    assert fired == []


def test_tick_on_file(projects_dir, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("x")
    (projects_dir / "p.fabrik.yaml").write_text(
        f"name: p\nroot: {root}\nloops:\n  l:\n    triggers:\n"
        '      - {type: on_file, glob: "*.txt", debounce: 0}\n'
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    s = _FakeSched()
    s.tick()  # init
    s.tick()  # debounce=0 → feuert
    assert len(fired) >= 1


# ---------------------------------------------------------------- _check_issue
def _issue(num, title="t", body="b"):
    return {"number": num, "title": title, "body": body}


def _mk_issue_test(monkeypatch, issues):
    s = _FakeSched()
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append((t, kw)))
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_token_safe", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t, payload=None: list(issues))
    trig = {"repo": "o/r", "poll": 0}
    return s, fired, trig


def test_check_issue_first_poll_no_fire(monkeypatch):
    s, fired, trig = _mk_issue_test(monkeypatch, [_issue(1)])
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == []


def test_check_issue_new_fires(monkeypatch):
    s, fired, trig = _mk_issue_test(monkeypatch, [_issue(1)])
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t, payload=None: [_issue(1), _issue(2, "neu", "body")])
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert len(fired) == 1
    assert fired[0][0] == "on_issue:#2"
    assert fired[0][1]["initial_vars"]["issue_number"] == 2
    assert fired[0][1]["initial_vars"]["issue_title"] == "neu"


def test_check_issue_filters_prs(monkeypatch):
    s, fired, trig = _mk_issue_test(monkeypatch, [])
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    monkeypatch.setattr(
        gitops, "_gh_api", lambda m, u, t, payload=None: [{"number": 5, "title": "pr", "pull_request": {}}]
    )
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == []  # PRs werden gefiltert


def test_check_issue_no_repo(monkeypatch):
    s = _FakeSched()
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    s._check_issue("p", "l", {}, {"type": "on_issue"})
    assert fired == []


def test_check_issue_no_token(monkeypatch):
    s = _FakeSched()
    fired = []
    api = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))

    monkeypatch.setattr(gitops, "_gh_token_safe", lambda role="dev": "")
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t, payload=None: api.append(1) or [])
    s._check_issue("p", "l", {"github": "o/r"}, {"repo": "o/r", "poll": 0})
    assert api == []
    assert fired == []


def test_check_issue_api_error(monkeypatch):
    s = _FakeSched()
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_token_safe", lambda role="dev": "tok")

    def _boom(m, u, t, payload=None):
        raise RuntimeError("down")

    monkeypatch.setattr(gitops, "_gh_api", _boom)
    s._check_issue("p", "l", {"github": "o/r"}, {"repo": "o/r", "poll": 0})
    assert fired == []


# ---------------------------------------------------------------- _check_issue reconcile (ADR 0014)
def _write_run(runs_dir, num, status="pass", loop="l", project="p"):
    d = runs_dir / project
    d.mkdir(parents=True, exist_ok=True)
    rec = {"loop": loop, "trigger": f"on_issue:#{num}", "status": status}
    (d / f"{loop}-{num}-{status}.json").write_text(json.dumps(rec))


def _mk_reconcile_test(monkeypatch, runs_dir, issues, **trig_extra):
    """Fake-API mit Pagination: liefert `issues` (älteste zuerst) seitenweise."""
    s = _FakeSched()
    fired, urls = [], []
    per = scheduler.GH_PER_PAGE

    def _api(m, u, t, payload=None):
        urls.append(u)
        page = int(u.split("&page=")[1].split("&")[0])
        return issues[(page - 1) * per : page * per]

    monkeypatch.setattr(scheduler.config, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_token_safe", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _api)
    return s, fired, urls, {"repo": "o/r", "poll": 0, "reconcile": True, **trig_extra}


def test_check_issue_reconcile_first_poll_fires_on_backlog(monkeypatch, tmp_path):
    """Erst-Poll feuert auf Bestand (älteste zuerst), gedrosselt auf den Default."""
    s, fired, urls, trig = _mk_reconcile_test(monkeypatch, tmp_path, [_issue(n) for n in (3, 5, 7)])
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#3", "on_issue:#5"]
    assert scheduler.DEFAULT_MAX_PER_POLL == 2
    assert "direction=asc" in urls[0]


def test_check_issue_reconcile_restart_skips_issues_with_any_run(monkeypatch, tmp_path):
    """Neue Scheduler-Instanz (Restart): Issues mit Run (auch fehlgeschlagen) feuern nicht erneut."""
    _write_run(tmp_path, 3, status="pass")
    _write_run(tmp_path, 5, status="error")  # fehlgeschlagen → kein Auto-Retry
    _write_run(tmp_path, 7, status="pass", loop="other")  # anderer Loop zählt nicht
    s, fired, _, trig = _mk_reconcile_test(monkeypatch, tmp_path, [_issue(n) for n in (3, 5, 7, 9)])
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#7", "on_issue:#9"]


def test_check_issue_reconcile_no_second_run_for_pending_retry_chain(monkeypatch, tmp_path):
    """Regression (#290, baut auf #258 auf): #42 hat Run 0 (fail) und eine pending-Retry-Kette
    mit claimed, noch nicht geschriebenem Versuch. Der Poll darf für #42 keinen zweiten Run
    starten — der Kette-Versuch ist der einzige Run. Neu aufgetauchtes #43 feuert unverändert."""
    _write_run(tmp_path, 42, status="fail")  # Run 0
    now = datetime.now(UTC)
    store = retry_chains.ChainStore(path=tmp_path / "chains.json")
    store.create("p", "l", "issue:o/r#42", next_attempt_at=now - timedelta(minutes=1), last_run_id="r1", now=now)
    assert store.claim("p", "l", "issue:o/r#42", "r2", now)  # Versuch läuft (kein Record)
    s, fired, _, trig = _mk_reconcile_test(monkeypatch, tmp_path, [_issue(42), _issue(43)])
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#43"]
    chain = store.get("p", "l", "issue:o/r#42")
    assert (chain.state, chain.claimed_run_id) == (retry_chains.ChainState.PENDING, "r2")  # unangetastet


def test_check_issue_reconcile_throttle_and_progress(monkeypatch, tmp_path):
    """max_per_poll begrenzt pro Poll; Folge-Polls arbeiten den Rest ab."""
    s, fired, _, trig = _mk_reconcile_test(monkeypatch, tmp_path, [_issue(n) for n in (1, 2, 3)], max_per_poll=1)
    for _ in range(4):
        s._check_issue("p", "l", {"github": "o/r"}, trig)
        if fired:
            _write_run(tmp_path, int(fired[-1].split("#")[1]))  # Run-Record wie engine.run_loop
    assert fired == ["on_issue:#1", "on_issue:#2", "on_issue:#3"]


def test_check_issue_reconcile_paginates_past_processed_page(monkeypatch, tmp_path):
    """Erste Seite komplett verarbeitet → nächste Seite wird gelesen; danach Stopp."""
    per = scheduler.GH_PER_PAGE
    for n in range(1, per + 1):
        _write_run(tmp_path, n)
    issues = [_issue(n) for n in range(1, per + 3)]
    s, fired, urls, trig = _mk_reconcile_test(monkeypatch, tmp_path, issues)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == [f"on_issue:#{per + 1}", f"on_issue:#{per + 2}"]
    assert len(urls) == 2


def test_check_issue_reconcile_label_filter_and_prs(monkeypatch, tmp_path):
    """Labels gehen (gequotet) an die API; PRs aus der Issues-API feuern nicht."""
    items = [{"number": 1, "title": "pr", "pull_request": {}}, _issue(2)]
    s, fired, urls, trig = _mk_reconcile_test(monkeypatch, tmp_path, items, labels=["ready for development"])
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#2"]
    assert "&labels=ready%20for%20development" in urls[0]


def test_check_issue_reconcile_invalid_max_per_poll_clamped(monkeypatch, tmp_path):
    s, fired, _, trig = _mk_reconcile_test(monkeypatch, tmp_path, [_issue(1), _issue(2)], max_per_poll=0)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#1"]


# ---------------------------------------------------------------- skip_blocked (ADR 0031)
def _dep_issue(num, total_blocked_by=0):
    return {**_issue(num), "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": total_blocked_by}}


def _mk_blocked_test(monkeypatch, tmp_path, issues, blockers: dict[int, list[tuple]]):
    """Reconcile-Test mit Fake-Blocker-API: `blockers` = {issue: [(nummer, state[, state_reason]), …]}."""
    s, fired, _, trig = _mk_reconcile_test(monkeypatch, tmp_path, issues, skip_blocked=True)
    dep_calls: list[int] = []

    def _blocked_by(repo, number, tok, kind="blocked_by"):
        dep_calls.append(number)
        return [
            {"number": b[0], "state": b[1], "state_reason": b[2] if len(b) > 2 else None}
            for b in blockers.get(number, [])
        ]

    monkeypatch.setattr(gitops, "_gh_dependencies", _blocked_by)
    return s, fired, trig, dep_calls


def test_skip_blocked_skips_open_blockers_without_using_quota(monkeypatch, tmp_path):
    """Blockiertes Issue startet nicht und belegt kein max_per_poll-Kontingent."""
    issues = [_dep_issue(1, 1), _dep_issue(2), _dep_issue(3)]
    s, fired, trig, _ = _mk_blocked_test(monkeypatch, tmp_path, issues, {1: [(9, "open")]})
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#2", "on_issue:#3"]


def test_skip_blocked_completed_blockers_do_not_block(monkeypatch, tmp_path):
    """Als completed geschlossener Blocker (PR gemergt) = erledigt; Alt-Issues ohne Grund ebenso."""
    blockers = {1: [(8, "closed", "completed"), (9, "closed")]}
    s, fired, trig, _ = _mk_blocked_test(monkeypatch, tmp_path, [_dep_issue(1, 2)], blockers)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#1"]


@pytest.mark.parametrize("reason", ["duplicate", "not_planned"])
def test_skip_blocked_duplicate_or_not_planned_still_blocks(monkeypatch, tmp_path, capsys, reason):
    """Die Vorarbeit fehlt weiter → Issue wartet; der Grund steht im Log."""
    s, fired, trig, _ = _mk_blocked_test(monkeypatch, tmp_path, [_dep_issue(1, 1)], {1: [(9, "closed", reason)]})
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == []
    assert f"#9 ({reason})" in capsys.readouterr().out


def test_skip_blocked_fires_after_blocker_closes(monkeypatch, tmp_path):
    """Kein Run-Record für das blockierte Issue → späterer Poll startet es."""
    blockers: dict[int, list[tuple]] = {1: [(9, "open")]}
    s, fired, trig, _ = _mk_blocked_test(monkeypatch, tmp_path, [_dep_issue(1, 1)], blockers)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == []
    blockers[1] = [(9, "closed", "completed")]
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#1"]


def test_skip_blocked_uses_summary_to_avoid_calls(monkeypatch, tmp_path):
    """Ohne Blocker (oder ohne Summary, z. B. alte API) kein Dependency-Call."""
    issues = [_dep_issue(1), _issue(2), _dep_issue(3, 1)]
    s, fired, trig, dep_calls = _mk_blocked_test(monkeypatch, tmp_path, issues, {})
    trig["max_per_poll"] = 5
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#1", "on_issue:#2", "on_issue:#3"]
    assert dep_calls == [3]


def test_skip_blocked_api_error_fails_closed(monkeypatch, tmp_path):
    """Dependency-API-Fehler → Poll bricht ab, nichts startet."""
    s, fired, trig, _ = _mk_blocked_test(monkeypatch, tmp_path, [_dep_issue(1, 1), _dep_issue(2)], {})

    def _boom(repo, number, tok, kind="blocked_by"):
        raise RuntimeError("HTTP 502")

    monkeypatch.setattr(gitops, "_gh_dependencies", _boom)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == []


def test_skip_blocked_off_ignores_dependencies(monkeypatch, tmp_path):
    """Ohne skip_blocked bleibt das Verhalten unverändert (kein Dependency-Call)."""
    s, fired, _, trig = _mk_reconcile_test(monkeypatch, tmp_path, [_dep_issue(1, 1)])
    monkeypatch.setattr(gitops, "_gh_dependencies", lambda *a: pytest.fail("kein Call erwartet"))
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#1"]


def test_skip_blocked_logs_only_on_change(monkeypatch, tmp_path, capsys):
    blockers: dict[int, list[tuple]] = {1: [(9, "open")]}
    s, _, trig, _ = _mk_blocked_test(monkeypatch, tmp_path, [_dep_issue(1, 2)], blockers)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert capsys.readouterr().out.count("wartet auf Blocker: #9\n") == 1
    blockers[1] = [(9, "open"), (10, "open")]
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert "#9, #10" in capsys.readouterr().out


def test_loop_run_triggers(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler.config, "RUNS_DIR", tmp_path)
    _write_run(tmp_path, 1, status="running")
    _write_run(tmp_path, 2, status="pass")
    (tmp_path / "p" / "broken.json").write_text("{nicht json")
    assert scheduler.loop_run_triggers("p", "l") == {"on_issue:#1", "on_issue:#2"}
    assert scheduler.loop_run_triggers("p", "l", scheduler.ACTIVE_RUN_STATUSES) == {"on_issue:#1"}
    assert scheduler.loop_run_triggers("missing", "l") == set()


def test_loop_run_triggers_unexpected_read_error_propagates(tmp_path, monkeypatch):
    """Unerwartete Read-Fehler (z. B. PermissionError) propagieren — ein
    unvollständiges Set würde die Reconcile-Idempotenz brechen (Doppel-Run)."""
    monkeypatch.setattr(scheduler.config, "RUNS_DIR", tmp_path)
    _write_run(tmp_path, 1, status="pass")
    (tmp_path / "p" / "boom.json").write_text("{}")

    real_read_text = Path.read_text

    def _boom(self, *args, **kwargs):
        if self.name == "boom.json":
            raise PermissionError("simulated")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(PermissionError):
        scheduler.loop_run_triggers("p", "l")


def test_loop_run_triggers_io_error_skipped(tmp_path, monkeypatch, capsys):
    """I/O-Fehler (Datei zwischen glob und read gelöscht) werden übersprungen."""
    monkeypatch.setattr(scheduler.config, "RUNS_DIR", tmp_path)
    _write_run(tmp_path, 1, status="pass")
    (tmp_path / "p" / "gone.json").write_text("{}")

    real_read_text = Path.read_text

    def _gone(self, *args, **kwargs):
        if self.name == "gone.json":
            raise FileNotFoundError("simulated")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _gone)
    assert scheduler.loop_run_triggers("p", "l") == {"on_issue:#1"}
    out = capsys.readouterr().out
    assert "gone.json" in out


def test_loop_run_triggers_wrong_toplevel_structure_skipped(tmp_path, monkeypatch, capsys):
    """Syntaktisch gültiges JSON mit falscher Top-Level-Struktur (z. B. [],
    null, String) wird übersprungen, statt mit AttributeError abzubrechen."""
    monkeypatch.setattr(scheduler.config, "RUNS_DIR", tmp_path)
    _write_run(tmp_path, 1, status="pass")
    (tmp_path / "p" / "list.json").write_text("[]")
    (tmp_path / "p" / "null.json").write_text("null")
    (tmp_path / "p" / "string.json").write_text('"boom"')
    assert scheduler.loop_run_triggers("p", "l") == {"on_issue:#1"}
    out = capsys.readouterr().out
    assert "list.json" in out
    assert "null.json" in out
    assert "string.json" in out


def test_check_issue_reconcile_unexpected_run_read_error_aborts_poll(monkeypatch, tmp_path, capsys):
    """Unerwarteter Fehler beim Lesen der Run-Records bricht den Reconcile-Poll
    ab (kein Feuer) — kein Doppel-Run, wenn ein Run-Record existiert."""
    s, fired, _, trig = _mk_reconcile_test(monkeypatch, tmp_path, [_issue(1), _issue(2)])

    def _boom(project, loop, statuses=None):
        raise PermissionError("simulated")

    monkeypatch.setattr(scheduler, "loop_run_triggers", _boom)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == []  # Poll abgebrochen, nichts gefeuert
    out = capsys.readouterr().out
    assert "on_issue Poll o/r fehlgeschlagen" in out


# ---------------------------------------------------------------- _check_stale_runs
def test_stale_runs_marks_old_running(runs_dir, projects_dir, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "p.fabrik.yaml").write_text(
        f'name: p\nroot: {root}\nloops:\n  l:\n    steps:\n      - {{type: shell, cmd: "true"}}\n'
    )
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    # Run vor 10h in 'running' → stale (Default-Limit 6h)
    old = time.time() - 10 * 3600
    from datetime import datetime

    started = datetime.fromtimestamp(old, tz=UTC).isoformat()
    (d / "r1.json").write_text(
        json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "running", "started": started})
    )
    sent = []
    monkeypatch.setattr(scheduler, "_notify", lambda spec, rec, **kw: sent.append(1) or [])
    s = _FakeSched()
    s._check_stale_runs()
    rec = json.loads((d / "r1.json").read_text())
    assert rec["status"] == "error"
    assert "stale" in rec["error"]
    assert rec["failure_kind"] == "interrupted"  # ADR 0035: Dead-Man's-Switch → interrupted


def test_stale_runs_write_is_atomic(runs_dir, projects_dir, monkeypatch):
    """Crash beim Rename: alter Record bleibt intakt (kein 0-Byte-/Halb-Record)."""
    from datetime import datetime

    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    started = datetime.fromtimestamp(time.time() - 10 * 3600, tz=UTC).isoformat()
    rec = {"id": "r1", "project": "p", "loop": "l", "status": "running", "started": started}
    (d / "r1.json").write_text(json.dumps(rec))

    def _boom(self, target):
        raise OSError("simulated crash")

    monkeypatch.setattr(Path, "replace", _boom)
    monkeypatch.setattr(scheduler, "_notify", lambda spec, rec, **kw: [])
    # I/O-Fehler beim Reap werden geschluckt (Run wird übersprungen), der alte Record bleibt intakt.
    _FakeSched()._check_stale_runs()
    assert json.loads((d / "r1.json").read_text()) == rec


def test_stale_runs_increments_reaped_metric(runs_dir, projects_dir, tmp_path, monkeypatch):
    """Dead-Man's-Switch inkrementiert die Reaper-KPI (Produktionsanbindung)."""
    from datetime import datetime

    from fabrik import metrics

    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "p.fabrik.yaml").write_text(
        f'name: p\nroot: {root}\nloops:\n  l:\n    steps:\n      - {{type: shell, cmd: "true"}}\n'
    )
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    old = time.time() - 10 * 3600
    started = datetime.fromtimestamp(old, tz=UTC).isoformat()
    (d / "r1.json").write_text(
        json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "running", "started": started})
    )
    monkeypatch.setattr(scheduler, "_notify", lambda spec, rec, **kw: [])
    before = metrics.FABRIK_REAPED_RUNS_TOTAL.value(reason="stale")
    s = _FakeSched()
    s._check_stale_runs()
    # Stale Run → error + genau eine Zählerhöhung.
    rec = json.loads((d / "r1.json").read_text())
    assert rec["status"] == "error"
    assert metrics.FABRIK_REAPED_RUNS_TOTAL.value(reason="stale") == before + 1


def test_stale_runs_ignores_fresh(runs_dir, monkeypatch):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    started = datetime.now(UTC).isoformat()
    (d / "r1.json").write_text(
        json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "running", "started": started})
    )
    s = _FakeSched()
    s._check_stale_runs()
    rec = json.loads((d / "r1.json").read_text())
    assert rec["status"] == "running"  # unverändert


def test_stale_runs_ignores_non_running(runs_dir, monkeypatch):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(json.dumps({"id": "r1", "project": "p", "loop": "l", "status": "pass"}))
    s = _FakeSched()
    s._check_stale_runs()
    rec = json.loads((d / "r1.json").read_text())
    assert rec["status"] == "pass"


def test_stale_runs_skips_corrupt(runs_dir, monkeypatch):
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bad.json").write_text("{not json")
    s = _FakeSched()
    s._check_stale_runs()  # darf nicht crashen


# ---------------------------------------------------------------- on_issue priority_labels (ADR 0021)
PRIO = ["p0", "p1", "p2", "p3"]


def _prio_issue(num, *labels):
    return {**_issue(num), "labels": [{"name": x} for x in labels]}


def test_priority_rank():
    assert scheduler._priority_rank(_prio_issue(1, "fabrik", "p2"), PRIO) == 2
    assert scheduler._priority_rank(_prio_issue(1, "p3", "p1"), PRIO) == 1  # höchste Stufe gewinnt
    assert scheduler._priority_rank(_prio_issue(1, "fabrik"), PRIO) == len(PRIO)
    assert scheduler._priority_rank({"number": 1, "labels": None}, PRIO) == len(PRIO)
    assert scheduler._priority_rank({"number": 1, "labels": ["p0"]}, PRIO) == len(PRIO)  # kein Label-Objekt


def test_check_issue_reconcile_priority_before_age(monkeypatch, tmp_path):
    """Höhere Priorität vor Alter; gleiche Stufe älteste zuerst; unlabelt zuletzt."""
    issues = [_prio_issue(1), _prio_issue(2, "p3"), _prio_issue(3, "p1"), _prio_issue(4, "p0"), _prio_issue(5, "p1")]
    s, fired, _, trig = _mk_reconcile_test(monkeypatch, tmp_path, issues, max_per_poll=3, priority_labels=PRIO)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#4", "on_issue:#3", "on_issue:#5"]


def test_check_issue_reconcile_priority_reads_all_pages(monkeypatch, tmp_path):
    """Mit Prioritäten kein Lazy-Abbruch: ein p0 auf Seite 2 schlägt ältere unlabelte Issues."""
    per = scheduler.GH_PER_PAGE
    issues = [_prio_issue(n) for n in range(1, per + 1)] + [_prio_issue(per + 1, "p0")]
    s, fired, urls, trig = _mk_reconcile_test(monkeypatch, tmp_path, issues, max_per_poll=1, priority_labels=PRIO)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == [f"on_issue:#{per + 1}"]
    assert len(urls) == 2


def test_check_issue_reconcile_priority_skips_processed(monkeypatch, tmp_path):
    _write_run(tmp_path, 2, status="error")
    issues = [_prio_issue(1, "p2"), _prio_issue(2, "p0")]
    s, fired, _, trig = _mk_reconcile_test(monkeypatch, tmp_path, issues, priority_labels=PRIO)
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert fired == ["on_issue:#1"]


def test_check_issue_event_mode_priority_order(monkeypatch):
    """Event-Modus: mehrere neue Issues in einem Poll feuern nach Priorität."""
    s, fired, trig = _mk_issue_test(monkeypatch, [_prio_issue(1)])
    trig["priority_labels"] = PRIO
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    new = [_prio_issue(3, "p2"), _prio_issue(2, "p0"), _prio_issue(1)]
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t, payload=None: list(new))
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert [t for t, _ in fired] == ["on_issue:#2", "on_issue:#3"]


def test_check_issue_event_mode_priority_same_tier_oldest_first(monkeypatch):
    """Event-Modus liest neueste zuerst — innerhalb einer Stufe trotzdem älteste zuerst."""
    s, fired, trig = _mk_issue_test(monkeypatch, [_prio_issue(1)])
    trig["priority_labels"] = PRIO
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    new = [_prio_issue(4, "p1"), _prio_issue(3), _prio_issue(2, "p1"), _prio_issue(1)]
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t, payload=None: list(new))
    s._check_issue("p", "l", {"github": "o/r"}, trig)
    assert [t for t, _ in fired] == ["on_issue:#2", "on_issue:#4", "on_issue:#3"]


# ---------------------------------------------------------------- reap_orphaned_runs
def test_reap_orphaned_runs_restart(runs_dir, monkeypatch):
    """running → error (Grund+finished), needs_approval unverändert, kein Alert,
    und r1 zählt danach nicht mehr als aktiv (loop_run_triggers)."""
    from fabrik import runs

    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": "r1", "project": "p", "loop": "l", "trigger": "on_pr:#1", "status": "running"}
    appr = {"id": "r2", "project": "p", "loop": "l", "trigger": "on_pr:#2", "status": "needs_approval"}
    (d / "r1.json").write_text(json.dumps(rec))
    (d / "r2.json").write_text(json.dumps(appr))
    (d / "broken.json").write_text("{nicht json")
    sent = []
    monkeypatch.setattr(scheduler, "_notify", lambda spec, r, **kw: sent.append(1) or [])
    assert runs.reap_orphaned_runs() == ["r1"]
    got = json.loads((d / "r1.json").read_text())
    assert got["status"] == "error"
    assert got["error"] == "abgebrochen: fabrik serve neu gestartet"
    assert got["finished"]
    assert got["failure_kind"] == "interrupted"  # ADR 0035: Reaper → interrupted
    assert json.loads((d / "r2.json").read_text()) == appr
    assert sent == []
    active = scheduler.loop_run_triggers("p", "l", scheduler.ACTIVE_RUN_STATUSES)
    assert active == {"on_pr:#2"}


def test_reap_orphaned_runs_write_failure_keeps_record(runs_dir, monkeypatch):
    """Schlägt das atomare Schreiben fehl, bleibt der alte Record intakt und wird übersprungen."""
    from fabrik import runs

    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": "r1", "project": "p", "loop": "l", "status": "running"}
    (d / "r1.json").write_text(json.dumps(rec))

    def boom(self, target):
        raise OSError("rename failed")

    monkeypatch.setattr(Path, "replace", boom)
    assert runs.reap_orphaned_runs() == []
    assert json.loads((d / "r1.json").read_text()) == rec


def test_reap_orphaned_runs_no_runs_dir(tmp_path, monkeypatch):
    from fabrik import config, runs

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "missing")
    assert runs.reap_orphaned_runs() == []


def test_cron_matches_zero_step_is_invalid():
    from datetime import datetime

    from fabrik.scheduler import cron_matches

    assert cron_matches("*/0 * * * *", datetime(2026, 1, 1, 12, 0)) is False


def test_trigger_vars_helpers():
    from fabrik.scheduler import issue_vars, pr_vars

    assert issue_vars({"number": 1, "title": "t", "body": None}) == {
        "issue_number": 1,
        "issue_title": "t",
        "issue_body": "",
    }
    v = pr_vars({"number": 7, "title": "t", "html_url": "u", "head": {"ref": "b", "sha": "s"}})
    assert (v["pr_number"], v["pr_head_ref"], v["pr_head_sha"], v["pr_body"]) == (7, "b", "s", "")


def test_cron_chain_pending_tolerates_corrupt_store(monkeypatch, tmp_path):
    from fabrik import retry_chains
    from fabrik.scheduler import _cron_chain_pending

    path = tmp_path / "retry-chains.json"
    path.write_text("{kaputt")
    store = retry_chains.ChainStore(path)
    monkeypatch.setattr(retry_chains, "ChainStore", lambda: store)
    spec = {"loops": {"l": {"triggers": [{"type": "cron", "cron": "* * * * *"}]}}}
    assert _cron_chain_pending("p", "l", spec) is False
