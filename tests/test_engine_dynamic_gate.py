"""Engine: dynamisches Gate (ADR 0010) — Step-Result needs_approval → Pause → Resume.

human_gate entscheidet ZUR LAUFEIT, ob menschliche Freigabe nötig ist.
Der Engine-Pfad ist analog zum statischen `approval: true`, aber Step-getrieben:
Pause mit Approval-Kontext, ctx-Flag persistiert, Resume führt den Step erneut
aus (der Step merkt sich den Zustand per ctx-Variable).
"""

from __future__ import annotations

from fabrikant.engine import run_loop


def _write_spec(projects_dir, tmp_path, name, steps_yaml):
    root = tmp_path / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    body = "\n".join("    " + ln for ln in steps_yaml.strip().splitlines())
    lines = [f"name: {name}", f"root: {root}", "loops:", "  s:", "    steps:", body]
    (projects_dir / f"{name}.fabrikant.yaml").write_text("\n".join(lines) + "\n")


def test_dynamic_gate_pauses_and_resumes(runs_dir, projects_dir, tmp_path, monkeypatch):
    from fabrikant import engine

    calls = {"n": 0}

    def _fake_gate(ctx, step, emit):
        calls["n"] += 1
        if calls["n"] == 1:
            ctx["human_gate_passed"] = "true"  # wie der echte Step
            return {
                "ok": True,
                "needs_approval": True,
                "approval": {"reason": "criticality", "what": "Kritische Änderung: ADR geändert"},
                "summary": "kritisch — Pause",
            }
        assert ctx.get("human_gate_passed") == "true"  # Flag überlebt die Pause
        return {"ok": True, "summary": "freigegeben (Resume)"}

    monkeypatch.setattr(engine, "_step_human_gate", _fake_gate)
    _write_spec(
        projects_dir,
        tmp_path,
        "dyn_gate",
        "- name: work\n  type: shell\n  cmd: echo hi\n"
        "- name: gate\n  type: human_gate\n  repo: o/r\n"
        "- name: merge\n  type: shell\n  cmd: echo merged\n",
    )
    rec = run_loop("dyn_gate", "s")
    assert rec["status"] == "needs_approval"
    assert rec["waiting_step"] == "gate"
    assert rec["approval"]["reason"] == "criticality"
    assert "ADR geändert" in rec["approval"]["what"]
    # ctx-Flag persistiert (Resume-Quelle)
    assert rec["vars"]["human_gate_passed"] == "true"
    # Steps danach nicht ausgeführt
    assert [s["name"] for s in rec["steps"]] == ["work", "gate"]

    rec2 = run_loop("dyn_gate", "s", resume_id=rec["id"])
    assert rec2["status"] == "pass"
    assert calls["n"] == 2
    gate = next(s for s in rec2["steps"] if s["name"] == "gate")
    assert gate["status"] == "pass"
    merge = next(s for s in rec2["steps"] if s["name"] == "merge")
    assert merge["status"] == "pass"


def test_dynamic_gate_not_critical_runs_through(runs_dir, projects_dir, tmp_path, monkeypatch):
    from fabrikant import engine

    monkeypatch.setattr(engine, "_step_human_gate", lambda ctx, step, emit: {"ok": True, "summary": "nicht kritisch"})
    _write_spec(
        projects_dir,
        tmp_path,
        "dyn_gate_ok",
        "- name: gate\n  type: human_gate\n  repo: o/r\n- name: merge\n  type: shell\n  cmd: echo merged\n",
    )
    rec = run_loop("dyn_gate_ok", "s")
    assert rec["status"] == "pass"
    assert "approval" not in rec


def test_dynamic_gate_log_shows_context(runs_dir, projects_dir, tmp_path, monkeypatch):
    from fabrikant import engine

    monkeypatch.setattr(
        engine,
        "_step_human_gate",
        lambda ctx, step, emit: {
            "ok": True,
            "needs_approval": True,
            "approval": {"reason": "criticality", "what": "Kritisch: Datenverlust möglich"},
        },
    )
    _write_spec(projects_dir, tmp_path, "dyn_gate_log", "- name: gate\n  type: human_gate\n  repo: o/r\n")
    rec = run_loop("dyn_gate_log", "s")
    log = (runs_dir / "dyn_gate_log" / f"{rec['id']}.log").read_text()
    assert "wartet auf Approval" in log
    assert "Kritisch: Datenverlust möglich" in log
