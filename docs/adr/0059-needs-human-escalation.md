# ADR 0059: `needs human` — Escalation to a Human on Blockage, Stagnation, and Scope Drift

**Status:** Vorgeschlagen (2026-11-16)
**Betrifft:** ADR 0011 (approval request protocol), ADR 0013 (AC verification in review),
ADR 0024 (verdict markers, reply lock),
ADR 0035 (error classes), ADR 0036 (run retry, exhausted jobs), ADR 0039 (convergent
review rounds).

## Context

A review loop can get stuck when it needs a human, with nothing moving across multiple
rounds:

- The reviewer reports the same findings every round and the verdict stays
  `changes_requested`.
- The builder answers each round that the blocker is not resolvable from here and needs a
  human, and changes nothing. The blocker lives outside the sandbox's reach — for example a
  change in another repository or by another team, or an environment that is not reachable
  from the sandbox.
- Every reply and every new head triggers the next review. The "needs a human" request exists
  only as free text in a PR comment. Nobody is notified, and nothing is visible in the fabrik
  UI.

This is the same failure ADR 0011 fixed for ADRs: the request for a human decision is buried
in PR comments, and there is no way to act on it. ADR 0011 only covers the case "a change
needs an ADR." What is missing is a general channel for the remaining cases in which a loop
cannot proceed on its own:

1. **Blockage:** The builder needs something outside its reach: access (to a live or fixture
   environment), a change in a foreign repository or by another team, or a policy or product
   decision.
2. **Stagnation:** Review and reply go in circles, the verdict does not change.
3. **Exhaustion:** `pr_review` reaches `max_rounds` (ADR 0039), a retry chain is
   `exhausted` (ADR 0036). Today both end as `fail` in the run log, without anyone needing to
   make a decision.
4. **Scope drift:** The solution deviates significantly from the issue's acceptance criteria,
   or is much larger than the issue suggested (another repository, another team,
   architecture).

## Decision

### 1. One state, one label, one gate

All four cases converge on **the same escalation state**. It feels like an approval: the
human sees a waiting run with a reason in the fabrik UI/CLI and acts there.

- **Durable state = label** `needs human` on the PR (or on the issue, if no PR exists). The
  label name is configurable per project (`human_label`, default `needs human`).
- **Visibility = run in the gate:** an `escalation` loop (trigger `on_pr`/`on_issue`, labels
  `[fabrik, needs human]`, `reconcile: true`, analogous to the `approval` loop from ADR
  0011) starts a run per marked item that pauses in `needs_approval`. The `ApprovalRequest`
  gets the new `reason` `escalation` and carries: `kind` (see below), `why` (summary of the
  blocker), `what` (the options for the human), `pr`/`issue`, and links to the triggering
  comments.
- **Notification:** as for approvals (UI badge, `notify` step, if configured).

### 2. Escalation kinds (`kind`)

A fixed, small set. New kinds only via an ADR change.

| `kind` | Trigger | Detected by |
|---|---|---|
| `blocked` | Builder needs access, a change in a foreign repo/team, or a decision; or an AC is not verifiable from the sandbox | Builder marker (§3); reviewer AC verification (§3) |
| `stalled` | `stall_rounds` (default 2) consecutive `changes_requested` verdicts without the builder changing code in between, or without new findings (ADR 0039: the reviewer reports only prior-round findings as open) | Deterministically from verdict and reply markers (§4) |
| `exhausted` | `pr_review` still `changes_requested` after `max_rounds`; retry chain `exhausted` (ADR 0036) | Engine, at the point where `fail` is set today |
| `scope` | Solution requires significantly more than the ACs, or a blocking finding lies outside the ACs | Builder marker (§3) or reviewer finding field `out_of_scope` (§3) |

### 3. Markers instead of free text

The builder escalates via a **structured marker** in its reply, not via the phrasing "needs
human":

```
<!-- fabrik-escalate kind=blocked re=<keys> -->
```

- The agent posts the reply itself (as with every `pr-reply` answer, the last line is
  exactly `<!-- fabrik-reply -->`); the host validates the marker like the others: `kind`
  only from the table, `re` only from the keys of this run. Only the host sets the label.
  The agent gains no additional rights for this.
