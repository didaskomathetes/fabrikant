"""JSON-Schema-Validierung (Issue #131): schema/fabrik-spec.schema.json.

Parität: jede Spec, die `specs._validate_spec` akzeptiert, muss auch das
Schema passieren (keine Regressionen). Die Tests hier decken die
Strukturprüfungen des Schemas ab (Typen, Enums, Pflichtfelder, Bereiche);
die semantischen Prüfungen (Backend-Registry, Cron-Intervalle, …) bleiben
in test_specs.py.
"""

from pathlib import Path

import pytest

from fabrik import specs
from fabrik.specschema import SCHEMA_PATH, load_schema, validate_spec

REPO_ROOT = Path(__file__).resolve().parent.parent


def _valid_spec() -> dict:
    """Minimal gültige Spec."""
    return {
        "name": "p",
        "loops": {
            "l1": {
                "steps": [{"type": "shell", "cmd": "echo hi"}],
            }
        },
    }


def _with_step(step: dict) -> dict:
    spec = _valid_spec()
    spec["loops"]["l1"]["steps"] = [step]
    return spec


# ------------------------------------------------------------------ Schema-Datei
def test_schema_file_is_valid_draft_2020_12():
    from jsonschema import Draft202012Validator

    assert SCHEMA_PATH.is_file()
    Draft202012Validator.check_schema(load_schema())


# ------------------------------------------------------------------ gültige Specs
def test_valid_minimal_spec():
    assert validate_spec(_valid_spec()) == []


def test_valid_full_spec():
    spec = _valid_spec()
    spec.update(
        {
            "description": "demo",
            "root": "/tmp/x",
            "max_parallel": 2,
            "github": "owner/repo",
            "repo": {"branch": "main", "subpath": "sub"},
            "commit_via": "api",
            "env": {"topic": "x"},
            "vars": {"repo": "owner/repo"},
            "extra_context": [{"name": "agents_md", "file": "AGENTS.md", "trusted": True}],
            "retry": {"max": 1, "backoff": ["5m"], "on": ["transient"], "max_wait": "1h"},
            "notify": {
                "slack": "https://hooks.slack.com/x",
                "webhooks": [{"name": "dd", "url": "https://x"}],
                "github_issue": True,
                "issue_labels": ["fabrik"],
                "events": ["run_failed", "run_passed"],
            },
            "protected": ["main"],
            "x-context": {"agents-md": {"name": "agents_md", "file": "AGENTS.md"}},
        }
    )
    spec["loops"]["l1"].update(
        {
            "description": "loop",
            "tier": 2,
            "worktree": False,
            "triggers": [
                {"type": "manual"},
                {"type": "cron", "cron": "*/5 * * * *", "on_start": True},
                {"type": "on_file", "glob": "*.py", "debounce": 30},
                {"type": "on_issue", "repo": "owner/repo", "labels": ["a"], "reconcile": True},
                {"type": "on_pr", "states": ["open", "closed"], "reconcile": "head"},
                {
                    "type": "on_webhook",
                    "secret_env": "WH_SECRET",
                    "map": {"x": "$.y"},
                    "replay_protection": True,
                },
                {"type": "on_alert", "source": "sentry", "secret_env": "AL_SECRET"},
                {"type": "on_slack", "token_env": "SLACK_TOKEN", "channels": ["C0123ABC"]},
            ],
        }
    )
    assert validate_spec(spec) == []


@pytest.mark.parametrize(
    "step",
    [
        {"type": "shell", "cmd": "echo", "env": {"A": "1"}, "timeout": 5, "failure_kind": "transient"},
        {"type": "git", "branch": "b", "commit": {"add": [":/"], "message": "m"}, "push": True},
        {"type": "pr", "repo": "o/r", "title": "t", "body": "b", "labels": ["a", "b"]},
        {"type": "agent", "prompt": "p", "backend": "opencode", "model": "m", "retries": 1},
        {"type": "report", "text": "t", "write": "out.md"},
        {"type": "http", "url": "https://x", "method": "POST", "expect": {"status": 201}, "emit": {"v": "body"}},
        {"type": "llm", "prompt": "p", "untrusted": ["issue_body"], "schema": {"x": {"type": "string"}}},
        {"type": "notify", "slack": "https://x", "message": "m"},
        {"type": "state", "action": "set", "key": "k", "value": 1},
        {"type": "verify", "checks": [{"type": "shell", "cmd": "true"}]},
        {"type": "review", "model": "m", "target": "diff"},
        {"type": "pr_review", "repo": "o/r", "model": "m", "max_rounds": 2, "fix_checks": ["make test"]},
        {"type": "pr_merge", "repo": "o/r", "pr": "1", "method": "squash"},
        {
            "type": "issue",
            "action": "create",
            "repo": "o/r",
            "title": "t",
            "labels": "a,b",
            "emit": "issue_number",
            "only_if": {"label": ["a", "b"]},
            "skip_if_empty": ["x"],
        },
        {"type": "human_gate", "max_files": 10, "keywords": ["gdpr"]},
        {"type": "alert_route", "routes": {"cat": "loop2"}, "fallback": "loop3"},
        {"type": "escalation", "item": "#1", "label": "needs human"},
    ],
)
def test_valid_step_types(step):
    assert validate_spec(_with_step(step)) == []


