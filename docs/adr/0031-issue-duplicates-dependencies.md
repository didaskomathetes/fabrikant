# ADR 0031: Duplicate and dependency detection in refine, blocker gate in develop

**Status:** Accepted (2026-10-21, merge #250; proposed 2026-10-20)
**Complements:** ADR 0006/0017 (refine loop), ADR 0014/0021 (`on_issue` reconcile),
ADR 0018 (`issue` step: actions `list` and `block`; new fields for `create` and `close`).

## Context

- The classify prompt already demanded "duplicate → close", but saw only its own
  issue. So it could only guess duplicates.
- `develop` started every `ready for development` issue immediately. Issues that build on
  another, still open issue ran against a missing base.
- AI-generated issues often presuppose foundations that **no issue** covers.
  Field finding 2026-10-20: #19 (Postgres) and #21 (reaper) distinguish deployment profiles
  `homelab|cloud`. The mechanism exists only as part of #28 (cloud rollout, p3), a
  homelab profile not at all. In addition, the already refined issues have no
  blocker relations: #160 says "Depends on #159" only in the text.

## Decision

1. **Single source of truth: native GitHub issue dependencies** ("blocked by").
   We use no marker in the body and no own state. Humans see and
   change the relation in the GitHub UI.
2. **Refine loop:** `list-open → list-closed → classify → dependencies → refine-body →
   create-prerequisite → link-blockers → apply-label → promote → close-issue → report`.
   - `list-open`/`list-closed` (`issue` action `list`): open fabrikant issues (≤ 100) and
     the 30 most recently closed as a text list `#N [labels] title — excerpt`. The
     lists count as untrusted.
   - `classify` checks for duplicates. A duplicate leads to `close` with `close_kind` and
     `duplicate_of`, behind the existing close gate (ADR 0017).
   - `dependencies` (own LLM step, focused prompt) yields `blocked_by`
     (≤ 5 open issues; tracking issues often bundle that many sub-issues) and at most **one** missing prerequisite
     (`prerequisite_title/body`): the smallest foundation that is neither implemented
     nor covered by an open issue.
   - `create-prerequisite` creates the prerequisite as an issue with label `fabrikant`.
     It thereby goes through refine itself (duplicate check, `needs approval` for
     architecture) and inherits the priority of the dependent issue (`inherit_labels`,
     whitelist `p0–p3`, never status labels).
   - `link-blockers` (action `block`) links `blocked_by` and the new
     prerequisite idempotently. Hallucinated numbers, closed issues and PRs
     are skipped, at most 6 (5 blockers + the prerequisite). Other API errors make the step fail.
   - Both steps run **before** `apply-label` and only for `ready for development` /
     `needs approval`. So `develop` never sees the label without the associated
     blockers. If a step fails, the issue gets no label (fail closed).
3. **Done = `completed`:** A blocker counts as done only if it is closed as
   `completed` (or without a reason, for old issues). Blockers closed as
   `duplicate`/`not_planned` keep blocking, because the
   preliminary work is then still missing. That is why refine closes with an explicit `state_reason`.
   A duplicate close with `duplicate_of` moves the relations of the dependents to the
   original before it closes. If that fails, the issue stays open.
4. **Gate in the scheduler** (`on_issue` option `skip_blocked: true`, only with
   `reconcile`): issues with unfinished blockers do not start and leave
   no run record. They fire on the first poll after the last blocker is
   done; for `develop` that is the PR merge via `Closes #N` (ADR 0013).
   `issue_dependencies_summary.total_blocked_by` saves the dependency call for issues
   without blockers. API errors abort the poll (fail closed). Blocked issues
   do not use up any `max_per_poll` quota. Event mode is excluded
   (spec validation).
5. **One-off backfill** (`deps-backfill` loop): the same step building blocks (YAML anchors
   under `x-shared-steps`), without relabel/close, once per open fabrikant issue.
   It runs at 1 run per 120 s, so that a run sees the prerequisite of the previous one.
   Procedure: pause `develop` → work off the backlog → deactivate `deps-backfill` →
   resume `develop`.

## Consequences

+ The develop side is deterministic: nothing starts with unfinished blockers,
  no matter whether refine, backfill or a human set the blocker.
+ Missing foundations become their own, prioritized issues, instead of `develop`
  building them implicitly or failing on them.
- The **detection** is an LLM assessment based on title and excerpt, without a look
  into the code:
  - A wrong blocker stops the issue until a human removes the relation.
    This is visible as the log line "wartet auf Blocker" ("waiting for blocker"), but not yet in the UI.
  - A missing blocker behaves as before.
  - A prerequisite can already exist in the code without any issue naming it.
    The develop run for it then becomes small or empty.
- **Prerequisite chains:** A prerequisite can create a further one during its refine
  (at most one per run). Architecture foundations typically end up at
  `needs approval`, i.e. with a human.
- **Duplicate prerequisites:** Parallel runs can create the same prerequisite.
  The duplicate check of the second one closes it with `duplicate_of` and moves the blockers
  over. This close is gated.
- A blocker that never closes as `completed` (e.g. rejected `needs approval`)
  blocks permanently. That is intended, a human decides. Cycles block
  both issues.
- With > 130 issues the context list is no longer enough. A preselection would then be needed
  (GitHub search, embeddings).
- The `refine` loop makes one more LLM call per issue (`dependencies`).

## Rejected alternatives

- **`Depends on #N` in the body + parser:** That would be a second format besides the
  GitHub relation, and it would be fragile.
- **Only escalate a missing prerequisite (`needs approval` instead of an issue):** That is
  safer against issue sprawl, but makes every case manual. That contradicts
  ADR 0017. The human sees architecture foundations anyway through the refine of the
  prerequisite.
- **Every close releases dependents:** A blocker closed as duplicate or `not_planned`
  would let issues start without their foundation.
- **Blocker check in the develop agent or in `_safe_run`:** In the agent the run would
  already have started, and an LLM decision would form the gate. In `_safe_run`
  manual runs would be blocked too.
