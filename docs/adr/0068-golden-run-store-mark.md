# ADR 0068: Golden Runs — Run-Store Mark + `eval --golden`

**Status:** Accepted (2026-11-24)
**Relates to:** ADR 0065 (Golden Runs, `fabrikant eval`), ADR 0034 (run store, JSON + SQL), `docs/roadmap.md` First win 2 (#130)

## Context

ADR 0065 gives us the golden baseline and the eval: `fabrikant eval <project>
<loop> [--name N]`. The three parameters have to be known by heart. In practice
you usually only know *which run* was the known-good one — "the run we signed
off last week" — and want to run the eval against it, not reconstruct
project/loop/name.

The gap (#130, gaps 1+2): the run store (ADR 0034) does not know whether a run
is marked as a golden baseline. The mark lives only in the sidecar file
`projects/<project>.golden.json` (`baseline_run`), which sits next to the spec
and has nothing to do with the run record itself.

## Decision

**Gap 1 — mark in the run store.** The run record gets a field `golden` (text,
nullable): the *name* of the golden baseline this run was captured as.

- `Run.golden: str = ""` in the model (`runmodel.py`), in `parse_run` (JSON) and
  in `_RUN_FIELDS` + the declarative `runs` table (`runschema.py`) + Alembic
  migration `0007` (SQL).
- `save_golden(project, name, golden)` marks the `baseline_run` **best-effort**:
  `mark_baseline(project, run_id, name)` sets `golden = name` on the run record.
  A missing run (e.g. CI with an empty `runs/`) is not an error — the mark is
  skipped (logged at debug level; other store errors are logged as warnings);
  marking runs inside the sidecar lock so concurrent captures stay ordered. The sidecar file stays the committed
  review source.
- **Single-name semantics.** A run carries at most one golden name, and at most
  one run carries a given name. `fabrikant golden capture` refuses to capture a
  run that is already the baseline of a *different* golden (it would silently
  replace that mark) unless `--force` is given.
- **One baseline run per golden name.** When a golden is re-captured with a
  different run, the previous baseline run loses its mark
  (`unmark_baseline`; only a mark equal to the name is cleared, so a run that is
  the baseline of another golden keeps that one).
- The sidecar file `projects/<project>.golden.json` remains the *source of
  truth and review source*. `golden` in the store is only a *relation*
  (run → baseline name), not a second copy of the baseline.

**Gap 2 — `eval --golden <run-id>`.** The eval can now be started from the run
whose mark resolves project/loop/name:

- `golden_for_run(run_id)` → `(project, name)` or `None` (unknown or not marked).
- `fabrikant eval --golden <run-id> [--yes]` resolves via the mark and then runs
  exactly like `fabrikant eval <project> <loop> [--name N]`. The classic
  positional arguments become optional (`nargs="?"`); without `--golden` and
  without `project loop` it is a usage error, and combining `--golden` with
  `project`, `loop` or `--name` is a usage error too (no silently ignored
  arguments).
- A stale mark is refused: if the golden's current `baseline_run` is not the
  given run (the golden was re-captured since), the eval stops instead of
  comparing against a different baseline than the one named.
- Exit contract unchanged: **0 = no regression**, 1 = regression, 2 =
  error/inconclusive.

**Gap 3 — CI workflow.** Stays as in ADR 0065: the workflow file is staged
under `docs/ci/golden-eval.yml` and is only moved to `.github/workflows/` once
the contributing App gets the `workflows` write permission. Not doable from
the code side — not pursued here.

**Gap 4 — user docs.** `docs/golden-evals.md` documents the whole workflow
(capture → mark → eval, both CLI forms, exit codes, CI opt-in).

## Consequences

- A known run can be evaluated directly (`fabrikant eval --golden <run-id>`) —
  project/loop/name are resolved from the store, not from memory.
- The mark is *best-effort* and *derived*: it is missing in environments
  without a run store (plain CI runners without `runs/`) and breaks nothing.
  The committed sidecar file remains the single source of truth.
- `golden` in the store is a text (the name), not a boolean; the name addresses
  the concrete baseline in the sidecar file. At most one run carries a given
  name; older marks (written before this rule) are caught by the stale-mark
  check of `eval --golden`.
- Migration `0007` is additive (`ADD COLUMN golden TEXT`, nullable) — existing
  DBs are upgraded without data loss; the `compare_metadata` check (schema ↔
  `runschema`) stays green because the declarative table object and the
  migration grow together.
