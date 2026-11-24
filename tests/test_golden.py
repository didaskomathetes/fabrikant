"""Golden Runs + Eval-Harness (Roadmap #130, auf ADR 0061 Replay aufbauend).

Covers ``stable_outcomes``/``capture_golden`` (pinned baseline), golden-file
load/save (``projects/<p>.golden.json``), ``compare_golden`` (regression
detection on the stable surface), ``run_eval`` (re-run via replay, trigger
``eval``) and the ``fabrikant golden`` / ``fabrikant eval`` CLI commands.
"""

from __future__ import annotations

import json
import threading

import pytest

from fabrikant import cli, golden, replay


def _write_spec(projects_dir, name="p", root=None, loops=None):
    """Minimal valid spec (same shape as test_cli / test_replay)."""
    root = root or projects_dir.parent / "proj"
    root.mkdir(parents=True, exist_ok=True)
    loops = loops or {"l": {"steps": [{"type": "shell", "cmd": "true"}]}}
    (projects_dir / f"{name}.fabrikant.yaml").write_text(
        f"name: {name}\nroot: {root}\nloops:\n"
        + "\n".join(
            f"  {ln}:\n    steps:\n" + "\n".join(f"      - {json.dumps(s)}" for s in ldef["steps"])
            for ln, ldef in loops.items()
        )
    )


def _write_run(runs_dir, project, rid, status="pass", loop="l", **extra):
    d = runs_dir / project
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": rid,
        "project": project,
        "loop": loop,
        "status": status,
        "started": "2024-01-01T00:00:00Z",
        "spec_hash": "abc123def456",
        "vars": {},
        "steps": [],
    }
    rec.update(extra)
    (d / f"{rid}.json").write_text(json.dumps(rec))
    return rec


def _write_golden(projects_dir, project, name, golden_dict):
    p = projects_dir / f"{project}.golden.json"
    data = {"schema": 1, "goldens": {}}
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
    data["goldens"][name] = golden_dict
    p.write_text(json.dumps(data, indent=2))
    return p


_STEPS = [
    {"name": "a", "type": "shell", "status": "pass", "summary": "ok", "verdict": ""},
    {"name": "review", "type": "review", "status": "pass", "summary": "ok", "verdict": "approved"},
]


# ------------------------------------------------------------------ stable_outcomes
def test_stable_outcomes_extracts_status_and_step_fields():
    rec = {"status": "pass", "steps": _STEPS}
    assert golden.stable_outcomes(rec) == {
        "status": "pass",
        "steps": [
            {"name": "a", "status": "pass", "verdict": ""},
            {"name": "review", "status": "pass", "verdict": "approved"},
        ],
    }


def test_stable_outcomes_empty_run():
    assert golden.stable_outcomes({"status": "fail", "steps": []}) == {"status": "fail", "steps": []}


# ------------------------------------------------------------------ capture_golden
def test_capture_golden_from_record(projects_dir, runs_dir):
    rec = _write_run(runs_dir, "p", "orig-1", status="pass", loop="l", steps=_STEPS)
    g = golden.capture_golden("p", "l", "orig-1", record=rec)
    assert g["loop"] == "l"
    assert g["baseline_run"] == "orig-1"
    assert g["spec_hash"] == "abc123def456"
    assert g["expect"]["status"] == "pass"
    assert g["expect"]["steps"][1]["verdict"] == "approved"


def test_capture_golden_lookup_by_id(projects_dir, runs_dir):
    _write_run(runs_dir, "p", "orig-1", status="pass", loop="l", steps=_STEPS)
    g = golden.capture_golden("p", "l", "orig-1")
    assert g["baseline_run"] == "orig-1"


def test_capture_golden_refuses_non_terminal(projects_dir, runs_dir):
    rec = _write_run(runs_dir, "p", "r1", status="running", loop="l")
    with pytest.raises(golden.GoldenError, match="not finished"):
        golden.capture_golden("p", "l", "r1", record=rec)


def test_capture_golden_refuses_loop_mismatch(projects_dir, runs_dir):
    rec = _write_run(runs_dir, "p", "r1", status="pass", loop="other")
    with pytest.raises(golden.GoldenError, match="belongs to loop"):
        golden.capture_golden("p", "l", "r1", record=rec)


def test_capture_golden_missing_run(projects_dir, runs_dir):
    with pytest.raises(golden.GoldenError, match="not found"):
        golden.capture_golden("p", "l", "nope")


