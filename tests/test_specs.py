"""Spec-Validierung: alle _validate_spec-Fehlerzweige + load_spec/load_projects-Edge-Cases.

Komplementär zu test_core.py (dort: root-Expansion, verify-Checks). Hier: die
übrigen Validierungs-Fehlerpfade, damit jeder Zweig von _validate_spec gedeckt ist.
"""

import pytest

from fabrikant import config, specs
from fabrikant.backends import known_backends


def _valid_spec(root: str) -> dict:
    """Minimal gültige Spec (root muss ein existierendes Verzeichnis sein)."""
    return {
        "name": "p",
        "root": root,
        "loops": {
            "l1": {
                "steps": [{"type": "shell", "cmd": "echo hi"}],
            }
        },
    }


def _step(spec: dict, step: dict) -> dict:
    """Ersetzt den einzigen Step des einzigen Loops und liefert die Spec."""
    spec["loops"]["l1"]["steps"] = [step]
    return spec


# ---------------------------------------------------------------- _validate_spec
def test_validate_ok(tmp_path):
    specs._validate_spec(_valid_spec(str(tmp_path)))  # kein Raise


def test_sandboxed_shell_requires_worktree(tmp_path):
    spec = _step(_valid_spec(str(tmp_path)), {"type": "shell", "cmd": "true", "sandboxed": True})
    spec["loops"]["l1"]["worktree"] = False
    with pytest.raises(ValueError, match="sandboxed shell"):
        specs._validate_spec(spec)


def test_validate_root_missing(tmp_path):
    with pytest.raises(ValueError, match="root"):
        specs._validate_spec(_valid_spec(str(tmp_path / "nope")))


def test_validate_no_loops(tmp_path):
    s = _valid_spec(str(tmp_path))
    s["loops"] = {}
    with pytest.raises(ValueError, match="loops"):
        specs._validate_spec(s)


def test_validate_loops_not_dict(tmp_path):
    s = _valid_spec(str(tmp_path))
    s["loops"] = ["x"]
    with pytest.raises(ValueError, match="loops"):
        specs._validate_spec(s)


def test_validate_loop_not_mapping(tmp_path):
    s = _valid_spec(str(tmp_path))
    s["loops"]["l1"] = "not-a-dict"
    with pytest.raises(ValueError, match="Mapping erwartet"):
        specs._validate_spec(s)


def test_validate_loop_no_steps(tmp_path):
    s = _valid_spec(str(tmp_path))
    del s["loops"]["l1"]["steps"]
    with pytest.raises(ValueError, match="steps fehlt"):
        specs._validate_spec(s)


def test_validate_bad_tier(tmp_path):
    s = _valid_spec(str(tmp_path))
    s["loops"]["l1"]["tier"] = 9
    with pytest.raises(ValueError, match="tier"):
        specs._validate_spec(s)


def test_validate_bad_step_type(tmp_path):
    # Membership is a semantic check (built-ins + plugins, ADR 0069), not a schema enum.
    with pytest.raises(ValueError, match=r"loop l1 step 0.*'bogus'"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "bogus"}))


@pytest.mark.parametrize("bad_type", [["shell"], {"type": "shell"}, 42, None])
def test_validate_unhashable_step_type_reports_error_not_crash(tmp_path, bad_type):
    """Unhashable/ungewöhnliche `type`-Werte (z. B. `type: [shell]` in YAML)
    dürfen ValueError liefern, nicht TypeError."""
    with pytest.raises(ValueError, match="steps/0/type"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": bad_type}))


def test_validate_shell_no_cmd(tmp_path):
    with pytest.raises(ValueError, match="shell ohne cmd"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "shell"}))


def test_validate_alert_route_ok(tmp_path):
    s = _valid_spec(str(tmp_path))
    s["loops"]["l2"] = {"steps": [{"type": "shell", "cmd": "echo x"}]}
    s["loops"]["l1"]["steps"] = [{"type": "alert_route", "routes": {"bug": "l2"}}]
    specs._validate_spec(s)  # kein Raise


