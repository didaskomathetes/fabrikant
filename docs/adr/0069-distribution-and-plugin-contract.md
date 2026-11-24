# ADR 0069: Distribution and Plugin Contract — `pip install fabrik`, Docker, Step Plugins

**Status:** Proposed (2026-11-24)
**Affects:** ADR 0047 (spec sources, templates), ADR 0056 (adapter registry), ADR 0063 (provider
registry, entry-point pattern), ADR 0034 (storage).

## Context

fabrik only runs from a source checkout:

- `config.FABRIK_HOME` is "three directories above `config.py`" — inside site-packages that is
  `lib/python3.x/`. Runs, state, `sessionlog.db`, `fabrik.env` hang off it.
- `web/`, `templates/`, `prompts/` and example `projects/` live outside `src/fabrik` and are not in
  the wheel.
- The step set is closed: `engine._dispatch_step` is an `if t == …` chain and the spec schema
  enumerates `type`. A user cannot add a node without patching core.

The goal is a framework whose users own only their specs, their custom nodes and a registration
entry point; fabrik itself is installed from PyPI or run from a Docker image.

## Decision

1. **Code vs. data.** Everything read-only that the program needs ships *inside* the package:
   `fabrik/web`, `fabrik/templates`, `fabrik/prompts`, `fabrik/examples` (bundled example specs),
   resolved via `importlib.resources`-style package paths (`config.PACKAGE_DIR`).
2. **Data directory.** Mutable data (runs, state, sessionlog, `fabrik.env`, user specs/templates)
   lives in `config.DATA_DIR`, resolved as `FABRIK_DATA` > `FABRIK_HOME` (legacy, checkout mode) >
   a platform default (`$XDG_DATA_HOME/fabrik`, i.e. `~/.local/share/fabrik`). Running from a git
   checkout keeps working unchanged: if the repo root contains `pyproject.toml` + `src/fabrik`,
   `FABRIK_HOME` defaults to it as before.
3. **`fabrik init [DIR]`** scaffolds a user project directory (`projects/`, `templates/`,
   `fabrik.env.example`, a starter spec, a plugin skeleton hint) so the first run needs no repo.
4. **Step registry (`fabrik.steps.registry`).** `register_step(type, handler, *, schema=None,
   replace=False)`. Built-in steps register through the same API. The engine dispatches via the
   registry; unknown types fail with the list of known types. Third parties register through the
   `fabrik.steps` entry-point group (same loader pattern as ADR 0063). Handler signature:
   `(ctx: dict, step: dict, emit: Callable[[str], None]) -> dict` returning the existing result
   dict (`ok`, `summary`, optional `failure_kind`).
5. **Spec validation** takes the allowed step `type` values from the registry (built-ins +
   plugins) instead of a schema enum. A plugin may provide a JSON-schema fragment for its own
   step fields, validated when present.
6. **Public SDK surface (`fabrik.sdk`).** Re-exports the only API plugins may rely on:
   `register_step`, `StepHandler`, `failed`/`ok` result helpers, `FailureKind`. Semver applies to
   this module (and the entry-point group names); everything else is internal.
7. **Docker.** A multi-stage image installs the wheel, runs as non-root, stores data in the
   `/data` volume (`FABRIK_DATA=/data`). Users extend with `FROM … && pip install their-plugins`.
8. **Release.** Tag `vX.Y.Z` triggers build → `twine check` → PyPI Trusted Publishing (OIDC, no
   tokens) and a GHCR image. `pysonar` moves to the dev group (not a runtime dependency).

## Consequences

- (+) `pip install fabrik && fabrik init && fabrik serve` works from any directory.
- (+) Custom nodes are plain Python packages with one entry point; no fork needed.
- (+) One registration mechanism for steps and hosting providers.
- (−) The `fabrik.sdk` surface becomes a compatibility promise.
- (−) Moving `web/`, `templates/`, `prompts/` into the package changes paths referenced in specs
  (`prompt_file: prompts/…` falls back to the bundled copy when no user file exists).
- (−) The `type` enum is gone from the schema; editor autocompletion for step types must come from
  the generated reference instead.
