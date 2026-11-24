# HTTP API

Served by `fabrikant serve` (default `127.0.0.1:8321`). Additional routes exist (e.g. `GET /api/metrics`
Prometheus gauges, `/api/webhook/...`, `/api/alert/...` see [triggers.md](triggers.md), `/api/login` etc. see
[auth.md](auth.md)); the table lists the main ones.

| Route | Method | Description |
|-------|--------|-------------|
| `/api/projects` | GET | Projects + loops + last run + github/notify |
| `/api/schedule` | GET | Schedule view: all cron loops with next fire times, last run, state + scheduled one-time overrides; `queue` = the actual queue (running/waiting/approvals, worker pool, slot context) |
| `/api/schedule/onetime` | POST `{"project","loop","when","note"?}` | Schedule a one-time run (`when`: `15:05` · `in 10 min` · ISO-8601); replaces an existing entry per loop; the scheduler fires it (disabled → dropped, paused → runs) |
| `/api/schedule/onetime` | DELETE `{"project","loop"}` | Remove a scheduled one-time run |
| `/api/projects/<name>/spec` | GET | Full (expanded) spec + `_hash` (optimistic lock) |
| `/api/projects/<name>/spec?raw=1` | GET | Raw, unexpanded spec (vars verbatim, `use:`/`insert:` un-inlined, all loop keys) + `_hash` — draft source for the UI graph editor |
| `/api/projects/<name>/spec` | PUT `{"loops"?,"remove_loops"?,"description"?,"max_parallel"?,"base_hash"?}` | Spec edit (graph view): merge + delete loops per loop, set top-level fields; `base_hash` ≠ current hash → 409. Validation + atomic write, comments preserved |
| `/api/projects/<name>/branches` | GET | Available branches + current branch (ADR 0004) |
| `/api/projects/<name>/branch` | POST `{"branch"}` | Switch the target branch (override, ADR 0004) |
| `/api/projects/<name>/state` | POST `{"state","loop"?,"until"?}` | Set `active`/`paused`/`disabled` for a project (or loop) (ADR 0019) |
| `/api/runs?project=&limit=` | GET | Run list (newest first) |
| `/api/runs?project=&q=&from=&to=&status=&failure_kind=&loop=&cursor=&limit=` | GET | History: free text (run ID, loop, trigger, status, note, step summaries; not the log), date range, status/failure class (comma-separated)/loop; cursor-paginated → `{runs, next_cursor}` (ADR 0034, 0035) |
| `/api/runs` | POST `{"project","loop","note"}` | Start a run; if the project is running, it is queued (202, `queued: true`, record status `queued`) and starts with priority over trigger runs (ADR 0015). The response contains `run_id` |
| `/api/run/<id>` | GET | Run record (steps, output) + `cost`: token/cost sum from the step usages, broken down per step and backend; `unpriced_steps` > 0 = the backend reported no cost (the sum is a lower bound) |
| `/api/kpis/costs?project=&from=&to=` | GET | Cost rollup per project (runs, tokens, `cost_usd`, per backend) over the period (`from`/`to` as in the history, `to` as a date = the whole day) + total; the run list carries `cost` per row (issue #126) |
| `/api/kpis?project=&from=&to=` | GET | Factory KPIs ([roadmap](roadmap.md#factory-kpis-draft-5-the-definitions-fabrikant-aims-to-compute)) per project/loop + total: cycle time (start → accept, successful runs: avg/median/max), judge veto rate (runs with a review and `changes_requested`), defect escape (proxy: an outcome failure after an earlier successful run of the same loop = escaped; runs with a red `verify`/veto/outcome failure otherwise = caught). Also per group: `failed` (status `fail`/`error`), `runtime_s` (count/sum/mean/max over all finished runs) and `cost` (tokens, `cost_usd`, `unpriced_steps` as in `/api/kpis/costs`). Rates are `null` without data. The same values as Prometheus gauges `fabrikant_kpi_*{project,loop}` in `GET /api/metrics`; KPI panel in the web UI (issue #127) |
| `/api/run/<id>/log?lines=N` | GET | Raw run log as plain text (tail via `lines`) |
| `/api/run/<id>/stream?project=` | GET | Live log + status changes as server-sent events (`log`/`status`/`end`); reconnect via `Last-Event-ID` (byte offset); secrets redacted (ADR 0038) |
| `/api/run/<id>/transcript?after=&limit=` | GET | Agent turns (prompt, tool calls, answers) from the SessionLog, paginated (ADR 0038) |
| `/api/run/<id>/events` | GET | Full session log as `{events, checkpoints, artifacts}` (all event kinds, redacted `data` as a JSON string); works for legacy JSON and SQL runs. Backs the web UI trace view (issue #128) |
| `/api/chat` | POST `{"message","project","req_id"}` | Chat (command or LLM); answers with `req_id` |
| `/api/chat/cancel` | POST `{"req_id"}` | Cancel a running chat request (UI button) |
| `/api/approve` / `/api/reject` | POST `{"project","run_id"}` | Approval gate; the response contains `resumed` (bool) |
| `/api/llm` | GET | LLM providers (never the key) + active provider |
| `/api/llm` | POST `{"provider"}` | Set the active LLM provider (persisted) |
| `/api/health` | GET | Liveness |

All `POST` routes check the origin (own host) and — if `FABRIKANT_TOKEN` is set — the bearer token; otherwise
`403`. With `FABRIKANT_AUTH=local`, **all** `/api/` routes (including `GET`) check session or token according to
the role (`401`/`403`); additionally `/api/login` (POST), `/api/logout` (POST), `/api/session` (GET).

Run start and approval semantics (`run_id` before the worker starts, `resumed`): [runs.md](runs.md).
