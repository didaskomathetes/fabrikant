# ADR 0023: `on_pr` reconcile per head SHA (`reconcile: head`)

- Status: accepted
- Date: 2026-10-20

## Context

Since #137, `review-pr` uses the `on_pr` trigger in event mode. Two properties
lose reviews in the process:

1. `_safe_run` silently discards a trigger run if the lock is held
   (`try_acquire_for_new_run`, non-blocking). `review-pr` is an agent loop and
   shares the project lock with `develop` (up to 1 h) and `pr-reply`.
2. Event mode remembers `updated_at` as "seen" before the run starts — a
   discarded trigger is never made up for. The first poll after a restart does
   not fire at all.

`reconcile: true` (ADR 0011) is restart-safe, but deduplicates per PR
(`on_pr:#N`): every PR would be reviewed exactly once, new pushes never.

## Decision

`on_pr` accepts `reconcile: head`. Reconcile as in ADR 0011, but the trigger key
contains the head SHA: `on_pr:#N@<sha>`. A poll fires for every matching PR for
whose current head no run record exists yet.

- New push → new SHA → exactly one new run.
- Comment/label without push → same SHA → no run.
- Discarded trigger (no record) → the next poll fires again.
- If `head.sha` is missing in the API response (theoretically; the pulls API
  always provides it) → key `on_pr:#N`; the PR then counts as done as soon as any
  run exists for it (`on_pr:#N` or `on_pr:#N@…`) — no double run for an already
  reviewed head.
- New variable `{pr_head_sha}`.
- `reconcile: true`/`false` stay unchanged; other values are a spec error.

## Consequences

+ Reviews are no longer lost under lock contention and restarts.
+ Opt-in, `approval` (ADR 0011) unchanged.
- After introduction: a one-time review of all open PRs whose head has no `@sha`
  record yet (old records are named `cron`/`on_pr:#N`).
- Every poll scans the loop's run records (as in ADR 0011/0014). One record per
  push instead of per PR makes the records grow faster; retention for run records
  is still missing (follow-up).
- Discarding while the lock is held remains; only the catching up is new. Real
  parallelism: worktree per run (#181/#238).
