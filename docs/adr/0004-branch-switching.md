# ADR 0004: Branch switching at runtime (UI)

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-17); UI selector removed (2026-11-04) — API and override state remain
**Decided by:** Dominic
**Context:** A project's target branch is fixed in the spec
(`repo.branch`). Switching branches (e.g. `fabrik` from `main` to
`fabrik/v0.3`) required a manual YAML edit; the web UI showed the
branch read-only.

## Decision

Branch override as runtime state, **no** spec change:

- `state/branch-overrides.json`: `{project: branch}` — a small JSON file in
  `STATE_DIR` (same pattern as `state/active_llm.json`).
- `specs.load_projects()` applies the override to the loaded spec at the end
  (`repo.branch`) — all consumers (server, engine, CLI, scheduler) see
  the same branch in a single place.
- The YAML remains the canonical default; the file (including comments) is never
  rewritten.
- API:
  - `GET /api/projects/<name>/branches` → `{branches: [...], current: "…"}`
    (local + remote branches, deduplicated, sorted)
  - `POST /api/projects/<name>/branch` `{"branch": "…"}` → validates that the
    branch exists in the repo (else 400), persists the override
- UI: a branch selector per project in the detail view (dropdown,
  `#llmselect` style).

On the next run the worktree syncs to `origin/<branch>`
(`gitops.ensure_worktree`) — no additional mechanism needed.

## Rationale

- **Why not rewrite the YAML?** `yaml.dump` destroys comments; the
  spec files are heavily commented. A separate state file keeps the
  spec as the single source of truth for the default and makes the override
  visible, editable and local per deployment.
- **Why apply at load time, not in the worktree?** All consumers read the
  branch from the spec; a single application point in `load_projects()`
  guarantees consistency (no second store that the engine/scheduler could
  miss).
- **Validation:** the POST route rejects non-existent branches (400) —
  a typo does not break only the next run.
- **Reset:** delete the entry (or the file) → the project falls back to the
  spec default.
- **No security impact:** the endpoints sit behind the existing
  `FABRIK_TOKEN` gate like all other POST routes; the branch switch itself
  is read-only towards the remote (only `fetch` + detached checkout).

## Amendment 2026-11-04

The branch selector in the detail view is removed (no practical use; a silent
per-deployment override). The branch continues to be shown read-only in the project list.
Endpoints and `state/branch-overrides.json` remain unchanged.
