# ADR 0015: Queue approvals while the project lock is held

**Status:** Accepted (2026-10-19, merge #145; proposed 2026-10-19; changed by ADR 0028)
**Context:** Runs of a project share a worktree and are serialized by a project
lock (`runs.project_lock`). A `needs_approval` run does not hold the lock during
the pause. If it was approved while another run of the same project was running,
`_chat_resume` rejected it with "already running". The approval was lost, and the
human had to wait and then click again. With many trigger runs (on_issue/on_pr
reconcile, ADR 0014) the project is almost always busy, so approving was
effectively blocked.

## Decision

- Approving while the lock is held is **accepted and queued**: the run record
  gets `approval_queued: <ISO time>` and stays `needs_approval`. A resume worker
  thread waits **blocking** for the project lock and then continues. `resumed` in
  the API response is `true`.
- After acquiring the lock the worker checks the status again: a **reject while
  waiting wins** (no resume).
- **Idempotent:** at most one worker per run per process (`_resume_workers`). A
  repeated approve reports "already approved".
- **Restart-safe:** `serve()` starts a worker again for all `needs_approval` runs
  with `approval_queued` (`requeue_approved_runs`).
- Serialization itself remains (shared worktree, see framework-v1-draft).

## Consequences

- Approvals are no longer lost. The UI shows the waiting state.
- A waiting worker blocks a thread until the running run ends. If that run hangs,
  the approval waits with it (the step timeout limits this).
- The CLI path (`fabrikant approve`, separate process) stays synchronous. Its lock
  is process-local and not affected by this problem.