- The reviewer may set `out_of_scope: true` per finding (extension of the finding schema from
  ADR 0039). If a blocking finding (`high`/P1) is `out_of_scope`, the review step escalates
  with `kind=scope` instead of posting `changes_requested` and sending the builder off.
- **AC verification (ADR 0013 stage 2):** The reviewer reports `met`/`unmet`/`unverifiable`
  per criterion. If a criterion is `unverifiable` in two consecutive rounds (for example a
  criterion that can only be checked against an environment not reachable from the sandbox),
  the review step escalates with `kind=blocked`. It does not send the builder off again,
  because the builder cannot prove it by definition. `unverifiable` still does not block the
  verdict (ADR 0013); it only stops the loop.
- **Do everything feasible first:** Before the builder escalates, it completes what is within
  its reach and does not preempt the human's decision (e.g. add tests, extend the PR body with
  the scope actually implemented). The escalation states what is done and what remains open.

### 4. Stop: loops respect the label

- `review-pr`, `pr-reply`, and `develop` skip items with `needs human`. This applies at the
  trigger (label exclusion) and additionally in the gate of `pr-status.py`
  (`merge-check` → `skip`), so a manual run does not slip through.
- Stagnation detection (`stalled`) runs deterministically in `pr-status.py` from the existing
  markers (`fabrik-review verdict=… sha=…`, the bare `<!-- fabrik-reply -->` reply marker) and
  the commit history of the PR branch. The reply marker carries no attributes, so "the builder
  changed nothing" is derived from the reply's content and the commit history, not from a
  marker attribute. It uses no LLM.

### 5. Human actions

In the gate of the fabrik UI/CLI, with the same operation as approvals:

| Action | Effect |
|---|---|
| **Resume** (optionally with a note) | Label removed. The note is posted as a PR comment from the human's account and is thus regular input for `pr-reply` (e.g. "the upstream PR is merged, please re-verify"). The `on_pr` reconcile triggers fire again — with a **fresh budget**: the run gets a new `max_rounds`/`stall_rounds` window, not a continuation of the old one. This is deliberate: for `exhausted`, `pr_review` has already hit `max_rounds`, so a plain re-trigger would immediately re-exhaust; for `stalled`, the same findings would re-trigger the same stall. The human's approval is an explicit decision to give the loop a new chance, and that chance must come with a new budget. |
| **Re-scope** | Like resume. The human has adjusted the ACs or PR body beforehand (e.g. `Closes` → `Refs`). The note tells the builder what changed. |
| **Give up** (= reject) | PR closed with a reasoning comment. The issue stays open, `needs human` stays on the issue, so `develop` does not pick it up again. |

If the human resolves the problem directly on GitHub (removes the label manually), the
escalation run ends on its own as done at the next reconcile.

### 6. Relationship to existing mechanisms

- **ADR 0011** remains authoritative for the "ADR needed" case: there is concrete content to
  approve (the ADR PR). `needs human` is for cases without such content.
- **ADR 0035/0036:** `environment` errors still alert via the alert path (ADR 0025). Only
  chains whose last error was `outcome` additionally become `exhausted` escalations.
  Infrastructure problems thus do not become PR labels.

## Consequences

+ Review/reply loops like the one in the context end after at most `stall_rounds`. No further
  tokens for rounds that change nothing.
+ One channel, one operation for all "needs a human" cases. The request is in the UI, not in
  comment number 7.
+ Exhausted fix rounds and retry chains become decisions instead of silent `fail`s.
+ The agent gains no new rights. The host sets the label and the run after validation.
- A new loop (`escalation`) and a new `ApprovalRequest.reason`. The UI must distinguish
  `escalation` from regular approvals (text, actions).
- A wrong `blocked` marker parks a PR that the builder could have resolved. Countermeasure:
  the marker requires the "done / open" list, and the human can resume with a note.
- `stall_rounds` too low stops loops that are just converging. The default 2 is chosen
  conservatively and is overridable per loop.

## Open points (not part of this decision)

- Automatically creating a follow-up issue in the foreign repository for `kind=blocked`, when
  the blocker is a concrete code change. Requires write access there and a rule for who may do
  it.
- Escalation from the `refine` loop (ADR 0006/0017), when an issue is already recognizable as
  not resolvable in this repository during refinement.
