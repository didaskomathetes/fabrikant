"""Scheduler: on_file debounce + cron matching + on_pr (reconcile)."""

import datetime as dt
import os

import fabrik


# ---------------------------------------------------------------- debounce
class _FakeScheduler(fabrik.Scheduler):
    """Echter Scheduler, Thread nie gestartet: Tests rufen _check_file direkt auf."""

    def __init__(self):
        super().__init__()
        self.fired = []


def test_debounce_full_cycle(tmp_path, monkeypatch):
    sched = _FakeScheduler()
    fired = []
    monkeypatch.setattr(fabrik.scheduler, "_safe_run", lambda n, lname, t, **kw: fired.append(t))

    f = tmp_path / "a.txt"
    f.write_text("v1")
    # mtime explizit setzen: Filesystem-Granularität (oft 1 s) darf das
    # "Datei geändert"-Erkennen nicht vom Timing abhängen machen.
    t0 = 1_700_000_000.0
    os.utime(f, (t0, t0))
    spec = {"root": str(tmp_path)}
    trig = {"glob": "*.txt", "debounce": 1.0}
    base = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)

    # poll 1: init, no fire
    sched._check_file("p", "l", spec, trig, base)
    assert fired == []
    # poll 2 (0.5s later, < debounce): no fire
    sched._check_file("p", "l", spec, trig, base + dt.timedelta(seconds=0.5))
    assert fired == []
    # poll 3 (2s after init, > debounce, mtime stable): fire once
    sched._check_file("p", "l", spec, trig, base + dt.timedelta(seconds=2))
    assert len(fired) == 1
    # poll 4 (3s, still stable): must NOT fire again (once per mtime)
    sched._check_file("p", "l", spec, trig, base + dt.timedelta(seconds=3))
    assert len(fired) == 1
    # modify file -> new (explicitly different) mtime -> window resets, no immediate fire
    f.write_text("v2")
    os.utime(f, (t0 + 5, t0 + 5))
    sched._check_file("p", "l", spec, trig, base + dt.timedelta(seconds=4))
    assert len(fired) == 1
    # after stable window again -> fire
    sched._check_file("p", "l", spec, trig, base + dt.timedelta(seconds=6))
    assert len(fired) == 2


def test_no_match_keeps_no_state(tmp_path):
    sched = _FakeScheduler()
    sched._check_file("p", "l", {"root": str(tmp_path)}, {"glob": "*.txt"}, dt.datetime(2024, 1, 1, tzinfo=dt.UTC))
    assert sched._seen_files == {}


def test_epoch_mtime_counts_as_match(tmp_path):
    """mtime 0.0 ist ein gültiger Treffer, kein „nichts gefunden“ (Issue #210)."""
    sched = _FakeScheduler()
    f = tmp_path / "a.txt"
    f.write_text("v1")
    os.utime(f, (0, 0))
    sched._check_file("p", "l", {"root": str(tmp_path)}, {"glob": "*.txt"}, dt.datetime(2024, 1, 1, tzinfo=dt.UTC))
    assert sched._seen_files["p:l:*.txt"]["mtime"] == 0.0


# ---------------------------------------------------------------- cron
def test_cron_every_minute():
    d = dt.datetime(2024, 5, 15, 10, 30)
    assert fabrik.cron_matches("* * * * *", d) is True


def test_cron_specific_time():
    d = dt.datetime(2024, 5, 15, 10, 30)
    assert fabrik.cron_matches("30 10 * * *", d) is True
    assert fabrik.cron_matches("31 10 * * *", d) is False


def test_cron_step_values():
    d = dt.datetime(2024, 5, 15, 10, 30)
    assert fabrik.cron_matches("*/15 * * * *", d) is True
    assert fabrik.cron_matches("*/20 * * * *", d) is False


def test_cron_range():
    d = dt.datetime(2024, 5, 15, 10, 30)
    assert fabrik.cron_matches("30 9-12 * * *", d) is True
    assert fabrik.cron_matches("30 11-12 * * *", d) is False


