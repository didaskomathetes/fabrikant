"""Engine: bedingtes Gate `approval_when` (ADR 0017).

Der Step pausiert VOR der Ausführung nur, wenn die Run-Variablen die Bedingung
erfüllen (AND über Variablen, OR über Werte). Sonst läuft er ohne Pause durch.
Resume führt den gepausten Step aus, ohne erneut zu gaten.
"""

from __future__ import annotations

import pytest

from fabrikant.engine import _approval_condition, run_loop


def _write_spec(projects_dir, tmp_path, name, steps_yaml, tier=None):
    root = tmp_path / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    body = "\n".join("    " + ln for ln in steps_yaml.strip().splitlines())
    lines = [f"name: {name}", f"root: {root}", "loops:", "  s:"]
    if tier is not None:
        lines.append(f"    tier: {tier}")
    lines += ["    steps:", body]
    (projects_dir / f"{name}.fabrikant.yaml").write_text("\n".join(lines) + "\n")


_STEPS = (
    "- name: gated\n  type: shell\n  cmd: echo gated-ran\n"
    "  approval_when:\n    label: [needs approval, close]\n"
    "- name: after\n  type: shell\n  cmd: echo after\n"
)


# ---------------------------------------------------------------- _approval_condition
@pytest.mark.parametrize(
    ("step", "ctx", "expected"),
    [
        ({}, {"label": "close"}, None),
        ({"approval": True}, {}, ""),
        ({"approval_when": {"label": "close"}}, {"label": "close"}, "label = close"),
        ({"approval_when": {"label": ["a", "close"]}}, {"label": "close"}, "label = close"),
        ({"approval_when": {"label": "close"}}, {"label": "ready for development"}, None),
        ({"approval_when": {"label": "close"}}, {}, None),  # fehlende Variable
        ({"approval_when": {"label": ""}}, {}, None),  # leerer Sollwert + fehlende Variable
        ({"approval_when": {"label": ["", "x"]}}, {}, None),  # leerer Wert in Liste + fehlende Variable
        ({"approval_when": {"n": 1}}, {"n": "1"}, "n = 1"),  # Vergleich als String
        ({"approval_when": {"a": "x", "b": "y"}}, {"a": "x", "b": "y"}, "a = x, b = y"),
        ({"approval_when": {"a": "x", "b": "y"}}, {"a": "x", "b": "z"}, None),  # AND
    ],
)
def test_approval_condition(step, ctx, expected):
    assert _approval_condition(step, ctx) == expected


# ---------------------------------------------------------------- run_loop
def test_condition_met_pauses_and_resume_runs_step(runs_dir, projects_dir, tmp_path):
    _write_spec(projects_dir, tmp_path, "aw_pause", _STEPS)
    rec = run_loop("aw_pause", "s", initial_vars={"label": "close"})
    assert rec["status"] == "needs_approval"
    assert rec["waiting_step"] == "gated"
    assert rec["approval"]["condition"] == "label = close"
    assert [s["status"] for s in rec["steps"]] == ["waiting"]

    rec2 = run_loop("aw_pause", "s", resume_id=rec["id"])
    assert rec2["status"] == "pass"
    assert [(s["name"], s["status"]) for s in rec2["steps"]] == [("gated", "pass"), ("after", "pass")]
    log = (runs_dir / "aw_pause" / f"{rec['id']}.log").read_text()
    assert "bedingung: label = close" in log


def test_condition_not_met_runs_through(runs_dir, projects_dir, tmp_path):
    _write_spec(projects_dir, tmp_path, "aw_through", _STEPS)
    rec = run_loop("aw_through", "s", initial_vars={"label": "ready for development"})
    assert rec["status"] == "pass"
    assert "approval" not in rec
    assert [s["status"] for s in rec["steps"]] == ["pass", "pass"]


def test_empty_allowed_string_missing_var_runs_through(runs_dir, projects_dir, tmp_path):
    """Regression: leerer Sollwert + fehlende Variable → kein Gate, kein KeyError."""
    steps = (
        "- name: gated\n  type: shell\n  cmd: echo gated-ran\n"
        '  approval_when:\n    label: ""\n'
        "- name: after\n  type: shell\n  cmd: echo after\n"
    )
    _write_spec(projects_dir, tmp_path, "aw_empty", steps)
    rec = run_loop("aw_empty", "s", initial_vars={})  # `label` fehlt
    assert rec["status"] == "pass"
    assert "approval" not in rec
    assert [s["status"] for s in rec["steps"]] == ["pass", "pass"]


def test_unmet_condition_does_not_replace_tier_gate(runs_dir, projects_dir, tmp_path):
    """T2: ein unerfülltes approval_when darf das End-Gate nicht aushebeln."""
    _write_spec(projects_dir, tmp_path, "aw_tier", _STEPS, tier=2)
    rec = run_loop("aw_tier", "s", initial_vars={"label": "ready for development"})
    assert rec["status"] == "needs_approval"
    assert rec["waiting_step"] == "tier-approval"
