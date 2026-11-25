"""Run-Slots (ADR 0028): max_parallel, Dedup, parallele Loops ohne Projekt-Lock."""

from __future__ import annotations

import json
import threading
import time

import pytest

from fabrikant import config, engine, runs


@pytest.fixture
def limit(monkeypatch):
    """max_parallel pro Test steuerbar (ohne Spec-Dateien); eigener Pool, unabhängig von FABRIKANT_WORKERS."""
    box = {"n": 2}
    pool = engine._WorkerPool(8)
    monkeypatch.setattr(engine, "_run_pool", pool)
    monkeypatch.setattr(runs, "max_parallel_for", lambda _p: box["n"])
    yield box
    pool.shutdown()


def test_claim_respects_limit(limit):
    a = runs.claim_run("slotp1", "a")
    b = runs.claim_run("slotp1", "b")
    assert a and b
    assert runs.claim_run("slotp1", "c") is None  # über dem Limit: verworfen
    a.release()
    c = runs.claim_run("slotp1", "c")
    assert c is not None
    b.release()
    c.release()


def test_claim_release_idempotent(limit):
    limit["n"] = 1
    a = runs.claim_run("slotp2", "a")
    a.release()
    a.release()
    b = runs.claim_run("slotp2", "b")
    assert b is not None  # Doppel-Release hat den Zähler nicht verfälscht
    assert runs.claim_run("slotp2", "c") is None
    b.release()


def test_claim_limits_are_per_project(limit):
    limit["n"] = 1
    a = runs.claim_run("slotp3", "a")
    b = runs.claim_run("slotp3-other", "a")
    assert a and b
    a.release()
    b.release()


def test_dedup_same_loop_and_issue(limit):
    limit["n"] = 5
    a = runs.claim_run("slotp4", "develop", {"issue_number": 7})
    assert runs.claim_run("slotp4", "develop", {"issue_number": 7}) is None
    other_issue = runs.claim_run("slotp4", "develop", {"issue_number": 8})
    other_loop = runs.claim_run("slotp4", "refine", {"issue_number": 7})
    assert other_issue and other_loop
    a.release()
    again = runs.claim_run("slotp4", "develop", {"issue_number": 7})
    assert again is not None
    for c in (again, other_issue, other_loop):
        c.release()


def test_dedup_pr_and_plain_loop(limit):
    limit["n"] = 5
    a = runs.claim_run("slotp5", "review-pr", {"pr_number": 3})
    assert runs.claim_run("slotp5", "review-pr", {"pr_number": "3"}) is None  # int/str gleich
    cron = runs.claim_run("slotp5", "cron-loop")
    assert runs.claim_run("slotp5", "cron-loop") is None  # ohne Bezug: ein Run pro Loop
    a.release()
    cron.release()


def test_blocking_claim_waits_for_slot(limit):
    limit["n"] = 1
    a = runs.claim_run("slotp6", "a")
    got = []

    def waiter():
        got.append(runs.claim_run("slotp6", "b", blocking=True))

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    assert not got  # wartet
    a.release()
    t.join(5)
    assert got and got[0] is not None
    got[0].release()


def test_blocking_claim_waits_for_dedup_key(limit):
    a = runs.claim_run("slotp7", "a", {"issue_number": 1})
    got = []
    t = threading.Thread(target=lambda: got.append(runs.claim_run("slotp7", "a", {"issue_number": 1}, blocking=True)))
    t.start()
    time.sleep(0.1)
    assert not got  # wartet auf den aktiven Run, statt verworfen zu werden
    a.release()
    t.join(5)
    assert got and got[0] is not None
    got[0].release()


def _write_spec(projects_dir, tmp_path, name, extra):
    root = tmp_path / f"r-{name}"
    root.mkdir()
    body = "loops:\n  l:\n    steps:\n      - {type: report, text: x}\n"
    (projects_dir / f"{name}.fabrikant.yaml").write_text(f"name: {name}\nroot: {root}\n{extra}{body}")


def test_max_parallel_for_default_and_spec(projects_dir, tmp_path):
    assert runs.max_parallel_for("unbekannt") == config.DEFAULT_MAX_PARALLEL
    _write_spec(projects_dir, tmp_path, "mp", "repo: {branch: main}\nmax_parallel: 5\n")
    assert runs.max_parallel_for("mp") == 5
    _write_spec(projects_dir, tmp_path, "mp_default", "repo: {branch: main}\n")
    assert runs.max_parallel_for("mp_default") == config.DEFAULT_MAX_PARALLEL