def test_validate_alert_route_cycle_rejected(tmp_path):
    s = _valid_spec(str(tmp_path))
    s["loops"] = {
        "triage": {"steps": [{"type": "alert_route", "routes": {"bug": "hotfix"}}]},
        "hotfix": {"steps": [{"type": "alert_route", "routes": {"bug": "triage"}}]},
    }
    with pytest.raises(ValueError, match="Route-Zyklus"):
        specs._validate_spec(s)


def test_validate_approval_when_ok(tmp_path):
    step = {"type": "shell", "cmd": "x", "approval_when": {"label": ["needs approval", "close"], "n": 1}}
    specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))  # kein Raise


@pytest.mark.parametrize(
    ("approval_when", "extra", "match"),
    [
        ({"label": "close"}, {"approval": True}, "schließen sich aus"),
        ({}, {}, "nicht-leeres Mapping"),
        ("label=close", {}, "nicht-leeres Mapping"),
        ({"label": []}, {}, "approval_when.label"),
        ({"label": [{"x": 1}]}, {}, "approval_when.label"),
    ],
)
def test_validate_approval_when_invalid(tmp_path, approval_when, extra, match):
    step = {"type": "shell", "cmd": "x", "approval_when": approval_when, **extra}
    with pytest.raises(ValueError, match=match):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))


def test_validate_approval_reason(tmp_path):
    ok = {"type": "shell", "cmd": "x", "approval": True, "approval_reason": "{reasoning}"}
    specs._validate_spec(_step(_valid_spec(str(tmp_path)), ok))  # kein Raise
    with pytest.raises(ValueError, match="approval_reason muss ein String"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {**ok, "approval_reason": ["x"]}))


def test_validate_http_no_url(tmp_path):
    with pytest.raises(ValueError, match="http ohne url"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "http"}))


def test_validate_llm_no_prompt(tmp_path):
    with pytest.raises(ValueError, match="llm ohne prompt"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "llm"}))


def test_validate_state_bad_action(tmp_path):
    with pytest.raises(ValueError, match="state action"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "state", "action": "bogus", "key": "k"}))


def test_validate_state_no_key(tmp_path):
    with pytest.raises(ValueError, match="state ohne key"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "state", "action": "get"}))


def test_validate_agent_no_prompt(tmp_path):
    with pytest.raises(ValueError, match="agent ohne prompt"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "agent"}))


def test_validate_agent_bad_backend(tmp_path):
    with pytest.raises(ValueError, match="agent backend"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "agent", "prompt": "x", "backend": "bogus"}))


def test_validate_agent_workdir_not_str(tmp_path):
    with pytest.raises(ValueError, match="workdir muss String"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "agent", "prompt": "x", "workdir": 5}))


def test_validate_agent_timeout_not_positive(tmp_path):
    with pytest.raises(ValueError, match="timeout"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "agent", "prompt": "x", "timeout": -1}))


def test_validate_agent_timeout_non_numeric(tmp_path):
    with pytest.raises(ValueError, match="timeout"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "agent", "prompt": "x", "timeout": "abc"}))


@pytest.mark.parametrize("role", ["dev", "reviewer"])
def test_validate_agent_github_identity_ok(tmp_path, role):
    step = {"type": "agent", "prompt": "x", "github_identity": role}
    specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))  # kein Raise


def test_validate_agent_github_identity_unknown(tmp_path):
    with pytest.raises(ValueError, match="github_identity muss einer von"):
        specs._validate_spec(
            _step(_valid_spec(str(tmp_path)), {"type": "agent", "prompt": "x", "github_identity": "me"})
        )


def test_validate_shell_github_identity_ok(tmp_path):
    step = {"type": "shell", "cmd": "true", "github_identity": "reviewer"}
    specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))  # no raise


