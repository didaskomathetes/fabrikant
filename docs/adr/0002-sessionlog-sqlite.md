# ADR 0002: Durable session log in SQLite (end schema)

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-17)
**Partially superseded by:** ADR 0034 (2026-10-21) — run JSON is no longer the source of truth and the session log no longer a projection; both are absorbed into the relational run store. The event schema remains the foundation.
**Issue:** #14

## Context

P0 stores runs as JSON files (`runs/<project>/<id>.json`). That is stateless
and right for homelab operation — but there is no queryable history,
no gapless event ordering and no agent telemetry (tool calls,
cost per session). The framework draft defines the phase boundary:
"Run JSON remains an importable subset of the P1 session-log schema — the schema is
defined in P1 (as the P2 end schema)."

Goal: a SQLite session log that carries the **end schema** in P1. P2 then swaps
only the storage backend (Postgres = SQLite + server), not the semantics.

## Decision

**One SQLite file as an append-only event store** next to the run JSONs. The
run JSON remains the single source of truth for the engine (stateless workers,
resume logic untouched). The session log is a **durable, queryable
projection** — not a second storage with its own semantics (same
architectural logic as the tracing exporter: export raw data, don't fork).

### Schema (v1)

```sql
CREATE TABLE sessions (
    session_id   TEXT PRIMARY KEY,          -- = run ID
    project      TEXT NOT NULL,
    loop         TEXT NOT NULL,
    trigger      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,             -- running|pass|fail|error|needs_approval
    spec_hash    TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,             -- ISO-8601 UTC
    finished_at  TEXT,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0,   -- raw data from the agent (no price table!)
    source       TEXT NOT NULL DEFAULT 'native'  -- native | import
);

CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    seq         INTEGER NOT NULL,           -- order within the session
    ts          TEXT NOT NULL,              -- ISO-8601 UTC
    kind        TEXT NOT NULL,              -- run_start|run_end|step_start|step_done|
                                            -- tool_call|message|cost|approval
    step_name   TEXT NOT NULL DEFAULT '',
    step_type   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{}'  -- JSON, redacted
);
CREATE INDEX idx_events_session ON events(session_id, seq);

CREATE TABLE artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    step_name   TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,              -- report | prompt | output
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE checkpoints (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    step_idx    INTEGER NOT NULL,
    step_name   TEXT NOT NULL,
    status      TEXT NOT NULL,              -- only 'pass' is checkpointed
    data        TEXT NOT NULL DEFAULT '{}', -- JSON: duration, usage, summary
    PRIMARY KEY (session_id, step_idx)
);
```

### Event kinds

| kind | Source | data |
|------|--------|------|
| `run_start` | engine at run start | `{"loop", "trigger"}` |
| `run_end` | engine at run end | `{status}` |
| `step_start` | engine before step dispatch | `{type}` |
| `step_done` | engine after step | `{status, duration_s, usage?}` |
| `tool_call` | agent step per new tool | `{tool}` |
| `message` | agent step at session end | `{text}` (capped) |
| `cost` | agent step at session end | `{tokens_in, tokens_out, cost_usd}` |
| `approval` | engine at gate pause | `{step}` |

### Invariants

1. **Append-only:** events/checkpoints/artifacts are never updated, only
   inserted. `sessions` is finalized via `UPDATE` (status, finished_at,
   token/cost totals) — the only mutable row per session.
2. **Redaction on the write path:** all `data` blobs and `message` texts go
   through `secrets._redact` before they are persisted.
3. **Raw data, no calculation:** `cost_usd` comes from the agent backend
   (opencode `cost` field). No price table in fabrik — an exporter computes
   prices from the raw data (tokens + model + provider).
4. **P0 run JSONs remain importable:** `fabrik import-runs` reads
   `runs/*.json` and projects them (sessions + step_done events +
   checkpoints, `source='import'`, totals backfilled from step usage).
   The import is idempotent: existing sessions are not overwritten
   (native runs win). Best effort: connect error → 0, broken
   files are skipped, commit per file.
5. **No engine dependency:** the log is best effort — a log failure
   (e.g. broken DB) does not fail a run. The run JSON remains the
   authoritative state.

### Storage

- Paths: `FABRIK_HOME/sessionlog.db` (override: `FABRIK_SESSIONLOG`)
- WAL mode (parallel reads while runs write)
- One global connection + lock (homelab scale: few runs, a lot of I/O
  would be oversized). P2: connection pool, then Postgres.

## Consequences

+ Queryable history (SQL) across all runs, including P0 runs via the importer
+ Agent telemetry (tool calls, cost) without a backend change to polling
+ Checkpoints as the P2 basis for worker resume (a stateless worker pulls the
  last checkpoint, not the whole JSON)
+ The schema is Postgres-compatible (no SQLite-specific types) → P2
  storage swap without a schema migration
- A second persistence file next to the run JSONs (deliberate redundancy:
  projection vs. source of truth)
- WAL files (`sessionlog.db-wal/-shm`) in `FABRIK_HOME` — backup scripts
  should back up the DB file with `sqlite3 .backup`, not with `cp`

## Alternatives (rejected)

- **Events directly in the run JSON:** no sequential queryability,
  file size explodes on agent runs (hundreds of tool calls), the resume path
  becomes more complicated.
- **Postgres from the start:** P2 infrastructure (server) for a P1 feature.
  SQLite is the end path minus the server — the draft's decision.
- **OTel collector as the log:** a daemon per homelab machine for telemetry
  that can be projected from the run data anyway. Exporters (OTel/Langfuse)
  come later **in addition**; they do not replace this log.
