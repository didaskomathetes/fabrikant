# ADR 0025: Alert deduplication by fingerprint (instead of an issue per run)

**Status:** Accepted (2026-10-19). Numbering: originally 0013, then 0014, since 2026-10-20
0025 (ADR number cleanup: 0013/0014 were assigned twice); personal name replaced by GitHub handle 2026-11-26
**Deciders:** didaskomathetes
**Code PR:** #125
(implementation, tests, config and this ADR)
**Context:** `_notify` (channel `github_issue`) opened a new GitHub issue for **every** failed
run. The title contained the run ID, so no two alerts were identical. A persistent
error in the 5-minute `review-pr` cron (pipe bug,
PR #124) produced 22 alert issues (#102–#123) for the same error — an
alert flood that buried the actual signal and required manual cleanup.

## Decision

Alerts are deduplicated by a stable **fingerprint** of the failure pattern:

- **Fingerprint** = SHA-256 over `project/loop` + first failed step +
  normalized error text, truncated to 16 hex characters (64 bits).
- **Normalization** removes run-specific tokens: UUIDs (8-4-4-4-12) and
  hex tokens ≥ 8 characters (run/session IDs, hashes). Short, meaningful
  numbers ("1 error(s)", "port 8080") are kept, so that different
  failure patterns do not collapse into one.
- The fingerprint sits as an invisible marker in the issue body
  (`<!-- fabrikant-alert:fp=… -->`).
- **Open issue with the same fingerprint** → the run is commented there
  (channel entry `issue#N(+)`), no new issue is created.
- **Closed issues do not count:** If the error occurs again after closing,
  a new issue is opened (the alert escalates anew).
- **Lookup error** → the issue is created anyway (fail-safe: better a
  duplicate than a lost alert).
- **Lookup paginates** over all open alert issues (page size
  `config.ALERT_OPEN_ISSUES_PAGE` = 100) until a page is incomplete, so that
  no relevant issue is overlooked even with > 100 open issues.

## Operational trade-offs

- **Collision risk:** 64-bit fingerprint. By the birthday paradox,
  ~4 billion *different* failure patterns per project/loop would be needed before the
  collision probability reaches 50 % — negligible given the real number of
  failure patterns. A collision would merge two *different*
  errors into one issue (no lost alert).
- **Secrets in the log_tail:** The last 15 log lines end up without redaction in the
  issue body/in comments; logs can contain tokens/keys. This ADR
  deliberately contains no log examples. Mitigation (explicit risk acceptance
  until redaction is implemented):
  1. **Ops-only visibility:** Alert issues are opened only in private
     Fabrikant project repos (e.g. `acme/my-app`);
     access = repo ACL (Fabrikant operators, no broad team, no
     public repo). No additional GitHub repo settings are needed
     as long as the repo stays private; the same
     precondition applies to new repos (private + operator ACL).
  2. **Risk acceptance:** It is deliberately accepted that logs can contain
     tokens/keys; owner: didaskomathetes (decider, above).
  3. **Follow-up issue with deadline:** A redaction layer for `log_tail` before the
     issue post has been created as
     #132
     (pattern-based, configurable, fail-safe). **Deadline:
     implementation within 2 weeks after the merge of this ADR (by 2026-10-31);
     owner: didaskomathetes.** Until then the risk acceptance from point 2 applies.
- **Rate limits:** The lookup reads all open alert issues (typically 1–3).
  Problematic only if alerts are never closed.
- **Rollback:** PR #125 is squash-merged → rollback =
  `git revert <squash-SHA>` (one commit). Affected files:
  `src/fabrikant/steps/notify.py`, `src/fabrikant/config.py`
  (`ALERT_OPEN_ISSUES_PAGE`), `tests/test_steps_notify.py`,
  `README.md`. Verification: `uv run pytest -m "not browser"` green;
  old issues without a marker are simply not matched — no data migration
  needed.

## Rejected alternatives

- **Dedup by title:** The title contains the error text but varies per
  run (IDs, numbers) — without normalization the same problem as before.
- **Time-based dedup (e.g. 24 h):** Time-based instead of error-based — a
  persistent error would open a new issue every 24 h.
- **No dedup (status quo):** Alert flood (observed: 22 issues for one error).

## Acceptance criteria

- **Tests** (in PR #125, `tests/test_steps_notify.py`): fingerprint stability
  (same errors → same fingerprint, run/session IDs irrelevant),
  dedup (open issue → comment, no second issue), pagination
  (> 1 page), lookup fallback (API error → issue anyway),
  title sanitization, max-pages abort.
- **Metric:** No failure pattern produces > 1 open alert issue; recurring
  runs increase the comment count of the existing issue.
- **Rollback test:** Reverting the notify.py commit leaves old issues without a marker
  unchanged (no match, no migration).
- **Coverage:** 91 % total (target ≥ 80 %), `notify.py` 100 % — measured
  2026-10-19 on branch `fabrikant/adr-125-alert-dedup` with
  `uv run pytest --cov -m "not browser"`.

## Consequences

- One issue per failure pattern (instead of per run); recurring runs are
  commented.
- Tests in `tests/test_steps_notify.py` (fingerprint stability, dedup,
  pagination, lookup fallback, title sanitization) — delivered in
  PR #125,
  not in this ADR PR.
- `ALERT_OPEN_ISSUES_PAGE` (page size of the lookup, default 100) —
  definition: `ALERT_OPEN_ISSUES_PAGE = 100` in
  `src/fabrikant/config.py`, introduced in
  PR #125
  (commit 75c0cbf, present on the PR branch). This ADR PR (base =
  PR branch #125) sees it after the merge of the stack.
