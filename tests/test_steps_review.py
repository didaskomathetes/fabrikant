"""Tests: review-Step (Cross-Model Cold Critic, ADR 0003, Issue #12)."""

import pytest

import fabrikant.steps.agent as agent_mod
from fabrikant.backends import registry
from fabrikant.engine import run_loop
from fabrikant.steps.review import _parse_verdict, _step_review


def _ctx(**extra):
    return {"project": "p", "run_id": "r1", "workdir": "/tmp/wd", **extra}


class _ReviewFake:
    """Fake opencode-Backend: Final-Text je Modell aus _RESPONSES."""

    @classmethod
    def create(cls, step, endpoint, model, stale_after):
        return cls(endpoint, model, stale_after=stale_after)

    def __init__(self, endpoint, model, stale_after=900):
        self._model = model
        self._n = 0

    def health(self):
        return True

    def create_task(self, workdir, label, permissions=None):
        return "s-fake"

    def send_prompt(self, sid, prompt, workdir):
        pass

    def poll(self, sid, workdir):
        self._n += 1
        if self._n == 1:
            return {"status": "running", "text": "", "tools": [], "tokens": {"input": 1, "output": 1}, "cost": 0}
        return {
            "status": "done",
            "text": _RESPONSES.get(self._model, "kein json hier"),
            "tools": [],
            "tokens": {"input": 3, "output": 2},
            "cost": 0.001,
        }

    def cancel(self, sid, workdir):
        pass

    def transcript(self, sid, workdir):
        return []


_RESPONSES = {
    "critic-1": '{"verdict": "approved", "findings": [], "summary": "sauber"}',
    "critic-2": (
        '{"verdict": "changes_requested", "findings": '
        '[{"severity": "high", "text": "Bug in Zeile 5"}, {"severity": "low", "text": "Tippfehler"}], '
        '"summary": "muss fixen"}'
    ),
}


@pytest.fixture
def fake_agent(monkeypatch):
    monkeypatch.setitem(registry.ADAPTERS, "opencode", _ReviewFake)
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)


def _step(model="critic-1", **extra):
    return {"name": "rev", "type": "review", "model": model, "backend": "opencode", "endpoint": "http://x:1", **extra}


def _noop(*a):
    pass


# ---------------------------------------------------------------- Parsing
def test_parse_verdict_ok():
    d = _parse_verdict('Vorwort\n{"verdict": "approved", "findings": [], "summary": "ok"}\nNachwort')
    assert d["verdict"] == "approved"
    assert d["findings"] == []


def test_parse_verdict_bad():
    assert _parse_verdict("kein json") is None
    assert _parse_verdict('{"verdict": "vielleicht", "findings": []}') is None
    assert _parse_verdict("") is None


# ---------------------------------------------------------------- Unit: Verdict-Pfade
def test_review_approved(fake_agent):
    res = _step_review(_ctx(last_agent_model="builder-model", last_agent_output="Builder-Output"), _step(), _noop)
    assert res["ok"] is True
    assert res["review"]["verdict"] == "approved"
    assert res["review"]["model"] == "critic-1"
    assert res["usage"]["prompt_tokens"] == 3


def test_review_changes_requested(fake_agent):
    res = _step_review(_ctx(last_agent_output="Builder-Output"), _step("critic-2"), _noop)
    assert res["ok"] is False
    assert res["review"]["verdict"] == "changes_requested"
    assert len(res["review"]["findings"]) == 2
    assert "changes_requested" in res["summary"]
    assert "Bug in Zeile 5" in res["summary"]


def test_review_model_must_differ_from_builder(fake_agent):
    res = _step_review(_ctx(last_agent_model="critic-1", last_agent_output="x"), _step("critic-1"), _noop)
    assert res["ok"] is False
    assert "≠ Builder-Modell" in res["summary"]


def test_review_no_target(fake_agent):
    res = _step_review(_ctx(), _step(), _noop)
    assert res["ok"] is False
    assert "nichts zu reviewen" in res["summary"]


def test_review_unparseable_output(fake_agent):
    res = _step_review(_ctx(last_agent_output="x"), _step("kein-model"), _noop)
    assert res["ok"] is False
    assert "kein JSON-Verdict" in res["summary"]


def test_review_uses_explicit_target(fake_agent, monkeypatch):
    captured = {}

    class _Cap(_ReviewFake):
        def send_prompt(self, sid, prompt, workdir):
            captured["prompt"] = prompt

    monkeypatch.setitem(registry.ADAPTERS, "opencode", _Cap)
    _step_review(_ctx(), _step(target="EXPLIZIT-ZIEL {project}"), _noop)
    assert "EXPLIZIT-ZIEL p" in captured["prompt"]