def test_validate_shell_github_identity_unknown(tmp_path):
    with pytest.raises(ValueError, match="github_identity muss einer von"):
        specs._validate_spec(
            _step(_valid_spec(str(tmp_path)), {"type": "shell", "cmd": "true", "github_identity": "me"})
        )


def test_validate_github_identity_rejected_on_other_step_types(tmp_path):
    with pytest.raises(ValueError, match="github_identity gilt nur für"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "git", "github_identity": "dev"}))


def test_validate_shell_sandboxed(tmp_path):
    specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "shell", "cmd": "true", "sandboxed": True}))
    for bad in (
        {"type": "shell", "cmd": "true", "sandboxed": "yes"},
        {"type": "agent", "prompt": "x", "sandboxed": True},
    ):
        with pytest.raises(ValueError, match="sandboxed gilt nur für shell"):
            specs._validate_spec(_step(_valid_spec(str(tmp_path)), bad))
@pytest.mark.parametrize("key", ["tools", "allowed_tools"])
def test_validate_agent_tool_lists(tmp_path, key):
    base = _valid_spec(str(tmp_path))
    specs._validate_spec(_step(base, {"type": "agent", "prompt": "x", key: ["Read", "Edit(/a.json)"]}))
    specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "agent", "prompt": "x", key: "Read,Grep"}))
    with pytest.raises(ValueError, match=f"{key} muss String oder Liste"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "agent", "prompt": "x", key: [1]}))
    for empty in ("", " ", " , ", [], [" "]):  # would mean "no restriction" — rejected
        with pytest.raises(ValueError, match=f"{key} darf nicht leer sein"):
            specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "agent", "prompt": "x", key: empty}))


def test_validate_agent_variant(tmp_path):
    base = _valid_spec(str(tmp_path))
    specs._validate_spec(_step(base, {"type": "agent", "prompt": "x", "variant": "medium"}))  # kein Raise
    with pytest.raises(ValueError, match="variant muss String sein"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "agent", "prompt": "x", "variant": 3}))


def test_validate_review_no_model(tmp_path):
    with pytest.raises(ValueError, match="review ohne model"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "review"}))


def test_validate_review_bad_backend(tmp_path):
    with pytest.raises(ValueError, match="review backend"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "review", "model": "m", "backend": "bogus"}))


def test_validate_root_missing_offline(tmp_path):
    specs._validate_spec(_valid_spec(str(tmp_path / "nope")), check_root=False)  # kein Raise


def test_validate_root_empty_offline(tmp_path):
    spec = _valid_spec(str(tmp_path))
    spec["root"] = ""
    with pytest.raises(ValueError, match="root"):
        specs._validate_spec(spec, check_root=False)


def test_validate_pr_review_ok(tmp_path):
    step = {
        "type": "pr_review",
        "repo": "o/r",
        "model": "m",
        "backend": "claude_code",
        "fix_backend": "opencode",
        "max_rounds": 2,
        "max_findings": 5,
    }
    specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))  # kein Raise


def test_validate_pr_review_no_repo(tmp_path):
    with pytest.raises(ValueError, match="pr_review ohne repo"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "pr_review", "model": "m"}))


def test_validate_pr_review_no_model(tmp_path):
    with pytest.raises(ValueError, match="pr_review ohne model"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "pr_review", "repo": "o/r"}))


@pytest.mark.parametrize("key", ["backend", "fix_backend"])
def test_validate_pr_review_bad_backend(tmp_path, key):
    step = {"type": "pr_review", "repo": "o/r", "model": "m", key: "claude"}
    with pytest.raises(ValueError, match=f"pr_review {key} 'claude' unbekannt"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))


@pytest.mark.parametrize("key,minimum", [("max_rounds", 0), ("max_findings", 1)])
def test_validate_pr_review_int_field_below_minimum(tmp_path, key, minimum):
    step = {"type": "pr_review", "repo": "o/r", "model": "m", key: minimum - 1}
    with pytest.raises(ValueError, match=f"{key} muss int >= {minimum}"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))