def test_cron_day_of_week():
    # 2024-05-15 is a Wednesday (weekday()=2 -> cron dow 3)
    d = dt.datetime(2024, 5, 15)
    assert fabrik.cron_matches("0 0 * * 3", d) is True
    assert fabrik.cron_matches("0 0 * * 4", d) is False


def test_cron_invalid_expression():
    d = dt.datetime(2024, 5, 15, 10, 30)
    assert fabrik.cron_matches("* * * *", d) is False  # only 4 fields
    assert fabrik.cron_matches("not a cron", d) is False


# ---------------------------------------------------------------- on_pr
class _FakePrScheduler(fabrik.Scheduler):
    """Echter Scheduler, Thread nie gestartet: Tests rufen _check_pr direkt auf."""


def _pr(number, updated_at, title="t", head="feature/x"):
    return {"number": number, "updated_at": updated_at, "title": title, "head": {"ref": head}}


def _mk_pr_test(monkeypatch, prs):
    sched = _FakePrScheduler()
    fired = []
    monkeypatch.setattr(fabrik.scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append((t, kw)))
    monkeypatch.setattr(fabrik.scheduler, "_gh_token_safe", lambda: "tok")
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: list(prs))
    trig = {"repo": "o/r", "poll": 0}  # poll=0 → kein Interval-Wait
    return sched, fired, trig


def test_on_pr_first_poll_initializes_no_fire(monkeypatch):
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert fired == []  # initialisieren, nicht feuern
    assert sched._pr_state[("p", "l")]["seen"] == {1: ("open", "t1")}


def test_on_pr_new_pr_fires(monkeypatch):
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    pr2 = {**_pr(2, "t2"), "head": {"ref": "feature/x", "sha": "abc"}}
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr(1, "t1"), pr2])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(fired) == 1
    assert fired[0][0] == "on_pr:#2"  # Event-Modus: kein @sha, auch wenn head.sha da ist
    assert fired[0][1]["initial_vars"]["pr_number"] == 2
    assert fired[0][1]["initial_vars"]["pr_head_ref"] == "feature/x"


def test_on_pr_vars_include_title_and_body(monkeypatch):
    """Payload → Variablen: pr_number, pr_title, pr_url, pr_head_ref, pr_head_sha, pr_body im ctx."""
    pr = {
        "number": 7,
        "updated_at": "t1",
        "title": "Mein PR",
        "body": "PR-Body-Text",
        "html_url": "https://github.com/o/r/pull/7",
        "head": {"ref": "feature/y", "sha": "abc123"},
    }
    sched, fired, trig = _mk_pr_test(monkeypatch, [pr])
    trig["reconcile"] = True  # Erst-Poll feuert
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(fired) == 1
    vars_ = fired[0][1]["initial_vars"]
    assert vars_ == {
        "pr_number": 7,
        "pr_title": "Mein PR",
        "pr_url": "https://github.com/o/r/pull/7",
        "pr_head_ref": "feature/y",
        "pr_head_sha": "abc123",
        "pr_body": "PR-Body-Text",
    }


def test_on_pr_body_defaults_to_empty(monkeypatch):
    """PR ohne Body → pr_body = leerer String (kein None im ctx)."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    trig["reconcile"] = True
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert fired[0][1]["initial_vars"]["pr_body"] == ""


def test_on_pr_updated_pr_fires(monkeypatch):
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr(1, "t2")])  # updated_at geändert
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(fired) == 1
    assert fired[0][0] == "on_pr:#1"


def test_on_pr_window_dropout_and_return_unchanged_no_fire(monkeypatch):
    """PR fällt aus dem begrenzten Fenster und kehrt unverändert zurück → kein Fire."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1"), _pr(2, "t2")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr(2, "t2")])  # PR 1 aus Fenster
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr(1, "t1"), _pr(2, "t2")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert fired == []


def test_on_pr_unchanged_no_fire(monkeypatch):
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # unverändert
    assert fired == []


