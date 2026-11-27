# ADR 0034: Typed run model + relational storage (SQLite / Postgres)

**Status:** Accepted (2026-10-21)
**Implementation:** #274 (steps #275–#279, follow-ups #280, #281)
**Affects:** replaces ADR 0002 in the role "run JSON = source of truth, session log =
projection". Touches ADR 0015, 0019, 0028, 0030 and PR #215 (run history).

## Context

A run is an untyped `dict` that engine, scheduler, server, chat, CLI and session log each
read and write according to their own idea. In the 2 244 records under `runs/`, 9 fields are
always present, 11 only sometimes; steps carry further fields depending on their type. `status` is a free
string without defined transitions, substates (`approval_queued`, `waiting_step ==
"tier-approval"`) are implicit. Records are overwritten completely (last writer wins),
approve/reject check and write without atomicity, queries read all files. Run JSON and
`sessionlog.db` hold the same data twice.

A different storage medium alone (such as JSON columns in a DB) does not solve this.

## Decision

1. **Runs are typed.** Run, step, approval, usage and review result become a domain model
   of dataclasses with enums for all status values, independent of persistence. No code
   outside the store works with run `dict`s any more.
2. **State machine.** Allowed transitions are defined once and enforced:
   run `running → needs_approval → running → pass | fail | error`, `needs_approval → rejected`,
   reaper `running | needs_approval → error`. Approval `pending → (queued →) approved | rejected`;
   `approval_queued` and the tier gate thereby become real states.
3. **Transitions instead of record writes.** Writing happens through operations of a `RunStore`
   (start and finish run/step, request and decide approval, reap). Each one is a
   transaction with a precondition; a disallowed or stale transition fails.
4. **Strictly relational schema, no JSON columns in the core.** Tables for runs, steps,
   review results, approvals, run variables (key/value with type) and events, with constraints
   for status, mandatory fields and foreign keys. Only exception: a text column documented as
   opaque for foreign payloads at system boundaries (e.g. tool calls of the agent backends).
5. **Search directly on the run tables** with normal, indexed queries; no separate
   search index. The log text remains unsearchable for now. An index comes only on measured
   need, with its own ADR.
6. **Technology:** SQLAlchemy Core + Alembic, no ORM. SQLite in the homelab, Postgres in the cloud,
   switched via a DB URL.
7. **One source of truth:** The DB replaces run JSON and `sessionlog.db`. Run logs, secrets,
   specs and the agent state files in the worktree remain files.
8. **Migration:** one-off import of the existing runs with a dry-run report; JSON is archived,
   no dual write.

**Order** (one PR each): (1) model + state machine, persistence still JSON; (2) move engine, chat,
CLI, scheduler to model and store operations; (3) SQL store (SQLite) incl. session log
and search; (4) import and switchover; (5) Postgres.

## Not part of this ADR

- Multiple instances against one DB (run slots, scheduler, worktrees are host-local) → own ADR.
- Remaining state (`state` step, pause, branch overrides, users) → follows into the same store; the
  spec option `file:` of the `state` step needs its own decision.

## Consequences

- (+) Invalid runs are representable neither in code nor in the DB; concurrent transitions are atomic.
- (+) Indexed queries instead of file scans, one store instead of two.
- (−) Every new field needs a migration (intended).
- (−) New dependencies (SQLAlchemy, Alembic, psycopg); runs no longer readable via `cat`/`jq` → CLI export.
- (−) Step 2 touches large parts of the engine; effort approx. 2–3 weeks.
- (−) Records with an unknown status are not representable in the model and are therefore excluded from
  the search results (visible only via `load_report`); deliberate
  behaviour change since step 1 — include in the PR text of #277 (SQL store).

## Open questions

1. Persist steps as `running` at start (new) instead of only at the end?
2. Run variables: all scalar variables or only those needed for resume?
3. Postgres tests in every CI or only nightly?