@pytest.mark.parametrize("value", ["2", 2.0, True])
def test_validate_pr_review_int_field_wrong_type(tmp_path, value):
    step = {"type": "pr_review", "repo": "o/r", "model": "m", "max_rounds": value}
    with pytest.raises(ValueError, match="max_rounds muss int"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))


# ------------------------------------------------- Step-Validierungs-Helfer (direkt)
def test_step_validators_map_covers_all_inline_types():
    """Jeder Step-Type mit Typ-spezifischer Validierung hat einen Eintrag in der Map."""
    assert set(specs._STEP_VALIDATORS) == {
        "shell",
        "http",
        "llm",
        "state",
        "verify",
        "agent",
        "review",
        "pr_review",
        "alert_route",
    }


def test_validate_step_shell_ok():
    assert specs._validate_step_shell({"type": "shell", "cmd": "x"}, "w", {}) == []


def test_validate_step_shell_missing_cmd():
    assert specs._validate_step_shell({"type": "shell"}, "w", {}) == ["w: shell ohne cmd"]


def test_validate_step_http_missing_url():
    assert specs._validate_step_http({"type": "http"}, "w", {}) == ["w: http ohne url"]


def test_validate_step_llm_missing_prompt():
    assert specs._validate_step_llm({"type": "llm"}, "w", {}) == ["w: llm ohne prompt"]


def test_validate_step_state_ok():
    assert specs._validate_step_state({"type": "state", "action": "set", "key": "k"}, "w", {}) == []


def test_validate_step_state_reports_all_errors():
    errs = specs._validate_step_state({"type": "state", "action": "bogus"}, "w", {})
    assert errs == ["w: state action muss get|set|add sein (ist 'bogus')", "w: state ohne key"]


def test_validate_step_verify_missing_checks():
    assert specs._validate_step_verify({"type": "verify"}, "w", {}) == ["w: verify braucht 'checks' als Liste"]


def test_validate_step_verify_bad_check_type():
    errs = specs._validate_step_verify({"type": "verify", "checks": ["not-a-dict", {"type": "bogus"}]}, "w", {})
    assert len(errs) == 2
    assert "verify-Check 0" in errs[0]
    assert "verify-Check 1" in errs[1]


def test_validate_step_agent_ok():
    step = {"type": "agent", "prompt": "p", "backend": "codex", "workdir": "/x", "timeout": 5}
    assert specs._validate_step_agent(step, "w", {}) == []


def test_validate_step_agent_prompt_file_suffices():
    assert specs._validate_step_agent({"type": "agent", "prompt_file": "p.md"}, "w", {}) == []


def test_validate_step_agent_reports_all_errors():
    errs = specs._validate_step_agent({"type": "agent", "backend": "bogus", "workdir": 5, "timeout": 0}, "w", {})
    assert errs == [
        "w: agent ohne prompt/prompt_file",
        f"w: agent backend 'bogus' unbekannt (verfügbar: {', '.join(known_backends())})",
        "w: agent workdir muss String sein",
        "w: agent timeout muss positive Zahl sein",
    ]


def test_validate_step_agent_timeout_none_ok():
    assert specs._validate_step_agent({"type": "agent", "prompt": "p", "timeout": None}, "w", {}) == []


def test_validate_backend_accepts_empty_and_all_known():
    for backend in (None, "", *known_backends()):
        assert specs._validate_backend({"backend": backend}, "backend", "w", "agent backend") == []


def test_validate_backend_rejects_unknown():
    errs = specs._validate_backend({"backend": "bogus"}, "backend", "w", "review backend")
    assert errs == [f"w: review backend 'bogus' unbekannt (verfügbar: {', '.join(known_backends())})"]


def test_validate_step_review_ok():
    assert specs._validate_step_review({"type": "review", "model": "m", "backend": "opencode"}, "w", {}) == []