def test_on_pr_respects_poll_interval(monkeypatch):
    sched = _FakePrScheduler()
    fired = []
    monkeypatch.setattr(fabrik.scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    monkeypatch.setattr(fabrik.scheduler, "_gh_token_safe", lambda: "tok")
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr(1, "t1")])
    trig = {"repo": "o/r", "poll": 9999}  # großes Intervall
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # init (setzt ts)
    api_calls = []
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: (api_calls.append(1), [_pr(2, "t2")])[1])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # innerhalb Intervall → kein API-Call
    assert api_calls == []
    assert fired == []


def test_on_pr_no_repo_no_fire(monkeypatch):
    sched = _FakePrScheduler()
    fired = []
    monkeypatch.setattr(fabrik.scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    sched._check_pr("p", "l", {}, {"type": "on_pr"})  # kein repo, kein github
    assert fired == []


def test_on_pr_no_token_no_fire(monkeypatch):
    """Kein GitHub-Token → sauberer Early-Return (kein API-Call, kein Fire)."""
    sched = _FakePrScheduler()
    fired = []
    api_calls = []
    monkeypatch.setattr(fabrik.scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    monkeypatch.setattr(fabrik.scheduler, "_gh_token_safe", lambda: None)
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: api_calls.append(1) or [])
    sched._check_pr("p", "l", {"github": "o/r"}, {"repo": "o/r", "poll": 0})
    assert api_calls == []
    assert fired == []


def test_on_pr_api_error_no_fire(monkeypatch):
    """GitHub-API-Fehler → Exception wird geschluckt (kein Fire, kein Crash)."""
    sched = _FakePrScheduler()
    fired = []
    monkeypatch.setattr(fabrik.scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append(t))
    monkeypatch.setattr(fabrik.scheduler, "_gh_token_safe", lambda: "tok")

    def _boom(m, u, t):
        raise RuntimeError("network down")

    monkeypatch.setattr(fabrik.scheduler, "_gh_api", _boom)
    sched._check_pr("p", "l", {"github": "o/r"}, {"repo": "o/r", "poll": 0})
    assert fired == []


# ---------------------------------------------------------------- on_pr states
def _state_of(url: str) -> str:
    """PR-State aus der Poll-URL extrahieren (?state=open|closed)."""
    for part in url.split("?")[1].split("&"):
        if part.startswith("state="):
            return part.split("=", 1)[1]
    return "open"


def _mk_pr_test_states(monkeypatch, prs_by_state: dict):
    """Wie _mk_pr_test, aber API-Antwort pro State (z. B. {"open": [...], "closed": [...]})."""
    sched = _FakePrScheduler()
    fired = []
    monkeypatch.setattr(fabrik.scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append((t, kw)))
    monkeypatch.setattr(fabrik.scheduler, "_gh_token_safe", lambda: "tok")
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: list(prs_by_state.get(_state_of(u), [])))
    trig = {"repo": "o/r", "poll": 0}
    return sched, fired, trig


def test_on_pr_states_default_open_only(monkeypatch):
    """Ohne `states` wird nur state=open gepollt (Default, backward-kompatibel)."""
    sched, fired, trig = _mk_pr_test_states(monkeypatch, {"open": [_pr(1, "t1")]})
    urls = []
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: (urls.append(u), [])[1])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(urls) == 1
    assert "state=open" in urls[0]


def test_on_pr_states_polls_each_state(monkeypatch):
    """states: [open, closed] → ein API-Call pro State pro Poll."""
    sched, fired, trig = _mk_pr_test_states(monkeypatch, {})
    urls = []
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: (urls.append(u), [])[1])
    trig["states"] = ["open", "closed"]
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(urls) == 2
    assert any("state=open" in u for u in urls)
    assert any("state=closed" in u for u in urls)


def test_on_pr_states_deduped_no_duplicate_runs(monkeypatch):
    """states: [open, open] → Dedup: ein API-Call pro eindeutigen State, neue PR
    feuert genau einmal (Regression: doppelte States → doppelte API-Calls + Runs)."""
    sched, fired, trig = _mk_pr_test_states(monkeypatch, {})
    calls = {"n": 0}

    def _api(m, u, t):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [_pr(1, "u1")]

    monkeypatch.setattr(fabrik.scheduler, "_gh_api", _api)
    trig["states"] = ["open", "open"]
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # init: leer
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # PR #1 neu
    assert calls["n"] == 2  # 2 Polls × 1 eindeutiger State (Dedup)
    assert len(fired) == 1
    assert fired[0][0] == "on_pr:#1"


