# ADR 0019: Pause/resume/disable per project and loop

**Status:** Accepted (2026-10-20, merge #207; proposed 2026-10-20); amendment 2026-10-25 (PR #326): queued runs that have not started yet are checked again at execution time; changed 2026-10-23: worktree lease instead of single-process guarantee, #237/PR #328

## Context

During failure cascades (e.g. `review-pr` failing every 5 minutes) or
maintenance there was no switch other than editing the spec or stopping the
server.

## Decision

- State `active` | `paused` | `disabled`, optionally `until` (auto-resume), per
  project and per loop.
- `paused`: scheduler triggers (cron/on_file/on_issue/on_pr) neither fire nor
  poll; manual runs are allowed. `disabled`: additionally no manual runs
  (UI/CLI/chat/API). Running runs are not affected.
- The project state beats the loop state (project != `active` → applies to all
  loops).
- Persisted in `state/run-states.json` (written atomically), **not** in the spec
  → survives restarts and spec changes. Only `paused`/`disabled` are stored;
  `active` = no entry.
- `until` is checked on every scheduler tick (`resume_expired`); expired entries
  count as `active` immediately.
- Audit log `state/run-state-changes.jsonl` (JSONL: ts, actor, project, loop,
  from, to, until) + stdout.
- Interfaces: `POST /api/projects/<p>/state` (`{state, loop?, until?}`), CLI
  `fabrikant pause|resume|disable`, chat, UI buttons.

**Amendment (PR #326):** Pause/disable is checked not only when enqueuing
(scheduler or UI/CLI/chat/API), but again at execution time: trigger runs waiting
in the worker pool queue are discarded if the loop has become
`paused`/`disabled` in the meantime; waiting manual runs (including resume) are
discarded if the loop has become `disabled` (queued record → `error`, a resume
stays `needs_approval` and can be approved again after the loop is resumed).
Running runs are still not affected.

## Worktree lease and single-process guarantee

Every run worktree has a lease file `<WORKTREES_DIR>/<project>/<run_id>.lease`
(JSON: `pid`, `heartbeat`, `token`; outside the Git tree). It is written before
the worktree is created, renewed by a heartbeat thread every
`config.WORKTREE_LEASE_HEARTBEAT_S` (30 s), and deleted on run end, pause and
worktree removal. `cleanup_orphaned_worktrees` skips a worktree whose lease
belongs to **another** process whose PID is alive and whose heartbeat is younger
than `config.WORKTREE_LEASE_TIMEOUT_S` (300 s). A missing/corrupt lease, the own
PID, a dead PID or an expired heartbeat do not protect; `needs_approval` still
protects.

A second process (server, `fabrikant run`/`approve`) therefore no longer deletes
worktrees of running runs. **Still open** and not supported: slots
(`max_parallel`), dedup and priority waiters (`runs.claim_run`) are
process-local; parallel processes on the same project can exceed the slot limit
and start the same trigger twice. Cross-process slots/dedup are a follow-up topic
(#280). Limitation: the PID check only applies on the same host. Details of the
lease implementation are in ADR 0028.

## Specs without `repo:`

Specs without `repo:` work directly in `root` without isolation; parallel runs
would overlap there through the Git index and files. Therefore `max_parallel: 1`
is fixed for these specs; a higher value in the spec is ignored.
`worktree: false` loops are exempt, because they use their own scratch workdir.

**Known gap:** The problem only exists with a shared file system
(single-machine/homelab operation). With container-based runs (one sandbox per
run with its own checkout, cf. #20) it goes away; syncing changes back for
repo-less specs remains open there.

## Consequences / non-goals

- Cron minutes missed during a pause are not caught up; `on_issue` in event mode
  re-initializes on the first poll after resume, `reconcile` triggers work
  through the existing stock.
- Roles (`operator`, #159) do not exist yet: access = the existing
  origin/`FABRIKANT_TOKEN` check (admin fallback). The `actor` in the audit log is
  `api:<client-ip>`, `cli:<user>` or `chat`.
- A global emergency stop and a circuit breaker (auto-pause after N failures) are
  deliberately not part of this change.
