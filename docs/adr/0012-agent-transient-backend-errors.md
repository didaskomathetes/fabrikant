# ADR 0012: Agent step resilient to transient backend errors

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-18)
**Decided by:** Fabrik (dogfooding, issue `pr-reply FAILED 20261018T141501Z`)
**Context:** The `pr-reply` loop (cron `*/15`, agent step via `opencode serve`)
ran into a 6h hang on 2026-10-18 and was only marked by the dead-man's switch as
`error` (`stale: >6h in 'running'`). The agent step has a step timeout
(1800s) that catches a hanging *session* — but **not** a
backend/transport error: if `be.poll()`/`be.transcript()` threw an exception
(e.g. `opencode serve` down/restarting mid-run), it propagated out of the
polling loop and killed the entire run. For a cron agent loop that fires every
15 min, a transient backend outage is not a run failure — the next
run 15 min later would start fresh anyway.

## Decision

The agent step (`src/fabrik/steps/agent.py`, `_step_agent_backend`) treats
**transport errors during polling** as transient:

- `be.poll()`/`be.transcript()` throw → the step does **not** kill the run,
  but logs the error, waits with backoff (`5s, 10s, …`, cap 30s) and
  keeps polling — **within the step timeout**.
- If the backend recovers before the timeout → the step runs to completion
  normally (`done`).
- If the timeout is reached while the backend keeps failing → the
  step reports `agent timeout after <X>s … · backend error: <cause>` (the
  cause is thus in the run log/record instead of just "timeout after 1800s").
- The session is **not** cancelled prematurely (cancel stays with the timeout /
  with `failed`).

**Unchanged:** status-based errors (`poll()` → `failed` + `retryable`)
restart the session (`step.retries`, default 2 → max. 3 attempts). That is
a different path (session restart) and stays as it is.

## Rationale
- **Cron loops are idempotent:** `pr-reply`/`review-pr` run periodically and
  are state-based deduplicated (`state/replied-comments.json`,
  `state/reviewed-prs.json`). A transient backend outage costs no
  progress — the next run catches up. Killing the run is pure
  friction (alert + issue + manual follow-up).
- **Symmetry with the existing retry:** the step already retries transient
  *status* errors. Transport errors are the same error class (backend
  briefly unreachable) — only the exception variant is missing.
- **Bounded:** the behavior is limited by the step timeout (no
  endless retry loop); in the worst case the step ends with the same timeout as
  before, just with better diagnostics.

## Rejected alternatives
- **Forward the exception immediately as a step failure (status quo):** kills cron runs
  on every backend blip → alert flood, exactly the observed problem.
- **Restart the session on a transport error (like `failed`+`retryable`):**
  would resend the prompt and execute already completed work (commits, replies)
  twice. For a short outage, continuing to poll (the session stays
  server-side) is the safer path.
- **Lower the dead-man's-switch threshold:** only catches *process* death, not
  backend blips; and a globally shorter window would wrongly mark long `develop` runs
  (3600s) as stale.

## Consequences
- Agent steps (all backends) are robust against transient
  backend/transport errors within the step timeout.
- Regression tests in `tests/test_steps_agent.py`
  (`test_agent_transient_transport_error_then_success`,
  `test_agent_persistent_transport_error_times_out`,
  `test_agent_transport_error_does_not_cancel_prematurely`).
- The dead-man's switch remains the backstop for *process* death (unchanged).
