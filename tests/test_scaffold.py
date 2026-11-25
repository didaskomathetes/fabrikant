"""`fabrikant init` scaffolding (ADR 0069)."""

from __future__ import annotations

import yaml

from fabrikant import cli, config, scaffold, specs


def test_init_creates_a_valid_project_and_never_overwrites(tmp_path):
    created = scaffold.init_project(tmp_path / "factory")
    names = {p.relative_to(tmp_path / "factory").as_posix() for p in created}
    assert names == {
        "projects/hello.fabrikant.yaml",
        "fabrikant.env",
        ".gitignore",
        "plugins/pyproject.toml",
        "plugins/my_steps.py",
    }
    spec = yaml.safe_load((tmp_path / "factory" / "projects" / "hello.fabrikant.yaml").read_text())
    assert spec["root"] == "."  # portable: resolved against the fabrikant home when loaded
    ignore = (tmp_path / "factory" / ".gitignore").read_text().splitlines()
    assert {"fabrikant.env", "runs/", "state/"} <= set(ignore)
    env = tmp_path / "factory" / "fabrikant.env"
    env.write_text("MINE=1\n")
    assert scaffold.init_project(tmp_path / "factory") == []
    assert env.read_text() == "MINE=1\n"


def test_plugin_skeleton_is_valid(tmp_path):
    import tomllib

    scaffold.init_project(tmp_path)
    meta = tomllib.loads((tmp_path / "plugins" / "pyproject.toml").read_text())
    assert meta["project"]["entry-points"]["fabrikant.steps"] == {"greet": "my_steps:greet"}
    compile((tmp_path / "plugins" / "my_steps.py").read_text(), "my_steps.py", "exec")


def test_cli_init(tmp_path, capsys):
    cli.main(["init", str(tmp_path / "x")])
    out = capsys.readouterr().out
    assert "hello.fabrikant.yaml" in out and "fabrikant serve" in out
    cli.main(["init", str(tmp_path / "x")])
    assert "fabrikant serve" in capsys.readouterr().out


def test_relative_root_resolves_against_home(tmp_path, monkeypatch):
    scaffold.init_project(tmp_path)
    monkeypatch.setattr(config, "FABRIKANT_HOME", tmp_path)
    spec = specs.load_spec(tmp_path / "projects" / "hello.fabrikant.yaml")
    assert spec["root"] == str(tmp_path.resolve())