# ------------------------------------------------------------------ file load/save
def test_load_goldens_empty_when_no_file(projects_dir):
    assert golden.load_goldens("p") == {}


def test_load_goldens_reads_file(projects_dir):
    _write_golden(projects_dir, "p", "l", {"loop": "l", "baseline_run": "x", "expect": {"status": "pass", "steps": []}})
    gs = golden.load_goldens("p")
    assert list(gs) == ["l"]
    assert gs["l"]["baseline_run"] == "x"


def test_load_goldens_bad_schema(projects_dir):
    (projects_dir / "p.golden.json").write_text(json.dumps({"schema": 99, "goldens": {}}))
    with pytest.raises(golden.GoldenError, match="schema"):
        golden.load_goldens("p")


def test_get_golden_unknown(projects_dir):
    _write_golden(projects_dir, "p", "a", {"loop": "l", "baseline_run": "x", "expect": {"status": "pass", "steps": []}})
    with pytest.raises(golden.GoldenError, match="unknown"):
        golden.get_golden("p", "nope")


def test_save_golden_merges_and_roundtrips(projects_dir):
    g1 = {"loop": "l1", "baseline_run": "a", "expect": {"status": "pass", "steps": []}}
    g2 = {"loop": "l2", "baseline_run": "b", "expect": {"status": "fail", "steps": []}}
    p = golden.save_golden("p", "l1", g1)
    golden.save_golden("p", "l2", g2)
    assert p.suffix == ".json"
    gs = golden.load_goldens("p")
    assert set(gs) == {"l1", "l2"}
    assert gs["l1"]["baseline_run"] == "a"
    # atomic write leaves no temp file behind
    assert not (projects_dir / "p.golden.json.tmp").exists()


# ------------------------------------------------------------------ compare_golden
def _golden_with(expect):
    return {"loop": "l", "baseline_run": "x", "expect": expect}


def test_compare_golden_identical_no_regression():
    g = _golden_with(
        {
            "status": "pass",
            "steps": [
                {"name": "a", "status": "pass", "verdict": ""},
                {"name": "review", "status": "pass", "verdict": "approved"},
            ],
        }
    )
    rec = {"status": "pass", "steps": _STEPS}
    assert golden.compare_golden(g, rec) == []


def test_compare_golden_status_regression():
    g = _golden_with({"status": "pass", "steps": []})
    rec = {"status": "fail", "steps": []}
    assert any("status" in d for d in golden.compare_golden(g, rec))


def test_compare_golden_verdict_regression():
    g = _golden_with({"status": "pass", "steps": [{"name": "review", "status": "pass", "verdict": "approved"}]})
    rec = {
        "status": "pass",
        "steps": [{"name": "review", "type": "review", "status": "pass", "verdict": "changes_requested"}],
    }
    assert any("review" in d and "verdict" in d for d in golden.compare_golden(g, rec))


def test_compare_golden_missing_step():
    g = _golden_with(
        {
            "status": "pass",
            "steps": [{"name": "a", "status": "pass", "verdict": ""}, {"name": "b", "status": "pass", "verdict": ""}],
        }
    )
    rec = {"status": "pass", "steps": [{"name": "a", "type": "shell", "status": "pass", "verdict": ""}]}
    assert any("'b'" in d and "missing in eval run" in d for d in golden.compare_golden(g, rec))


def test_compare_golden_extra_step():
    g = _golden_with({"status": "pass", "steps": [{"name": "a", "status": "pass", "verdict": ""}]})
    rec = {
        "status": "pass",
        "steps": [
            {"name": "a", "type": "shell", "status": "pass", "verdict": ""},
            {"name": "c", "type": "shell", "status": "pass", "verdict": ""},
        ],
    }
    assert any("'c'" in d and "missing in golden" in d for d in golden.compare_golden(g, rec))


def _steps(*pairs):
    return [{"name": n, "type": "shell", "status": st, "verdict": ""} for n, st in pairs]


def _expect(*pairs, status="pass"):
    return _golden_with({"status": status, "steps": [{"name": n, "status": st, "verdict": ""} for n, st in pairs]})


def test_compare_golden_duplicate_step_names_not_masked():
    g = _expect(("a", "pass"), ("a", "pass"))
    rec = {"status": "pass", "steps": _steps(("a", "pass"), ("a", "fail"))}
    assert any("step 1" in d and "status" in d for d in golden.compare_golden(g, rec))


