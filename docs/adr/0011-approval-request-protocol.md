# ADR 0011: Approval request protocol (ADR PR stacked on the original PR → human approves via fabrik → merge)

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-18, revised 2026-10-19); amendment accepted 2026-10-24 (PR #302 merged): gate directly on the `pr_merge` step instead of on a separate `approve` step

## Context

The dual-identity pipeline (ADR 0010) has a **deadlock** when a change
requires an ADR/a critical decision:

1. The **reviewer** (`review-pr` loop / `pr_review` step) asks for an ADR for
   an architecture change.
2. The **developer agent** (`pr-reply` loop / fix round) refuses to create the
   ADR — AGENTS.md: *"never change specs/ADRs without explicit
   confirmation"*.
3. There is **no formal approval request channel** for agent work outside
   the `develop` loop:
   - `human_gate` only exists in the `develop` loop and only fires if the diff
     **already** contains an ADR file (path heuristic). Since the agent does not
     create the ADR, the gate never fires.
   - The `pr-reply` agent can only write a PR comment — no
     actionable approval request.
4. Result: the request for approval is buried in PR comments, there is no
   clear way to approve → the PR is never merged. (Example: PR #72.)

**First protocol version (2026-10-18) and its failure:** the first version
had the agent create a `needs approval` **issue**; an `approval` loop
(`on_issue` trigger) paused at the approval gate. In the field (issue #88, 2026-10-18)
that failed for two reasons:

1. **Event trigger vs. reconciling controller:** `on_issue` only fires on
   *new* issues (the first poll initializes `seen` without firing). Issues that are
   already open at the loop's first poll (after server start / spec reload)
   are swallowed forever. The `approval` loop never fired (0 runs
   in the session log) — the approval request never appeared in the UI.
2. **Approval without content is pointless:** the human approves an abstract
   issue description; only *afterwards* does the agent create the ADR. The human never sees
   the concrete ADR before approving.

## Decision

**The approval request IS the ADR PR:** the agent drafts the ADR and opens
a **stacked ADR PR** (base = branch of the original PR, label
`needs approval`). The `approval` loop (reconciling `on_pr` trigger) pauses
at the approval gate; the human approves via the fabrik UI/CLI; the run merges the
ADR PR. The approval thus applies to the concrete ADR content.

**Protocol (3 phases):**

1. **Request** — the agent (today: the `pr-reply` loop) that recognizes that a
   change needs human approval (ADR/spec, architecture decision):
   - branches an ADR branch off the head branch of the original PR,
   - drafts the ADR (`docs/adr/NNN-…md`, next free number, style of the
     existing ADRs: context / decision / consequences),
   - opens a **stacked ADR PR** (base = original PR branch, labels
     `fabrik` + `needs approval` — both labels are mandatory for the
      approval trigger, see below),
   - replies to the triggering PR comment with a pointer to the ADR PR
     and **does NOT attempt the change** on the original PR.
2. **Pause** — the `approval` loop (trigger `on_pr` + labels `fabrik` +
    `needs approval` (AND) + `reconcile: true`) starts a run for the ADR PR
    that **pauses** at the `approval: true` gate of the `pr_merge` step. The run is
    in `needs_approval` and visible in the fabrik UI (approve/reject).
3. **Grant + merge** — the human approves via the **fabrik UI/CLI**
   (`fabrik approve fabrik <run_id>`). The run resumes; the `pr_merge` step
   squash-merges the ADR PR (stacked) with the developer token. The original PR
   can then be re-reviewed/merged.

**Loop (`approval` in `projects/fabrik.fabrik.yaml`):**

```yaml
approval:
  tier: 1
  triggers:
    - type: on_pr
      repo: didaskomathetes/quark-playground
      labels: [fabrik, needs approval]   # AND semantics (client-side)
      reconcile: true
      poll: 300
  steps:
    - name: merge-adr-pr
      type: pr_merge
      repo: didaskomathetes/quark-playground
      method: squash
      approval: true        # gate directly on the merge → run pauses (needs_approval)
      approval_reason: "{pr_title}"  # PR title/link appear in the approval context
```

**Amendment (PR #302):** the gate sits directly on the `pr_merge` step instead of on an
upstream `shell` step `approve`. The approval context (`ApprovalRequest`)
carries PR link and title in a typed way so that the human sees the concrete action
to be approved. Resume compatibility: runs already paused with the old
`approve` step remain readable; new runs pause at the merge step.

**Engine change: `reconcile: true` on `on_pr`** (opt-in, `scheduler.py`):

- The first poll ALSO fires on *existing* matching PRs (not just
  `seen` initialization).
- Every poll only fires on PRs **without** an active run (`running`/
  `needs_approval`) — dedup via `_has_active_pr_run` (scans
  `runs/<project>/*.json` for `loop` + `trigger` + status).
- Without `reconcile` the event behavior remains unchanged (the first poll
  initializes, fires on new/updated).
- In addition: `on_pr` now supports `labels` filtering (previously ignored),
  with **AND semantics** (client-side): the PR must have *all* required labels.
  The GitHub API only filters server-side with OR — without the client-side
  AND filter, `needs approval` alone would also hit non-fabrik PRs
  (the repo `quark-playground` hosts several projects; field finding 2026-10-19:
  the loop fired on PRs #3/#4/#7 without any label).

**Why `tier: 1`:** the loop does near-T4 work (merging an ADR PR). The
control here comes from the **front** `approval: true` gate — the human
approves the concrete ADR *before* merging — and from the cross-model review
of the ADR PR (`fabrik` label → `review-pr` loop, ADR 0003/0010), not from a
synthetic tier gate at the end. `tier: 1` therefore sets no end gate; the
T4 requirement "human gate, no auto-complete" (v1 draft §1.1) is
met by front gate + review.

## Consequences

+ The approval request comes **with the concrete ADR** (stacked PR) — the
  human approves the actual content
+ The ADR PR is a **durable, visible artifact** (GitHub + fabrik UI),
  not buried in comments
+ The reconciling trigger is **robust against restarts/spec reloads** —
  existing ADR PRs are picked up, no duplicate runs
+ Approval via the **existing** fabrik mechanism (UI/CLI) — one
  consistent approval path, no second protocol
+ No deadlock: the agent drafts the ADR + opens a PR, the human approves, fabrik merges
+ The ADR PR goes through the normal review pipeline (`fabrik` label →
  `review-pr`, `sonar-scan`)
- An additional loop + one run per approval request
- The ADR is **drafted** by the agent (builder model) — human
  approval is the gate; draft quality depends on the agent
- On reject (`fabrik reject`) the run ends; the ADR PR stays open and is
  closed manually
- Stacked PR: the ADR PR must be merged **before** the original PR
  (base = PR branch)
- Merge conflicts: if the original PR advances (code fix via
  `pr-reply`), the stacked ADR PR may conflict. The `pr-reply` agent
  rebases stacked ADR PRs (base = PR branch) after every push to the
  original PR branch (prompt step 5b) — conflict prevention, not
  resolution in the merge step.

## Alternatives (rejected)

- **`needs approval` issue + `on_issue` loop (first version, 2026-10-18):**
  the event trigger swallows existing issues (first poll initializes);
  approving an abstract description without ADR content is pointless. Failed
  in the field (issue #88).
- **Approval via GitHub comment** (`approved` in the issue/PR): a second,
  parallel approval protocol next to `fabrik approve` — inconsistent, two
  ways to the same approval.
- **Only a PR comment with a marker:** exactly the problem we are solving
  (buried in comments, not actionable).
- **Extend `human_gate` to `pr-reply`:** `human_gate` is a
  diff criticality check for the `develop` loop; here the agent needs a
  **proactive** request for approval before it creates the ADR — different semantics.
- **Cron-based reconciliation** (list `needs approval` PRs every N minutes):
  more moving parts, per-PR run dedup is needed anyway;
  `reconcile: true` achieves the same with a smaller, testable
  change to the existing trigger.
