# ADR 0067: Template Library — Curated, Documented, Versioned Loop Templates

**Status:** Draft (2026-11-23, branch `feat/template-library`)
**Betrifft:** ADR 0047 (spec sources, vars & templates), `docs/roadmap.md` First win 3 (#133), `src/fabrikant/spec_expand.py`

## Context

The spec-as-standard story (First win 3, #131/#132/#133) needs an on-ramp: a new
project (or a new user) should be able to start with a *known-good* loop shape
instead of writing a spec from scratch. The ADR 0047 template mechanism
(`use:`/`with:`) already exists and is used by fabrikant's own dogfooding specs, but
the library is thin: three templates, no documentation, no way to discover them,
and no way to scaffold a project from one.

The core use cases a factory should offer out of the box:

- **feature loop** — issue → branch → agent → lint/test → PR → review → merge (`develop`, shipped)
- **PR review** — cross-model review of labeled PRs (`review-pr`, shipped)
- **docs** — documentation changes as PRs, no auto-merge
- **hotfix** — fast fix path onto a release branch
- **dogfood** — the minimal self-hosting loop (ADR 0001 on-ramp)

## Decision

**Library.** `src/fabrikant/templates/` (bundled with the package) is the curated
template library. Every template is an ADR 0047 file (`params:` + `loop:`),
documented in [docs/templates.md](../templates.md) (usage, required parameters,
example spec), and covered by
a validation test that expands it with sample parameters and checks the result
against the spec schema (`tests/test_templates.py`). New templates are added by
adding a file + docs + test entry — no engine change.

Five bundled templates ship with this ADR: `develop`, `review-pr`, `docs`,
`hotfix`, `dogfood` (plus `escalation`, shipped earlier). Design constraints for
the new ones:

- **No auto-merge for docs/dogfood** — they have no merge step, and `approval: true` on the PR
  step makes a human approve before the PR is opened. Only
  `develop`/`hotfix` merge automatically, and `hotfix` always passes a
  `human_gate`.
- **`hotfix` cuts ahead of dependency gates** (`skip_blocked: false`, ADR 0031
  opt-out) and targets a release branch (`base` is a required parameter).
- **`dogfood` is intentionally minimal** — one loop, no refine, no review loop,
  no escalation. It is the starting point; projects grow from it.

**Discovery & scaffolding.** `fabrikant template list` shows every template
(bundled + user dir, with the user dir winning on name collisions — the same
resolution order as `spec_expand.find_template`) with its description and
required parameters. `fabrikant template create <name> [--project P] [--out DIR]
[--force]` writes `<DIR>/<P>.fabrikant.yaml` with one loop instance of the
template; required parameters (`null` defaults) are emitted as
`param: null  # TODO` lines. The scaffold is valid YAML and fails
`fabrikant validate` until the TODOs are filled in — a deliberate, explicit state.

## Consequences

- New projects start from a documented, schema-validated loop shape instead of
  an empty spec; the template file is the single source for both docs and
  behavior.
- `templates.py` reuses `spec_expand._load_template`/`find_template` — one
  parser, one resolution order; the library cannot drift from what the engine
  accepts.
- Template content is versioned with the code (repo files); per-template
  "versioning" is git history — no separate version numbers (YAGNI).
- User templates in `FABRIKANT_DATA/templates` already overrode bundled ones
  (ADR 0047); `template list`/`create` make that mechanism visible.