def test_on_pr_state_change_open_to_closed_fires(monkeypatch):
    """State-Wechsel open→closed feuert bei states: [open, closed] (AC d)."""
    pr_open = {**_pr(1, "t1"), "state": "open"}
    pr_closed = {**_pr(1, "t2"), "state": "closed"}
    sched, fired, trig = _mk_pr_test_states(monkeypatch, {"open": [pr_open]})
    trig["states"] = ["open", "closed"]
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # init: PR offen
    assert fired == []
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [pr_closed] if _state_of(u) == "closed" else [])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # PR jetzt geschlossen
    assert len(fired) == 1
    assert fired[0][0] == "on_pr:#1"


def test_on_pr_closed_pr_ignored_with_default_states(monkeypatch):
    """Default states=[open]: eine PR, die verschwindet (geschlossen), feuert nicht."""
    sched, fired, trig = _mk_pr_test_states(monkeypatch, {"open": [_pr(1, "t1")]})
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # init
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [])  # PR geschlossen
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert fired == []


def test_on_pr_states_unchanged_no_fire(monkeypatch):
    """states: [open, closed] → unveränderte PRs (gleicher State + updated_at) feuern nicht."""
    pr_open = {**_pr(1, "t1"), "state": "open"}
    pr_closed = {**_pr(2, "t9"), "state": "closed"}
    sched, fired, trig = _mk_pr_test_states(monkeypatch, {"open": [pr_open], "closed": [pr_closed]})
    trig["states"] = ["open", "closed"]
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # init
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # unverändert
    assert fired == []


# ---------------------------------------------------------------- on_pr reconcile (ADR 0011)
def test_on_pr_reconcile_first_poll_fires_on_existing(monkeypatch):
    """reconcile: true → Erst-Poll feuert auf bestehende PRs (kein Swallow)."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    trig["reconcile"] = True
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(fired) == 1
    assert fired[0][0] == "on_pr:#1"
    assert fired[0][1]["initial_vars"]["pr_number"] == 1


def test_on_pr_reconcile_skips_prs_with_any_prior_run(monkeypatch):
    """reconcile: true → PR mit bisherigem Run (egal welcher Status) wird übersprungen."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1"), _pr(2, "t2")])
    trig["reconcile"] = True
    monkeypatch.setattr(fabrik.scheduler, "loop_run_triggers", lambda n, ln: {"on_pr:#1"})
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert [f[0] for f in fired] == ["on_pr:#2"]


def test_on_pr_reconcile_lifecycle_no_refire_after_finish(monkeypatch):
    """reconcile: Lebenszyklus running → pass → nächster Poll startet dieselbe PR nicht neu."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    trig["reconcile"] = True
    runs: set[str] = set()
    monkeypatch.setattr(fabrik.scheduler, "loop_run_triggers", lambda n, ln: set(runs))
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # 1. Poll: feuert
    assert len(fired) == 1
    runs.add("on_pr:#1")  # Run existiert jetzt (running, später pass/error)
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr(1, "t2")])  # PR aktualisiert
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(fired) == 1  # kein Doppel-Run


def _pr_sha(number, updated_at, sha):
    return {**_pr(number, updated_at), "head": {"ref": "feature/x", "sha": sha}}


def test_on_pr_reconcile_head_fires_per_head_sha(monkeypatch):
    """reconcile: head (ADR 0023) → Schlüssel inkl. Head-SHA; neuer Push feuert erneut, gleicher nicht."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr_sha(1, "t1", "aaa")])
    trig["reconcile"] = "head"
    runs: set[str] = set()
    monkeypatch.setattr(fabrik.scheduler, "loop_run_triggers", lambda n, ln: set(runs))
    sched._check_pr("p", "l", {"github": "o/r"}, trig)  # Erst-Poll feuert
    assert [f[0] for f in fired] == ["on_pr:#1@aaa"]
    assert fired[0][1]["initial_vars"]["pr_head_sha"] == "aaa"
    runs.add("on_pr:#1@aaa")
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr_sha(1, "t2", "aaa")])  # Kommentar, kein Push
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(fired) == 1
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr_sha(1, "t3", "bbb")])  # neuer Push
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert [f[0] for f in fired] == ["on_pr:#1@aaa", "on_pr:#1@bbb"]


