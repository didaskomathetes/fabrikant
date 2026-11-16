"""Fehlerklasse im Run (ADR 0035): Engine, Shell-Step, Spec-Validierung, Run-Store, Suche, Alerts."""

from __future__ import annotations

import json

import pytest

import fabrik
from fabrik import runsearch, specs
from fabrik.failures import classify_exception
from fabrik.runmodel import FailureKind
from fabrik.runstore import parse_run
from fabrik.steps import notify
from fabrik.steps.shell import _step_shell


def _spec(projects_dir, name: str, steps: str) -> str:
    (projects_dir / f"{name}.fabrik.yaml").write_text(f"name: {name}\nroot: /tmp\nloops:\n  t:\n    steps:\n{steps}")
    return name


def _shell(name: str, cmd: str, extra: str = "") -> str:
    return f"      - type: shell\n        name: {name}\n        cmd: '{cmd}'\n{extra}"


# ---------------------------------------------------------------- Engine
def test_run_takes_kind_of_aborting_step(runs_dir, projects_dir):
    name = _spec(projects_dir, "fk_abort", _shell("s1", "exit 127") + _shell("s2", "echo nie"))
    rec = fabrik.run_loop(name, "t")
    assert rec["status"] == "fail"
    assert rec["steps"][0]["failure_kind"] == "environment"
    assert rec["failure_kind"] == "environment"
    # persistiert und typisiert lesbar
    stored = json.loads((runs_dir / name / f"{rec['id']}.json").read_text())
    assert parse_run(stored).failure_kind is FailureKind.ENVIRONMENT


def test_gate_false_run_takes_first_failed_step(runs_dir, projects_dir):
    steps = (
        _shell("rot", "exit 1", "        gate: false\n        failure_kind: outcome\n")
        + _shell("weg", "exit 127", "        gate: false\n")
        + _shell("ok", "true")
    )
    rec = fabrik.run_loop(_spec(projects_dir, "fk_gatefalse", steps), "t")
    assert rec["status"] == "fail"
    assert rec["failure_kind"] == "outcome"


def test_unclassified_exit_is_unknown(runs_dir, projects_dir):
    rec = fabrik.run_loop(_spec(projects_dir, "fk_unknown", _shell("s1", "exit 1")), "t")
    assert rec["failure_kind"] == "unknown"


def test_passing_run_has_no_kind(runs_dir, projects_dir):
    rec = fabrik.run_loop(_spec(projects_dir, "fk_pass", _shell("s1", "true")), "t")
    assert rec["status"] == "pass"
    assert "failure_kind" not in rec
    assert "failure_kind" not in rec["steps"][0]


def test_step_exception_is_classified_by_type(runs_dir, projects_dir):
    """Exception im Step → error; Klasse aus dem Exception-Typ (hier fehlendes Repo → ConfigError)."""
    steps = "      - type: pr\n        name: pr\n"
    rec = fabrik.run_loop(_spec(projects_dir, "fk_exc", steps), "t")
    assert rec["status"] == "error"
    assert rec["steps"][0]["failure_kind"] == "environment"
    assert rec["failure_kind"] == "environment"


def test_step_timeout_is_unknown(runs_dir, projects_dir):
    rec = fabrik.run_loop(_spec(projects_dir, "fk_timeout", _shell("s1", "sleep 5", "        timeout: 1\n")), "t")
    assert rec["steps"][0]["status"] == "timeout"
    assert rec["failure_kind"] == "unknown"


def test_new_run_record_carries_trigger_key(runs_dir, projects_dir):
    """ADR 0036: neue Run-Records tragen den kanonischen Ketten-Schlüssel (``trigger_key``)."""
    spec = (
        "name: fk_tkey\nroot: /tmp\ngithub: owner/repo\nloops:\n  t:\n"
        "    triggers:\n      - {type: on_issue}\n    steps:\n" + _shell("s1", "true")
    )
    (projects_dir / "fk_tkey.fabrik.yaml").write_text(spec)
    rec = fabrik.run_loop("fk_tkey", "t", trigger="on_issue:#42")
    assert rec["trigger_key"] == "issue:owner/repo#42"
    stored = json.loads((runs_dir / "fk_tkey" / f"{rec['id']}.json").read_text())
    assert stored["trigger_key"] == "issue:owner/repo#42"


def test_new_run_without_chain_has_empty_trigger_key(runs_dir, projects_dir):
    """Kein Issue-/PR-/Cron-Bezug → leerer Schlüssel (keine Kette)."""
    rec = fabrik.run_loop(_spec(projects_dir, "fk_tkey_none", _shell("s1", "true")), "t")
    assert rec["trigger_key"] == ""


