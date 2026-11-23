"""Spec-Quellen, Pfadauflösung, vars, Loop-Templates (ADR 0047)."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fabrik import cli, config, spec_expand, spec_sources, specs
from fabrik.steps.agent import _step_agent

LOOP = "loops:\n  l1:\n    steps:\n      - type: shell\n        cmd: echo hi\n"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _spec_text(tmp_path, name=None) -> str:
    return (f"name: {name}\n" if name else "") + f"root: {tmp_path}\n" + LOOP


# ---------------------------------------------------------------- Suchpfad
def test_search_order_env_then_data_then_registered_then_bundled(tmp_path, monkeypatch, projects_dir):
    env_dir, repo, bundled = tmp_path / "envdir", tmp_path / "repo", tmp_path / "_bundled-projects"
    _write(env_dir / "a.fabrik.yaml", _spec_text(tmp_path))
    _write(projects_dir / "b.fabrik.yaml", _spec_text(tmp_path))
    _write(repo / ".fabrik" / "fabrik.yaml", _spec_text(tmp_path))
    _write(bundled / "d.fabrik.yaml", _spec_text(tmp_path))
    monkeypatch.setenv(config.PROJECTS_PATH_ENV, f"{env_dir}:")
    spec_sources.register_target(repo)
    names = [spec_sources.default_name(f) for f in spec_sources.spec_files()]
    assert names == ["a", "b", "repo", "d"]


def test_search_dirs_dedupes_and_skips_empty_entries(tmp_path, monkeypatch, projects_dir):
    monkeypatch.setenv(config.PROJECTS_PATH_ENV, f"{projects_dir}::{projects_dir}")
    assert spec_sources.search_dirs().count(projects_dir) == 1


def test_env_entry_may_be_target_repo(tmp_path, monkeypatch):
    repo = tmp_path / "svc"
    _write(repo / ".fabrik" / "fabrik.yaml", _spec_text(tmp_path))
    monkeypatch.setenv(config.PROJECTS_PATH_ENV, str(repo))
    assert spec_sources.spec_files() == [repo / ".fabrik" / "fabrik.yaml"]


def test_json_specs_listed_after_yaml(tmp_path, projects_dir):
    _write(projects_dir / "z.fabrik.yaml", "")
    _write(projects_dir / "a.fabrik.json", "{}")
    assert [f.name for f in spec_sources.spec_files()] == ["z.fabrik.yaml", "a.fabrik.json"]


# ---------------------------------------------------------------- Zielrepo-Spec + Registrierung
def test_target_spec_defaults_name_and_root_to_repo(tmp_path):
    repo = tmp_path / "myapp"
    f = _write(repo / ".fabrik" / "fabrik.yaml", LOOP)
    spec = specs.load_spec(f)
    assert spec["name"] == "myapp" and spec["root"] == str(repo)


def test_register_target_is_idempotent_and_persisted(tmp_path):
    repo = tmp_path / "svc"
    _write(repo / ".fabrik" / "fabrik.yaml", LOOP)
    spec_sources.register_target(repo)
    spec_sources.register_target(repo)
    assert spec_sources.registered_targets() == [repo.resolve()]


def test_register_target_requires_spec(tmp_path):
    with pytest.raises(ValueError, match="Keine Spec"):
        spec_sources.register_target(tmp_path)


def test_registered_targets_tolerates_broken_registry():
    assert spec_sources.registered_targets() == []
    config.REGISTRY_FILE.parent.mkdir(parents=True)
    config.REGISTRY_FILE.write_text("{kaputt")
    assert spec_sources.registered_targets() == []
    config.REGISTRY_FILE.write_text(json.dumps(["/x", 3]))
    assert spec_sources.registered_targets() == [Path("/x")]


@pytest.mark.parametrize("data", ["123", '"foo"', '{"a": "/x"}', "null"])
def test_registered_targets_ignores_non_list_registry(data):
    config.REGISTRY_FILE.parent.mkdir(parents=True)
    config.REGISTRY_FILE.write_text(data)
    assert spec_sources.registered_targets() == []


def test_register_target_write_is_atomic(tmp_path, monkeypatch):
    """Crash beim Schreiben (vor dem Rename) lässt das bestehende Register intakt."""
    first, second = tmp_path / "a", tmp_path / "b"
    for repo in (first, second):
        _write(repo / ".fabrik" / "fabrik.yaml", LOOP)
    spec_sources.register_target(first)
    before = config.REGISTRY_FILE.read_text()

    def crash(self, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", crash)
    with pytest.raises(OSError):
        spec_sources.register_target(second)
    assert config.REGISTRY_FILE.read_text() == before


def test_cli_project_add(tmp_path, capsys):
    repo = tmp_path / "svc"
    _write(repo / ".fabrik" / "fabrik.yaml", LOOP)
    cli.main(["project", "add", str(repo)])
    assert "registriert" in capsys.readouterr().out
    assert specs.load_projects()["svc"]["root"] == str(repo.resolve())
    with pytest.raises(SystemExit):
        cli.main(["project", "add", str(tmp_path / "leer")])


# ---------------------------------------------------------------- Konflikte
def test_conflict_first_source_wins_and_is_reported(tmp_path, monkeypatch, projects_dir):
    env_dir = tmp_path / "envdir"
    _write(env_dir / "dup.fabrik.yaml", _spec_text(tmp_path, "dup") + "description: env\n")
    _write(projects_dir / "dup.fabrik.yaml", _spec_text(tmp_path, "dup") + "description: data\n")
    monkeypatch.setenv(config.PROJECTS_PATH_ENV, str(env_dir))
    projects, conflicts = specs.collect_projects()
    assert projects["dup"]["description"] == "env"
    assert len(conflicts) == 1 and "dup" in conflicts[0] and str(projects_dir) in conflicts[0]


def test_validate_exits_nonzero_on_conflict(tmp_path, monkeypatch, projects_dir, capsys):
    env_dir = tmp_path / "envdir"
    _write(env_dir / "dup.fabrik.yaml", _spec_text(tmp_path, "dup"))
    _write(projects_dir / "dup.fabrik.yaml", _spec_text(tmp_path, "dup"))
    monkeypatch.setenv(config.PROJECTS_PATH_ENV, str(env_dir))
    with pytest.raises(SystemExit) as e:
        cli.cmd_validate(argparse.Namespace(specs=[], offline=False))
    assert e.value.code == 1
    assert "mehrfach" in capsys.readouterr().out


def test_validate_ok_without_conflict(tmp_path, projects_dir, capsys):
    _write(projects_dir / "ok.fabrik.yaml", _spec_text(tmp_path))
    with pytest.raises(SystemExit) as e:
        cli.cmd_validate(argparse.Namespace(specs=[], offline=False))
    assert e.value.code == 0 and "ok" in capsys.readouterr().out


def test_broken_spec_is_kept_with_error(tmp_path, projects_dir):
    _write(projects_dir / "bad.fabrik.yaml", "- nope\n")
    assert "Mapping" in specs.load_projects()["bad"]["_error"]


# ---------------------------------------------------------------- Pfadauflösung
def test_resolve_relative_prefers_spec_dir_then_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write(home / "prompts" / "p.md", "home")
    monkeypatch.setattr(config, "FABRIK_HOME", home)
    spec = tmp_path / "proj" / "x.fabrik.yaml"
    assert spec_sources.resolve_relative("prompts/p.md", spec) == home / "prompts" / "p.md"
    local = _write(spec.parent / "prompts" / "p.md", "local")
    assert spec_sources.resolve_relative("prompts/p.md", spec) == local
    assert spec_sources.resolve_relative("prompts/p.md", None) == home / "prompts" / "p.md"
    assert spec_sources.resolve_relative(local, spec) == local  # absolut bleibt


def test_agent_prompt_file_relative_to_spec(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FABRIK_HOME", tmp_path / "home")
    monkeypatch.delenv("FABRIK_AGENT_BACKEND", raising=False)
    spec = tmp_path / "proj" / "x.fabrik.yaml"
    _write(spec.parent / "p.md", "Hallo {who}")
    ctx = {"spec_path": str(spec), "who": "Welt", "project": "p", "run_id": "r"}
    res = _step_agent(ctx, {"prompt_file": "p.md"}, lambda m: None)
    assert res["summary"] == "skipped (no agent backend)"  # Prompt gelesen, kein Backend konfiguriert


def test_agent_prompt_file_missing_reports_home_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FABRIK_HOME", tmp_path / "home")
    res = _step_agent({"spec_path": str(tmp_path / "x.fabrik.yaml")}, {"prompt_file": "nope.md"}, lambda m: None)
    assert not res["ok"] and str(tmp_path / "home" / "nope.md") in res["summary"]


# ---------------------------------------------------------------- vars
def test_substitute_only_known_keys_and_keeps_types():
    out = spec_expand.substitute(
        {"a": "x/{repo}/{issue_number}", "b": "{labels}", "c": ["{n}", 5], "d": "{repo}"},
        {"repo": "o/r", "labels": ["p0"], "n": 3},
    )
    assert out == {"a": "x/o/r/{issue_number}", "b": ["p0"], "c": [3, 5], "d": "o/r"}


def test_vars_applied_to_whole_spec(tmp_path):
    f = _write(
        tmp_path / "v.fabrik.yaml",
        f"vars: {{repo: o/r}}\nroot: {tmp_path}\ngithub: '{{repo}}'\n"
        "loops:\n  l1:\n    triggers: [{type: on_pr, repo: '{repo}'}]\n    steps:\n"
        "      - {type: shell, cmd: 'echo {repo} {issue_number}'}\n",
    )
    spec = specs.load_spec(f)
    assert spec["github"] == "o/r"
    assert spec["loops"]["l1"]["triggers"][0]["repo"] == "o/r"
    assert spec["loops"]["l1"]["steps"][0]["cmd"] == "echo o/r {issue_number}"


def test_substitute_copies_container_values():
    labels = ["p0", "p1"]
    out = spec_expand.substitute({"a": "{labels}", "b": "{labels}"}, {"labels": labels})
    out["a"].append("p9")
    assert labels == ["p0", "p1"] and out["b"] == ["p0", "p1"]


def test_chained_vars_resolved():
    spec = spec_expand.expand_spec(
        yaml.safe_load("vars: {a: '{b}-x', b: '{c}', c: v, l: '{ls}', ls: [1, 2]}\ngithub: '{a}'\nx: '{l}'")
    )
    assert spec["github"] == "v-x" and spec["x"] == [1, 2]


@pytest.mark.parametrize("cycle", ["{a: '{a}'}", "{a: '{b}', b: 'x{a}'}"])
def test_cyclic_vars_rejected(cycle):
    with pytest.raises(ValueError, match="zyklische Referenz"):
        spec_expand.expand_spec(yaml.safe_load(f"vars: {cycle}"))


@pytest.mark.parametrize("bad", ["[1]", "{project: x}", "{'a b': 1}"])
def test_invalid_vars_rejected(bad):
    with pytest.raises(ValueError, match="vars"):
        spec_expand.expand_spec(yaml.safe_load(f"vars: {bad}"))


# ---------------------------------------------------------------- Templates
def _template(tmp_path, name="t", body=None) -> Path:
    body = body or (
        "params: {repo: null, mode: fast, label: ready}\n"
        "loop:\n  tier: 1\n  description: d\n"
        "  triggers: [{type: on_issue, repo: '{repo}', labels: ['{label}']}]\n"
        "  steps:\n    - {name: a, type: shell, cmd: 'echo {mode} {issue_number}'}\n"
        "    - {name: b, type: shell, cmd: 'echo b'}\n"
    )
    return _write(tmp_path / "templates" / f"{name}.loop.yaml", body)


def _expand(tmp_path, loops: str, extra="") -> dict:
    data = yaml.safe_load(f"{extra}\nloops:\n{loops}")
    return spec_expand.expand_spec(data, tmp_path / "x.fabrik.yaml")["loops"]


def test_template_params_precedence_with_over_vars_over_default(tmp_path):
    _template(tmp_path)
    loops = _expand(
        tmp_path,
        "  l1: {use: t, with: {mode: slow}}\n  l2: {use: t}\n",
        "vars: {repo: o/r, mode: vars}",
    )
    assert loops["l1"]["steps"][0]["cmd"] == "echo slow {issue_number}"
    assert loops["l2"]["steps"][0]["cmd"] == "echo vars {issue_number}"
    trig = loops["l1"]["triggers"][0]
    assert trig["repo"] == "o/r" and trig["labels"] == ["ready"]
    assert "use" not in loops["l1"] and "with" not in loops["l1"]


def test_template_default_used_without_override(tmp_path):
    _template(tmp_path)
    loops = _expand(tmp_path, "  l1: {use: t, with: {repo: a/b}}\n")
    assert loops["l1"]["steps"][0]["cmd"].startswith("echo fast")


def test_template_missing_required_param(tmp_path):
    _template(tmp_path)
    with pytest.raises(ValueError, match="braucht Parameter: repo"):
        _expand(tmp_path, "  l1: {use: t}\n")


def test_template_loop_keys_override(tmp_path):
    _template(tmp_path)
    loops = _expand(tmp_path, "  l1: {use: t, with: {repo: a/b}, tier: 3, description: eigen}\n")
    assert loops["l1"]["tier"] == 3 and loops["l1"]["description"] == "eigen"


def test_template_insert_steps_after(tmp_path):
    _template(tmp_path)
    loops = _expand(
        tmp_path,
        "  l1:\n    use: t\n    with: {repo: a/b}\n    insert:\n"
        "      - {after: a, steps: [{name: x, type: shell, cmd: 'echo {mode}'}]}\n",
    )
    steps = loops["l1"]["steps"]
    assert [s["name"] for s in steps] == ["a", "x", "b"]
    assert steps[1]["cmd"] == "echo fast"


@pytest.mark.parametrize(
    ("insert", "msg"),
    [
        ("{after: a}", "insert erwartet"),
        ("{steps: []}", "insert erwartet"),
        ("{after: 5, steps: []}", "insert erwartet"),
        ("[1]", "insert erwartet"),
        ("[{after: zzz, steps: []}]", "kein Step"),
        ("5", "Liste"),
    ],
)
def test_template_insert_errors(tmp_path, insert, msg):
    _template(tmp_path)
    ins = insert if insert.startswith("[") or insert == "5" else f"[{insert}]"
    with pytest.raises(ValueError, match=msg):
        _expand(tmp_path, f"  l1: {{use: t, with: {{repo: a/b}}, insert: {ins}}}\n")


def test_template_unknown_and_unsafe_names(tmp_path):
    for name in ("nope", "../t", 5):
        with pytest.raises(ValueError, match="nicht gefunden"):
            _expand(tmp_path, f"  l1: {{use: '{name}'}}\n")


def test_template_insert_without_after_and_unnamed_step(tmp_path):
    """Unbenannter Template-Step + insert ohne after → ValueError statt KeyError."""
    _template(tmp_path, body="params: {}\nloop:\n  steps:\n    - {type: shell, cmd: x}\n")
    with pytest.raises(ValueError, match="insert erwartet"):
        _expand(tmp_path, "  l1: {use: t, insert: [{steps: [{type: shell, cmd: y}]}]}\n")


def test_template_insert_with_own_steps_rejected(tmp_path):
    _template(tmp_path)
    with pytest.raises(ValueError, match="insert kann nicht mit eigenen steps"):
        _expand(
            tmp_path,
            "  l1:\n    use: t\n    with: {repo: a/b}\n    steps: [{name: own, type: shell, cmd: x}]\n"
            "    insert: [{after: b, steps: [{type: shell, cmd: y}]}]\n",
        )


@pytest.mark.parametrize("key", ["bas", "modell"])
def test_template_unknown_with_key_rejected(tmp_path, key):
    _template(tmp_path)
    with pytest.raises(ValueError, match=f"kennt with-Parameter nicht: {key}"):
        _expand(tmp_path, f"  l1: {{use: t, with: {{repo: a/b, {key}: x}}}}\n")


def test_template_with_key_used_only_as_placeholder_or_in_insert(tmp_path):
    """with-Keys ohne params-Eintrag sind erlaubt, wenn Template oder insert sie referenzieren."""
    _template(tmp_path)
    loops = _expand(
        tmp_path,
        "  l1:\n    use: t\n    with: {repo: a/b, issue_number: 7, extra: z}\n"
        "    insert: [{after: a, steps: [{name: x, type: shell, cmd: 'echo {extra}'}]}]\n",
    )
    assert loops["l1"]["steps"][0]["cmd"] == "echo fast 7"
    assert loops["l1"]["steps"][1]["cmd"] == "echo z"


def test_template_with_must_be_mapping(tmp_path):
    _template(tmp_path)
    with pytest.raises(ValueError, match="with muss"):
        _expand(tmp_path, "  l1: {use: t, with: [1]}\n")


@pytest.mark.parametrize("body", ["- x\n", "loop: 3\n", "loop: {}\nparams: [1]\n"])
def test_template_malformed(tmp_path, body):
    _template(tmp_path, body=body)
    with pytest.raises(ValueError, match="Template"):
        _expand(tmp_path, "  l1: {use: t}\n")


def test_template_lookup_order(tmp_path, monkeypatch):
    spec_t = _template(tmp_path / "spec")
    user_t = _template(tmp_path / "user")
    bundled_t = _template(tmp_path / "bundled")
    monkeypatch.setattr(config, "USER_TEMPLATES_DIR", user_t.parent)
    monkeypatch.setattr(config, "TEMPLATES_DIR", bundled_t.parent)
    spec = tmp_path / "spec" / "x.fabrik.yaml"
    assert spec_expand.find_template("t", spec) == spec_t
    assert spec_expand.find_template("t", None) == user_t
    user_t.unlink()
    assert spec_expand.find_template("t", None) == bundled_t


# ---------------------------------------------------------------- Mitgelieferte Templates / Dogfooding-Spec
ROOT = Path(__file__).resolve().parent.parent
SECOND_PROJECT = """\
name: app
root: {root}
github: acme/app
vars: {{repo: acme/app, builder_model: test-builder, reviewer_model: test-reviewer}}
loops:
  develop:
    use: develop
    with: {{prompt_file: develop.prompt.md, lint_cmd: make lint, test_cmd: make test}}
  review-pr: {{use: review-pr, with: {{prompt_file: review.prompt.md}}}}