def test_validate_step_review_reports_all_errors():
    errs = specs._validate_step_review({"type": "review", "backend": "bogus"}, "w", {})
    assert errs == [
        "w: review ohne model (Pflicht, ≠ Builder-Modell)",
        f"w: review backend 'bogus' unbekannt (verfügbar: {', '.join(known_backends())})",
    ]


def test_validate_step_pr_review_ok():
    step = {
        "type": "pr_review",
        "repo": "o/r",
        "model": "m",
        "backend": "claude_code",
        "fix_backend": "opencode",
        "max_rounds": 2,
        "max_findings": 5,
    }
    assert specs._validate_step_pr_review(step, "w", {}) == []


def test_validate_step_pr_review_reports_all_errors():
    errs = specs._validate_step_pr_review(
        {"type": "pr_review", "backend": "bogus", "max_rounds": -1, "max_findings": 0}, "w", {}
    )
    assert errs == [
        "w: pr_review ohne repo (owner/name)",
        "w: pr_review ohne model (Pflicht, ≠ Builder-Modell)",
        f"w: pr_review backend 'bogus' unbekannt (verfügbar: {', '.join(known_backends())})",
        "w: max_rounds muss int >= 0 sein (ist -1)",
        "w: max_findings muss int >= 1 sein (ist 0)",
    ]


# ---------------------------------------------------------------- Trigger-Validierung
def _trigger_spec(root: str, trig: dict, github: str | None = None) -> dict:
    """Spec mit einem Loop, der den gegebenen Trigger hat."""
    spec = _valid_spec(root)
    spec["loops"]["l1"]["triggers"] = [trig]
    if github:
        spec["github"] = github
    return spec


def test_validate_on_pr_ok(tmp_path):
    specs._validate_spec(_trigger_spec(str(tmp_path), {"type": "on_pr", "repo": "o/r"}))  # kein Raise


def test_validate_on_pr_states_ok(tmp_path):
    trig = {"type": "on_pr", "repo": "o/r", "states": ["open", "closed"]}
    specs._validate_spec(_trigger_spec(str(tmp_path), trig))  # kein Raise


def test_validate_on_pr_repo_from_spec_github(tmp_path):
    """repo darf auf Spec-Ebene (`github`) stehen — Trigger ohne repo ist dann ok."""
    specs._validate_spec(_trigger_spec(str(tmp_path), {"type": "on_pr"}, github="o/r"))  # kein Raise


def test_validate_on_pr_no_repo(tmp_path):
    """on_pr ohne repo (Trigger- UND Spec-Ebene) → Config-Fehler (kein Silent-Noop)."""
    with pytest.raises(ValueError, match="on_pr ohne repo"):
        specs._validate_spec(_trigger_spec(str(tmp_path), {"type": "on_pr"}))


def test_validate_on_issue_no_repo(tmp_path):
    with pytest.raises(ValueError, match="on_issue ohne repo"):
        specs._validate_spec(_trigger_spec(str(tmp_path), {"type": "on_issue"}))


def test_validate_on_pr_bad_states_value(tmp_path):
    with pytest.raises(ValueError, match="states"):
        specs._validate_spec(_trigger_spec(str(tmp_path), {"type": "on_pr", "repo": "o/r", "states": ["bogus"]}))


def test_validate_on_pr_empty_states(tmp_path):
    with pytest.raises(ValueError, match="states"):
        specs._validate_spec(_trigger_spec(str(tmp_path), {"type": "on_pr", "repo": "o/r", "states": []}))


def test_validate_on_pr_states_not_list(tmp_path):
    with pytest.raises(ValueError, match="states"):
        specs._validate_spec(_trigger_spec(str(tmp_path), {"type": "on_pr", "repo": "o/r", "states": "open"}))


@pytest.mark.parametrize("rec", [True, False, "head", "updated"])
def test_validate_on_pr_reconcile_ok(tmp_path, rec):
    trig = {"type": "on_pr", "repo": "o/r", "reconcile": rec}
    specs._validate_spec(_trigger_spec(str(tmp_path), trig))  # kein Raise


