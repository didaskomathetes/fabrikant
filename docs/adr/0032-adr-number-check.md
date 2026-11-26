# ADR 0032: ADR numbers — assignment, collision check, renumbering

**Status:** Accepted (2026-10-20, merge #252; proposed 2026-10-20)
**Origin:** Issue #134, PR #204 (there as "ADR 0019", itself a collision with
0019-pause-resume-disable). #204 was wrongly closed as a duplicate of #201;
#201 contained only a README change, script, ADR and merge check were lost.

## Context

Agents choose the ADR number as the "next free one in `docs/adr/`", namely in the state of
their own branch. Parallel branches therefore inevitably collide. This applies to
develop and to stacked ADR PRs from pr-reply (ADR 0011). References like "ADR 0013"
then become ambiguous. Field finding 2026-10-20:

- On `main`, 0013, 0014 and 0018 were each assigned twice, 0023 three times.
- Three open PRs claimed 0024/0025.
- Code still referred to branch numbers: "ADR 0006" for dual identity, merged as 0010.

## Decision

1. **Script `scripts/check-adr-numbers.py`** (pattern: `scripts/pr-status.py`), base
   `origin/main`:
   - `check`: ADRs **new** in the branch with a number already assigned on the base
     → exit 1. Only against the base, so that it is deterministic.
   - `next`: highest number on the base **and in all open PRs** + 1. The PR of the
     own branch does not count. Without `gh` only the base applies, with a warning.
   - `fix`: renames only colliding **new** ADRs, also skipping past numbers of open
     PRs. Renamed are file name, title line and file path references;
     `ADR NNNN` only in lines added by the branch. ADRs of the base and
     runtime data (`runs/`, `state/`) are never touched.
2. **Merge gate:** `pr-status.py merge-check` returns `wait` as long as the PR creates an ADR with
   a number assigned on the base. This also catches collisions between two
   PRs, because after the merge of the first its number is on the base. So a
   duplicate number never lands on `main`.
3. **develop loop:** shell step `check-adr` (`fix` → `check`) after the agent, before
   lint/test/ship. Deterministic, not dependent on the prompt; the repair is
   tested and committed along with the rest.
4. **Prompts:** develop and pr-reply get the number via `next`. pr-reply has no
   shell steps and, after creating or after a rebase, runs `fix` and then
   `check` itself.
5. **CI invariants** (`tests/test_adr_index.py`):
   - Numbers are unique.
   - The title line matches the file name.
   - The index is complete and sorted.
   - Every `ADR NNNN` reference points to an existing ADR.
6. **Renumbering of legacy cases:** The ADR merged first keeps its number; the
   others move to the end in merge order. References are changed according to
   context, not by search/replace.
7. **Gaps stay gaps:** A number once assigned on `main` is never
   reassigned (0007: approval-request-protocol, 2026-10-18 → 0011). Otherwise old
   references in commits, issues and run logs would become ambiguous.

## Consequences

- Of two parallel PRs, the second is renumbered after the merge of the first. The
  trigger is given by `merge-check`, the repair is done by `fix`.
- `ADR NNNN` references in lines added by the branch count as references to the
  renamed ADR. In case of a collision, references to the base ADR of the same number cannot
  be distinguished; this stays rare and must be checked in review.
- The reference check only finds non-existent numbers. A reference to the
  *wrong* but existing ADR (the 0006/0010 case) remains a matter for review.
- If two *new* ADRs have the same number, `fix` moves the alphabetically later one.
  On a renumbering branch the moved ADRs also count as new; do not
  run `fix` blindly there.
- Numbers reflect the merge order only approximately; the ADR dates in the header
  are authoritative.
