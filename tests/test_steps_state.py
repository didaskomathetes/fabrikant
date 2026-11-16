"""Steps: state (persistenter State über Runs hinweg)."""

from __future__ import annotations

import json

import pytest

from fabrik.steps.state import _step_state


def _ctx(tmp_path, project="p"):
    return {"root": str(tmp_path), "project": project, "run_id": "r1"}


def _default_file(tmp_path, project="p"):
    return tmp_path / "state" / f"{project}.json"


def test_get_missing_file_is_empty(tmp_path):
    ctx = _ctx(tmp_path)
    res = _step_state(ctx, {"type": "state", "action": "get", "key": "x"}, lambda _m: None)
    assert res["ok"] is True
    assert res["state_value"] == ""
    assert ctx["state_value"] == ""


def test_set_writes_file_and_emits(tmp_path):
    ctx = _ctx(tmp_path)
    res = _step_state(ctx, {"type": "state", "action": "set", "key": "x", "value": "hello"}, lambda _m: None)
    assert res["ok"] is True
    assert res["state_value"] == "hello"
    assert ctx["state_value"] == "hello"
    data = json.loads(_default_file(tmp_path).read_text())
    assert data == {"x": "hello"}


def test_get_after_set(tmp_path):
    ctx = _ctx(tmp_path)
    _step_state(ctx, {"type": "state", "action": "set", "key": "x", "value": "v1"}, lambda _m: None)
    ctx2 = _ctx(tmp_path)
    res = _step_state(ctx2, {"type": "state", "action": "get", "key": "x"}, lambda _m: None)
    assert res["state_value"] == "v1"
    assert ctx2["state_value"] == "v1"


def test_set_overwrites_existing(tmp_path):
    ctx = _ctx(tmp_path)
    _step_state(ctx, {"type": "state", "action": "set", "key": "x", "value": "a"}, lambda _m: None)
    res = _step_state(ctx, {"type": "state", "action": "set", "key": "x", "value": "b"}, lambda _m: None)
    assert res["state_value"] == "b"
    assert json.loads(_default_file(tmp_path).read_text()) == {"x": "b"}


def test_add_numeric_accumulates(tmp_path):
    ctx = _ctx(tmp_path)
    for _ in range(3):
        _step_state(ctx, {"type": "state", "action": "add", "key": "count", "value": "1"}, lambda _m: None)
    data = json.loads(_default_file(tmp_path).read_text())
    assert data == {"count": 3}


def test_add_accumulates_over_set_string(tmp_path):
    """`set` speichert einen String; `add` muss ihn numerisch coerzen (3 + 4 = 7)."""
    ctx = _ctx(tmp_path)
    _step_state(ctx, {"type": "state", "action": "set", "key": "n", "value": "3"}, lambda _m: None)
    res = _step_state(ctx, {"type": "state", "action": "add", "key": "n", "value": "4"}, lambda _m: None)
    assert res["state_value"] == 7
    assert json.loads(_default_file(tmp_path).read_text()) == {"n": 7}


def test_add_non_numeric_appends_list(tmp_path):
    ctx = _ctx(tmp_path)
    _step_state(ctx, {"type": "state", "action": "add", "key": "seen", "value": "a"}, lambda _m: None)
    res = _step_state(ctx, {"type": "state", "action": "add", "key": "seen", "value": "b"}, lambda _m: None)
    assert json.loads(_default_file(tmp_path).read_text()) == {"seen": ["a", "b"]}
    # state_value ist die serialisierte Liste
    assert json.loads(res["state_value"]) == ["a", "b"]


def test_value_substitution(tmp_path):
    ctx = _ctx(tmp_path)
    ctx["issue_number"] = "42"
    res = _step_state(
        ctx, {"type": "state", "action": "set", "key": "last", "value": "issue-{issue_number}"}, lambda _m: None
    )
    assert res["state_value"] == "issue-42"


def test_custom_file(tmp_path):
    ctx = _ctx(tmp_path)
    res = _step_state(
        ctx, {"type": "state", "action": "set", "key": "k", "value": "v", "file": "state/custom.json"}, lambda _m: None
    )
    assert res["ok"] is True
    assert (tmp_path / "state" / "custom.json").is_file()
    assert not _default_file(tmp_path).exists()


def test_no_partial_file_on_crash(tmp_path, monkeypatch):
    """Atomic-Write: ein Fehler beim Dump hinterlässt keine .tmp-Datei (Step re-raise, Engine fängt)."""
    import fabrik.steps.state as st

    ctx = _ctx(tmp_path)

    def _boom(*a, **k):
        raise ValueError("simulated crash")

    monkeypatch.setattr(st.json, "dump", _boom)
    with pytest.raises(ValueError):
        _step_state(ctx, {"type": "state", "action": "set", "key": "x", "value": "v"}, lambda _m: None)
    # keine .tmp-Rückstände in state/
    leftovers = [p for p in (tmp_path / "state").glob(".state-*.tmp")] if (tmp_path / "state").exists() else []
    assert leftovers == []


def test_path_traversal_blocked(tmp_path):
    ctx = _ctx(tmp_path)
    res = _step_state(
        ctx, {"type": "state", "action": "set", "key": "x", "value": "v", "file": "../escape.json"}, lambda _m: None
    )
    assert res["ok"] is False
    assert "BLOCKED" in res["summary"]
    assert not (tmp_path.parent / "escape.json").exists()


def test_unknown_action_fails(tmp_path):
    ctx = _ctx(tmp_path)
    res = _step_state(ctx, {"type": "state", "action": "delete", "key": "x"}, lambda _m: None)
    assert res["ok"] is False
    assert "unbekannte action" in res["summary"]


def test_missing_key_fails(tmp_path):
    ctx = _ctx(tmp_path)
    res = _step_state(ctx, {"type": "state", "action": "set"}, lambda _m: None)
    assert res["ok"] is False
    assert "key fehlt" in res["summary"]
