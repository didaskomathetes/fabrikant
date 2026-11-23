# ADR 0009: `run_id` before worker start + `resumed` flag in the API

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Draft (2026-10-19) — requires approval (PR #76)

## Context

The web UI (`web/app.js`) started a run via `POST /api/runs` and then polled
the run history (`GET /api/runs?project=…`) to find the new run. That was a
**start race**: the worker thread writes the run record
asynchronously; shortly after the POST the new run is not yet visible in the
history → the UI polled against the empty/old state.

An analogous problem at the approval gate: `POST /api/approve`/`/api/reject` only
returned a reply string. The UI did not know whether a resume worker had actually
been started (e.g. it fails if the project lock is held) → it
could not decide whether to keep following the run.

## Decision

Two **additive** API extensions (no breaking change):

1. **`run_id` before worker start:** `POST /api/runs` generates the `run_id`
   (`config.now_id()`) **before** the worker thread starts and returns it in the
   response. The UI then polls specifically via `GET /api/run/<id>`
   (`pollRunById`) — no searching in the history, no start race.
2. **`resumed` flag:** `POST /api/approve`/`/api/reject` additionally return
   `resumed` (bool). `true` only if a resume worker was actually started for
   the run; `false` if the run is already running, finished
   or the project lock is held. The UI follows the
   run only if `resumed` is true.
3. **Internal contract:** `_chat_resume()` (chat.py) returns the tuple
   `(reply, resumed)`; the server passes `resumed` on to the UI.

## Rationale

- **Why `run_id` before the start?** The ID can be generated deterministically
  (`config.now_id()`) without executing the run. Returning it as a handle
  makes the run addressable immediately — the race disappears structurally
  instead of being worked around with a polling heuristic.
- **Why `resumed` as a bool and not just reply text?** A machine-readable
  flag can be evaluated deterministically; a reply string would be parsing heuristics
  in the UI.
- **Why additive instead of breaking?** Existing clients that only
  read `reply` remain compatible (additive JSON field). Covered by
  `test_post_approve_legacy_client`.

## Consequences

- The UI (`web/app.js`) uses `pollRunById` and `r.resumed` — defensively
  (`if (r && r.resumed)`) so that older clients without the field do not break.
- `run_id` uniqueness depends on `config.now_id()` (timestamp-based);
  collisions under high parallelism are unlikely but not
  ruled out — stronger ID generation if needed (follow-up).
- The README API table + the `chat.py` module docstring document the new
  contract for integrators.
