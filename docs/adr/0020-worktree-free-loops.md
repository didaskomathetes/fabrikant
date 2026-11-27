# ADR 0020: Worktree-free loops

**Status:** Accepted (2026-10-20, merge #231; proposed 2026-10-20; changed by ADR 0028)

## Context

Loops of a project normally share the Git worktree. They are therefore serialized
with the project lock, even if a loop only executes spec-validating or other
steps that need no worktree. Such loops should be able to run in parallel without
risking Git changes or a shared working state.

## Decision

- By default a loop uses the project worktree and the project lock.
- With `worktree: false`, every run gets its own empty scratch workdir under
  `state/scratch/<run_id>` and its own stable lock per loop. Such loops run in
  parallel with other loops of the project, but not twice.
- `worktree: false` is only allowed if the loop contains no `git`, `pr`, `agent`,
  `review` or `pr_review` step, neither directly nor in nested `verify.checks`.
- The lock choice is compared with the loaded loop snapshot before the run. If
  the spec changes between choosing the lock and running, a change from a loop
  lock to a worktree run falls back to the project lock.
- The scratch workdir is removed after every run. Cleanup errors are recorded in
  the run log and do not terminate the run.

## Consequences / non-goals

- Worktree-free loops must not do any Git or PR work; their scratch workdir is no
  substitute for a persistent checkout.
- The existing priority handling for approvals and manual runs applies per lock
  key and stays separate between independent loop locks.
- The scratch workdir is emptied at start, so that a reused `run_id` does not
  take over old content.