def test_on_pr_reconcile_head_retries_dropped_trigger(monkeypatch):
    """reconcile: head → ein verworfener Trigger (Lock belegt, kein Run-Record) feuert beim nächsten Poll erneut."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr_sha(1, "t1", "aaa")])
    trig["reconcile"] = "head"
    monkeypatch.setattr(fabrik.scheduler, "loop_run_triggers", lambda n, ln: set())  # nie ein Record
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert [f[0] for f in fired] == ["on_pr:#1@aaa", "on_pr:#1@aaa"]


def test_on_pr_reconcile_head_without_sha_falls_back_to_pr_key(monkeypatch):
    """Fehlt head.sha in der API-Antwort → PR-Schlüssel `on_pr:#N` (wie reconcile: true)."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    trig["reconcile"] = "head"
    monkeypatch.setattr(fabrik.scheduler, "loop_run_triggers", lambda n, ln: {"on_pr:#1"})
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert fired == []


def test_on_pr_reconcile_head_without_sha_matches_sha_records(monkeypatch):
    """Fehlt head.sha, aber es gibt einen `on_pr:#N@<sha>`-Record → kein Doppel-Run."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    trig["reconcile"] = "head"
    monkeypatch.setattr(fabrik.scheduler, "loop_run_triggers", lambda n, ln: {"on_pr:#1@aaa", "on_pr:#12"})
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert fired == []


def test_on_pr_reconcile_true_ignores_head_sha(monkeypatch):
    """reconcile: true bleibt PR-genau (ADR 0011): neuer Head feuert nicht erneut."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr_sha(1, "t1", "bbb")])
    trig["reconcile"] = True
    monkeypatch.setattr(fabrik.scheduler, "loop_run_triggers", lambda n, ln: {"on_pr:#1"})
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert fired == []


def test_on_pr_paginates(monkeypatch):
    """Mehr als GH_PER_PAGE PRs → weitere Seiten werden gelesen (Event-Modus)."""
    per = fabrik.scheduler.GH_PER_PAGE
    page1 = [_pr(n, "t") for n in range(1, per + 1)]
    page2 = [_pr(per + 1, "t")]
    sched, fired, trig = _mk_pr_test(monkeypatch, [])
    urls = []

    def fake(m, u, t):
        urls.append(u)
        return page1 if "page=1" in u else page2

    monkeypatch.setattr(fabrik.scheduler, "_gh_api", fake)
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(urls) == 2
    assert len(sched._pr_state[("p", "l")]["seen"]) == per + 1


def test_on_pr_pagination_capped(monkeypatch):
    """Pagination endet spätestens bei RECONCILE_MAX_PAGES."""
    full = [_pr(n, "t") for n in range(fabrik.scheduler.GH_PER_PAGE)]
    sched, fired, trig = _mk_pr_test(monkeypatch, [])
    urls = []
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: (urls.append(u), full)[1])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert len(urls) == fabrik.scheduler.RECONCILE_MAX_PAGES


def test_on_pr_labels_filter_in_url(monkeypatch):
    """on_pr mit labels → API-URL enthält den Label-Filter (quote für Leerzeichen)."""
    sched, fired, trig = _mk_pr_test(monkeypatch, [])
    urls = []
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: (urls.append(u), [])[1])
    trig["labels"] = ["needs approval"]
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert any("labels=needs%20approval" in u for u in urls)


