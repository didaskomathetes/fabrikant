# ADR 0017: Refine loop — human gate only for `needs approval` / `close`

**Status:** Accepted (2026-10-19); change of 2026-10-21 accepted (2026-10-21)
**Deciders:** Dominic
**Change 2026-10-21:** close as a proposal with the label `close proposed`,
criteria for `needs approval` (see §"Change 2026-10-21").
**Supersedes:** ADR 0006 §"Human gate" and ADR 0001 §1–2 (triage gate as human
gate). The rest of ADR 0006 (steps, label semantics, close path) still applies.

## Context

ADR 0006 put a static `approval: true` on `apply-label`: **every** refine run
paused, even when the issue was already fully specified and was classified as
`ready for development` (field finding 2026-10-19, issue #16: an issue with
complete acceptance criteria still had to be approved).

This contradicts the later line:
- ADR 0010: "PR pipeline without humans in the loop — except for critical
  decisions". The `develop` loop has `human_gate` for that (ADR paths, diff size,
  keywords, LLM) plus cross-model PR review.
- framework-v1-draft, guiding principle 3: "A human sits only at
  irreversible/accountability gates."

ADR 0001 §2 justified the triage gate by saying it was *the* human gate for
`develop`. Since ADR 0010 `develop` secures itself — the refine gate for
uncritical issues is therefore redundant.

In addition, `needs approval` was manual twice over: approving only set the label
`needs approval`, after which the human had to relabel by hand on GitHub.

## Decision

In the refine loop the human decides **only** when the classify step detects
architecture impact / contradiction with ADRs (`needs approval`) or a close:

| `classify` | before the pause | Approve | Reject |
|---|---|---|---|
| `ready for development` | label immediately, **no pause** → `develop` starts | — | — |
| `needs approval` | label `needs approval` immediately (visible on GitHub) | `needs approval` → `ready for development` (develop starts) | label stays, run ends; human decides manually |
| `close proposed` | label `close proposed` + section "Close-Vorschlag" (close proposal) in the body immediately | reasoning comment + close | label stays, run ends; human decides manually |

**Engine: conditional gate `approval_when`** (step field, `engine._approval_condition`):

```yaml
approval_when:
  label: "needs approval"      # value or list of values
```

- Pauses BEFORE the step only if every named run variable has one of the values
  (AND across variables, OR across values, comparison as string). Missing
  variable → no gate.
- Same resume semantics as `approval: true`: after approval the paused step runs
  without another gate. The approval context carries the satisfied condition
  (`approval.condition`, log line `condition:`).
- Cannot be combined with `approval: true` (spec validation).
- Does **not** replace the tier gate (T2+) — only static `approval: true` does,
  because `approval_when` may stay unsatisfied.

**Refine loop:** `classify → … → propose-close → apply-label →
promote (approval_when: needs approval) → close-issue (approval_when: close proposed) →
report`. The classify prompt says explicitly that `ready for development` goes
into development without approval. The criteria for `needs approval` are in the
classify prompt (`projects/fabrikant.fabrikant.yaml`) and are pinned by
`tests/test_fabrikant_spec.py`; when in doubt, `ready for development`.

## Rationale

- **One gate per critical decision:** uncritical issues are secured in `develop`
  by `human_gate` + review; duplicate gates cost attention and train reflexive
  waving through.
- **Approval = decision:** approving `needs approval` means "may be developed"
  and sets the matching label — no follow-up work on GitHub.
- **Close stays gated** (rationale from ADR 0006 unchanged): a wrong duplicate
  detection is hardly reversible for the reporter.
- **`approval_when` instead of a step-result gate:** the condition is declared in
  the spec (checkable, testable) instead of hidden in shell logic; `human_gate`
  remains for dynamic, diff-based criticality (ADR 0010).

## Consequences

+ Uncritical issues flow into `develop` without human interaction
+ `needs approval` issues are immediately marked as such on GitHub
+ Generic engine feature, usable for other loops too
- The classify assessment (LLM) now decides whether a human sees the issue before
  development. A misclassification as `ready for development` instead of
  `needs approval` is only caught by `human_gate` on the PR diff.
- Refine runs that are already paused (old spec) continue with the new spec after
  approval: `needs approval` runs then pause a second time at the `promote` step
  (one-time transition effect).

## Rejected alternatives

- **Remove the gate completely:** closing without approval is too risky (ADR 0006).
- **Condition in shell (`exit` + step result):** shell steps cannot pause; a
  dedicated step type just for this would be more surface than one field.
- **Keep only labelling `needs approval` (without a pause):** then there would be
  no approval path in the fabrikant UI; the human would have to search on GitHub.

## Change 2026-10-21 (accepted)

**Context:** The classify prompt named topics instead of reasons and ended with
"when in doubt: `needs approval`" — almost every issue touching the engine was
gated (e.g. #241, #237). A gated close was visible only in the fabrikant UI, not on
GitHub.

**Decision:**
- `needs approval` only with a named reason (architecture/ADR, very large,
  critical); when in doubt `ready for development`. The reasons are in the prompt.
- Close becomes a proposal: label `close proposed` and reasoning in the body
  immediately, close only after approval — for every `close_kind`. Values and
  consequences are defined by ADR 0031: `duplicate` (with `duplicate_of`;
  blockers of the dependents move to the original), `completed` (unblocks
  dependent issues), `not_planned` (dependents stay blocked); empty = GitHub
  default `completed`.

**Rejected alternatives:**
- *Close duplicates / "already solved" without a gate:* a reopen does not trigger
  refine again (reconcile remembers the issue number), and a `completed` close
  immediately unblocks dependent issues for develop (ADR 0031). Mistakes would be
  silent and expensive, while approving a close is rare and quick.
- *Reject → `needs approval`:* would need a reject hook in the engine; the manual
  path is enough and matches rejecting `needs approval`.
