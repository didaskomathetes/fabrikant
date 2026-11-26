# ADR 0013: Issue closing + verification of acceptance criteria

**Status:** Accepted (2026-11-04) — stages 0–2 implemented, stage 3 remains
optional (only if a need is demonstrated)
**Deciders:** Dominic
**Context issue:** proposal "QA agent after merge" (chat 2026-10-19)

## Context

1. **Issues are never closed.** The `develop` PR only refers to the issue as
   "Issue #N", without a GitHub closing keyword. After the merge the issue stays
   open, and nobody closes it.
2. **Acceptance criteria are not checked anywhere.** The `refine` loop
   (ADR 0006) writes testable AC into the issue. The `pr_review` step
   (ADR 0010), however, only sees the diff and not the issue. Nobody checks
   whether the PR meets the AC.
3. **Proposal:** a separate QA agent should verify the issue (above all the AC)
   against the new code state after the merge. The `refine` loop should supply a
   block of QA suggestions for it.

Critical review of the proposal against the guiding principles
(`framework-v1-draft.md`):

- **Principle 3 ("verification before review"):** a check that runs only after
  the merge finds the defect when it is already on `main`. The only way out then
  is reopen or revert. Most AC can be checked **before** the merge.
- **Failure path:** a QA fail that creates a *new* issue starts
  refine → develop → QA → new issue. That is an unbounded feedback loop, like the
  one that already happened with alert issues under the `fabrikant` label.
- **§1.4 / ADR 0008:** a QA block is LLM text derived from an untrusted issue
  body. An agent that *executes* "QA suggestions" is an injection surface.
- **ADR 0011:** event triggers miss state. There is no `on_merge` trigger.
- **Principle 6 (don't overbuild):** a dedicated QA loop plus a dedicated QA
  block is new infrastructure before the simple levers are exhausted.

## Decision

Staged. Each stage is useful on its own; the next one comes only when needed.

### Stage 0 — closing keyword (implemented)
The body of the `develop` PR starts with `Closes #{issue_number}`. GitHub closes
the issue on the (squash) merge to `main`.

**Rule "the PR is enough to close":** deterministic, no LLM judgement.
develop PR ↔ issue is 1:1, so the PR closes the issue. Partial PRs (`Refs #N`)
only come into being when a human creates them deliberately.

**Multi-issue PRs:** the 1:1 assumption holds for the automatic develop pipeline
(one issue → one PR → `Closes #N`). A PR that affects several issues (multiple
`Closes #N`, or created manually) closes all linked issues on merge — this is
intentional, but outside the automatic pipeline. Such cases require an
**explicit human override**: the author checks that every linked issue is really
fully resolved before the PR is merged. The automatic develop pipeline does not
produce such PRs.

### Stage 1 — AC with a verification hint (instead of a separate QA block)
`refine`/`classify` writes **every** AC as
`- [ ] <Kriterium> — Prüfung: <Test | Befehl | Beobachtung>`
(criterion — check: test | command | observation; the German keywords are the
literal format used by the prompt).

- A single source of truth (the AC). A separate QA block would duplicate the AC
  and drift away from them.
- The verification hint is a **hint** for reviewer/QA, not an executable command.
  Deterministic checks run only as `shell` steps defined in the spec (`lint`,
  `test`, …), never from issue text.
- This matches the "scenarios/holdouts" idea from `framework-v1-draft.md` §7.
  Real holdouts (hidden from the builder) are not achievable in the same repo,
  because the develop agent can read the issue. The compensation is
  cross-model: checker ≠ builder (principle 4).

### Stage 2 — AC verification in `pr_review` (before the merge)
If the run knows an `issue_number` (develop loop), `pr_review` loads the AC
section of the issue and passes it to the reviewer **as untrusted data**.

- The verdict contract (ADR 0003) is extended by
  `acceptance: [{criterion, status: met | unmet | unverifiable, evidence}]`.
- At least one `unmet` → `changes_requested` → the bounded fix rounds from
  ADR 0010 apply. No new loop.
- `unverifiable` does not block, but ends up in the review text and in the run
  record.
- The prompt-based `review-pr` loop (PRs outside of develop) reads the linked
  issue (`Closes #N`) and checks the AC as well, without a gate.

### Stage 3 — post-merge QA (only if a need is demonstrated)
Optional `qa` step **in the develop run after `pr_merge`**. This needs no new
loop and no `on_merge` trigger.

- Read-only agent (no push, no write token), checker ≠ builder, checks the AC
  against the current state of `main`. Returns a JSON verdict per AC.
- The failure path is deterministic (§1.4 rule 3): a `shell` step **reopens**
  the issue with the missed AC as a comment and the label `qa-failed`. No new
  issue is created, and develop does not restart (`on_issue` only fires for new
  issues). A human decides.
- **Introduction criterion:** stage 3 comes only if defects show up after the
  merge despite stage 2. Measurable as defect escape rate (§5), e.g. bug issues
  that point to merged fabrikant PRs.

## Rejected alternatives

- **Separate QA loop now:** overbuild (principle 6). Needs an
  `on_merge`/reconcile trigger and covers hardly more than stage 2.
- **Separate QA suggestion block in the issue:** duplicate of the AC, drifts. Is
  replaced by the verification hint per AC.
- **QA closes the issue (instead of `Closes #N`):** this fails closed when the QA
  agent is down: issues then stay open forever (cf. the Copilot pipe bug of
  2026-10-19, which made every review run fail). With `Closes #N` plus reopen on
  missed AC, closing issues stays independent of the availability of the agents.
- **A QA fail creates a new issue:** unbounded feedback loop (see above).
- **An LLM decides whether the PR fully resolves the issue:** not deterministic,
  and 1:1 develop PR ↔ issue makes it unnecessary.

## Consequences

+ Issues close with the merge (stage 0, immediately).
+ AC are checked before the merge. Missed AC trigger the existing fix rounds
  (stage 2).
+ No new loop and no new trigger. Checker ≠ builder stays intact.
- Stage 1 changes the `classify` schema and the prompt of the `refine` loop.
  Existing issues have AC without a verification hint: stage 2 must read both
  formats.
- Stage 2 extends the verdict contract from ADR 0003 (addendum needed there) and
  costs a larger review prompt.
- PRs outside of develop (pr-reply, refactor, manual) only close issues if their
  author sets `Closes #N`.
- Addenda on acceptance: ADR 0006 (AC format), ADR 0003/0010 (verdict contract,
  AC gate).