def test_on_pr_labels_and_semantics(monkeypatch):
    """labels: [a, b] → AND-Semantik: PR braucht ALLE Labels (GitHub-API ist nur OR)."""
    prs = [
        {**_pr(1, "t1"), "labels": [{"name": "fabrik"}, {"name": "needs approval"}]},  # beide → match
        {**_pr(2, "t2"), "labels": [{"name": "needs approval"}]},  # nur eines → kein Match
        {**_pr(3, "t3"), "labels": [{"name": "fabrik"}]},  # nur eines → kein Match
        {**_pr(4, "t4"), "labels": []},  # keine → kein Match
    ]
    sched, fired, trig = _mk_pr_test(monkeypatch, prs)
    trig["labels"] = ["fabrik", "needs approval"]
    trig["reconcile"] = True  # Erst-Poll feuert auf bestehende PRs
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert [f[0] for f in fired] == ["on_pr:#1"]  # nur PR mit ALLEN Labels


def test_bounded_seen_below_limit_unchanged():
    seen = {1: ("open", "a"), 2: ("open", "b")}
    assert fabrik.scheduler._bounded_seen(seen, keep={2}, limit=5) == seen


def test_bounded_seen_retention_keeps_current_and_newest_history():
    seen = {n: ("open", str(n)) for n in range(1, 11)}
    out = fabrik.scheduler._bounded_seen(seen, keep={1, 2}, limit=5)
    assert set(out) == {1, 2, 8, 9, 10}  # aktuell + neueste Historie


def test_bounded_seen_keeps_all_current_even_over_limit():
    seen = {n: ("open", "x") for n in range(1, 8)}
    assert set(fabrik.scheduler._bounded_seen(seen, keep=set(seen), limit=3)) == set(seen)


_real_bounded = fabrik.scheduler._bounded_seen


def test_on_pr_evicted_pr_returning_does_not_refire(monkeypatch):
    """Alte PR aus `seen` verdrängt, aus dem Poll verschwunden, unverändert zurück → kein Fire."""
    monkeypatch.setattr(fabrik.scheduler, "_bounded_seen", lambda s, keep: _real_bounded(s, keep, limit=3))
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    for n in range(2, 7):  # PR 1 verschwindet, Historie läuft über das Limit
        monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t, n=n: [_pr(n, f"t{n}")])
        sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert 1 not in sched._pr_state[("p", "l")]["seen"]
    fired.clear()
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr(1, "t1"), _pr(6, "t6")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert fired == []
    assert sched._pr_state[("p", "l")]["seen"][1] == ("open", "t1")  # still neu gelernt