def test_repoless_spec_is_forced_to_one_parallel_run(projects_dir, tmp_path):
    """ADR 0028: ohne `repo:` keine Isolation → max_parallel fest 1, auch bei höherem Spec-Wert."""
    _write_spec(projects_dir, tmp_path, "norepo", "max_parallel: 5\n")
    assert runs.max_parallel_for("norepo") == 1
    first = runs.claim_run("norepo", "l", {"issue_number": 1})
    assert first is not None
    assert runs.claim_run("norepo", "l", {"issue_number": 2}) is None  # sonst Dedup-fremd, aber Limit 1
    first.release()
    again = runs.claim_run("norepo", "l", {"issue_number": 2})
    assert again is not None
    again.release()


def test_max_parallel_spec_validation(tmp_path):
    from fabrikant.specs import _validate_spec

    spec = {"root": str(tmp_path), "loops": {"l": {"steps": [{"type": "report", "text": "x"}]}}}
    _validate_spec({**spec, "max_parallel": 2})
    for bad in (0, -1, "2", True, 1.5):
        with pytest.raises(ValueError, match="max_parallel"):
            _validate_spec({**spec, "max_parallel": bad})


def test_long_develop_does_not_block_other_loops(monkeypatch, limit):
    """AC: ein laufender develop-Run blockiert review-pr/pr-reply/refine nicht."""
    limit["n"] = 4
    release_develop = threading.Event()
    started: dict[str, threading.Event] = {n: threading.Event() for n in ("develop", "review-pr", "pr-reply", "refine")}

    def fake_run_loop(project, loop, **k):
        started[loop].set()
        if loop == "develop":
            release_develop.wait(10)

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    engine._safe_run("parproj", "develop", "on_issue:#1", {"issue_number": 1})
    assert started["develop"].wait(5)
    engine._safe_run("parproj", "review-pr", "cron")
    engine._safe_run("parproj", "pr-reply", "cron")
    engine._safe_run("parproj", "refine", "on_issue:#2", {"issue_number": 2})
    for name in ("review-pr", "pr-reply", "refine"):
        assert started[name].wait(5), f"{name} wurde vom develop-Run blockiert"
    release_develop.set()


def test_safe_run_drops_over_limit_and_releases(monkeypatch, limit):
    limit["n"] = 1
    gate = threading.Event()
    ran: list[str] = []

    def fake_run_loop(project, loop, **k):
        ran.append(loop)
        gate.wait(10)

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    engine._safe_run("dropproj", "a", "cron")
    deadline = time.monotonic() + 5
    while not ran and time.monotonic() < deadline:
        time.sleep(0.01)
    engine._safe_run("dropproj", "b", "cron")  # über dem Limit → verworfen
    time.sleep(0.2)
    assert ran == ["a"]
    gate.set()
    # Slot wird frei (auch nach Exception)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        c = runs.claim_run("dropproj", "c")
        if c:
            c.release()
            return
        time.sleep(0.01)
    pytest.fail("Slot nach Run nicht freigegeben")


def test_safe_run_releases_slot_on_exception(monkeypatch, limit):
    limit["n"] = 1
    done = threading.Event()

    def boom(*a, **k):
        done.set()
        raise RuntimeError("kaputt")

    monkeypatch.setattr(engine, "run_loop", boom)
    engine._safe_run("boomproj", "a", "cron")
    assert done.wait(5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        c = runs.claim_run("boomproj", "b")
        if c:
            c.release()
            return
        time.sleep(0.01)
    pytest.fail("Slot nach fehlgeschlagenem Run nicht freigegeben")


def test_safe_run_skips_if_record_exists(runs_dir, monkeypatch, limit):
    """AC: Trigger mit Issue/PR-Bezug wird verworfen, wenn ein Run-Record für
    denselben Trigger bereits auf Disk liegt. Die Work kann lange im
    Pool-Queue gesessen haben, während ein früherer Run für denselben Trigger
    bereits abgeschlossen und sein Record geschrieben wurde — die Poll-Zeit-
    Dedup reicht dann nicht (Doppel-Runs, Issue #226, Okt 2026)."""
    limit["n"] = 4
    d = runs_dir / "dedupproj"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(
        json.dumps({"id": "r1", "project": "dedupproj", "loop": "develop", "trigger": "on_issue:#1", "status": "pass"})
    )
    ran: list[str] = []

    def fake_run_loop(project, loop, **k):
        ran.append(loop)

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    engine._safe_run("dedupproj", "develop", "on_issue:#1", {"issue_number": 1})
    time.sleep(0.3)
    assert ran == [], "Run trotz bestehendem Record gestartet"


def test_safe_run_runs_if_no_record(runs_dir, monkeypatch, limit):
    """AC: Ohne bestehenden Record wird der Trigger-Run gestartet."""
    limit["n"] = 4
    done = threading.Event()
    ran: list[str] = []

    def fake_run_loop(project, loop, **k):
        ran.append(loop)
        done.set()

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    engine._safe_run("dedupproj2", "develop", "on_issue:#1", {"issue_number": 1})
    assert done.wait(5)
    assert ran == ["develop"]


def _paused(project: str, loop: str, variables: dict, run_id: str = "r1", status: str = "needs_approval") -> None:
    d = config.RUNS_DIR / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps({"id": run_id, "loop": loop, "status": status, "vars": variables}))