# ---------------------------------------------------------------- Engine-Level
def _write_spec(projects_dir, tmp_path, name, steps_yaml):
    root = tmp_path / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    body = "\n".join("    " + ln for ln in steps_yaml.strip().splitlines())
    (projects_dir / f"{name}.fabrikant.yaml").write_text(
        f"name: {name}\nroot: {root}\nloops:\n  s:\n    steps:\n{body}\n"
    )
    return root


class _DualFake:
    """Builder + Reviewer in einem Fake: Final-Text je Modell."""

    @classmethod
    def create(cls, step, endpoint, model, stale_after):
        return cls(endpoint, model, stale_after=stale_after)

    def __init__(self, endpoint, model, stale_after=900):
        self._model = model
        self._n = 0

    def health(self):
        return True

    def create_task(self, workdir, label, permissions=None):
        return f"s-{self._model}"

    def send_prompt(self, sid, prompt, workdir):
        pass

    def poll(self, sid, workdir):
        self._n += 1
        if self._n == 1:
            return {"status": "running", "text": "", "tools": [], "tokens": {"input": 1, "output": 1}, "cost": 0}
        if self._model == "builder-model":
            return {
                "status": "done",
                "text": "Builder-Output: alles gebaut",
                "tools": ["bash"],
                "tokens": {"input": 4, "output": 2},
                "cost": 0.0005,
            }
        return {
            "status": "done",
            "text": _RESPONSES.get(self._model, "keine"),
            "tools": [],
            "tokens": {"input": 3, "output": 2},
            "cost": 0.001,
        }

    def cancel(self, sid, workdir):
        pass

    def transcript(self, sid, workdir):
        return []


_SPEC_AGENT_REVIEW = (
    "- name: build\n  type: agent\n  backend: opencode\n"
    "  endpoint: http://127.0.0.1:9\n  model: builder-model\n  prompt: Bau was\n"
    "- name: critique\n  type: review\n  backend: opencode\n"
    "  endpoint: http://127.0.0.1:9\n  model: {critic}\n"
)


def test_engine_review_pass(fresh_projects, tmp_path, monkeypatch):
    projects_dir, fresh_db_env = fresh_projects
    monkeypatch.setitem(registry.ADAPTERS, "opencode", _DualFake)
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    _write_spec(projects_dir, tmp_path, "revpass", _SPEC_AGENT_REVIEW.format(critic="critic-1"))
    rec = run_loop("revpass", "s", trigger="test")
    assert rec["status"] == "pass"
    rev = rec["steps"][1]
    assert rev["status"] == "pass"
    assert rev["review"]["verdict"] == "approved"
    # Reviewer hat den Builder-Output als Ziel bekommen (Cross-Model-Funktionsnachweis)
    assert rev["usage"]["model"] == "critic-1"


def test_engine_review_fail(fresh_projects, tmp_path, monkeypatch):
    projects_dir, fresh_db_env = fresh_projects
    monkeypatch.setitem(registry.ADAPTERS, "opencode", _DualFake)
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    _write_spec(projects_dir, tmp_path, "revfail", _SPEC_AGENT_REVIEW.format(critic="critic-2"))
    rec = run_loop("revfail", "s", trigger="test")
    assert rec["status"] == "fail"
    rev = rec["steps"][1]
    assert rev["status"] == "fail"
    assert rev["review"]["verdict"] == "changes_requested"
    assert "Bug in Zeile 5" in rev["summary"]


def test_engine_review_same_model_fails(fresh_projects, tmp_path, monkeypatch):
    projects_dir, fresh_db_env = fresh_projects
    monkeypatch.setitem(registry.ADAPTERS, "opencode", _DualFake)
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    spec = _SPEC_AGENT_REVIEW.format(critic="builder-model")
    _write_spec(projects_dir, tmp_path, "revsame", spec)
    rec = run_loop("revsame", "s", trigger="test")
    assert rec["status"] == "fail"
    assert "≠ Builder-Modell" in rec["steps"][1]["summary"]


@pytest.fixture
def fresh_projects(projects_dir, tmp_path, monkeypatch):
    """Isoliertes Session-Log-File, damit Engine-Tests keinen Stray-DB hinterlassen."""
    monkeypatch.setenv("FABRIKANT_SESSIONLOG", str(tmp_path / "sl" / "test.db"))
    import fabrikant.sessionlog as sl

    monkeypatch.setattr(sl, "_conn", None)
    yield projects_dir, tmp_path / "sl" / "test.db"


# ---------------------------------------------------------------- Fehlerklasse (ADR 0035)
def test_review_kinds(fake_agent):
    assert _step_review(_ctx(last_agent_output="x"), _step("critic-2"), _noop)["failure_kind"] == "outcome"
    assert _step_review(_ctx(), _step(), _noop)["failure_kind"] == "environment"
    assert _step_review(_ctx(last_agent_output="x"), _step(model=""), _noop)["failure_kind"] == "environment"