def test_valid_bundled_specs():
    """Parität: alle mitgelieferten Specs passieren das Schema."""
    for f in sorted(REPO_ROOT.glob("projects/*.fabrik.*")):
        spec = specs.load_spec(f, check_root=False)
        assert validate_spec(spec) == [], f"{f.name}: {validate_spec(spec)}"


# ------------------------------------------------------------------ ungültige Specs
def test_missing_loops():
    errs = validate_spec({"name": "p"})
    assert any("loops" in e for e in errs)


def test_loops_empty():
    errs = validate_spec({"loops": {}})
    assert errs


def test_unknown_top_level_key():
    errs = validate_spec({**_valid_spec(), "loopz": {}})
    assert any("loopz" in e for e in errs)


def test_unknown_step_field():
    errs = validate_spec(_with_step({"type": "shell", "cmd": "echo", "bogus": 1}))
    assert any("bogus" in e for e in errs)


def test_unknown_trigger_field():
    spec = _valid_spec()
    spec["loops"]["l1"]["triggers"] = [{"type": "cron", "cron": "* * * * *", "bogus": 1}]
    assert any("bogus" in e for e in validate_spec(spec))


@pytest.mark.parametrize(
    ("step", "needle"),
    [
        ({"type": "shell"}, "cmd"),
        ({"type": "http"}, "url"),
        ({"type": "llm"}, "prompt"),
        ({"type": "state", "action": "set"}, "key"),
        ({"type": "verify"}, "checks"),
        ({"type": "review"}, "model"),
        ({"type": "pr_review", "model": "m"}, "repo"),
        ({"type": "alert_route"}, "routes"),
    ],
)
def test_missing_required_step_field(step, needle):
    errs = validate_spec(_with_step(step))
    assert any(needle in e for e in errs)


@pytest.mark.parametrize(
    ("step", "needle"),
    [
        ({"type": "shell", "cmd": "x", "failure_kind": "bogus"}, "failure_kind"),
        ({"type": "state", "action": "bogus", "key": "k"}, "action"),
        ({"type": "issue", "action": "bogus"}, "action"),
        ({"type": "pr_merge", "pr": "1", "method": "fast-forward"}, "method"),
        ({"type": "http", "url": "u", "expect": {"status": "200"}}, "status"),
    ],
)
def test_bad_enum_values(step, needle):
    errs = validate_spec(_with_step(step))
    assert any(needle in e for e in errs)


def test_bad_tier_range():
    spec = _valid_spec()
    spec["loops"]["l1"]["tier"] = 5
    assert validate_spec(spec)


def test_max_parallel_non_numeric_string_rejected():
    # `vars`-Substitution liefert Zahl-Strings — die akzeptiert das Schema;
    # echte Nicht-Zahlen nicht (Bereich >= 1 prüft die Python-Validierung).
    spec = _valid_spec()
    spec["max_parallel"] = "abc"
    assert validate_spec(spec)
    spec["max_parallel"] = "3"  # Zahl-String aus vars: ok
    assert validate_spec(spec) == []


def test_bad_retry_block():
    spec = _valid_spec()
    spec["retry"] = {"max": 1, "backoff": ["5x"]}
    assert any("backoff" in e for e in validate_spec(spec))


def test_extra_context_exactly_one_source():
    # zwei Quellen in einem Eintrag → ungültig
    spec = _valid_spec()
    spec["extra_context"] = [{"name": "x", "file": "a", "cmd": "b"}]
    assert validate_spec(spec)
    # keine Quelle → ungültig
    spec["extra_context"] = [{"name": "x"}]
    assert validate_spec(spec)


def test_unknown_step_type():
    errs = validate_spec(_with_step({"type": "bogus"}))
    assert any("bogus" in e for e in errs)


def test_unknown_trigger_type():
    spec = _valid_spec()
    spec["loops"]["l1"]["triggers"] = [{"type": "bogus"}]
    assert any("bogus" in e for e in validate_spec(spec))


def test_on_webhook_requires_secret_env():
    spec = _valid_spec()
    spec["loops"]["l1"]["triggers"] = [{"type": "on_webhook"}]
    assert any("secret_env" in e for e in validate_spec(spec))


def test_on_slack_requires_token_and_channels():
    spec = _valid_spec()
    spec["loops"]["l1"]["triggers"] = [{"type": "on_slack", "token_env": "T"}]
    errs = validate_spec(spec)
    assert any("channels" in e for e in errs)


def test_internal_keys_are_ignored():
    """Laufzeit-Buchführung (`_path`, …) stört die Schema-Prüfung nicht."""
    spec = _valid_spec()
    spec["_path"] = "/x/y.fabrik.yaml"
    assert validate_spec(spec) == []


def test_error_paths_are_json_pointer_like():
    spec = _valid_spec()
    spec["loops"]["l1"]["steps"] = [{"type": "shell"}]
    errs = validate_spec(spec)
    assert any(e.startswith("loops/l1/steps/0") for e in errs)
