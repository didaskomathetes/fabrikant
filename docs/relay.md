# Relay tasks — guide

Decision and rationale: [ADR 0037](adr/0037-relay-tasks.md). This page covers writing a brief, the flow and how to
intervene.

## Brief template (issue body)

Title = task. `## Goal`, `## Session` and `## Verify` are required; the rest is optional. Sessions work in
English; the brief may be written in another language.

````markdown
## Goal
What is true when the task is finished (one paragraph). What is explicitly out of scope.

## Session
How a single session works:
- Sources / targets and how to reach them (profile names, endpoints, never secrets)
- Unit of work (e.g. batches of 200 rows, keyed by `source_id`; idempotent upsert)
- Required backup before the first write (e.g. an export or snapshot of the target)
- Checkpoints: "stop as blocked after the first batch for review"

## Verify
Invariants fixed before work starts (the auditor checks exactly these):
- target item count == source pairs after filter X
- no duplicate `source_id` in the target
- content hash per item over (question, answer, source_id) matches the source
- required metadata present: shop, date, channel
- 20 random items compared side by side

```verify
uv run python -m my_import --verify --dry-run
```

## Audit access
Read-only access for the auditor (profile / role / API key name). Without it the audit stops.

## Settings
max_sessions: 30
session_minutes: 30
scope: src/my_import/**, docs/my_import.md
````

- **`## Verify` is pinned.** The first session stores its hash. A later change blocks the task;
  removing `relay: blocked` adopts the new version.
- **```` ```verify ````** (optional): a command fabrikant runs after every session. Its last output
  line is the progress evidence (stall detection, status comment). Without it, the `progress`
  field in the handoff counts.
- **Lint:** fabrikant writes `.fabrikant/relay/<N>/ruff.toml` (`exclude = ["*"]`) so worker scripts and audit
  evidence stay byte-for-byte and don't fail the target repo's ruff check. An existing file is kept.
- **`scope`**: paths a session may commit in addition to `.fabrikant/relay/<N>/`. Everything else
  blocks the task and is **reverted** at check-in (tracked → HEAD, untracked → deleted), so
  unblocking does not re-block on the same paths; the spec's `relay.forbid` always wins.
  The check compares against the commit at checkout: commits the agent makes itself are folded
  back into the worktree (fabrikant commits). An uncommitted merge of `origin/<base>` is allowed:
  paths that come unchanged from the base don't count, an out-of-scope edit falls back to the
  base's version, and fabrikant commits the complete merge. A merge of anything else is dropped.
- **`session_minutes`** is capped by `relay.max_session_minutes` (default 35, because of
  `FABRIKANT_STALE_HOURS`).

## Flow

1. Create an issue with the brief and set the label `fabrikant-relay`. The label must come from a login in
   `relay.allowed`.
2. `relay-dispatch` (default every 15 min) starts the first session, then the due task that has waited longest.
   At most one relay run per project runs at a time.
3. Each session commits and pushes to `fabrikant/relay/<N>` and updates the status comment. Commits are made as
   the bot (`relay.bot_login`). On `done`, the result (`## Result` in the handoff) is in the status comment; after
   the audit, so is its summary.
4. `status: done` → label `relay: done` → `relay-audit`:
   - Phase 1, blind: own queries, read-only. Only files directly in `audit/blind/` are published (max. 1 MB);
     working copies belong in `.fabrikant-session/audit/work/`.
   - Phase 2: checks the worker's scripts and logs.
   - `AUDIT.md` with a verdict; `pass` → label `relay: audited`. The verdict is bound to the trail hash (last
     line of `AUDIT.md`, status comment).
   - With `relay.trail_archive`: after `pass`, archive the trail (`audit-<k>`), verify the archive, then remove
     `.fabrikant/relay/<N>/` from the branch with a delete commit, so the PR contains only the change. Link and
     hash are in the status comment and in a block at the end of the PR body (archive link, ref, `AUDIT.md`,
     hash, verification command; fabrikant only replaces the text between the `fabrikant-relay-trail` markers and
     removes it when a later session restores the trail).
5. A human signs off: spot-check the audit in the target system, open a PR from the branch for code, close the
   issue.

## Intervening

| Situation | What to do |
|---|---|
| Checkpoint / question (`relay: blocked`) | Read the status comment, amend the brief if needed, remove the label |
| Stall, failed runs (no `retry`, class not in `retry.on`, `retry.max` exhausted), `max_sessions` | Find the cause (run history: search `relay #N`), raise `max_sessions` if needed, remove the label |
| Brief edited by someone else | Review it, then set the label `fabrikant-relay` again (= approval of that version) |
| Audit `fail` | Read `AUDIT.md`; remove the label → the next session works through the findings |
| Continue after an archived trail | Remove the labels `relay: done`/`relay: audited`; the next session restores the trail from the archive (hash check) |
| Continue yourself | Check out the branch, work per `prompts/relay/session.prompt.md`, update the handoff, push |
| Stop | `fabrikant pause <project> relay-dispatch` or remove the label `fabrikant-relay` |

## Failed runs

A run whose `publish` did not complete is a failure. Without `relay.retry` it blocks the task immediately. With
`relay.retry` (same semantics as run retry, ADR 0036) only `interrupted`/`transient` failures (failure classes,
ADR 0035) are retried after the backoff, at most `max` additional times; if the run reports a `retry_after` (e.g.
a rate-limit reset), the retry waits until then, at most `max_wait`. The relay steps report their class through
the exit code (75 = `transient`: network, GitHub 5xx, rate limit; 78 = `environment`: 401/403, wrong
`bot_login`). `unknown` always blocks.

## Limits

- Without `FABRIKANT_SANDBOX=1` (ADR 0054) agents run as the OS user. The harness checks prevent drift, not
  intent, which is why fabrikant only executes briefs from `allowed` logins.
- Local files per run: `.fabrikant-session/` is the exchange directory with the agent (brief, `SESSION.md`, audit
  working state); the sandbox carries it into the container and back, except `audit/work/` (listed in
  `.fabrikant-session/.fabrikant-local`). Plan, handoff snapshot and check-in live in `.fabrikant-harness/` and
  never reach the agent in the sandbox.
- Read-only access for the auditor is provided by the project; fabrikant cannot enforce it.
- The auditor can only check the invariants. It reports unspecified transformations; a human has to judge them.

## Configuration (ADR 0037)

For tasks that don't fit into a single run (historical import, migration, analysis over days). An issue with the
label `fabrikant-relay` is the assignment. It runs in many short sessions, the handoff file lives on the branch
`fabrikant/relay/<N>`. On `done`, an independent, initially blind audit checks the result. Enable per project:

```yaml
relay:
  allowed: [alice]             # required: only these logins may approve briefs (label + body)
  forbid: [infrastructure/**]  # paths that sessions never commit
  builder: {model: ...}        # sessions (github_identity dev)
  reviewer: {model: ...}       # audit (github_identity reviewer, read-only)
  bot_login: my-builder-app    # required: read the status comment only from this identity; commit author
  retry: {max: 3, backoff: [5m, 30m, 2h], "on": [interrupted, transient], max_wait: 24h}  # like run retry (ADR 0036); off by default
  trail_archive: {type: git-ref}  # after audit pass, archive the trail + remove it from the branch (git-ref | local)
  # optional: repo, base, label, dispatch (cron), max_session_minutes, verify_timeout,
  #           audit_timeout, audit: false
```

From this fabrikant generates the loops `relay-dispatch` (cron, no agent), `relay-session` and `relay-audit`.
A human unblocks (checkpoint, stall, changed `## Verify`) by removing the label `relay: blocked`. The dispatcher
needs `FABRIKANT_URL` (default `http://127.0.0.1:8321`) and, if applicable, `FABRIKANT_TOKEN` for
`POST /api/runs`.

**Trail archive** (ADR 0037, addendum 2026-11-03): with `trail_archive`, `audit-publish` stores the trail
(`.fabrikant/relay/<N>/`) immutably after `pass` (`git-ref`: hidden ref `refs/fabrikant/relay/<N>/audit-<k>`;
`local`: `FABRIKANT_DATA/relay-archive/`), checks the archive against the trail hash and only then removes the
trail from the branch with a delete commit. The PR diff and merge thus contain only the actual change. The
verdict is bound to the hash (last line of `AUDIT.md`, status comment). If a later session or a repeated audit
needs the trail, it is restored from the archive (hash check, otherwise the step fails). Without `trail_archive`
the trail stays on the branch; the status comment warns as soon as a PR from the branch exists.
