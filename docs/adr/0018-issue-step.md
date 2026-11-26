# ADR 0018: Native `issue` step

**Status:** Accepted (2026-10-20, merge #184; proposed 2026-10-20)
**Extends:** ADR 0001 §5 (`gh` CLI as a stopgap until the `issue` step exists).

## Context

Triage/refine/sweep loops performed issue operations via `shell` + `gh`. This is
fragile (substitution in the shell string, ADR 0008 needs `env:` workarounds,
mktemp/trap boilerplate) and the API calls cannot be tested in isolation.

## Decision

- New deterministic step type `issue` (`create|edit|label|close`) based on the
  existing GitHub REST helpers (`_gh_api`, `GITHUB_TOKEN`, as in
  `pr`/`pr_merge`). Values go to the API as a JSON payload, never through a shell.
- `create` sets `issue_number`/`issue_url` in the variable store.
- Conditional execution local to the step via `only_if` (same semantics as
  `approval_when`, shared helper `_cond_matches`) and `skip_if_empty`; `edit`
  supports `append` + `unless_contains` (idempotency marker instead of a shell
  `case`).
- `label` creates missing labels implicitly (GitHub behaviour). The previous
  `ensure-labels` steps (`gh label create --color`) are dropped; label colours
  have to be set manually once. Deliberately no fifth action type.
- Triage, refine and refactor-sweep loops use `issue` steps. The sweep agent now
  only delivers JSON candidates; an `llm` step extracts them, and at most 3
  `create` steps create the issues. Some specs stay on `shell` + `gh` for now
  (out of scope).
- Token protection: `GITHUB_TOKEN` never appears in step results; in addition,
  the existing secret masking applies to logs and run records.

## Consequences

- Less shell injection surface, testable issue logic without `gh`.
- Further specs can be migrated analogously later.
