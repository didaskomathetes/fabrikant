# ADR 0061: Replay — Deterministic Re-Execution of a Finished Run

**Status:** Accepted (2026-11-21); proposed 2026-11-20, accepted on merge to main
**Betrifft:** ADR 0036 (run retry, retry chains), ADR 0034 (run store), ADR 0002 (session log)

## Context

"Definitive local runtime" (framework-v1-draft.md §5) requires that every run can be
replayed: re-execute a finished run with pinned inputs and compare the result. This is the
basis for the eval harness (#130) and the replayability KPI.

Today a run can only be *resumed* (after approval) or *retried* (on transient failure,
ADR 0036). Neither re-executes a finished run from scratch with the original inputs:

- **Resume** continues the same run record from the first non-passing step.
- **Retry** starts a new run of the same trigger chain, but with *fresh* trigger variables
  (re-polled issue/PR state), not the original ones.

What is missing: take a finished run, pin its inputs (spec, variables, trigger, note),
execute a new run, and diff the two.

## Decision

### What "deterministic" means in fabrik

A replay pins the **inputs** of a run, not the outputs of non-deterministic components:

| Pinned (identical between original and replay) | Not pinned (may differ) |
|---|---|
| Spec (checked via `spec_hash`; mismatch is reported, not blocking) | LLM/agent outputs (model sampling, no model pinning exists) |
| Run variables (`record["vars"]` of the original) | External state (repo HEAD, issues, webhooks) |
| Loop, trigger, note | Timestamps, run id, workdir |
| Step order (from the spec) | Tool side effects |

The replay is therefore **input-deterministic**: same spec + same vars + same loop ⇒ the
same steps execute in the same order with the same inputs. Where outputs diverge (LLM
steps, external state) is exactly what the diff report surfaces — that is the point of the
feature, not a defect.

### `fabrik replay <run-id>`

- Loads the original run record; it must be in a **terminal** status
  (`pass`/`fail`/`error`/`rejected`). Replaying a `queued`, `running` or
  `needs_approval` run is refused.
- Starts a **new run** via the normal `run_loop` path (claims, worktree, session log,
  store — all unchanged) with:
  - `initial_vars` = the original record's `vars` (freshly derived base keys — `run_id`,
    `workdir`, `branch`, … — win over replayed values, same precedence as resume),
  - `trigger="replay"`, `note` = `replay of <run-id>`,
  - `replay_of=<run-id>` — a new record field linking the replay to its original
    (analogous to `retry_of`, ADR 0036). Replaying a replay is allowed; the link points
    to the direct parent.
- **Spec-hash check:** the current spec's hash is compared with the original record's
  `spec_hash`. A mismatch is **reported** (in the CLI output and the diff report) but
  does not block the replay — a changed spec is a legitimate thing to measure.

### Diff report

`fabrik replay` prints a diff of original vs. replay after the run finishes:

- Overall status (and `failure_kind` on failure)
- Per step (matched by index, since step order is pinned by the spec): name, type, status,
  summary, verdict
- `spec_hash` match/mismatch

The report is plain text on stdout; the structured data is already in both run records
(`fabrik runs export`), so tooling can diff records directly.

### Record field `replay_of`

Stored like `retry_of` (runmodel, JSON + SQL store, schema). Empty string = not a replay.
The field is informational: it does not affect claims, retry chains, or scheduling.

## Consequences

- New module `fabrik/replay.py` (`replay_run`, `diff_runs`); new CLI subcommand `replay`.
- `run_loop` gains a `replay_of` parameter (default `""` — no behavior change for all
  existing callers).
- The eval harness (#130) builds on `replay_run` + `diff_runs` as its execution primitive.
- No change to the spec format, the engine's step execution, or the store's write path
  (one new column, default `''`).

## Alternatives considered

- **Re-executing the same record (overwriting it):** destroys the original — the diff
  needs both. Rejected.
- **A `resume`-like continuation of a finished run:** a finished run has no non-passing
  step to continue from; it would be a full re-execution with resume's bookkeeping
  (`resumed: true`, attempt inheritance) — the wrong semantics. Rejected.
- **Pinning model versions/seeds:** fabrik has no model registry or seed control for
  agent/LLM backends; pinning would require backend cooperation that does not exist.
  Documented as out of scope; the diff report makes divergence visible instead.
