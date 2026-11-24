# Installing fabrikant and writing step plugins

See [ADR 0069](adr/0069-distribution-and-plugin-contract.md) for the design.

## Install

```bash
pip install fabrikant            # or: docker run -v fabrikant-data:/data -p 8321:8321 ghcr.io/<owner>/fabrikant
mkdir my-factory && cd my-factory
fabrikant init                   # projects/hello.fabrikant.yaml, fabrikant.env, plugins/ skeleton
fabrikant validate projects/hello.fabrikant.yaml
fabrikant serve                  # UI on http://127.0.0.1:8321
```

The directory containing `fabrikant.env` is the fabrikant *home* (runs, state, session log) whenever you start
fabrikant from it; elsewhere set `FABRIKANT_HOME`. Without either, an installed fabrikant uses
`~/.local/share/fabrikant`. Specs: `<home>/projects/*.fabrikant.yaml` (plus `FABRIKANT_PROJECTS_PATH`).

## Custom steps

A step plugin is a function plus an entry point in a normal Python package:

```python
# my_steps.py
from fabrikant.sdk import ok, failed, FailureKind

def greet(ctx, step, emit):
    emit("greeting")
    return ok(f"hello {step.get('who', 'world')}")
```

```toml
# pyproject.toml of the plugin package
[project.entry-points."fabrikant.steps"]
greet = "my_steps:greet"
```

After `pip install` of the package (same environment as fabrikant), `type: greet` is valid in every spec.
`fabrikant.sdk` is the only stable API: `register_step`, `StepPlugin` (entry-point target for steps with a JSON
schema or `needs_worktree`), `ok`, `failed`, `FailureKind`, `substitute`. Plugins cannot override built-in
step types, and a plugin that fails to import is logged and skipped.

## Docker

The image runs `fabrikant serve` with `FABRIKANT_HOME=/data`. Add plugins by extending it:

```dockerfile
FROM ghcr.io/<owner>/fabrikant:latest
RUN pip install my-fabrikant-steps
```
