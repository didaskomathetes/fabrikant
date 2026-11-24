"""Step: human_gate (ADR 0010) — Kritizitäts-Check, Pause bei kritisch, fail-closed."""

from __future__ import annotations

import pytest

from fabrikant import gitops
from fabrikant.steps import human_gate

FILES_ADR = [{"filename": "docs/adr/0001-x.md", "additions": 1, "deletions": 1, "patch": "+x"}]
FILES_SMALL = [{"filename": "src/a.py", "additions": 5, "deletions": 2, "patch": "+x"}]
FILES_KEYWORD = [{"filename": "src/db.py", "additions": 10, "deletions": 0, "patch": "+ DROP TABLE users"}]


def _ctx():
    return {"project": "p", "loop": "l", "run_id": "r1", "workdir": "/tmp", "pr_number": "7"}


def _patch(monkeypatch, files, llm=None, llm_ok=True):
    """GitHub-API + LLM faken. llm=None → LLM-Call soll nicht stattfinden."""
    calls = {"api": [], "llm": 0}
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")

    def _api(method, url, token, payload=None, accept=None):
        calls["api"].append(url)
        return files

    monkeypatch.setattr(gitops, "_gh_api", _api)

    def _llm(ctx, step, emit):
        calls["llm"] += 1
        calls["llm_step"] = step
        if llm is None and llm_ok:
            raise AssertionError("LLM-Call ohne konfiguriertes Ergebnis (llm: false erwartet)")
        if not llm_ok:
            return {"ok": False, "summary": "provider down", "output": "garbage"}
        if llm.get("gate_critical"):
            ctx["gate_critical"] = True
            ctx["gate_reason"] = llm.get("gate_reason", "kritisch")
        return {"ok": True, "summary": "ok", "usage": {"total_tokens": 1}}

    monkeypatch.setattr(human_gate, "_step_llm", _llm)
    return calls


def test_adr_path_triggers_pause(monkeypatch):
    calls = _patch(monkeypatch, FILES_ADR, llm={"gate_critical": False})
    ctx = _ctx()
    res = human_gate._step_human_gate(ctx, {"repo": "o/r"}, lambda m: None)
    assert res["needs_approval"] is True
    assert "ADR" in res["approval"]["what"]
    assert ctx["human_gate_passed"] == "true"  # Flag für Resume persistierbar
    assert calls["llm"] == 0  # Heuristik reicht — kein LLM-Call


def test_diff_size_triggers_pause(monkeypatch):
    files = [{"filename": f"f{i}.py", "additions": 1, "deletions": 0, "patch": "+x"} for i in range(50)]
    _patch(monkeypatch, files, llm={"gate_critical": False})
    res = human_gate._step_human_gate(_ctx(), {"repo": "o/r"}, lambda m: None)
    assert res["needs_approval"] is True
    assert "Dateien" in res["approval"]["what"]


def test_diff_lines_triggers_pause(monkeypatch):
    files = [{"filename": "big.py", "additions": 2000, "deletions": 0, "patch": "+x"}]
    _patch(monkeypatch, files, llm={"gate_critical": False})
    res = human_gate._step_human_gate(_ctx(), {"repo": "o/r"}, lambda m: None)
    assert res["needs_approval"] is True
    assert "Zeilen" in res["approval"]["what"]


def test_keyword_triggers_pause(monkeypatch):
    _patch(monkeypatch, FILES_KEYWORD, llm={"gate_critical": False})
    res = human_gate._step_human_gate(_ctx(), {"repo": "o/r"}, lambda m: None)
    assert res["needs_approval"] is True
    assert "Keyword" in res["approval"]["what"]


def test_llm_critical_triggers_pause(monkeypatch):
    calls = _patch(monkeypatch, FILES_SMALL, llm={"gate_critical": True, "gate_reason": "Architektur-Change"})
    res = human_gate._step_human_gate(_ctx(), {"repo": "o/r"}, lambda m: None)
    assert res["needs_approval"] is True
    assert "Architektur-Change" in res["approval"]["what"]
    assert calls["llm"] == 1


def test_not_critical_passes(monkeypatch):
    calls = _patch(monkeypatch, FILES_SMALL, llm={"gate_critical": False, "gate_reason": "ok"})
    res = human_gate._step_human_gate(_ctx(), {"repo": "o/r"}, lambda m: None)
    assert res["ok"] is True
    assert res.get("needs_approval") is None
    assert "nicht kritisch" in res["summary"]
    assert calls["llm"] == 1


def test_llm_failure_fails_closed(monkeypatch):
    _patch(monkeypatch, FILES_SMALL, llm=None, llm_ok=False)
    res = human_gate._step_human_gate(_ctx(), {"repo": "o/r"}, lambda m: None)
    assert res["ok"] is False
    assert "fail-closed" in res["summary"]
    assert res["output"] == "garbage"  # Roh-Antwort fürs Debugging im Run-Record


def test_llm_provider_and_budget_passthrough(monkeypatch):
    """provider/model/max_tokens/timeout aus der Step-Def landen im llm-Step;
    ohne Angabe gilt dessen Default (kein hartes Mini-Budget)."""
    calls = _patch(monkeypatch, FILES_SMALL, llm={"gate_critical": False})
    step = {"repo": "o/r", "provider": "copilot", "model": "m", "max_tokens": 4000, "timeout": 60}
    human_gate._step_human_gate(_ctx(), step, lambda m: None)
    sent = calls["llm_step"]
    assert (sent["provider"], sent["model"], sent["max_tokens"], sent["timeout"]) == ("copilot", "m", 4000, 60)
    human_gate._step_human_gate(_ctx(), {"repo": "o/r"}, lambda m: None)
    assert not {"provider", "model", "max_tokens", "timeout"} & calls["llm_step"].keys()


def test_llm_disabled_skips_llm(monkeypatch):
    calls = _patch(monkeypatch, FILES_SMALL, llm=None)
    res = human_gate._step_human_gate(_ctx(), {"repo": "o/r", "llm": False}, lambda m: None)
    assert res["ok"] is True
    assert calls["llm"] == 0


def test_resume_after_approval_skips_check(monkeypatch):
    calls = _patch(monkeypatch, FILES_ADR, llm={"gate_critical": False})
    ctx = _ctx()
    ctx["human_gate_passed"] = "true"  # aus record["vars"] nach Resume
    res = human_gate._step_human_gate(ctx, {"repo": "o/r"}, lambda m: None)
    assert res["ok"] is True
    assert "Resume" in res["summary"]
    assert calls["api"] == []  # kein API-Call beim Resume


def test_custom_thresholds(monkeypatch):
    _patch(monkeypatch, FILES_SMALL, llm={"gate_critical": False})
    res = human_gate._step_human_gate(_ctx(), {"repo": "o/r", "max_lines": 3}, lambda m: None)
    assert res["needs_approval"] is True


def test_missing_repo_raises():
    with pytest.raises(ValueError, match="repo"):
        human_gate._step_human_gate(_ctx(), {}, lambda m: None)