def test_paused_run_blocks_second_claim(limit):
    _paused("pausep1", "approval", {"pr_number": 5})
    assert runs.claim_run("pausep1", "approval", {"pr_number": 5}) is None
    other = runs.claim_run("pausep1", "approval", {"pr_number": 6})  # anderer PR
    other_loop = runs.claim_run("pausep1", "review", {"pr_number": 5})  # anderer Loop
    assert other and other_loop
    other.release()
    other_loop.release()


def test_paused_run_does_not_block_resume(limit):
    _paused("pausep2", "approval", {"pr_number": 5})
    held = runs.claim_run("pausep2", "approval", {"pr_number": 5}, blocking=True)
    assert held is not None
    held.release()


def test_paused_run_does_not_block_own_resume_claim(limit):
    """Nicht-blockierender Claim mit `exclude_run_id` (CLI-/Chat-Resume): der
    eigene pausierte Record blockiert nicht, ein fremder tut es schon."""
    _paused("pausep4", "approval", {"pr_number": 5}, run_id="r1")
    own = runs.claim_run("pausep4", "approval", {"pr_number": 5}, exclude_run_id="r1")
    assert own is not None
    own.release()
    _paused("pausep4", "approval", {"pr_number": 6}, run_id="r2")
    assert runs.claim_run("pausep4", "approval", {"pr_number": 6}, exclude_run_id="r1") is None


def test_finished_or_unreferenced_records_do_not_block(limit):
    _paused("pausep3", "approval", {"pr_number": 5}, status="done")
    _paused("pausep3", "cron-loop", {}, run_id="r2")
    for loop, v in (("approval", {"pr_number": 5}), ("cron-loop", None)):
        c = runs.claim_run("pausep3", loop, v)
        assert c is not None
        c.release()


def test_safe_run_dedupes_queued_trigger(monkeypatch, limit):
    """Pool ausgelastet: zweiter Poll für denselben Trigger reiht nicht erneut ein."""
    pool = engine._WorkerPool(1)
    monkeypatch.setattr(engine, "_run_pool", pool)
    gate = threading.Event()
    ran: list[str] = []
    monkeypatch.setattr(engine, "run_loop", lambda p, loop, **k: (ran.append(k["trigger"]), gate.wait(10)))
    try:
        engine._safe_run("qp", "blocker", "cron")
        deadline = time.monotonic() + 5
        while not ran and time.monotonic() < deadline:
            time.sleep(0.01)
        engine._safe_run("qp", "approval", "on_pr:#1", {"pr_number": 1})
        engine._safe_run("qp", "approval", "on_pr:#1", {"pr_number": 1})
        assert ("qp", "approval", "on_pr:#1") in engine._queued_triggers
        gate.set()
        deadline = time.monotonic() + 5
        while "on_pr:#1" not in ran and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        assert ran.count("on_pr:#1") == 1
        assert ("qp", "approval", "on_pr:#1") not in engine._queued_triggers  # nach Start wieder frei
        engine._safe_run("qp", "approval", "on_pr:#1", {"pr_number": 1})  # erneut einreihbar
        time.sleep(0.3)
        assert ran.count("on_pr:#1") == 2
    finally:
        gate.set()
        pool.shutdown()


