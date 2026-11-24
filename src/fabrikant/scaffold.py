"""`fabrikant init`: scaffold a user project directory (ADR 0069).

Creates `projects/hello.fabrikant.yaml`, `fabrikant.env` and a `plugins/` skeleton in a directory that then
acts as the fabrikant home (runs, state, specs) whenever fabrikant is started from it."""

from __future__ import annotations

from pathlib import Path

from . import config

STARTER_SPEC = "hello.fabrikant.yaml"
ENV_TEMPLATE = "fabrikant.env.example"
PLUGIN_PYPROJECT = """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "my-fabrikant-steps"
version = "0.1.0"
dependencies = ["fabrikant"]

# The registration entry point: step type `greet` -> function my_steps.greet
[project.entry-points."fabrikant.steps"]
greet = "my_steps:greet"

[tool.hatch.build.targets.wheel]
include = ["my_steps.py"]
"""
PLUGIN_SKELETON = '''"""Example custom step; install with `pip install -e plugins/`, then use
`- {type: greet, name: hi, who: world}` in a loop (docs/plugins.md)."""

from fabrikant.sdk import ok


def greet(ctx, step, emit):
    emit("greeting")
    return ok(f"hello {step.get('who', 'world')}")
'''


def init_project(dest: Path) -> list[Path]:
    """Scaffold `dest`; returns the files created. Existing files are never overwritten."""
    dest = dest.resolve()
    (dest / "projects").mkdir(parents=True, exist_ok=True)
    spec = (config.PACKAGE_DIR / "examples" / STARTER_SPEC).read_text(encoding="utf-8")
    files = {
        dest / "projects" / STARTER_SPEC: spec.replace("root: .", f"root: {dest}"),
        dest / config.ENV_FILE_NAME: (config.PACKAGE_DIR / "examples" / ENV_TEMPLATE).read_text(encoding="utf-8"),
        dest / "plugins" / "pyproject.toml": PLUGIN_PYPROJECT,
        dest / "plugins" / "my_steps.py": PLUGIN_SKELETON,
    }
    created = []
    for path, content in files.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)
    return created