def test_validate_on_pr_reconcile_bad_value(tmp_path):
    """Tippfehler wie `reconcile: heads` → Config-Fehler statt stillem PR-Modus."""
    with pytest.raises(ValueError, match="reconcile"):
        specs._validate_spec(_trigger_spec(str(tmp_path), {"type": "on_pr", "repo": "o/r", "reconcile": "heads"}))


def test_validate_skip_blocked_requires_on_issue_reconcile(tmp_path):
    """ADR 0031: im Event-Modus ginge ein blockiertes Issue verloren → Config-Fehler."""
    ok = {"type": "on_issue", "repo": "o/r", "reconcile": True, "skip_blocked": True}
    specs._validate_spec(_trigger_spec(str(tmp_path), ok))  # kein Raise
    for bad in ({**ok, "reconcile": False}, {"type": "on_pr", "repo": "o/r", "reconcile": True, "skip_blocked": True}):
        with pytest.raises(ValueError, match="skip_blocked"):
            specs._validate_spec(_trigger_spec(str(tmp_path), bad))


def test_validate_on_start_only_bool_on_cron(tmp_path):
    ok = {"type": "cron", "cron": "*/30 * * * *", "on_start": True}
    specs._validate_spec(_trigger_spec(str(tmp_path), ok))  # kein Raise
    for bad in ({**ok, "on_start": "yes"}, {"type": "manual", "on_start": True}):
        with pytest.raises(ValueError, match="on_start"):
            specs._validate_spec(_trigger_spec(str(tmp_path), bad))


def test_validate_unknown_trigger_type(tmp_path):
    with pytest.raises(ValueError, match="triggers/0/type: .* is not one of"):
        specs._validate_spec(_trigger_spec(str(tmp_path), {"type": "bogus"}))


def test_validate_trigger_not_mapping(tmp_path):
    s = _valid_spec(str(tmp_path))
    s["loops"]["l1"]["triggers"] = ["not-a-dict"]
    with pytest.raises(ValueError, match="trigger 0"):
        specs._validate_spec(s)


# ---------------------------------------------------------------- load_spec
def test_load_spec_rejects_non_mapping(tmp_path):
    f = tmp_path / "bad.fabrikant.yaml"
    f.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="Mapping"):
        specs.load_spec(f)


def test_load_spec_derives_name_from_stem(tmp_path):
    f = tmp_path / "myproj.fabrikant.yaml"
    f.write_text(f"root: {tmp_path}\nloops:\n  l1:\n    steps:\n      - type: shell\n        cmd: echo hi\n")
    spec = specs.load_spec(f)
    assert spec["name"] == "myproj"


# ---------------------------------------------------------------- load_projects
def test_load_projects_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "nope")
    assert specs.load_projects() == {}


def test_load_projects_marks_broken_spec_with_error(projects_dir, tmp_path):
    """Defekte Spec (ungültiger Step-Type) wird nicht verworfen, sondern mit
    `_error` gemarke — so sieht die UI den Grund statt einer leisen Lücke."""
    (projects_dir / "broken.fabrikant.yaml").write_text(
        f"root: {tmp_path}\nloops:\n  l1:\n    steps:\n      - type: bogus\n"
    )
    projects = specs.load_projects()
    assert "broken" in projects
    assert "_error" in projects["broken"]
    assert "type" in projects["broken"]["_error"]


# ---------------------------------------------------------------- approval-Loop (ADR 0011)
def _approval_loop_spec(root: str) -> dict:
    """Der `approval`-Loop aus projects/fabrikant.fabrikant.yaml (ADR 0011).

    Pinnt die Struktur, auf der das Approval-Request-Protokoll aufbaut:
    on_pr-Trigger (reconcile) auf 'needs approval' + statisches approval-Gate
    am pr_merge-Step (mergt den gestapelten ADR-PR nach Freigabe).
    """
    return {
        "name": "p",
        "root": root,
        "loops": {
            "approval": {
                "tier": 1,
                "triggers": [
                    {
                        "type": "on_pr",
                        "repo": "o/r",
                        "labels": ["fabrikant", "needs approval"],
                        "reconcile": True,
                        "poll": 300,
                    }
                ],
                "steps": [
                    {
                        "name": "merge-adr-pr",
                        "type": "pr_merge",
                        "repo": "o/r",
                        "method": "squash",
                        "approval": True,
                        "approval_reason": "{pr_title}",
                    },
                ],
            }
        },
    }


