"""`fabrikant init`: scaffold a user project directory (ADR 0069).

Creates `projects/hello.fabrikant.yaml`, `fabrikant.env`, `.gitignore` and a `plugins/` skeleton in a directory that then
acts as the fabrikant home (runs, state, specs) whenever fabrikant is started from it."""

from __future__ import annotations

from pathlib import Path

from . import config

STARTER_SPEC = "hello.fabrikant.yaml"
# Local state and secrets that must never be committed (fabrikant.env may hold tokens).
GITIGNORE_HEADER = b"# fabrikant local state and secrets\n"
GITIGNORE_RULES = (b"fabrikant.env", b"runs/", b"state/", b"sessionlog.db*", b".fabrikant/")
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
`- {type: greet, name: hi, who: world}` in a loop
(https://github.com/didaskomathetes/fabrikant/blob/main/docs/plugins.md)."""

from fabrikant.sdk import ok


def greet(ctx, step, emit):
    emit("greeting")
    return ok(f"hello {step.get('who', 'world')}")
'''


def _ensure_gitignore(path: Path) -> bool:
    """Add the scaffold's ignore rules and return whether the file was created."""
    try:
        with path.open("xb") as ignore:
            ignore.write(GITIGNORE_HEADER + b"\n".join(GITIGNORE_RULES) + b"\n")
        return True
    except FileExistsError:
        pass
    with path.open("r+b") as ignore:
        content = ignore.read()
        effective_rules = set()
        for line in content.splitlines():
            # A later negation can override an earlier rule, including via a wildcard.
            if line.startswith(b"!"):
                effective_rules.clear()
            else:
                effective_rules.add(line)
        missing = [rule for rule in GITIGNORE_RULES if rule not in effective_rules]
        if missing:
            if content and not content.endswith(b"\n"):
                ignore.write(b"\n")
            ignore.write(GITIGNORE_HEADER + b"\n".join(missing) + b"\n")
    return False


def init_project(dest: Path) -> list[Path]:
    """Scaffold `dest` and return the files created. Append missing rules to an existing .gitignore."""
    dest = dest.resolve()
    (dest / "projects").mkdir(parents=True, exist_ok=True)
    spec = (config.PACKAGE_DIR / "examples" / STARTER_SPEC).read_text(encoding="utf-8")
    files = {
        dest / "projects" / STARTER_SPEC: spec,
        dest / config.ENV_FILE_NAME: (config.PACKAGE_DIR / "examples" / ENV_TEMPLATE).read_text(encoding="utf-8"),
        dest / "plugins" / "pyproject.toml": PLUGIN_PYPROJECT,
        dest / "plugins" / "my_steps.py": PLUGIN_SKELETON,
    }
    gitignore = dest / ".gitignore"
    created = [gitignore] if _ensure_gitignore(gitignore) else []
    for path, content in files.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)
    return created
