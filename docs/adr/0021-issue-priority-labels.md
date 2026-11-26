# ADR 0021: Issue prioritization via phase labels (`priority_labels`)

**Status:** Accepted (2026-10-20, merge #213; proposed 2026-10-19)
**Context:** `refine` and `develop` work through the issue stock via `on_issue`
reconcile (ADR 0014), **oldest first**. The issues now carry phase labels
`p0`–`p3` according to `docs/framework-v1-draft.md` §6. Every phase must be
useful on its own and builds on the previous one. An old p3 issue (e.g. cloud
deployment) should therefore not be refined or developed before a new p0 issue
(e.g. secret redaction).

## Decision

- New optional trigger option `priority_labels` for `on_issue`: an ordered label
  list, highest priority first. The rank of an issue is the index of the
  **first** label of the list that it carries. Issues without a priority label
  come last.
- Within a level "oldest first" applies (ADR 0014), sorted by issue number, and
  therefore also in event mode, which queries GitHub newest first.
- **Reconcile mode:** with `priority_labels` the lazy early stop of the
  pagination is dropped. All pages (≤ `RECONCILE_MAX_PAGES`) are read, then
  sorted, then cut to `max_per_poll`. Otherwise a p0 on page 2 would disappear
  behind older unprioritized issues.
- **Event mode:** several new issues from one poll fire in priority order.
- The labels are in the spec, not in the code. In
  `projects/fabrikant.fabrikant.yaml`, `refine` and `develop` use `[p0, p1, p2, p3]`
  (YAML anchor `*priority_labels`).

## Consequences

- Without `priority_labels` nothing changes (backwards compatible).
- With priorities, every reconcile poll reads all pages (≤ 10 GitHub requests per
  loop and 300 s). This is uncritical at the size of the repo.
- Prioritization applies **per loop**. There is no coordination between `refine`
  and `develop`; both still compete for the project lock.
- A p0 label set after a p3 run has started does not interrupt that run. It only
  takes effect at the next poll.
