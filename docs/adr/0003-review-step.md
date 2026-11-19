# ADR 0003: `review` step — cross-model cold critic

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-17); addendum 2026-11-04 (ADR 0013 stage 2):
verdict contract extended with `acceptance`
**Issue:** #12

## Context

The review-on-PR use case was solved prompt-based in P0 (ADR 0001,
`review-pr` loop with qwen3:4b): a loop that is touched manually or by a
scheduler after a PR run. That is cumbersome and keeps the review out of
the run semantics (no verdict in the run record, no gate behavior).

Goal: an engine step that runs **before** the PR step and cleanly resets
the run on `changes_requested`.

## Decision

**New step type `review`:** starts an agent session with a **different
model** than the builder step (fresh context, no bias — using the same
model type to review the code it wrote is self-praise).

- Step definition: `type: review`, `model` (required), `backend`/`endpoint` as
  for agent, optional `target` (default: output of the last agent step) and
  `prompt_file` (replaces the review template).
- **Cross-model check:** the engine tracks the model of the last
  agent step (`ctx["last_agent_model"]`). Review with the same model →
  step fails. (An engine check, not just a documentation promise.)
- **Verdict contract:** the reviewer answers with strict JSON
  (`verdict: approved | changes_requested`, `findings[]`, `summary`).
  Parsing is tolerant (first `{` to last `}`); unreadable output →
  step fails (no silent pass).
- **Run semantics:** verdict + findings land in the run record
  (`step.review`). `changes_requested` → step fail → run fail
  (gate: true, findings in the step summary). So the reviewer can reset the
  run; the builder cannot.
- Reuse: `_step_agent_backend` from the agent step (session lifecycle,
  orphan protection, usage recording, session-log events) — no second
  backend path.

## Consequences

+ Verdict in the run record → queryable (session log), exportable
  (tracing exporter), gate-capable
+ Review in the same run as build (deterministic processing order)
+ Cold critic is enforceable, not just recommended
- The review model must be reachable (second backend/model in the homelab)
- Reviewer output is LLM text: the verdict JSON contract is an
  agreement, not a protocol — hence unparseable output fails instead of
  guessing

## Alternatives (rejected)

- **Review as a second agent step + prompt:** no cross-model check,
  no verdict in the record, no run reset — exactly the P0 detour this
  replaces.
- **Review in the PR step (before the push):** the PR step stays simple; review
  before PR is the clean order and testable without git infrastructure.

## Addendum 2026-11-04 (ADR 0013, stage 2)

The verdict contract is extended with the optional field `acceptance`:
`[{criterion, status: met | unmet | unverifiable, evidence}]` — one entry
per acceptance criterion of the linked issue (set by `pr_review`,
which passes the ACs to the reviewer as untrusted data). Missing field →
empty list. An invalid structure (not a list, entry not a dict, empty
criterion, unknown status) is **rejected** (no verdict → step
fails), not silently ignored. `acceptance` lands in the run record
(`step.review`) — including `unverifiable`, which does not block.
