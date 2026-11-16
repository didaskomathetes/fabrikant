"""Steps: secret redaction + LLM schema validation."""

import json

import fabrik

_SECRET = "supersecretvalue123"


# ---------------------------------------------------------------- redaction
def test_redact_masks_env_secret(monkeypatch):
    monkeypatch.setenv("FABRIK_TEST_TOKEN", _SECRET)
    assert fabrik._redact(f"token={_SECRET} ok") == "token=[REDACTED] ok"


def test_redact_leaves_non_secret(monkeypatch):
    monkeypatch.setenv("FABRIK_TEST_TOKEN", _SECRET)
    assert fabrik._redact("nothing sensitive here") == "nothing sensitive here"


def test_redact_record_masks_steps_and_error(monkeypatch):
    monkeypatch.setenv("FABRIK_TEST_TOKEN", _SECRET)
    rec = {
        "steps": [{"summary": f"used {_SECRET}", "output": _SECRET}],
        "error": f"failed with {_SECRET}",
    }
    out = fabrik._redact_record(rec)
    assert _SECRET not in json.dumps(out)
    assert "[REDACTED]" in out["steps"][0]["summary"]
    assert "[REDACTED]" in out["error"]


# ---------------------------------------------------------------- schema
def test_validate_schema_ok():
    assert fabrik._validate_schema({"a": "x", "b": 1}, {"a": "string", "b": "integer"}) == []


def test_validate_schema_missing_key():
    errs = fabrik._validate_schema({"a": "x"}, {"a": "string", "b": "integer"})
    assert any("b" in e for e in errs)


def test_validate_schema_type_mismatch():
    assert fabrik._validate_schema({"a": 5}, {"a": "string"})


def test_validate_schema_bool_rejected_as_integer():
    # bool is a subclass of int — must be explicitly rejected for "integer"
    assert fabrik._validate_schema({"a": True}, {"a": "integer"})


def test_validate_schema_number_accepts_int_and_float():
    assert fabrik._validate_schema({"a": 1}, {"a": "number"}) == []
    assert fabrik._validate_schema({"a": 1.5}, {"a": "number"}) == []


def test_validate_schema_default_fills_missing_key():
    parsed = {"a": "x"}
    assert fabrik._validate_schema(parsed, {"a": "string", "b": {"type": "string", "default": ""}}) == []
    assert parsed == {"a": "x", "b": ""}  # Default landet im Ergebnis (→ ctx-Variable)


def test_validate_schema_default_does_not_override_value():
    parsed = {"b": "gesetzt"}
    assert fabrik._validate_schema(parsed, {"b": {"type": "string", "default": ""}}) == []
    assert parsed["b"] == "gesetzt"


def test_validate_schema_default_is_type_checked():
    errs = fabrik._validate_schema({}, {"b": {"type": "integer", "default": "x"}})
    assert any("soll integer sein" in e for e in errs)


# ---------------------------------------------------------------- schema-enum
def test_validate_schema_enum_accepts_whitelisted_value():
    schema = {"label": {"type": "string", "enum": ["needs approval", "ready to develop"]}}
    assert fabrik._validate_schema({"label": "ready to develop"}, schema) == []


def test_validate_schema_enum_rejects_other_value():
    schema = {"label": {"type": "string", "enum": ["needs approval", "ready to develop"]}}
    errs = fabrik._validate_schema({"label": 'x"; rm -rf /; "'}, schema)
    assert any("nicht in erlaubten Werten" in e for e in errs)


def test_validate_schema_enum_rejects_wrong_type():
    schema = {"label": {"type": "string", "enum": ["a", "b"]}}
    assert fabrik._validate_schema({"label": 5}, schema)


def test_validate_schema_plain_string_ignores_enum_semantics():
    # String-Form bleibt unverändert (rückwärtskompatibel)
    assert fabrik._validate_schema({"a": "irgendwas"}, {"a": "string"}) == []


# ---------------------------------------------------------------- git-step: neue Branches
def _mk_origin_and_detached_wt(tmp_path):
    """Bare origin + Clone mit main + DETACHED Worktree (wie die Engine)."""
    import subprocess

    def g(cwd, *a):
        subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True)

    origin = tmp_path / "origin.git"
    origin.mkdir()
    g(tmp_path, "init", "--bare", "-q", str(origin))
    clone = tmp_path / "clone"
    g(tmp_path, "clone", "-q", str(origin), str(clone))
    g(clone, "config", "user.email", "t@t.t")
    g(clone, "config", "user.name", "t")
    (clone / "a.txt").write_text("a\n")
    g(clone, "add", ".")
    g(clone, "commit", "-qm", "init")
    g(clone, "checkout", "-qB", "main")
    g(clone, "push", "-qu", "origin", "main")
    wt = tmp_path / "wt"
    g(clone, "worktree", "add", "-q", "-d", str(wt), "HEAD")  # detached, wie Engine
    return clone, wt


def test_git_step_new_branch_without_remote_ref(tmp_path):
    """Regression (PR #32 Review): fehlender Remote-Ref = NEUER Branch —
    früher RuntimeError (rev-parse check=True + checkout -B b b), jetzt
    Branch aus der Base-HEAD."""
    from fabrik.steps.git import _step_git

    _, wt = _mk_origin_and_detached_wt(tmp_path)
    r = _step_git({"workdir": str(wt)}, {"branch": "feat/neu"})
    assert r["ok"]
    import subprocess

    cur = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert cur == "feat/neu"


def test_git_step_tracks_existing_remote_branch(tmp_path):
    """Bestehender Remote-Ref: Upstream tracken (Verhalten unverändert)."""
    from fabrik.steps.git import _step_git

    clone, wt = _mk_origin_and_detached_wt(tmp_path)
    # Remote-Branch existieren lassen
    import subprocess

    subprocess.run(
        ["git", "-C", str(clone), "push", "-q", "origin", "main:refs/heads/feat/existiert"],
        check=True,
        capture_output=True,
    )
    r = _step_git({"workdir": str(wt)}, {"branch": "feat/existiert"})
    assert r["ok"]
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    origin_ref = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "origin/feat/existiert"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == origin_ref
