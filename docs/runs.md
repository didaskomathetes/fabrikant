# Runs

A run is a record in the run store (status, steps, variables, approvals) plus a human-readable live log
`runs/<project>/<id>.log`. Step output is capped at 200 KB. Status: `queued`, `running`, `needs_approval`, `pass`,
`fail`, `error`, `rejected` (`pass`, `fail`, `error`, `rejected` are terminal). The raw `.log` is served via
`GET /api/run/<id>/log` and shown on demand in the run panel via the log button (toggle between the formatted
record view and the raw log). The run panel also has a **Trace** tab: a chronological step timeline from the
session log (`GET /api/run/<id>/events`) showing each step's status, duration and usage (tokens/cost), with agent
events (prompt / tool call / message) as drill-down items and the run's artifacts listed at the end (issue #128).

**Single node:** on start, `fabrikant serve` sets every `running` record to `error` (restart recovery,
`needs_approval` stays). Two server processes sharing a `runs/` directory are therefore not supported.

Agent steps write their **conversation** to the log: the prompt (input, truncated), the assistant texts and tool
calls (live, per completed message) as well as the final text (output). This makes it traceable after a run what
the agent was given, what it did and what it answered.

`POST /api/runs` returns `run_id` **before** the worker starts — the UI (`web/app.js`) then polls via
`GET /api/run/<id>` (avoids the start race in which the new run wasn't yet visible in the history).
`POST /api/approve`/`/api/reject` returns `resumed` (bool): `true` only if a resume worker was started for the
run (e.g. after an approval gate); `false` if the run is already running or finished. Integrators should read
`resumed` defensively (`if (r && r.resumed)`) — older clients without the field stay compatible (additive API
change).

## Failure classes (ADR 0035)

Failed runs (`fail`/`error`) and steps carry `failure_kind`; the UI shows it as a badge (loop list, history, run
modal), the history filters by it.

| Class | Meaning | Examples |
|---|---|---|
| `interrupted` | aborted from outside | server restart (reaper), dead-man's switch |
| `transient` | external dependency briefly gone | exit 75 (`EX_TEMPFAIL`), network error, HTTP 429/5xx (except 501/505), `git fetch` without network, agent backend unreachable, hanging session |
| `environment` | environment/configuration broken — needs a human | exit 126/127/78 (`EX_CONFIG`), missing file, invalid step configuration, project/loop unknown, HTTP 401/403, token missing |
| `outcome` | genuine negative result | tests red (`failure_kind: outcome` on the step), review `changes_requested`, merge rejected |
| `unknown` | not classified (default, also legacy data) | everything else |

Classification happens at the source (`src/fabrikant/failures.py`): steps report the class in the result or raise
typed errors; text patterns exist only as a fallback for foreign CLI output (agent CLIs, `git`). With
`transient`, a run can carry `retry_after` (UTC), e.g. from `Retry-After`. The class doesn't change the flow
behavior; auto-retry builds on it (ADR 0036).

## Failure summary and log excerpt

A failed shell step's summary (`exit <code>: <reason>`) names the last line of its output (stdout and stderr) that
carries a failure marker: a pytest `FAILED`/`ERROR` line or `N failed` summary, a traceback, `Fatal Python error`,
an `error:` line (uv, ruff, compilers) or a `rot:` verdict of `scripts/ci-matrix-pytest.py`. Without a marker
it stays the last output line. The run log shows a failure-aware excerpt of the step output: a window around each
failure marker plus the last lines, at most 120 lines, with the left-out lines stated (`… (N lines)` and a
closing `[excerpt: …; full output in the run record]`). The run record keeps the full output.

## Verdict (business result)

The run status says whether the machinery ran; a step can still report a negative *result* — e.g. `relay-audit`:
`audit-publish` runs green and publishes `fail`. A shell step reports this with a dedicated output line
`::fabrikant-verdict::<word>` (a–z, `_`, `-`, max. 20 characters). The line disappears from output and summary
and lands as `verdict` in the step record; the run's verdict is that of the last step that reported one. The UI
shows it as a badge next to the status (list, history, run detail, step): `pass`/`done` green, `fail`/`blocked`
red, otherwise neutral. Flow, retry and alerts stay untouched. Relay reports `pass`/`fail` (audit) or the
handoff status (`active`/`blocked`/`done`, session).

## Run retry (ADR 0036)

Failed runs are optionally repeated automatically — one **retry chain** with backoff per
`(project, loop, trigger)`. Off by default; enabled via a `retry:` block at project or loop level (the loop
replaces the project block completely, `max: 0` turns it off):

```yaml
retry:
  max: 3                      # additional attempts after the first run
  backoff: [5m, 30m, 2h]      # wait before attempt 1..n (the last one keeps applying)
  on: [interrupted, transient]  # only these classes are retried
  max_wait: 24h               # upper bound for a reported `retry_after`
```

Short network blips (DNS, dropped connections) usually never reach the run retry: GitHub/GitLab API calls
and `git fetch`/`push`/`pull` retry themselves first, with growing waits (5 s, 15 s, 30 s, 60 s; about 2 minutes
in total, `config.NET_RETRY_DELAYS_S`). Only a network error that outlasts these waits fails the step as
`transient`.

Only classes in `on` (always a subset of `interrupted`/`transient`) trigger a retry;
`unknown`/`outcome`/`environment` end the chain. Every attempt passes all gates again (approval, blockers) and
fetches fresh trigger variables from GitHub. If the chain exhausts the budget, the job is listed in the UI under
**Shelved** (class, last error, attempt count) with the actions **retry now** (run with the full budget; without
a free slot it waits with priority like other manual runs) and **discard**. A running cron tick is skipped as long
as the chain is `pending`; `reconcile` still counts every run as done. A closed issue or a merged/closed PR ends
the chain (`dismissed`) — unless the `on_pr` trigger itself fires on closed PRs (`states: [.., closed]`); a
restart during an attempt consumes it (`interrupted`). As long as the chain is `pending`, there is no
`run_failed`; the final failure arrives as `run_exhausted` — also with `events: [run_failed]`.

**One-off inventory reconciliation** (`fabrikant retry-backfill`, idempotent): creates a chain for triggers whose
*entire* runs failed (inventory from before `retry` was introduced) — if the class of the last run is in `on` of
the loop's policy → `pending` (starts on the next scheduler tick after the backoff; a `retry_after` of the last
run counts, capped at `max_wait`), every other class → `exhausted` (lands in the "Shelved" list for review). The
old runs don't count against the budget — the first attempt is attempt 1 with the full budget. Whether the
trigger still exists (issue closed, PR merged) is not checked by the reconciliation: the attempt queries GitHub on
the first tick and then files the chain as `dismissed`. Existing chains and groups with non-failed runs stay
untouched.