def test_run_level_abort_is_classified(runs_dir, projects_dir, monkeypatch):
    """Abbruch außerhalb der Steps (z. B. git fetch beim Worktree-Aufbau) → Klasse der Exception."""
    from fabrik import engine
    from fabrik.failures import GitError

    def _boom(*a, **k):
        raise GitError("git fetch fehlgeschlagen", "fatal: unable to access '…': Could not resolve host: github.com")

    monkeypatch.setattr(engine, "_build_ctx", _boom)
    rec = fabrik.run_loop(_spec(projects_dir, "fk_runlevel", _shell("s1", "true")), "t")
    assert rec["status"] == "error"
    assert rec["failure_kind"] == "transient"


# ---------------------------------------------------------------- Shell
def _ctx(tmp_path):
    return {"workdir": str(tmp_path), "project": "p", "run_id": "r"}


def test_shell_exit_127_is_environment(tmp_path):
    res = _step_shell(_ctx(tmp_path), {"cmd": "pysonar-gibt-es-nicht"}, lambda m: None)
    assert res["exit_code"] == 127
    assert res["failure_kind"] == "environment"


def test_shell_spec_kind_applies_to_other_exit_codes(tmp_path):
    res = _step_shell(_ctx(tmp_path), {"cmd": "exit 1", "failure_kind": "outcome"}, lambda m: None)
    assert res["failure_kind"] == "outcome"


@pytest.mark.parametrize(("code", "kind"), [(75, "transient"), (78, "environment")])
def test_shell_script_reports_kind_via_sysexits(tmp_path, code, kind):
    """Skript meldet die Klasse selbst (sysexits) — hat Vorrang vor `failure_kind` der Spec."""
    res = _step_shell(_ctx(tmp_path), {"cmd": f"exit {code}", "failure_kind": "outcome"}, lambda m: None)
    assert res["failure_kind"] == kind


@pytest.mark.parametrize("cmd", ["kill -9 $$", "kill -TERM $$", "exit 137"])
def test_shell_signal_death_ignores_spec_kind(tmp_path, cmd):
    """OOM-Kill/SIGTERM (Exit -n bzw. 128+n) ist kein rotes Testergebnis: Spec-`outcome` greift nicht."""
    res = _step_shell(_ctx(tmp_path), {"cmd": cmd, "failure_kind": "outcome"}, lambda m: None)
    assert res["failure_kind"] == "unknown"


def test_shell_spec_kind_does_not_override_127(tmp_path):
    res = _step_shell(_ctx(tmp_path), {"cmd": "nicht-da-xyz", "failure_kind": "outcome"}, lambda m: None)
    assert res["failure_kind"] == "environment"


def test_shell_placeholder_clash_is_environment(tmp_path):
    step = {"cmd": "echo {x}", "env": {"x": "1"}}
    assert _step_shell(_ctx(tmp_path), step, lambda m: None)["failure_kind"] == "environment"


# ---------------------------------------------------------------- Spec-Validierung
@pytest.mark.parametrize("value", ["outcome", "transient", "environment"])
def test_spec_accepts_failure_kind(projects_dir, value):
    _spec(projects_dir, "fk_ok", _shell("s", "true", f"        failure_kind: {value}\n"))
    assert "_error" not in specs.load_projects()["fk_ok"]


@pytest.mark.parametrize("value", ["interrupted", "unknown", "kaputt"])
def test_spec_rejects_failure_kind(projects_dir, value):
    _spec(projects_dir, "fk_bad", _shell("s", "true", f"        failure_kind: {value}\n"))
    assert "failure_kind" in specs.load_projects()["fk_bad"]["_error"]


@pytest.mark.parametrize("value", ["[outcome]", "{a: 1}"])
def test_spec_rejects_non_string_failure_kind(projects_dir, value):
    _spec(projects_dir, "fk_type", _shell("s", "true", f"        failure_kind: {value}\n"))
    assert "failure_kind muss" in specs.load_projects()["fk_type"]["_error"]


def test_spec_rejects_failure_kind_on_non_shell_step(projects_dir):
    """Nur shell-Steps lesen `failure_kind`; sonst wäre es still wirkungslos."""
    step = "      - type: llm\n        name: s\n        prompt: p\n        failure_kind: outcome\n"
    _spec(projects_dir, "fk_llm", step)
    assert "nur für shell-Steps" in specs.load_projects()["fk_llm"]["_error"]


# ---------------------------------------------------------------- Run-Store (Altbestand)
def _rec(status, **extra):
    return {"id": "r1", "project": "p", "loop": "l", "status": status, **extra}