def test_on_pr_new_pr_above_floor_still_fires(monkeypatch):
    monkeypatch.setattr(fabrik.scheduler, "_bounded_seen", lambda s, keep: _real_bounded(s, keep, limit=2))
    sched, fired, trig = _mk_pr_test(monkeypatch, [_pr(1, "t1"), _pr(2, "t2"), _pr(3, "t3")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    monkeypatch.setattr(fabrik.scheduler, "_gh_api", lambda m, u, t: [_pr(3, "t3"), _pr(4, "t4")])
    sched._check_pr("p", "l", {"github": "o/r"}, trig)
    assert [f[0] for f in fired] == ["on_pr:#4"]


# ---------------------------------------------------------------- on_pr Fire-Entscheidung (reiner Helfer)
def _fk(**overrides):
    """_pr_fire_kind mit Default-Kontext aufrufen (Event-Modus, PR #1 als
    unverändert gesehene PR mit neuem updated_at "t2"); Abweichung via kwargs."""
    base = {
        "prev": {1: ("open", "t1")},
        "state": "open",
        "updated_at": "t2",
        "num": 1,
        "floor": 0,
        "reconcile": False,
        "per_head": False,
        "head_sha": "",
        "done": set(),
    }
    base.update(overrides)
    return fabrik.scheduler._pr_fire_kind(**base)


def test_fire_kind_event_first_poll_none():
    """Event: Erst-Poll (prev=None) → initialisieren, nicht feuern."""
    assert _fk(prev=None) is None


def test_fire_kind_event_unchanged_none():
    """Event: State + updated_at unverändert → kein Fire (Dedup)."""
    assert _fk(updated_at="t1") is None


def test_fire_kind_event_updated():
    """Event: gleicher State, neues updated_at → 'aktualisiert'."""
    assert _fk() == "aktualisiert"


def test_fire_kind_event_state_change():
    """Event: State-Wechsel open→closed → 'state-wechsel' (auch bei neuem updated_at)."""
    assert _fk(state="closed") == "state-wechsel open→closed"


def test_fire_kind_event_new_pr():
    """Event: unbekannte PR-Nummer über dem Wasserstand → 'neu'."""
    assert _fk(num=2) == "neu"


def test_fire_kind_event_new_pr_below_floor_none():
    """Event: unbekannte PR unter dem Wasserstand → verdrängt, schon gesehene PR."""
    assert _fk(num=2, floor=5) is None


def test_fire_kind_event_new_pr_at_floor_none():
    """Event: unbekannte PR genau am Wasserstand (≤) → ebenfalls kein Fire."""
    assert _fk(num=3, floor=3) is None


def test_fire_kind_reconcile_first_poll_fires_neu():
    """Reconcile: Erst-Poll feuert AUCH (prev=None) — bestehende PR wird aufgenommen."""
    assert _fk(reconcile=True, prev=None) == "neu"


def test_fire_kind_reconcile_known_pr():
    """Reconcile: bekannte PR ohne bisherigen Run → 'reconcile' (nachholen)."""
    assert _fk(reconcile=True) == "reconcile"


def test_fire_kind_reconcile_new_above_floor():
    """Reconcile: unbekannte PR über dem Wasserstand → 'neu'."""
    assert _fk(reconcile=True, num=2) == "neu"


def test_fire_kind_reconcile_unknown_at_floor():
    """Reconcile: unbekannte PR am Wasserstand → nicht 'neu', sondern 'reconcile'
    (Nummer ≤ floor gilt als gesehene, verdrängte PR — Verhalten des Originals)."""
    assert _fk(reconcile=True, num=2, floor=2) == "reconcile"


def test_fire_kind_reconcile_done_none():
    """Reconcile: Trigger steht in `done` (jeder Status) → kein Fire."""
    assert _fk(reconcile=True, done={"on_pr:#1"}) is None


def test_fire_kind_head_fires_per_head_sha():
    """reconcile: head → bekannte PR, Head-SHA ohne Record → 'head <sha[:7]>'."""
    assert _fk(reconcile=True, per_head=True, head_sha="abc123def456") == "head abc123d"


def test_fire_kind_head_first_poll_fires_neu():
    """reconcile: head → Erst-Poll feuert als 'neu' (auch mit Head-SHA)."""
    assert _fk(reconcile=True, per_head=True, prev=None, head_sha="abc123def") == "neu"


def test_fire_kind_head_sha_record_done_none():
    """reconcile: head → `on_pr:#N@<sha>`-Record existiert → kein Fire."""
    assert _fk(reconcile=True, per_head=True, head_sha="abc123def456", done={"on_pr:#1@abc123def456"}) is None


def test_fire_kind_head_without_sha_pr_record_done_none():
    """reconcile: head ohne head.sha → PR-Schlüssel `on_pr:#N` in `done` → kein Fire."""
    assert _fk(reconcile=True, per_head=True, done={"on_pr:#1"}) is None


def test_fire_kind_head_without_sha_sha_record_done_none():
    """reconcile: head ohne head.sha → irgendein `on_pr:#N@…`-Record zählt als
    erledigt (ADR 0023); fremde PRs (`on_pr:#12`, `on_pr:#2`) stören nicht."""
    assert _fk(reconcile=True, per_head=True, done={"on_pr:#1@aaa", "on_pr:#12", "on_pr:#2"}) is None


def test_fire_kind_head_without_sha_unrelated_records_fires():
    """reconcile: head ohne head.sha, aber nur fremde Records → PR feuert trotzdem."""
    assert _fk(reconcile=True, per_head=True, done={"on_pr:#2@bbb", "on_pr:#12"}) == "reconcile"