def test_compare_golden_duplicate_name_count_difference():
    g = _expect(("a", "pass"), ("a", "pass"))
    rec = {"status": "pass", "steps": _steps(("a", "pass"))}
    diffs = golden.compare_golden(g, rec)
    assert any("step count" in d for d in diffs)
    assert any("step 1" in d and "missing in eval run" in d for d in diffs)


def test_compare_golden_reorder_detected():
    g = _expect(("a", "pass"), ("b", "pass"))
    rec = {"status": "pass", "steps": _steps(("b", "pass"), ("a", "pass"))}
    diffs = golden.compare_golden(g, rec)
    assert any("step 0" in d and "expected name 'a'" in d for d in diffs)
    assert any("step 1" in d and "expected name 'b'" in d for d in diffs)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"status": "pass", "steps": []},  # no expect
        {"loop": "l", "baseline_run": "x", "expect": {}},
        {"loop": "l", "baseline_run": "x", "expect": {"steps": []}},  # status missing
        {"loop": "l", "baseline_run": "x", "expect": {"status": None, "steps": []}},
        {"loop": "l", "baseline_run": "x", "expect": {"status": "pass"}},  # steps missing
        {"loop": "l", "baseline_run": "x", "expect": {"status": "pass", "steps": ["a"]}},
        {"loop": "l", "baseline_run": "x", "expect": {"status": "pass", "steps": [{"status": "pass"}]}},
        {"loop": "l", "baseline_run": 5, "expect": {"status": "pass", "steps": []}},
        {"loop": 1, "baseline_run": "x", "expect": {"status": "pass", "steps": []}},
        "nope",
    ],
)
def test_compare_golden_malformed_raises(bad):
    with pytest.raises(golden.GoldenError, match="malformed"):
        golden.compare_golden(bad, {"status": "pass", "steps": []})


def test_load_goldens_rejects_malformed_golden(projects_dir):
    _write_golden(projects_dir, "p", "l", {"loop": "l", "baseline_run": "x", "expect": {}})
    with pytest.raises(golden.GoldenError, match="malformed"):
        golden.load_goldens("p")


def test_save_golden_rejects_malformed(projects_dir):
    with pytest.raises(golden.GoldenError, match="malformed"):
        golden.save_golden("p", "l", {"loop": "l"})
    assert not (projects_dir / "p.golden.json").exists()


def test_golden_roundtrip_dataclass():
    d = {
        "loop": "l",
        "baseline_run": "x",
        "spec_hash": "h",
        "expect": {"status": "pass", "steps": _expect(("a", "pass"))["expect"]["steps"]},
    }
    assert golden.Golden.from_dict(d).to_dict() == d


# ------------------------------------------------------------------ project validation
@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "", ".", "..", ".hidden", "a b", "x/../y"])
def test_project_traversal_rejected(projects_dir, bad):
    with pytest.raises(golden.GoldenError, match="invalid project"):
        golden.golden_path(bad)
    with pytest.raises(golden.GoldenError, match="invalid project"):
        golden.load_goldens(bad)
    with pytest.raises(golden.GoldenError, match="invalid project"):
        golden.save_golden(bad, "l", {})


def test_project_valid_names_accepted(projects_dir):
    assert golden.golden_path("my-proj_1.x").parent == projects_dir


# ------------------------------------------------------------------ volatile baseline
@pytest.mark.parametrize("status", ["timeout", "error"])
def test_capture_golden_refuses_volatile_steps(projects_dir, runs_dir, status):
    rec = _write_run(runs_dir, "p", "r1", status="fail", loop="l", steps=_steps(("a", "pass"), ("slow", status)))
    with pytest.raises(golden.GoldenError, match="volatile") as e:
        golden.capture_golden("p", "l", "r1", record=rec)
    assert "slow" in str(e.value)


def test_capture_golden_allows_plain_fail_step(projects_dir, runs_dir):
    rec = _write_run(runs_dir, "p", "r1", status="fail", loop="l", steps=_steps(("a", "fail")))
    assert golden.capture_golden("p", "l", "r1", record=rec)["expect"]["status"] == "fail"


# ------------------------------------------------------------------ concurrent save
def test_save_golden_concurrent_no_lost_updates(projects_dir):
    n = 12
    errors: list[BaseException] = []

    def _save(i):
        g = {"loop": f"l{i}", "baseline_run": f"r{i}", "expect": {"status": "pass", "steps": []}}
        try:
            golden.save_golden("p", f"g{i}", g)
        except BaseException as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=_save, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert set(golden.load_goldens("p")) == {f"g{i}" for i in range(n)}
    assert not list(projects_dir.glob("*.tmp"))


