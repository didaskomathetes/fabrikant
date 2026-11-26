# ADR 0071: Agent results are data; the engine performs side effects

**Status:** Proposed (2026-11-26)
**Affects:** ADR 0003 (`review` verdict contract), ADR 0010 (`pr_review`, dual identity),
ADR 0024 (review marker, `pr-reply`), ADR 0035 (failure classes), ADR 0054 (sandbox).

## Context

An `agent` step is `ok` when its session ends without a backend error. What the agent achieved
is only in its free text, which nothing reads.

The `review-pr` loop asks the agent to post its own GitHub review. On 2026-11-26 posting failed
with HTTP 403 (a GitHub App token may not call `gh api user`). The agent said so in its final
message, and the run was recorded as PASS.

Three things are wrong with this design:

- **No reliable outcome.** Success is whatever the session status says.
- **Side effects need no judgement.** Posting a review is deterministic work given to an LLM
  (against guiding principle 1: the conductor is code).
- **Token next to untrusted input.** The agent reads PR text and diffs while holding a token that
  can write reviews.

The engine already does better elsewhere: `llm` validates structured output against a schema,
`review`/`pr_review` require a JSON verdict, and `pr_review` posts the review itself.

## Decision

### 1. Principle

A step that needs judgement returns **data**. The engine or a deterministic host step validates
that data and performs the side effect. A step that cannot deliver valid data fails. An agent's
statement that it succeeded is information, never proof.

### 2. `review-pr` uses `pr_review`

The `review-pr` template switches from a prompt-driven `agent` step to `pr_review` with
`max_rounds: 0` (review only). The agent gets no `github_identity`; the engine collects the input
and posts with the reviewer token.

`pr_review` is extended to what the prompt-driven loop does today:

- **Head binding:** the review carries the reviewed commit as `commit_id`. If the head moved, nothing
  is posted. Today the step posts without it, so a push during the review can receive an approval for
  code nobody reviewed.
- **Marker and idempotency** (ADR 0024): verdict marker built by the host, at most one review per SHA.
- **Inline comments:** the verdict contract's `findings[]` gains optional `path`, `line` and
  `suggestion`. Lines outside the diff move into the summary.
- **Own PRs:** `COMMENT` instead of `APPROVE`/`REQUEST_CHANGES`.
- **Review context:** project rules, the quality report, linked acceptance criteria and open threads
  are supplied by the engine or `extra_context`. A `prompt_file` replaces the built-in template.
- **Review-only outcome:** with `max_rounds: 0`, a posted `changes_requested` is a successful run
  with verdict `changes_requested`, not a failure. A review that could not be posted is a failure.

The posting rules exist in `scripts/pr-status.py` (`post-review`) and, partly, in `pr_review`. They
move into the package as one implementation that both call.

### 3. Structured results for `agent` steps

For loops with no dedicated step type, `agent` gets an opt-in `schema:` with the syntax of the `llm`
step:

- The engine tells the agent to write a JSON object to a result file in the session directory
  (`.fabrikant-session/`, which the sandbox carries back). The engine removes any older file before
  the session and the new one after reading it.
- The object is `{"success": bool, "result": "<one paragraph>", ...schema keys}`.
- A missing file, invalid JSON, a schema violation or `success: false` fails the step with class
  `outcome`. `result` becomes the step summary.
- Schema keys become run variables under the `llm` rules (existing keys are never overwritten). The
  values are untrusted (ADR 0008): they reach shell steps as env values or files, never substituted
  into `cmd`.
- The agent cannot choose the failure class, so that untrusted input cannot steer retries.
- Without `schema:` the step behaves as today.

`pr-reply` follows the same split with the existing `reply-context` and `post-replies` commands: the
agent writes drafts and the host posts them.

## Consequences

+ A failed post fails the run, with its real cause in the summary.
+ In the sandbox, a review agent holds no GitHub token: a malicious PR can still skew a verdict but
  cannot post, push or call the API as fabrikant.
+ One implementation of the posting rules.
- Outside the sandbox the agent process can still use the host user's own `gh` login, so the token
  property only holds with `FABRIKANT_SANDBOX`.
- The reviewer sees only what the engine supplies. It can no longer fetch more from GitHub by itself,
  and large diffs are capped.
- A structured result proves that the agent answered in the agreed form, not that its claim is true.
  Outcomes that matter still need a deterministic check or a host-side action.
- Specs that copied the old `review-pr` prompt keep the old behaviour until they migrate.

## Alternatives (rejected)

- **Check afterwards that the review exists** (`review-check`): finds this one failure, but keeps the
  token with the agent and needs a new check for every side effect.
- **Only a `success` flag:** the agent grades itself and can be wrong.
- **Parse the agent's final message:** review text contains code fences and braces, and CLIs add
  prose around answers; a file is unambiguous.
- **A generic `agent` result plus a `post-review` shell step for `review-pr`:** works, but duplicates
  what `pr_review` already does.

## Open questions

1. Should a missing or invalid result get one bounded retry, as `pr_review` does for verdicts?
2. Does a restricted agent without a write tool need the final message as a fallback?