"""


def test_second_project_in_few_lines_uses_bundled_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATES_DIR", ROOT / "templates")
    text = SECOND_PROJECT.format(root=tmp_path)
    assert len(text.splitlines()) <= 10 + 3  # Spec-Kern ≤ 10 Zeilen (+ name/root/github)
    spec = specs.load_spec(_write(tmp_path / "app.fabrik.yaml", text))
    dev, rev = spec["loops"]["develop"], spec["loops"]["review-pr"]
    assert [s["name"] for s in dev["steps"]] == [
        "prepare", "changes", "lint", "test", "ship", "pr", "review", "gate", "merge",
    ]  # fmt: skip
    assert dev["triggers"][0]["repo"] == "acme/app" and dev["triggers"][0]["poll"] == 60
    assert dev["steps"][2]["cmd"] == "make lint"
    assert dev["steps"][1]["model"] == "test-builder"
    assert "acme/app" not in rev["steps"][0]["prompt_file"]
    assert rev["triggers"][0] == {
        "type": "on_pr", "repo": "acme/app", "labels": ["fabrik"], "exclude_labels": ["needs human"],
        "poll": 300, "reconcile": "head",
    }  # fmt: skip


def test_bundled_templates_missing_required_params_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATES_DIR", ROOT / "templates")
    with pytest.raises(ValueError, match="braucht Parameter"):
        _expand(tmp_path, "  d: {use: develop}\n")


def test_fabrik_spec_has_no_hardcoded_repo_outside_vars():
    raw = yaml.safe_load((ROOT / "projects" / "fabrik.fabrik.yaml").read_text())
    body = {k: v for k, v in raw.items() if k != "vars"}
    assert "didaskomathetes/quark-playground" not in json.dumps(body)
    assert raw["vars"]["repo"] == "didaskomathetes/quark-playground"


def test_fabrik_spec_expands_with_bundled_templates(monkeypatch):
    monkeypatch.setattr(config, "TEMPLATES_DIR", ROOT / "templates")
    path = ROOT / "projects" / "fabrik.fabrik.yaml"
    raw = yaml.safe_load(path.read_text())
    spec = spec_expand.expand_spec(raw, path)
    assert [s["name"] for s in spec["loops"]["develop"]["steps"]][:4] == ["prepare", "changes", "check-adr", "lint"]
    assert spec["loops"]["develop"]["triggers"][0]["labels"] == ["ready for development"]
    assert spec["loops"]["review-pr"]["steps"][0]["model"] == raw["vars"]["reviewer_model"]
    assert spec["loops"]["pr-reply"]["steps"][0]["model"] == spec["loops"]["develop"]["steps"][1]["model"]


def test_fabrik_spec_builder_model_differs_from_reviewer_model():
    """Cold Critic (ADR 0001/0003): Builder-Modell ≠ Reviewer-Modell — die
    Laufzeitprüfung in `pr_review`/`review` darf mit der Dogfooding-Konfiguration
    nicht anfallen. Der Reviewer läuft ohne Copilot (opencode + lokales Qwen)."""
    raw = yaml.safe_load((ROOT / "projects" / "fabrik.fabrik.yaml").read_text())
    v = raw["vars"]
    assert v["builder_model"].strip() != v["reviewer_model"].strip()
    assert v["reviewer_backend"] == "opencode"
    assert "copilot" not in (v["reviewer_backend"], v["reviewer_model"], v["gate_provider"])
    assert v["gate_provider"] == "llama-cpp"
    assert raw["max_parallel"] == 1  # llama.cpp-Server verträgt nur eine Verbindung


def test_fabrik_spec_review_steps_use_opencode_not_copilot(monkeypatch):
    """Die expandierten Review-Steps (develop/pr_review + review-pr) nutzen
    `reviewer_backend` (opencode), nicht den Template-Default `copilot` —
    Regression: `with:` übergibt nur `reviewer_model`, `reviewer_backend` kommt
    aus `vars` (ADR 0047: vars überschreiben Template-Defaults)."""
    monkeypatch.setattr(config, "TEMPLATES_DIR", ROOT / "templates")
    path = ROOT / "projects" / "fabrik.fabrik.yaml"
    raw = yaml.safe_load(path.read_text())
    spec = spec_expand.expand_spec(raw, path)
    review = [s for s in spec["loops"]["develop"]["steps"] if s["name"] == "review"][0]
    gate = [s for s in spec["loops"]["develop"]["steps"] if s["name"] == "gate"][0]
    rp = spec["loops"]["review-pr"]["steps"][0]
    for step in (review, rp):
        assert step["backend"] == "opencode"
        assert step["model"] == raw["vars"]["reviewer_model"]
    assert gate["provider"] == "llama-cpp"


# ---------------------------------------------------------------- spec_path / config
def test_spec_path_reuses_given_projects(tmp_path, monkeypatch):
    def no_reload():
        raise AssertionError("load_projects darf nicht erneut laufen")

    monkeypatch.setattr(specs, "load_projects", no_reload)
    spec = tmp_path / "a.fabrik.yaml"
    assert specs.spec_path("a", {"a": {"_path": str(spec)}}) == spec
    assert specs.spec_path("b", {}) == config.PROJECTS_DIR / "b.fabrik.yaml"


def test_fabrik_data_from_env_file(tmp_path):
    """FABRIK_DATA aus fabrik.env wirkt auf die Pfad-Konstanten (Env-File vor den Konstanten geladen)."""
    home, data = tmp_path / "home", tmp_path / "data"
    _write(home / "fabrik.env", f"FABRIK_DATA={data}\n")
    env = {k: v for k, v in os.environ.items() if k != "FABRIK_DATA"} | {"FABRIK_HOME": str(home)}
    out = subprocess.run(
        [sys.executable, "-c", "from fabrik import config; print(config.PROJECTS_DIR)"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == str(data / "projects")
