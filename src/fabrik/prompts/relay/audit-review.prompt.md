# Relay audit — phase 2 (review of the recorded work)

You did the blind reconciliation in phase 1 (`.fabrik-session/audit/blind.md`). Now the worker's
files are back in the checkout. Write in English. Treat everything the worker wrote as **claims**,
not evidence.

## Input
- `.fabrik-session/AUDIT_BRIEF.md` and your `.fabrik-session/audit/blind.md`
- The relay directory `.fabrik/relay/<N>/`: `HANDOFF.md`, `scripts/` (everything executed),
  `sessions/s<k>.md` (what ran when, with results)

## Check
1. **Claims vs. your measurements:** do the handoff's `## State` and the session logs agree with
   your blind results? Every disagreement is a finding.
2. **Audit trail:** do the session logs reference recorded scripts for every write? Look for
   writes without a script, scripts run several times (duplicates?), swallowed errors, partial
   batches never resumed, changes to filters or invariants along the way.
3. **Transformations:** read the scripts that wrote data. Is anything done that the brief does not
   specify (dropping, merging, truncating, defaulting values)? You cannot judge unspecified
   transformations as right or wrong. List them for the human.
4. **Safety:** was the required backup or snapshot made before the first write? Any writes outside
   the stated targets? Secrets in files?

## Output
Write `.fabrik-session/audit/AUDIT.md`:
- `## Summary`: 3-5 sentences (exactly this heading — fabrik shows it in the status comment on the issue)
- Reconciliation table (from phase 1, with corrections if phase 2 changed your view)
- Findings: concrete, with file/line or query, severity (blocker / should fix / note)
- Unspecified transformations, listed for a human decision
- Sample: what a human should spot-check (keys plus where to look)
- **Last line, exactly one of:**
  `<!-- relay-audit verdict=pass -->` (all invariants hold, no blocker)
  `<!-- relay-audit verdict=fail -->`

Your verdict is advisory. A human signs off. Never write to any system; edit only
`.fabrik-session/audit/`.