def test_safe_run_skips_when_paused_while_queued(monkeypatch, limit):
    """AC: Ein im Pool-Queue wartender Trigger-Run startet nicht mehr, wenn der
    Loop inzwischen paused/disabled ist (ADR 0019) — der Scheduler prüft nur
    beim Einreihen."""
    pool = engine._WorkerPool(1)
    monkeypatch.setattr(engine, "_run_pool", pool)
    gate = threading.Event()
    ran: list[str] = []
    monkeypatch.setattr(engine, "run_loop", lambda p, loop, **k: (ran.append(loop), gate.wait(10)))
    try:
        engine._safe_run("pausedproj", "blocker", "cron")
        deadline = time.monotonic() + 5
        while not ran and time.monotonic() < deadline:
            time.sleep(0.01)
        # Loop wird pausiert, während der develop-Run im Pool-Queue wartet
        monkeypatch.setattr(engine.pause, "effective_state", lambda _p, _l: "paused")
        engine._safe_run("pausedproj", "develop", "cron")
        gate.set()
        time.sleep(0.3)
        assert ran == ["blocker"], "Trigger-Run trotz Pause gestartet"
    finally:
        gate.set()
        pool.shutdown()


def test_safe_run_sets_done_after_run_and_when_dropped(monkeypatch, limit):
    """`done` meldet dem Scheduler das Ende eines Trigger-Runs — auch wenn er verworfen wird."""
    limit["n"] = 1
    started, gate = threading.Event(), threading.Event()

    def fake_run_loop(project, loop, **k):
        started.set()
        gate.wait(10)

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    ran = threading.Event()
    engine._safe_run("doneproj", "a", "cron", done=ran)
    assert started.wait(5)  # "a" hält den einzigen Slot …
    dropped = threading.Event()
    engine._safe_run("doneproj", "b", "cron", done=dropped)  # … also wird "b" verworfen
    assert dropped.wait(5)
    assert not ran.is_set()
    gate.set()
    assert ran.wait(5)


def test_safe_run_sets_done_when_already_queued(monkeypatch):
    """Ist derselbe Trigger schon eingereiht, gibt es nichts abzuwarten: `done` sofort gesetzt."""
    monkeypatch.setitem(engine._queued_triggers, ("qproj", "l", "cron"), time.time())
    done = threading.Event()
    engine._safe_run("qproj", "l", "cron", done=done)
    assert done.is_set()


def _callbacks() -> tuple[dict[str, threading.Event], dict]:
    events = {"claimed": threading.Event(), "rejected": threading.Event()}
    return events, {"on_claimed": events["claimed"].set, "on_rejected": events["rejected"].set}


def test_safe_run_calls_on_claimed_before_run(monkeypatch, limit):
    events, cbs = _callbacks()
    order: list[str] = []

    def fake_run_loop(*a, **k):
        order.append("after-claimed" if events["claimed"].is_set() else "before-claimed")

    monkeypatch.setattr(engine, "run_loop", fake_run_loop)
    engine._safe_run("cbproj", "a", "on_slack:C1:1.0", {}, **cbs)
    assert events["claimed"].wait(5)
    deadline = time.monotonic() + 5
    while not order and time.monotonic() < deadline:
        time.sleep(0.01)
    assert order == ["after-claimed"] and not events["rejected"].is_set()


def test_safe_run_calls_on_rejected_over_limit(monkeypatch, limit):
    limit["n"] = 0
    events, cbs = _callbacks()
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: pytest.fail("kein Run über dem Limit"))
    engine._safe_run("cbproj2", "a", "on_slack:C1:1.0", {}, **cbs)
    assert events["rejected"].wait(5) and not events["claimed"].is_set()


def test_safe_run_calls_on_rejected_when_paused(monkeypatch, limit):
    from fabrikant import pause

    events, cbs = _callbacks()
    monkeypatch.setattr(pause, "effective_state", lambda *_a: "paused")
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: pytest.fail("kein Run bei Pause"))
    engine._safe_run("cbproj3", "a", "on_slack:C1:1.0", {}, **cbs)
    assert events["rejected"].wait(5) and not events["claimed"].is_set()


def test_safe_run_skips_slack_trigger_with_record(runs_dir, monkeypatch, limit):
    """on_slack-Trigger identifiziert eine einzelne Nachricht: bestehender Record → verworfen."""
    d = runs_dir / "slackproj"
    d.mkdir(parents=True, exist_ok=True)
    trigger = "on_slack:C1:200.0"
    (d / "r1.json").write_text(
        json.dumps({"id": "r1", "project": "slackproj", "loop": "triage", "trigger": trigger, "status": "pass"})
    )
    events, cbs = _callbacks()
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: pytest.fail("Doppel-Run"))
    engine._safe_run("slackproj", "triage", trigger, {}, **cbs)
    assert events["rejected"].wait(5)


