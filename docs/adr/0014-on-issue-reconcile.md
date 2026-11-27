# ADR 0014: Reconcile mode for the `on_issue` trigger

**Status:** Accepted (2026-10-19); amendment 2026-10-28 (ADR 0036 implemented):
auto-retry is possible through the retry policy (ADR 0036, decision 7) — the
"No auto-retry" section still applies unchanged to `reconcile` itself
**Deciders:** Fabrikant (dogfooding, issue #131)
**Context:** The `on_issue` trigger remembered seen issues only in memory. The
first poll after every server start marked all open issues as seen without
firing. Every issue that already existed at (re)start, or that was created during
downtime, was never processed — among them the refactor issues #63–#65 from
`refactor-sweep` and the roadmap backlog #16–#34. In addition, the poll only saw
the 50 newest issues.

## Decision

Like `on_pr` (ADR 0011), `on_issue` gets the opt-in option `reconcile: true`
(`src/fabrikant/scheduler.py`):

- **Persistent source instead of an in-memory set:** the trigger fires for every
  matching open issue for which the loop has **never** had a run — checked via
  the run records (`runs/<project>/*.json`, `loop` +
  `trigger == "on_issue:#N"`). This survives restarts and works through the
  existing stock.
- **No auto-retry:** "already had a run" counts regardless of status (including
  `error`/`fail`) — `reconcile` never starts a second run for a trigger, so that
  permanently broken issues do not spin in an endless loop. Automatic retry
  exists exclusively as a bounded, class-based retry policy (ADR 0036,
  decision 7); "retry stays manual" no longer applies because of it.
- **Throttling:** at most `max_per_poll` new runs per poll and loop (default
  **2**, values < 1 are raised to 1), **oldest issues first**
  (`sort=created&direction=asc`). Without throttling, `refine` would start ~25
  runs, each with an approval gate, at once when activated.
- **Pagination:** in reconcile mode up to 10 pages of 50 issues are read (lazily
  — only as many as needed until `max_per_poll` candidates are found), so that
  processed old issues do not permanently hide newer ones.
- **Shared run lookup:** `loop_run_triggers(project, loop, statuses)` is the one
  helper for looking up runs by trigger; `_has_active_pr_run` (on_pr) uses it
  with `ACTIVE_RUN_STATUSES`.
- Without `reconcile` the event behaviour stays unchanged.

Enabled in `projects/fabrikant.fabrikant.yaml` for `refine` and `develop`.

## Consequences

- A run that is discarded by the project lock (`_safe_run`, non-blocking) leaves
  no record — the next poll fires again. Reconcile thereby makes lost trigger
  events self-healing.
- If a run record is deleted, the issue fires again (deliberate: this way a retry
  can be forced even without the UI).
- Every poll reads the project's run records once (O(runs)); uncritical at a
  300 s poll interval. If `runs/` grows large, an index is the next step.
- An issue that gets the label *afterwards* fires in reconcile mode (as in event
  mode).
