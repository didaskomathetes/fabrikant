# Installing fabrik and writing step plugins

See [ADR 0069](adr/0069-distribution-and-plugin-contract.md) for the design.

## Install

```bash
pip install fabrik            # or: docker run -v fabrik-data:/data -p 8321:8321 ghcr.io/<owner>/fabrik
mkdir my-factory && cd my-factory
fabrik init                   # projects/hello.fabrik.yaml, fabrik.env, plugins/ skeleton
fabrik validate projects/hello.fabrik.yaml
fabrik serve                  # UI on http://127.0.0.1:8321
```

The directory containing `fabrik.env` is the fabrik *home* (runs, state, session log) whenever you start
fabrik from it; elsewhere set `FABRIK_HOME`. Without either, an installed fabrik uses
`~/.local/share/fabrik`. Specs: `<home>/projects/*.fabrik.yaml` (plus `FABRIK_PROJECTS_PATH`).

## Custom steps

A step plugin is a function plus an entry point in a normal Python package:

```python
# my_steps.py
from fabrik.sdk import ok, failed, FailureKind

def greet(ctx, step, emit):
    emit("greeting")
    return ok(f"hello {step.get('who', 'world')}")
```

```toml
# pyproject.toml of the plugin package
[project.entry-points."fabrik.steps"]
greet = "my_steps:greet"
```

After `pip install` of the package (same environment as fabrik), `type: greet` is valid in every spec.
`fabrik.sdk` is the only stable API: `register_step`, `StepPlugin` (entry-point target for steps with a JSON
schema or `needs_worktree`), `ok`, `failed`, `FailureKind`, `substitute`. Plugins cannot override built-in
step types, and a plugin that fails to import is logged and skipped.

## Docker

The image runs `fabrik serve` with `FABRIK_HOME=/data`. Add plugins by extending it:

```dockerfile
FROM ghcr.io/<owner>/fabrik:latest
RUN pip install my-fabrik-steps
```
