# ADR 0069: Distribution and Plugin Contract — `pip install fabrikant`, Docker, Step Plugins

**Status:** Accepted (2026-11-24, merged to main)
**Affects:** ADR 0047 (spec sources, templates), ADR 0056 (adapter registry), ADR 0063 (provider
registry, entry-point pattern), ADR 0034 (storage).

## Context

fabrikant only runs from a source checkout:

- `config.FABRIKANT_HOME` is "three directories above `config.py`" — inside site-packages that is
  `lib/python3.x/`. Runs, state, `sessionlog.db`, `fabrikant.env` hang off it.
- `web/`, `templates/`, `prompts/` and example `projects/` live outside `src/fabrikant` and are not in
  the wheel.
- The step set is closed: `engine._dispatch_step` is an `if t == …` chain and the spec schema
  enumerates `type`. A user cannot add a node without patching core.

The goal is a framework whose users own only their specs, their custom nodes and a registration
entry point; fabrikant itself is installed from PyPI or run from a Docker image.

## Decision

1. **Code vs. data.** Everything read-only that the program needs ships *inside* the package:
   `fabrikant/web`, `fabrikant/templates`, `fabrikant/prompts/relay`, `fabrikant/examples` (bundled example specs),
   resolved via `importlib.resources`-style package paths (`config.PACKAGE_DIR`).
2. **Home and data directory.** Mutable data (runs, state, sessionlog, `fabrikant.env`) lives in
   `config.FABRIKANT_HOME`, resolved as `FABRIKANT_HOME` env > the checkout root (if the code runs from a
   source checkout: `pyproject.toml` + `src/fabrikant`) > `$XDG_DATA_HOME/fabrikant` (default
   `~/.local/share/fabrikant`). `FABRIKANT_DATA` keeps its ADR 0047 meaning (user specs/templates,
   defaults to the home). Checkout mode therefore behaves exactly as before.
3. **`fabrikant init [DIR]`** scaffolds a user project directory (`projects/`, `templates/`,
   `fabrikant.env.example`, a starter spec, a plugin skeleton hint) so the first run needs no repo.
4. **Step registry (`fabrikant.steps.registry`).** `register_step(type, handler, *, schema=None,
   needs_worktree=False, replace=False)` holds *third-party* step types; built-ins stay dispatched
   by the engine and **cannot be shadowed** by a plugin. The engine falls back to the registry for
   non-built-in types and fails unknown types with the list of known ones. Plugins are discovered
   through the `fabrikant.steps` entry-point group (same loader pattern as ADR 0063; a broken entry
   point is logged and skipped). Handler signature: `(ctx: dict, step: dict, emit: Callable[[str],
   None]) -> dict` returning the existing result dict (`ok`, `summary`, optional failure fields).
5. **Spec validation** checks the step `type` against the registry (built-ins + plugins) at load
   time instead of via a schema enum (the schema only requires a string). A plugin may provide a
   JSON-schema fragment for its step mapping, validated when present; `verify` checks may use
   plugin types too.
6. **Public SDK surface (`fabrikant.sdk`).** Re-exports the only API plugins may rely on:
   `register_step`, `StepHandler`, `StepPlugin`, `ok`/`failed` result helpers, `FailureKind`, `substitute`. Semver applies to
   this module (and the entry-point group names); everything else is internal.
7. **Docker.** A multi-stage image installs the wheel, runs as non-root, stores data in the
   `/data` volume (`FABRIKANT_HOME=/data`). Users extend with `FROM … && pip install their-plugins`.
8. **Release.** Tag `vX.Y.Z` triggers build → `twine check` → PyPI Trusted Publishing (OIDC, no
   tokens) and a GHCR image. `pysonar` moves to the dev group (not a runtime dependency).

## Consequences

- (+) `pip install fabrikant && fabrikant init && fabrikant serve` works from any directory.
- (+) Custom nodes are plain Python packages with one entry point; no fork needed.
- (+) One registration mechanism for steps and hosting providers.
- (−) The `fabrikant.sdk` surface becomes a compatibility promise.
- (−) `web/`, `templates/`, `prompts/relay/` move into the package. Project-specific prompts
  (`prompts/fabrikant-*`) stay user data; `prompt_file` resolves spec dir → `FABRIKANT_HOME` → `FABRIKANT_DATA`
  → bundled package assets.
- (−) The `type` enum is gone from the schema; editor autocompletion for step types must come from
  the generated reference instead.
