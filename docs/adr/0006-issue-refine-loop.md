# ADR 0006: Issue refine loop (replaces triage)

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-18); §"Human gate" **replaced by ADR 0017** (2026-10-19);
addendum 2026-11-04 (ADR 0013 stage 1): AC format with verification hint; personal name replaced by GitHub handle 2026-11-26
**Decided by:** didaskomathetes
**Context:** The `triage` loop (ADR 0001) only classified issues (label +
acceptance criteria for `ready to develop`). Practical feedback: issues need more
preparation before development, and there was no clean branch for
duplicate/already-done/pointless issues. In addition a bug: `{issue_number}`
was not substituted in the git step (branch `fabrikant/auto/{issue_number}`
was created literally) — fixed in the git and pr steps (variable substitution for
`branch` and `head`/`base` respectively).

## Decision

The `triage` loop becomes the **`refine` loop** (`projects/fabrikant.fabrikant.yaml`):

| Step | Type | Effect |
|---|---|---|
| `classify` | llm | Read issue → `label` + `reasoning` + `background` + `acceptance_criteria` + `close_reason` (JSON, untrusted marking) |
| `ensure-labels` | shell | Ensure labels `needs approval` + `ready for development` exist |
| `refine-body` | shell | Append `## Hintergrund (auto)` + `## Acceptance Criteria (auto)` to the issue body (idempotency guard via the `<!-- Refine (auto) -->` marker, mktemp + trap cleanup, 60 s timeout) |
| `apply-label` | shell, ~~**approval: true**~~ (ADR 0017: no gate) | Apply label (human gate); no-op on `close` |
| `close-issue` | shell | On `close`: reason as comment + `gh issue close` (never delete) |
| `report` | report | Summary into the run record |

**Label semantics:**
- `ready for development` — clearly implementable, consistent with docs/ADRs →
  the `develop` loop triggers (label renamed from `ready to develop`).
- `needs approval` — architectural impact/conflict with ADRs → the issue is
  refined anyway; a human decides and relabels manually.
- `close` — duplicate (with reference), already solved or pointless →
  the issue is **closed with a reason comment**, not deleted.

> **Replaced by ADR 0017:** the gate now applies only to `needs approval` (approve →
> `ready for development`) and `close`; `ready for development` runs through
> without pause. The following paragraph is historical.

**Human gate:** `apply-label` has `approval: true` — the human looks at the
refined issue and approves before label/close take effect. Nothing is
implemented automatically.

## Rationale
- **Refine instead of triage:** triage only delivered a label; the actual
  work (writing ACs, establishing context) was missing. Refine makes the issue
  developer-ready — the `develop` agent gets ACs + background in the issue.
- **Close as a third class:** without a close path, duplicates/nonsense ended up as
  `needs approval` and clogged the queue. Close with a comment keeps the
  audit trail (no delete).
- **Background, not only the error:** feature requests have no error —
  the prompt asks for background on the issue (cause/context/motivation).

## Rejected alternatives
- **LLM writes the complete issue body:** risk that the LLM
  loses original content. Instead: additive sections (background + ACs)
  on the original body, idempotent.
- **Close without a human gate:** duplicate detection can be wrong; close is
  (almost) irreversible for the reporter → stays behind the approval gate.
- **Keep the old label `ready to develop`:** rename to
  `ready for development` in the same step, since the loop is redefined anyway
  (one label, one name).

## Consequences
- The `develop` loop triggers on `ready for development` (spec adjusted).
- Existing issues with `ready to develop` must be relabeled manually
  (one-off).
- The git step substitutes `branch`, the pr step `head`/`base` (regression tests in
  `tests/test_steps_git_pr.py`); spec tests in `tests/test_fabrikant_spec.py`.
- ADR 0001 §1 (triage as T1 + step approval) is replaced by this ADR for the
  `refine` loop; the other loops remain unchanged.

## Addendum 2026-11-04 (ADR 0013, stage 1)

`classify` writes **every** acceptance criterion in the format
`- [ ] <criterion> — Prüfung: <test | command | observation>` (prompt +
response schema). The verification hint is a pointer for reviewers/QA, **not an
executable command** — deterministic checks only run as `shell` steps defined
in the spec. Existing issues without a verification hint stay
readable: `pr_review` accepts both formats (ADR 0013, consequences).
