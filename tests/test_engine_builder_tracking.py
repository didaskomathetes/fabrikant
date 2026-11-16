"""Engine: Builder-Tracking (letztes Agent-Modell + Backend) für review/pr_review."""

from __future__ import annotations

from fabrik import engine


def test_agent_step_records_builder_model_and_backend(runs_dir, projects_dir, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (projects_dir / "bt.fabrik.yaml").write_text(
        f"name: bt\nroot: {root}\nloops:\n  s:\n    steps:\n"
        "    - name: build\n      type: agent\n      prompt: x\n      variant: medium\n"
        "    - name: after\n      type: shell\n      cmd: 'true'\n"
    )
    seen = {}

    def fake_dispatch(ctx, step, emit):
        if step["type"] == "agent":
            return {"ok": True, "usage": {"model": "builder-m", "provider": "claude_code"}}
        seen.update(ctx)
        return {"ok": True}

    monkeypatch.setattr(engine, "_dispatch_step", fake_dispatch)
    engine.run_loop("bt", "s")
    assert seen["last_agent_model"] == "builder-m"
    assert seen["last_agent_backend"] == "claude_code"
    assert seen["last_agent_variant"] == "medium"