## Run store (ADR 0034)

Runs are read and written only via the run store (`fabrikant.runstore.STORE`); the backend is selected by
`FABRIKANT_RUN_STORE`:

- `sql` (default): relational schema (SQLAlchemy Core + Alembic) under `FABRIKANT_DB_URL`, default
  `sqlite:///$FABRIKANT_HOME/state/fabrikant.db` (WAL, `busy_timeout`, foreign keys). Tables for runs, steps,
  usage, review results, verify checks, approvals, run variables and the session log (`run_events`,
  `run_artifacts`, `run_checkpoints`); the DB enforces status, required fields and foreign keys. The schema
  migrates to `head` automatically on first access (`src/fabrikant/migrations`). Run search is an indexed
  `SELECT`. Run logs (`runs/<project>/<id>.log`) stay files.
- `json`: the former storage `runs/<project>/<id>.json` (fallback, tests).

Existing run JSON and `sessionlog.db` are taken over by `fabrikant migrate-runs` (first `--dry-run`: writes
nothing and lists every record that can't be mapped or that differs, with run ID and reason; idempotent). After a
real import, the imported run JSON is moved to `state/runs-archive/` (never deleted; layout like `runs/`) —
broken or differing records stay in `runs/`. Sessions without a run record (e.g. test leftovers) are counted, not
imported.

In the SQL store, runs are no longer readable via `cat`/`jq`: `fabrikant runs show <run_id>` shows a single run
(fields, steps, session log counts), `fabrikant runs export <run_id>` prints record + session log as JSON.

## Session log (SQLite, ADR 0002)

In the SQL store (default) the session log lives in the run store (see above); what follows describes the JSON
operation.

Next to the run JSONs there is an append-only event store: `$FABRIKANT_HOME/sessionlog.db` (override:
`FABRIKANT_SESSIONLOG`), WAL mode. Tables: `sessions` (one row per run), `events`
(`run_start`/`step_start`/`step_done`/`tool_call`/`message`/`cost`/`approval`/`run_end`), `artifacts`,
`checkpoints` (only `pass` steps).

The run JSON stays the single source of truth for the engine — the log is a durable, queryable projection (raw
data: tokens + cost from the agent, no price table). Agent steps additionally land tool-call events per newly
seen tool. Existing P0 runs are imported via `fabrikant import-runs` (idempotent, `source='import'` — native
sessions win). Log errors never fail a run (best effort).
