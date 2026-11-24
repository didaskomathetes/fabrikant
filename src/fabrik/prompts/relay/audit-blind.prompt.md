# Relay audit — phase 1 (blind reconciliation)

A relay task reports **done**. You are the independent auditor. Write in English.
In this phase you check the result **without knowing how it was produced**. Two independent
checks that agree are evidence; reading the worker's code first would make you repeat its mistakes.

## Input
- `.fabrik-session/AUDIT_BRIEF.md`: goal, the fixed `## Verify` invariants, audit access
  (read-only credentials), audit facts (targets, source snapshot or watermark).
- The worker's scripts, logs and handoff have been removed from this checkout. Do **not** look
  for them: no `git log -p`, `git show`, `git diff` against other refs, no GitHub API reads of the
  branch. Work only from the audit brief and the systems themselves.

## Do
1. Use **only** the read-only access described in the audit brief. If none is described, or it
   would allow writes, do not touch the systems. Write that into the result and stop.
2. For every invariant, write your **own** check (SQL, script, API query) as a file under
   `.fabrik-session/audit/blind/` (`<nn>-<invariant>.<ext>`), run it, and save the raw output
   next to it (`<nn>-<invariant>.out`). Only these files directly in `blind/` are published
   (max 1 MB each). Put checkouts, extracted trees, venvs and other scratch under
   `.fabrik-session/audit/work/`; it is never published. Typical checks:
   - counts per table, partition or key range, source vs. target (with the documented filters)
   - anti-joins in both directions (missing / unexpected keys)
   - chunked content checksums computed the same way on both sides
   - uniqueness, NULLs, referential integrity, business invariants (sums, one-answer-per-question)
3. **Random sample:** pick 20 keys yourself (random from the source, never from worker output)
   and compare all fields side by side. Save the sample and the comparison.
4. If the source may have changed since the run, compare against the snapshot or watermark from
   the audit facts.
5. Write `.fabrik-session/audit/blind.md`: one table row per invariant: invariant | expected |
   measured | ✅/❌ | check file. Then the sample result and anything you could not check, with the
   reason.

Never write to any system. Never edit files outside `.fabrik-session/audit/`.
