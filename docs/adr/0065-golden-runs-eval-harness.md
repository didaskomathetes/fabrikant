# ADR 0065: Golden Runs — Pinned Baseline + Eval Harness

**Status:** Accepted (2026-11-21); proposed on the `feat/golden-runs` branch
**Betrifft:** ADR 0061 (replay, `replay_of`), ADR 0034 (run store), `docs/roadmap.md` First win 2 (#130)

## Context

"Definitive local runtime" (framework-v1-draft.md §5) requires that pipeline
changes are *provably* non-regressing, not just "feels the same". ADR 0061 gives
us deterministic re-execution of a finished run (replay). What replay alone
cannot answer is: *is this re-execution the same as the last time we trusted
this loop?* That comparison needs a pinned, trusted baseline to compare against.

The gap (#130): mark a known-good run's outcome as a baseline, then — after a
pipeline/spec change — re-run the loop and report whether the stable outcome
drifted. The end goal is a CI gate that fails when a loop regresses.

## Decision

**Golden** pins the *stable outcome* of a loop as a reviewable, committed
baseline artifact next to its spec: `projects/<projekt>.golden.json`. A golden
records, per named baseline:

- `loop` — the loop the baseline belongs to
- `baseline_run` — the run id it was captured from
- `spec_hash` — the spec hash at capture time (informational)
- `expect` — the stable outcome: run `status` + per-step `status`/`verdict`

**Only stable, decision-relevant fields are captured** — never volatile
LLM free-text (`summary`), durations, or token counts, which vary between runs
and would make the gate flaky.

**Eval** (`run_eval`) loads a golden, re-executes the loop with the baseline
run's pinned inputs (via `replay.replay_run`, trigger `eval`, linked through
`replay_of`), and compares the stable outcome to the golden. `diffs == []` →
no regression. A changed `spec_hash` between baseline and eval is *expected*
(that is exactly when we run an eval) and is therefore not an error — only the
stable-outcome comparison decides regression. Any deviation on the stable
surface (new/missing step, flipped review verdict, status flip) forces a
deliberate re-baseline (re-capture).

CLI:
- `fabrik golden capture <project> <loop> <run-id> [--name N]` — pin a baseline
- `fabrik golden show <project> [--name N]` — list goldens
- `fabrik eval <project> <loop> [--name N] [--yes]` — run an eval; **Exit 0 =
  no regression** (the CI-gate contract)

CI: the eval is **opt-in** via a dedicated `workflow_dispatch` workflow — it
does not run on every push/PR. The job fails on regression, so "this pipeline
change did not regress my loops" becomes a provable, blocking statement on
demand. The workflow file is **staged at `docs/ci/golden-eval.yml`** (its
content is final); its home is `.github/workflows/golden-eval.yml`, which the
contributing GitHub App cannot push to without the `workflows` write
permission. Once that permission is granted, the file moves to its final path
unchanged.

## Consequences

- A golden is a **source-controlled artifact** (committed beside the spec), so
  baselines are reviewable, diffable, and versioned like code. A stale baseline
  is a visible change, not a silent drift.
- The eval is a *real* re-execution (all steps, side effects included), exactly
  like replay (ADR 0061) — so an eval has the same cost and side-effect profile
  as a replay. It is therefore gated behind `--yes` / opt-in, never run
  implicitly.
- Capturing is cheap (read one finished run); only the eval re-executes. This
  keeps the common path (pin a baseline) fast.
- The stable-outcome contract is deliberately narrow. If a loop's meaningful
  signal lives in a volatile field (e.g. free-text summary), a golden cannot
  gate on it today — that would need a per-loop outcome projection, which is out
  of scope here.