def test_safe_run_callback_error_does_not_stop_run(monkeypatch, limit, capsys):
    done = threading.Event()
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: done.set())

    def boom():
        raise RuntimeError("geheim")

    engine._safe_run("cbproj4", "a", "cron", on_claimed=boom)
    assert done.wait(5)
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "geheim" not in out


def test_paused_run_blocks_exclusive_group_mates(limit, monkeypatch):
    """An approval gate holds the issue/PR for the whole `exclusive_group`: a group-mate must not run on a
    head the paused run will change once approved. Other PRs, loops outside the group and the resume
    of the paused run itself still go through."""
    from fabrikant import specs

    spec = {
        "repo": {"url": "x"},
        "max_parallel": 5,
        "loops": {"review": {"exclusive_group": "pr"}, "reply": {"exclusive_group": "pr"}, "other": {}},
    }
    monkeypatch.setattr(specs, "load_projects", lambda: {"pausegrp": spec})
    limit["n"] = 5
    _paused("pausegrp", "reply", {"pr_number": 5}, run_id="r1")
    assert runs.claim_run("pausegrp", "review", {"pr_number": 5}) is None  # group-mate, same PR
    claims = [
        runs.claim_run("pausegrp", "review", {"pr_number": 6}),  # other PR
        runs.claim_run("pausegrp", "other", {"pr_number": 5}),  # loop without a group
        runs.claim_run("pausegrp", "reply", {"pr_number": 5}, exclude_run_id="r1"),  # its own resume
    ]
    assert all(claims)
    for c in claims:
        c.release()


def _group_spec(monkeypatch, project: str) -> None:
    from fabrikant import specs

    spec = {
        "repo": {"url": "x"},
        "max_parallel": 5,
        "loops": {"review": {"exclusive_group": "pr"}, "reply": {"exclusive_group": "pr"}, "other": {}},
    }
    monkeypatch.setattr(specs, "load_projects", lambda: {project: spec})


def test_queued_run_waits_for_paused_group_mate(limit, monkeypatch):
    """Review point PR #20: a queued (blocking) run waited for the running group-mate, but started as soon
    as the mate released its claim to pause for approval. It must wait until the approval is decided."""
    _group_spec(monkeypatch, "pausegrq")
    monkeypatch.setattr(runs, "_PAUSED_RECHECK_S", 0.05)
    limit["n"] = 5
    review = runs.claim_run("pausegrq", "review", {"pr_number": 5})
    assert review is not None
    got: list = []
    waiter = threading.Thread(
        target=lambda: got.append(runs.claim_run("pausegrq", "reply", {"pr_number": 5}, blocking=True))
    )
    waiter.start()
    waiter.join(0.2)
    assert waiter.is_alive() and not got  # waits for the running review
    _paused("pausegrq", "review", {"pr_number": 5}, run_id="r1")  # review pauses for approval …
    review.release()  # … and releases its claim
    waiter.join(0.3)
    assert waiter.is_alive() and not got  # the approval gate still holds the PR
    # While it waits on the approval, it must not hold back trigger runs of the project (ADR 0015).
    trigger = runs.claim_run("pausegrq", "other", {"pr_number": 9})
    assert trigger is not None
    trigger.release()
    _paused("pausegrq", "review", {"pr_number": 5}, run_id="r1", status="rejected")  # decided
    waiter.join(5)
    assert not waiter.is_alive() and got[0] is not None
    got[0].release()


def test_queued_resume_is_not_blocked_by_its_own_record(limit, monkeypatch):
    """Blocking claim with `exclude_run_id`: the run being resumed does not hold up its own claim; a
    different paused group-mate still would."""
    _group_spec(monkeypatch, "pausegrr")
    limit["n"] = 5
    _paused("pausegrr", "review", {"pr_number": 5}, run_id="r1")
    own = runs.claim_run("pausegrr", "reply", {"pr_number": 5}, blocking=True, exclude_run_id="r1")
    assert own is not None
    own.release()
    # Same loop: a manual run is not held by its own loop's paused run (ADR 0043, unchanged).
    same = runs.claim_run("pausegrr", "review", {"pr_number": 5}, blocking=True)
    assert same is not None
    same.release()