def test_legacy_failed_run_reads_as_unknown():
    run = parse_run(_rec("error", steps=[{"name": "s", "status": "fail"}]))
    assert run.failure_kind is FailureKind.UNKNOWN
    assert run.steps[0].failure_kind is FailureKind.UNKNOWN


def test_non_failed_run_has_no_kind():
    run = parse_run(_rec("pass", steps=[{"name": "s", "status": "pass", "failure_kind": "transient"}]))
    assert run.failure_kind is None
    assert run.steps[0].failure_kind is None


def test_stored_kind_and_retry_after_are_read():
    run = parse_run(_rec("fail", failure_kind="transient", retry_after="2026-10-26T12:00:00+00:00"))
    assert run.failure_kind is FailureKind.TRANSIENT
    assert run.retry_after == "2026-10-26T12:00:00+00:00"


# ---------------------------------------------------------------- Suche
def _write(runs_dir, run_id, status, kind=None):
    d = runs_dir / "p"
    d.mkdir(exist_ok=True)
    rec = _rec(status, id=run_id, started=f"2026-10-2{run_id[-1]}T00:00:00+00:00")
    if kind:
        rec["failure_kind"] = kind
    (d / f"{run_id}.json").write_text(json.dumps(rec))


def test_search_filters_by_failure_kind(runs_dir):
    _write(runs_dir, "r1", "fail", "transient")
    _write(runs_dir, "r2", "error", "environment")
    _write(runs_dir, "r3", "error")  # Altbestand → unknown
    _write(runs_dir, "r4", "pass")
    page = runsearch.search_runs("p", failure_kind="environment")
    assert [r["id"] for r in page["runs"]] == ["r2"]
    page = runsearch.search_runs("p", failure_kind="unknown,transient")
    assert [(r["id"], r["failure_kind"]) for r in page["runs"]] == [("r3", "unknown"), ("r1", "transient")]
    assert {r["id"]: r["failure_kind"] for r in runsearch.search_runs("p")["runs"]}["r4"] == ""


def test_api_passes_failure_kind_filter(runs_dir, live_server, clean_token):
    import urllib.request

    _write(runs_dir, "r1", "fail", "transient")
    _write(runs_dir, "r2", "error", "environment")
    with urllib.request.urlopen(f"{live_server()}/api/runs?project=p&failure_kind=transient") as r:
        data = json.loads(r.read())
    assert [x["id"] for x in data["runs"]] == ["r1"]


# ---------------------------------------------------------------- Alerts
def _failed_record(kind: str | None) -> dict:
    rec = _rec("fail", steps=[{"name": "scan", "status": "fail", "summary": "exit 1: Quality Gate ERROR"}])
    if kind:
        rec["failure_kind"] = kind
    return rec


def test_alert_payload_carries_kind(monkeypatch, runs_dir):
    posted = []
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: posted.append(payload) or (200, ""))
    spec = {"notify": {"webhooks": [{"name": "dd", "url": "http://x"}]}}
    notify._notify(spec, _failed_record("environment"))
    assert posted[0]["failure_kind"] == "environment"
    assert "braucht einen Menschen" in posted[0]["message"]


def test_alert_legacy_record_is_unknown(monkeypatch, runs_dir):
    posted = []
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: posted.append(payload) or (200, ""))
    notify._notify({"notify": {"slack": "http://x"}}, _failed_record(None))
    assert "[unknown]" in posted[0]["text"]


def test_alert_passed_run_has_no_kind(monkeypatch, runs_dir):
    posted = []
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: posted.append(payload) or (200, ""))
    spec = {"notify": {"webhooks": [{"url": "http://x"}], "events": ["run_passed"]}}
    notify._notify(spec, _rec("pass"))
    assert "failure_kind" not in posted[0]


def test_fingerprint_ignores_kind():
    """ADR 0035 Nr. 5: der Dedup-Fingerprint (ADR 0025) bleibt unverändert — sonst öffnete der Umstieg
    für jeden offenen Alert ein neues Issue."""
    assert notify._alert_fingerprint(_failed_record("transient")) == notify._alert_fingerprint(
        _failed_record("outcome")
    )


def test_unknown_loop_is_environment(runs_dir, projects_dir):
    """Projekt/Loop unbekannt → environment; bleibt ein RuntimeError für bestehende Aufrufer."""
    name = _spec(projects_dir, "fk_noloop", _shell("s1", "true"))
    with pytest.raises(RuntimeError) as exc:
        fabrik.run_loop(name, "gibtsnicht")
    assert classify_exception(exc.value).kind is FailureKind.ENVIRONMENT
