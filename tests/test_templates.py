"""Template library (ADR 0067): every bundled template loads, expands and validates;
`list_templates` + `scaffold_spec` + the `template` CLI subcommand."""

import pytest
import yaml

from fabrikant import config, templates
from fabrikant.spec_expand import expand_spec, placeholders
from fabrikant.specschema import validate_spec

# Sample `with:` values that satisfy each template's required params.
SAMPLE_PARAMS = {
    "develop": {
        "repo": "o/r",
        "prompt_file": "p.md",
        "lint_cmd": "true",
        "test_cmd": "true",
        "builder_model": "b",
        "reviewer_model": "r",
    },
    "review-pr": {"repo": "o/r", "prompt_file": "p.md", "reviewer_model": "r"},
    "escalation": {"repo": "o/r"},
    "docs": {"repo": "o/r", "prompt_file": "p.md", "builder_model": "b"},
    "hotfix": {
        "repo": "o/r",
        "prompt_file": "p.md",
        "lint_cmd": "true",
        "test_cmd": "true",
        "builder_model": "b",
        "reviewer_model": "r",
        "base": "release/1.0",
    },
    "dogfood": {"repo": "o/r", "prompt_file": "p.md", "test_cmd": "true", "builder_model": "b"},
}


def test_at_least_five_templates():
    names = {t["name"] for t in templates.list_templates()}
    assert len(names) >= 5
    assert set(SAMPLE_PARAMS) <= names


def test_list_templates_reports_required_params():
    by_name = {t["name"]: t for t in templates.list_templates()}
    assert by_name["develop"]["required"] and "repo" in by_name["develop"]["required"]
    assert by_name["escalation"]["required"] == ["repo"]
    assert all(t["source"] in ("bundled", "user") for t in templates.list_templates())
    assert all(t["description"] for t in templates.list_templates())


@pytest.mark.parametrize("name", sorted(SAMPLE_PARAMS))
def test_template_expands_and_validates(name):
    spec = {"name": "t", "loops": {name: {"use": name, "with": SAMPLE_PARAMS[name]}}}
    expanded = expand_spec(spec)
    assert validate_spec(expanded) == []


@pytest.mark.parametrize("name", sorted(SAMPLE_PARAMS))
def test_template_defaults_cover_all_placeholders(name):
    """Every `{name}` in the template loop is resolvable from params/with (no stray literals)."""
    tpl = yaml.safe_load(templates.template_files()[name].read_text())
    unresolved = placeholders(tpl["loop"]) - set(tpl.get("params") or {})
    # Engine runtime variables are allowed to stay (they resolve at run time).
    assert unresolved <= {
        "issue_number",
        "issue_title",
        "run_id",
        "pr_number",
        "pr_title",
        "fabrikant_home",
    }, f"{name}: unresolved placeholders {unresolved}"


def test_scaffold_writes_valid_yaml(tmp_path):
    path = templates.scaffold_spec("docs", "demo", tmp_path)
    data = yaml.safe_load(path.read_text())
    assert data["name"] == "demo"
    loop = data["loops"]["docs"]
    assert loop["use"] == "docs"
    # Required params are written as TODO nulls.
    assert loop["with"]["repo"] is None
    assert loop["with"]["prompt_file"] is None


def test_scaffold_refuses_overwrite_without_force(tmp_path):
    templates.scaffold_spec("docs", "demo", tmp_path)
    with pytest.raises(FileExistsError):
        templates.scaffold_spec("docs", "demo", tmp_path)
    templates.scaffold_spec("docs", "demo", tmp_path, force=True)


def test_scaffold_unknown_template(tmp_path):
    with pytest.raises(ValueError):
        templates.scaffold_spec("does-not-exist", "demo", tmp_path)


def test_scaffold_invalid_project_name(tmp_path):
    with pytest.raises(ValueError):
        templates.scaffold_spec("docs", "bad name!", tmp_path)


def test_user_template_overrides_bundled(tmp_path, monkeypatch):
    """Same name in the user dir wins over the bundled template."""
    user_dir = tmp_path / "user_templates"
    user_dir.mkdir()
    (user_dir / "escalation.loop.yaml").write_text(
        "params:\n  repo: null\nloop:\n  description: user override\n"
        "  triggers: [{type: manual}]\n  steps: [{name: r, type: report, text: x}]\n"
    )
    monkeypatch.setattr(config, "USER_TEMPLATES_DIR", user_dir)
    files = templates.template_files()
    assert files["escalation"].parent == user_dir
    by_name = {t["name"]: t for t in templates.list_templates()}
    assert by_name["escalation"]["source"] == "user"
    assert "user override" in by_name["escalation"]["description"]


def test_cli_template_list(capsys):
    import fabrikant.cli as cli

    args = type("A", (), {"template_cmd": "list"})()
    cli.cmd_template(args)
    out = capsys.readouterr().out
    for name in SAMPLE_PARAMS:
        assert name in out


def test_cli_template_create(capsys, tmp_path):
    import fabrikant.cli as cli

    args = type(
        "A",
        (),
        {
            "template_cmd": "create",
            "name": "dogfood",
            "project": "myproj",
            "out": str(tmp_path),
            "force": False,
        },
    )()
    cli.cmd_template(args)
    out = capsys.readouterr().out
    assert "myproj.fabrikant.yaml" in out
    data = yaml.safe_load((tmp_path / "myproj.fabrikant.yaml").read_text())
    assert data["loops"]["dogfood"]["use"] == "dogfood"


def test_cli_template_create_error_exit(capsys, tmp_path):
    import fabrikant.cli as cli

    args = type(
        "A",
        (),
        {
            "template_cmd": "create",
            "name": "does-not-exist",
            "project": "x",
            "out": str(tmp_path),
            "force": False,
        },
    )()
    with pytest.raises(SystemExit) as e:
        cli.cmd_template(args)
    assert e.value.code == 1


def test_scaffold_without_required_params_writes_empty_with(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "noargs.loop.yaml").write_text(
        "params:\n  poll: 60\nloop:\n  description: d\n  triggers: [{type: manual}]\n"
        "  steps: [{name: r, type: report, text: x}]\n"
    )
    monkeypatch.setattr(config, "USER_TEMPLATES_DIR", user_dir)
    path = templates.scaffold_spec("noargs", "demo", tmp_path / "out")
    assert yaml.safe_load(path.read_text())["loops"]["noargs"] == {"use": "noargs", "with": {}}


def test_broken_template_is_listed_with_error_and_does_not_hide_others(tmp_path, monkeypatch, capsys):
    import fabrikant.cli as cli

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "bad.loop.yaml").write_text("loop: [not, a, mapping\n")  # YAML syntax error
    (user_dir / "worse.loop.yaml").write_text("just: a string\n")  # no `loop:` mapping
    monkeypatch.setattr(config, "USER_TEMPLATES_DIR", user_dir)
    by_name = {t["name"]: t for t in templates.list_templates()}
    assert "error" in by_name["bad"] and "error" in by_name["worse"]
    assert "error" not in by_name["develop"]
    with pytest.raises(SystemExit) as e:
        cli.cmd_template(type("A", (), {"template_cmd": "list"})())
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "✖ bad" in out and "● develop" in out
