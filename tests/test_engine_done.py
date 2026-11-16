"""Engine: vorzeitiges, erfolgreiches Run-Ende (`done: True` im Step-Result).

Ein Step meldet "nichts mehr zu tun" (z. B. `pr` ohne Commits): der Run endet
als `pass`, die restlichen Steps werden nicht ausgeführt, sondern als
`skipped` protokolliert. Das Tier-Gate gilt weiterhin.
"""

from __future__ import annotations

import pytest

from fabrik import engine, sessionlog
from fabrik.engine import STEP_SKIPPED, run_loop


def _write_spec(projects_dir, tmp_path, name, steps_yaml, tier=None):
    root = tmp_path / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    body = "\n".join("    " + ln for ln in steps_yaml.strip().splitlines())
    lines = [f"name: {name}", f"root: {root}", "loops:", "  s:"]
    if tier is not None:
        lines.append(f"    tier: {tier}")
    lines += ["    steps:", body]
    (projects_dir / f"{name}.fabrik.yaml").write_text("\n".join(lines) + "\n")
    return root


def _steps(marker_dir):
    """before → stop (meldet done) → after1 → after2; after* hinterlassen Marker-Dateien."""
    return (
        "- name: before\n  type: shell\n  cmd: echo before\n"
        "- name: stop\n  type: shell\n  cmd: echo stop\n"
        f"- name: after1\n  type: shell\n  cmd: touch {marker_dir}/after1\n"
        f"- name: after2\n  type: shell\n  cmd: touch {marker_dir}/after2\n"
    )


@pytest.fixture
def done_on(monkeypatch):
    """Lässt den Step mit dem gegebenen Namen `done: True` melden."""
    names: set[str] = set()
    orig = engine._dispatch_step

    def _fake(ctx, step, emit):
        if step.get("name") in names:
            return {"ok": True, "done": True, "summary": "nichts zu tun"}
        return orig(ctx, step, emit)

    monkeypatch.setattr(engine, "_dispatch_step", _fake)
    return names


def test_done_skips_remaining_steps(runs_dir, projects_dir, tmp_path, done_on):
    done_on.add("stop")
    _write_spec(projects_dir, tmp_path, "done_skip", _steps(tmp_path))
    rec = run_loop("done_skip", "s")
    assert rec["status"] == "pass"
    assert [(s["name"], s["status"]) for s in rec["steps"]] == [
        ("before", "pass"),
        ("stop", "pass"),
        ("after1", STEP_SKIPPED),
        ("after2", STEP_SKIPPED),
    ]
    assert "stop hat den Run beendet" in rec["steps"][2]["summary"]
    assert not (tmp_path / "after1").exists()
    assert not (tmp_path / "after2").exists()
    log = (runs_dir / "done_skip" / f"{rec['id']}.log").read_text()
    assert "2 Step(s) übersprungen" in log
    # Skipped-Steps erhalten wie ausgeführte Steps ein step_done-Event (Session-Log).
    db = sessionlog.connect()
    skipped = [
        r[0]
        for r in db.execute(
            "SELECT step_name FROM events WHERE session_id=? AND kind='step_done' AND status=? ORDER BY seq",
            (rec["id"], STEP_SKIPPED),
        )
    ]
    assert skipped == ["after1", "after2"]


def test_done_on_last_step_skips_nothing(runs_dir, projects_dir, tmp_path, done_on):
    done_on.add("after2")
    _write_spec(projects_dir, tmp_path, "done_last", _steps(tmp_path))
    rec = run_loop("done_last", "s")
    assert rec["status"] == "pass"
    assert [s["status"] for s in rec["steps"]] == ["pass"] * 4
    log = (runs_dir / "done_last" / f"{rec['id']}.log").read_text()
    assert "übersprungen" not in log


def test_done_with_failed_ok_is_still_a_failure(runs_dir, projects_dir, tmp_path, monkeypatch):
    """`done` zählt nur bei ok=True — ein fehlgeschlagener Step bleibt `fail`."""
    orig = engine._dispatch_step

    def _fake(ctx, step, emit):
        if step.get("name") == "stop":
            return {"ok": False, "done": True, "summary": "kaputt"}
        return orig(ctx, step, emit)

    monkeypatch.setattr(engine, "_dispatch_step", _fake)
    _write_spec(projects_dir, tmp_path, "done_fail", _steps(tmp_path))
    rec = run_loop("done_fail", "s")
    assert rec["status"] == "fail"
    assert [s["status"] for s in rec["steps"]] == ["pass", "fail"]


def test_done_keeps_tier_gate_and_resume_skips_rest(runs_dir, projects_dir, tmp_path, done_on):
    """T2: vorzeitiges Ende pausiert am Tier-Gate; Resume führt keine skipped Steps aus."""
    done_on.add("stop")
    _write_spec(projects_dir, tmp_path, "done_tier", _steps(tmp_path), tier=2)
    rec = run_loop("done_tier", "s")
    assert rec["status"] == "needs_approval"
    assert rec["waiting_step"] == "tier-approval"
    assert [s["name"] for s in rec["steps"]] == ["before", "stop", "after1", "after2", "tier-approval"]

    rec2 = run_loop("done_tier", "s", resume_id=rec["id"])
    assert rec2["status"] == "pass"
    assert [s["status"] for s in rec2["steps"][2:4]] == [STEP_SKIPPED, STEP_SKIPPED]
    assert not (tmp_path / "after1").exists()