def test_approval_loop_spec_valid(tmp_path):
    """Der approval-Loop ist eine gültige Spec (reconcile-Trigger + Gate + Merge)."""
    spec = _approval_loop_spec(str(tmp_path))
    specs._validate_spec(spec)  # kein Raise
    loop = spec["loops"]["approval"]
    trig = loop["triggers"][0]
    assert trig["type"] == "on_pr"
    # AND-Filter: nur fabrikant-PRs mit 'needs approval' (nicht alle 'needs approval')
    assert trig["labels"] == ["fabrikant", "needs approval"]
    # reconcile: true ist der Kern — ohne sie feuert der Trigger nicht auf
    # bestehende ADR-PRs (Erst-Poll initialisiert nur).
    assert trig["reconcile"] is True
    # Das statische Gate ist der Kern des Protokolls: ohne `approval: true`
    # würde der Run nicht pausieren und die Freigabe nie erbeten.
    gate = [s for s in loop["steps"] if s.get("approval")]
    assert len(gate) == 1
    merge = [s for s in loop["steps"] if s.get("type") == "pr_merge"]
    assert len(merge) == 1
    assert merge[0].get("method") == "squash"


def test_retry_cron_warning_printed_once(monkeypatch, capsys):
    """Specs werden jeden Tick neu geladen — die Warnung erscheint nur einmal."""
    monkeypatch.setattr(specs, "_printed_retry_warnings", set())
    spec = {
        "name": "p",
        "retry": {"max": 3, "backoff": ["25m"]},
        "loops": {"l": {"triggers": [{"type": "cron", "cron": "0 * * * *"}]}},
    }
    ldef = spec["loops"]["l"]
    assert specs._validate_retry_cron(spec, "l", ldef) == []
    assert specs._validate_retry_cron(spec, "l", ldef) == []
    assert capsys.readouterr().out.count("Ticks werden übersprungen") == 1


@pytest.mark.parametrize(
    "extra",
    [{"fix_timeout": 0}, {"fix_check_timeout": "x"}, {"fix_variant": 3}, {"fix_checks": "make"}, {"fix_checks": [""]}],
)
def test_validate_pr_review_fix_keys_rejected(tmp_path, extra):
    step = {"type": "pr_review", "repo": "o/r", "model": "m", **extra}
    with pytest.raises(ValueError, match=next(iter(extra))):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))


def test_validate_pr_review_fix_keys_ok(tmp_path):
    step = {
        "type": "pr_review",
        "repo": "o/r",
        "model": "m",
        "fix_timeout": 60,
        "fix_variant": "medium",
        "fix_checks": ["ruff check ."],
    }
    specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))


# ---------------------------------------------------------------- done_marker
@pytest.mark.parametrize("value", [True, False])
def test_validate_done_marker_ok(tmp_path, value):
    specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "shell", "cmd": "x", "done_marker": value}))


@pytest.mark.parametrize("value", ["true", 1, None])
def test_validate_done_marker_not_bool(tmp_path, value):
    with pytest.raises(ValueError, match="done_marker muss true/false sein"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), {"type": "shell", "cmd": "x", "done_marker": value}))


def test_validate_done_marker_only_shell(tmp_path):
    step = {"type": "http", "url": "http://x", "done_marker": True}
    with pytest.raises(ValueError, match="done_marker gilt nur für shell-Steps"):
        specs._validate_spec(_step(_valid_spec(str(tmp_path)), step))
