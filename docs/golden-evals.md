# Golden Runs & Eval

Golden runs pin the **stable outcome** of a loop as a reviewable, committed
baseline and, after a pipeline/spec change, check whether the loop regressed.
ADR 0065 (harness) + ADR 0068 (run-store mark + `eval --golden`).

## Concept

- **Golden** = `projects/<project>.golden.json`, kept *next to the spec* and a
  source-control artifact: reviewable, diffable, versioned like code. An old
  baseline is a visible change, not silent drift.
- Only the **stable, decision-relevant surface** is captured: run `status` +
  per step `status`/`verdict`. Never volatile LLM free text (`summary`),
  durations or token counts — they would make the gate flaky.
- **Eval** = a real re-execution of the loop with the pinned inputs of the
  baseline run (via replay, ADR 0061). `diffs == []` → no regression. A changed
  `spec_hash` is *expected* (that is exactly when you eval) and not an error —
  only the outcome comparison decides.

## Workflow

### 1. Capture a baseline

After a known, signed-off run:

```sh
fabrikant golden capture <project> <loop> <run-id> [--name N]
```

Creates `projects/<project>.golden.json` (default name = loop name).
**Best-effort mark:** the run record in the store gets `golden = <name>`
(ADR 0068). Without a run store (e.g. a CI runner without `runs/`) the mark is
skipped — the sidecar file stays the review source. Re-capturing a golden with
a different run moves the mark to the new run.

### 2. Show baselines

```sh
fabrikant golden show <project> [--name N]
```

Lists the goldens of a project (name, loop, baseline run, `spec_hash`,
expectation).

### 3. Run the eval

Two equivalent forms:

```sh
# explicit
fabrikant eval <project> <loop> [--name N] [--yes]

# via the baseline run (ADR 0068) — project/loop/name resolved from the store
fabrikant eval --golden <run-id> [--yes]
```

`--golden` cannot be combined with `project`, `loop` or `--name`, and it is
refused if the run is no longer the golden's current baseline (the golden was
re-captured since).

The eval re-executes the loop (all steps, side effects included — like replay)
and prints a report: changed `spec_hash`, status/verdict deviations per step,
`diffs`.

**Exit codes (CI contract):**

| Code | Meaning |
| ---- | ------- |
| `0`  | no regression (gate green) |
| `1`  | regression — the stable surface drifts from the baseline |
| `2`  | infrastructure/usage error or inconclusive (e.g. waiting for approval) |

Without `--yes` the eval asks before re-executing (required without a TTY,
since it is a real re-execution with side effects).

## CI gate (opt-in)

The eval does **not** run on every push/PR. It is an on-demand, blocking
statement: "this pipeline change did not regress my loops". The workflow file is
staged at [`ci/golden-eval.yml`](ci/golden-eval.yml) and is only moved to
`.github/workflows/golden-eval.yml` once the contributing App gets the
`workflows` write permission (ADR 0065, gap 3).

## Re-baseline

A regression (exit `1`) forces a deliberate decision: either the change is
correct → capture again (`fabrikant golden capture …`), or the change is a bug →
revert it. A golden is never "silently outdated" — drift is a visible, blocking
report.
