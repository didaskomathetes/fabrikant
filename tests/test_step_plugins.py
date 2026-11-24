"""Third-party step plugins: registry, entry points, dispatch, spec validation (ADR 0069)."""

from __future__ import annotations

import pytest

from fabrik import config, engine, sdk, specs
from fabrik.steps import registry
from fabrik.steps.registry import StepPlugin, StepRegistryError, register_step


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(registry, "_PLUGINS", {})
    monkeypatch.setattr(registry, "_entry_points_loaded", True)  # no real entry points in unit tests


def _hello(ctx, step, emit):
    emit("hi")
    return sdk.ok(f"hello {step.get('who', 'world')}")


def _spec(tmp_path, step, **loop):
    return {"name": "p", "root": str(tmp_path), "loops": {"l": {"steps": [step], **loop}}}


def test_register_and_lookup():
    register_step("hello", _hello)
    assert registry.get_step("hello").handler is _hello
    assert "hello" in registry.known_types() and registry.known_types()[: len(config.STEP_TYPES)] == config.STEP_TYPES


@pytest.mark.parametrize("name", ["", "Hello", "1x", "a-b", "shell", "verify"])
def test_register_rejects_bad_or_builtin_names(name):
    with pytest.raises(StepRegistryError):
        register_step(name, _hello)


def test_register_rejects_non_callable_and_duplicates():
    with pytest.raises(StepRegistryError, match="not callable"):
        register_step("x", "nope")  # type: ignore[arg-type]
    register_step("x", _hello)
    with pytest.raises(StepRegistryError, match="already"):
        register_step("x", _hello)
    register_step("x", _hello, replace=True)


def test_dispatch_runs_plugin_and_builtins_still_win(monkeypatch):
    register_step("hello", _hello)
    lines: list[str] = []
    res = engine._dispatch_step({}, {"type": "hello", "who": "you"}, lines.append)
    assert res == {"ok": True, "summary": "hello you"} and lines == ["hi"]
    assert engine._dispatch_step({}, {"type": "report", "text": "t"}, lines.append)["summary"] == "t"


def test_dispatch_unknown_type_raises():
    with pytest.raises(ValueError, match="nope"):
        engine._dispatch_step({}, {"type": "nope"}, lambda _m: None)


def test_spec_accepts_registered_plugin_and_rejects_unregistered(tmp_path):
    step = {"type": "hello", "who": "x"}
    with pytest.raises(ValueError, match="'hello'"):
        specs._validate_spec(_spec(tmp_path, step))
    register_step("hello", _hello)
    specs._validate_spec(_spec(tmp_path, step))


def test_plugin_schema_is_enforced(tmp_path):
    schema = {"type": "object", "required": ["who"], "properties": {"who": {"type": "string"}}}
    register_step("hello", _hello, schema=schema)
    specs._validate_spec(_spec(tmp_path, {"type": "hello", "who": "x"}))
    with pytest.raises(ValueError, match="who"):
        specs._validate_spec(_spec(tmp_path, {"type": "hello"}))
    with pytest.raises(ValueError, match="string"):
        specs._validate_spec(_spec(tmp_path, {"type": "hello", "who": 3}))


def test_plugin_can_be_a_verify_check(tmp_path):
    register_step("hello", _hello)
    specs._validate_spec(_spec(tmp_path, {"type": "verify", "checks": [{"type": "hello"}]}))


def test_needs_worktree_plugin_is_rejected_in_worktree_free_loop(tmp_path):
    register_step("edit", _hello, needs_worktree=True)
    assert "edit" in registry.worktree_types()
    with pytest.raises(ValueError, match="edit"):
        specs._validate_spec(_spec(tmp_path, {"type": "edit"}, worktree=False))


def test_entry_points_are_loaded_once_and_broken_ones_skipped(monkeypatch, caplog):
    class EP:
        def __init__(self, name, target):
            self.name, self._t = name, target

        def load(self):
            if isinstance(self._t, Exception):
                raise self._t
            return self._t

    eps = [
        EP("good", _hello),
        EP("rich", StepPlugin("rich", _hello, schema={"type": "object"}, needs_worktree=True)),
        EP("broken", ImportError("boom")),
        EP("shell", _hello),  # shadows a built-in -> rejected, skipped
    ]
    monkeypatch.setattr(
        "importlib.metadata.entry_points", lambda group: eps if group == registry.ENTRY_POINT_GROUP else []
    )
    monkeypatch.setattr(registry, "_entry_points_loaded", False)
    assert registry.plugin_types() == ["good", "rich"]
    assert registry.get_step("rich").needs_worktree
    assert "broken" in caplog.text and "shell" in caplog.text
    eps.append(EP("late", _hello))
    assert "late" not in registry.plugin_types()  # discovered once per process
    registry.load_entry_points(force=True)
    assert "late" in registry.plugin_types()


def test_sdk_helpers():
    assert sdk.ok("fine", output="o") == {"ok": True, "summary": "fine", "output": "o"}
    res = sdk.failed("bad", sdk.FailureKind.OUTCOME)
    assert res["ok"] is False and res["summary"] == "bad"
    assert sdk.substitute("a {x}", {"x": 1}) == "a 1"
