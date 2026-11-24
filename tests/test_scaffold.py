"""`fabrik init` scaffolding (ADR 0069)."""

from __future__ import annotations

import yaml

from fabrik import cli, scaffold, specs


def test_init_creates_a_valid_project_and_never_overwrites(tmp_path):
    created = scaffold.init_project(tmp_path / "factory")
    names = {p.relative_to(tmp_path / "factory").as_posix() for p in created}
    assert names == {
        "projects/hello.fabrik.yaml",
        "fabrik.env",
        "plugins/pyproject.toml",
        "plugins/my_steps.py",
    }
    spec = yaml.safe_load((tmp_path / "factory" / "projects" / "hello.fabrik.yaml").read_text())
    assert spec["root"] == str((tmp_path / "factory").resolve())
    specs._validate_spec(spec)
    env = tmp_path / "factory" / "fabrik.env"
    env.write_text("MINE=1\n")
    assert scaffold.init_project(tmp_path / "factory") == []
    assert env.read_text() == "MINE=1\n"


def test_plugin_skeleton_is_valid(tmp_path):
    import tomllib

    scaffold.init_project(tmp_path)
    meta = tomllib.loads((tmp_path / "plugins" / "pyproject.toml").read_text())
    assert meta["project"]["entry-points"]["fabrik.steps"] == {"greet": "my_steps:greet"}
    compile((tmp_path / "plugins" / "my_steps.py").read_text(), "my_steps.py", "exec")


def test_cli_init(tmp_path, capsys):
    cli.main(["init", str(tmp_path / "x")])
    out = capsys.readouterr().out
    assert "hello.fabrik.yaml" in out and "fabrik serve" in out
    cli.main(["init", str(tmp_path / "x")])
    assert "fabrik serve" in capsys.readouterr().out
