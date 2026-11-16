"""Engine: Token-Usage und Kosten eines fehlgeschlagenen Steps im Session-Log.

Ein fehlgeschlagener Step (``ok: False``) trägt sein Usage im ``step_done``-
Event — die Summen aus diesen Events sind die einzige Total-Quelle
(:func:`fabrik.sessionlog.session_totals`), damit geht die Kosten eines
Fehlschlags nicht verloren.
"""

from __future__ import annotations

import json

from fabrik import engine, sessionlog
from fabrik.engine import run_loop

USAGE = {"prompt_tokens": 3, "completion_tokens": 4, "cost_usd": 0.5}


def _write_spec(projects_dir, tmp_path, name):
    root = tmp_path / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    (projects_dir / f"{name}.fabrik.yaml").write_text(
        f"name: {name}\nroot: {root}\nloops:\n  s:\n    steps:\n"
        "    - name: broken\n      type: agent\n      prompt: x\n"
    )
    return root


def test_failed_step_usage_lands_in_step_done_event(runs_dir, projects_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine,
        "_dispatch_step",
        lambda ctx, step, emit: {
            "ok": False,
            "summary": "kaputt",
            "usage": USAGE,
            "failure_kind": "outcome",
        },
    )
    _write_spec(projects_dir, tmp_path, "fail_usage")
    rec = run_loop("fail_usage", "s")
    assert rec["status"] == "fail"
    assert rec["steps"][0]["status"] == "fail"
    # Das Step-Record behält das Usage ebenfalls (Tracing-Baustein).
    assert rec["steps"][0]["usage"] == USAGE

    # Das step_done-Event des fehlgeschlagenen Steps trägt das Usage.
    db = sessionlog.connect()
    rows = db.execute(
        "SELECT data FROM events WHERE session_id=? AND kind='step_done' AND step_name='broken'",
        (rec["id"],),
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["usage"] == USAGE

    # Session-Summen (aus step_done-Events) enthalten das Usage des Fehlschlags.
    assert sessionlog.session_totals(rec["id"]) == {"tokens_in": 3, "tokens_out": 4, "cost_usd": 0.5}
