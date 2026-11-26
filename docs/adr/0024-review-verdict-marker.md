# ADR 0024: Review as a GitHub review with verdict marker, inline threads, reply lock

**Status:** Accepted (2026-10-19, merge #130; proposed 2026-10-19)
**Deciders:** Human (approval pending)
**Context:** PR #124 was stuck in a review loop. The PR was created by the
personal account — the same account that `review-pr` reviews with. GitHub forbids
both approve **and** request-changes on one's own PRs
(`Can not approve your own pull request`); ADR 0010 only solves this for
`develop` PRs (the app creates, the PAT reviews). Result: the review only ended
up as a PR comment, `reviews` stayed empty, and the merge check in `pr-reply`
(latest review `APPROVED`) could never apply. Retries posted the same review
twice.

Secondly, `pr-reply` relied on thread replies (`in_reply_to`) as a self-loop
lock. PR comments (issue comments API) have no threads — every answer was
top-level and counted as "new" in the next run. Only the LLM's judgement ("sounds
like my own answer") prevented the endless loop.

Thirdly, the reviewer could only write a block of prose — concrete, line-precise
remarks and applicable suggestions were not possible.

## Decision

**One review per head SHA as a GitHub review object**, posted and evaluated
**deterministically** by `scripts/pr-status.py` (not by the agent):

- **Summary** = review body, the last line is the verdict marker
  `<!-- fabrikant-review verdict=approved|changes_requested sha=<headRefOid> -->`.
- **Inline comments** (optional, at the reviewer's discretion, max. 10) in the
  same review, preferably with ```` ```suggestion ```` blocks.
- `post-review`: event `COMMENT` on own PRs, otherwise `APPROVE` /
  `REQUEST_CHANGES` from the marker; `commit_id` = marker SHA; idempotent per SHA
  (retry protection); if GitHub rejects inline lines (HTTP 422), the review is
  posted without inline comments and the remarks move into the summary.
- `merge-check`: approval = the latest marker of the reviewer account
  (`gh api user`) is `approved` **for the current head SHA**, otherwise a GitHub
  review `APPROVED` without marker (human, `pr_review` step). Blocked by
  `CHANGES_REQUESTED` without marker and by **open review threads**.
- `pr-reply` answers inline comments in the thread, applies fitting suggestions
  and resolves the thread (`resolve-thread`) only if the concern at that line is
  fully implemented. Rejected or only partially implemented points stay open and
  block the merge until a human decides.
- `<!-- fabrikant-reply -->` — last line of every `pr-reply` answer; `new-comments`
  never returns such comments as new. Sources: PR comments, review summaries,
  inline comments (state keys `<num>-<id>`, `<num>-review-<id>`,
  `<num>-inline-<id>`).

## Rationale

- `event: COMMENT` is allowed on own PRs — one mechanism for bot and human PRs,
  without a new identity.
- SHA binding: after new commits the old approval is invalid — a re-review is
  mandatory, as with "dismiss stale reviews".
- Inline threads are real threads and have a resolved status — that is the
  natural place for "done?" instead of an LLM judgement over comment chains.
- Script instead of prompt jq: testable (`tests/test_pr_status.py`), no LLM
  guessing over raw JSON, no double posts.

## Rejected alternatives

- **Second identity for human PRs** (the app approves): cleanest GitHub
  semantics, but the app would need review rights and ADR 0010 (app = developer)
  would be watered down.
- **Verdict as a PR comment + separate inline review:** two posts, two
  notifications, not atomic.
- **`reviewDecision`:** stays empty on own PRs.
- **Author filter for replies:** reviewer and developer share the account on
  human PRs — not distinguishable.

## Consequences

- Human PRs with the `fabrikant` label are merged automatically after LLM approval
  (marker, no open threads) — like bot PRs. Whoever does not want that sets no
  `fabrikant` label.
- Limit: `pr-reply` uses the same account as the reviewer and *could* write a
  review marker or resolve threads without justification. The prompt forbids the
  former; comments are untrusted. Real separation would need separate tokens for
  the two loops.
- The model has to derive inline lines correctly from the diff; mistakes only
  cost the line binding (422 fallback), not the review.
- Existing answers without a marker are in the state (`replied-comments.json`)
  and stay hidden.