def test_save_golden_failure_cleans_temp(projects_dir, monkeypatch):
    monkeypatch.setattr(golden.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    g = {"loop": "l", "baseline_run": "x", "expect": {"status": "pass", "steps": []}}
    with pytest.raises(OSError, match="boom"):
        golden.save_golden("p", "l", g)
    assert not list(projects_dir.glob("*.tmp"))


def test_replay_trigger_constants():
    assert (replay.TRIGGER_REPLAY, replay.TRIGGER_EVAL) == ("replay", "eval")


# ------------------------------------------------------------------ run_eval
def _fake_eval_record(orig, status="pass"):
    return {
        "id": "eval-1",
        "project": orig["project"],
        "loop": orig["loop"],
        "status": status,
        "spec_hash": "newhash9999",
        "replay_of": orig["id"],
        "steps": [dict(s) for s in orig.get("steps", [])],
    }


def test_run_eval_uses_eval_trigger_and_compares(projects_dir, runs_dir, monkeypatch):
    _write_spec(projects_dir, "p", loops={"l": {"steps": [{"type": "shell", "cmd": "true"}]}})
    orig = _write_run(runs_dir, "p", "orig-1", status="pass", loop="l", steps=_STEPS)
    g = golden.capture_golden("p", "l", "orig-1", record=orig)
    golden.save_golden("p", "l", g)
    calls = []

    def _fake_run_loop(*a, **k):
        calls.append(k)
        return _fake_eval_record(orig)

    monkeypatch.setattr(replay, "run_loop", _fake_run_loop)
    golden_, baseline, record, diffs = golden.run_eval("p", "l", "l", actor="t")
    assert calls[0]["trigger"] == replay.TRIGGER_EVAL
    assert calls[0]["replay_of"] == "orig-1"
    assert calls[0]["note"] == "eval of orig-1"
    assert record["id"] == "eval-1"
    assert diffs == []  # identical stable surface
    assert baseline["id"] == "orig-1"


def test_run_eval_detects_regression(projects_dir, runs_dir, monkeypatch):
    _write_spec(projects_dir, "p")
    orig = _write_run(runs_dir, "p", "orig-1", status="pass", loop="l", steps=_STEPS)
    g = golden.capture_golden("p", "l", "orig-1", record=orig)
    golden.save_golden("p", "l", g)
    monkeypatch.setattr(replay, "run_loop", lambda *a, **k: _fake_eval_record(orig, status="fail"))
    _, _, record, diffs = golden.run_eval("p", "l")
    assert record["status"] == "fail"
    assert diffs  # non-empty → regression


def test_run_eval_wrong_loop(projects_dir, runs_dir):
    _write_spec(projects_dir, "p", loops={"other": {"steps": [{"type": "shell", "cmd": "true"}]}})
    orig = _write_run(runs_dir, "p", "orig-1", status="pass", loop="other", steps=[])
    g = golden.capture_golden("p", "other", "orig-1", record=orig)
    golden.save_golden("p", "l1", g)  # golden named l1, but its loop is "other"
    with pytest.raises(golden.GoldenError, match="belongs to loop"):
        golden.run_eval("p", "l1")  # request loop l1, golden says "other"


def test_run_eval_missing_baseline(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_golden(
        projects_dir, "p", "l", {"loop": "l", "baseline_run": "gone", "expect": {"status": "pass", "steps": []}}
    )
    with pytest.raises(golden.GoldenError, match=r"baseline run .* not found"):
        golden.run_eval("p", "l")


# ------------------------------------------------------------------ eval_report
def test_eval_report_no_regression():
    g = {"loop": "l", "baseline_run": "o", "spec_hash": "abc123def456", "expect": {"status": "pass", "steps": []}}
    base = {"id": "o", "status": "pass"}
    rec = {"id": "e", "status": "pass", "spec_hash": "newhash9999"}
    out = golden.eval_report(g, base, rec, [])
    assert "no regression" in out
    assert "abc123def456" in out
    assert "newhash9999" in out


def test_eval_report_regression():
    out = golden.eval_report(
        _golden_with({"status": "pass", "steps": []}),
        {"id": "o", "status": "pass"},
        {"id": "e", "status": "fail"},
        ["status: expected 'pass', eval yields 'fail'"],
    )
    assert "REGRESSION" in out


# ------------------------------------------------------------------ CLI
def test_cmd_golden_capture(projects_dir, runs_dir, capsys):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "orig-1", status="pass", loop="l", steps=_STEPS)
    args = type("A", (), {"golden_cmd": "capture", "project": "p", "loop": "l", "run_id": "orig-1", "name": None})()
    cli.cmd_golden(args)  # success path: no SystemExit
    assert "orig-1" in capsys.readouterr().out
    assert (projects_dir / "p.golden.json").is_file()
    assert list(golden.load_goldens("p")) == ["l"]


def test_cmd_golden_show(projects_dir, capsys):
    _write_golden(
        projects_dir,
        "p",
        "l",
        {
            "loop": "l",
            "baseline_run": "x",
            "spec_hash": "abc123",
            "expect": {"status": "pass", "steps": [{"name": "a", "status": "pass", "verdict": ""}]},
        },
    )
    cli.cmd_golden(type("A", (), {"golden_cmd": "show", "project": "p", "name": None})())
    out = capsys.readouterr().out
    assert "l  loop=l" in out
    assert "baseline=x" in out


def test_cmd_golden_show_one(projects_dir, capsys):
    _write_golden(
        projects_dir,
        "p",
        "l1",
        {"loop": "l", "baseline_run": "x", "spec_hash": "", "expect": {"status": "pass", "steps": []}},
    )
    _write_golden(
        projects_dir,
        "p",
        "l2",
        {"loop": "l", "baseline_run": "y", "spec_hash": "", "expect": {"status": "pass", "steps": []}},
    )
    cli.cmd_golden(type("A", (), {"golden_cmd": "show", "project": "p", "name": "l1"})())
    out = capsys.readouterr().out
    assert "l1" in out and "l2" not in out


def _stub_eval(monkeypatch, diffs):
    base = {"id": "orig-1", "project": "p", "loop": "l", "status": "pass", "spec_hash": "abc123def456"}
    rec = _fake_eval_record(base)
    g = {"loop": "l", "baseline_run": "orig-1", "spec_hash": "abc123def456", "expect": {"status": "pass", "steps": []}}
    monkeypatch.setattr(cli, "run_eval", lambda *a, **k: (g, base, rec, diffs))
    monkeypatch.setattr(cli, "claim_run", lambda *a, **k: type("C", (), {"scope": "p", "release": lambda self: None})())


def test_cmd_eval_pass(projects_dir, capsys, monkeypatch):
    _write_golden(projects_dir, "p", "l", {"loop": "l", "baseline_run": "x", "expect": {"status": "pass", "steps": []}})
    _stub_eval(monkeypatch, [])
    with pytest.raises(SystemExit) as e:
        cli.cmd_eval(type("A", (), {"project": "p", "loop": "l", "name": None, "yes": True})())
    assert e.value.code == 0
    assert "no regression" in capsys.readouterr().out


def test_cmd_eval_regression(projects_dir, capsys, monkeypatch):
    _write_golden(projects_dir, "p", "l", {"loop": "l", "baseline_run": "x", "expect": {"status": "pass", "steps": []}})
    _stub_eval(monkeypatch, ["status: expected 'pass', eval yields 'fail'"])
    with pytest.raises(SystemExit) as e:
        cli.cmd_eval(type("A", (), {"project": "p", "loop": "l", "name": None, "yes": True})())
    assert e.value.code == 1
    assert "REGRESSION" in capsys.readouterr().out


def test_cmd_eval_unknown_golden(projects_dir, capsys):
    with pytest.raises(SystemExit) as e:
        cli.cmd_eval(type("A", (), {"project": "p", "loop": "l", "name": "nope", "yes": True})())
    assert e.value.code == cli.EXIT_ERROR
    assert "unknown" in capsys.readouterr().out


def test_cmd_eval_no_tty_without_yes_aborts(projects_dir, capsys, monkeypatch):
    _write_golden(projects_dir, "p", "l", {"loop": "l", "baseline_run": "x", "expect": {"status": "pass", "steps": []}})
    calls = []
    monkeypatch.setattr(cli, "run_eval", lambda *a, **k: calls.append(1))
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    with pytest.raises(SystemExit) as e:
        cli.cmd_eval(type("A", (), {"project": "p", "loop": "l", "name": None, "yes": False})())
    assert e.value.code == cli.EXIT_ERROR
    assert "--yes" in capsys.readouterr().out
    assert calls == []
