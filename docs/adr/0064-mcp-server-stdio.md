# ADR 0064: MCP Server — fabrik as a stdio Endpoint for MCP-Capable Agents

**Status:** Accepted (2026-11-21); proposed on the `feat/mcp-server` branch.
Amendment 2026-11-22: scope clarified as the *local* profile; hardening and opt-in approvals added;
hosted profile deferred to a future ADR.
Amendment 2026-11-23: the transport is the official MCP Python SDK instead of hand-rolled JSON-RPC
(reverses the original "zero extra dependencies" decision); spec-write tools added (opt-in).
**Betrifft:** ADR 0034 (run store), ADR 0030 (auth/roles), ADR 0061 (replay)

## Context

Roadmap #132 ("Spec as a standard") wants fabrik's runs, specs and approvals to be
reachable from *any* MCP-capable agent (Claude Desktop, opencode, a coding agent, a
custom orchestrator). Today those capabilities are only reachable through the HTTP
API + web UI (ADR 0030) or the `fabrik` CLI. An external agent would have to speak
raw HTTP, handle sessions/CSRF, and parse HTML-free JSON — none of which is what an
agent harness expects.

The Model Context Protocol (MCP) is the de-facto stdio/JSON-RPC convention for
exposing tools to agents. Giving fabrik a first-class MCP server means an agent can
list projects, read a spec, inspect runs, and approve/reject a pending run using the
same primitives the UI uses — no HTTP, no auth handshake, no scraping.

## Decision

### Transport: the official MCP Python SDK over stdio

*Amendment 2026-11-23.* The first version hand-rolled MCP (protocol `2025-03-26`, newline-delimited
JSON-RPC over stdio) to keep fabrik free of extra dependencies. That is reversed: the server is built
on the **official `mcp` Python SDK** (a normal, core dependency; the exact version range is pinned in
`pyproject.toml`). Reasons:

- **Agents must connect reliably.** A hand-rolled server has to track every protocol revision itself and
  was never tested against a real client; the SDK is what clients are tested against.
- **Features we would otherwise hand-roll:** tool annotations (`readOnlyHint`, `destructiveHint`, ...)
  that clients use to decide what to auto-approve, typed tool arguments, output schemas and structured
  results, protocol version negotiation, and the HTTP transports and OAuth integration needed by the
  hosted profile.
- The cost is a heavier dependency tree (Pydantic, Starlette, uvicorn, httpx, ...). Accepted: MCP is a
  headline feature of fabrik, and the dependency is the official, maintained one.

The transport for the local profile is stdio.

- Entry points: `fabrik mcp` (CLI subcommand) and the `fabrik-mcp` console script.
- The process is **long-lived**: the MCP client starts it and keeps the pipe open,
  exchanging messages. Run/approval actions spawn worker threads inside this process
  exactly like the HTTP server does, so the process must stay alive while a triggered
  run executes. If the client kills it, the next fabrik server start reaps the
  orphaned runs (`reap_orphaned_runs`).

### Profiles

This server is the **local profile**: stdio, no authentication, the OS user is the identity.
A **hosted profile** needs a different transport (streamable HTTP), per-client
authentication, an identity in the audit actor instead of the constant `mcp`, and RBAC
reusing the ADR 0030 roles (`viewer` read tools, `operator` start/approve/reject, `admin`
the rest). It must refuse to start without authentication, as `auth.check_bind` does for the
HTTP server. The hosted profile is out of scope here; the official MCP SDK, now used for the local profile too, is the
basis for it (it provides both transports and an OAuth 2.1 resource-server integration).
See `docs/threat-model.md`.

### Protocol handling

Handshake, version negotiation, `ping`, `tools/list`, `tools/call`, error mapping and malformed
input are handled by the SDK. A tool that raises a domain error returns an **`isError: true`** result
so the agent sees the message in-band. Tool visibility depends on flags (see *Tools*): 10 tools by
default.

### Tools (runs, specs, approvals)

Each handler wraps an existing fabrik primitive — no new business logic, only a
thin adapter. Actions are audited with actor `mcp`.

| Tool | Wraps | Category |
|---|---|---|
| `list_projects` | `specs.load_projects` | specs |
| `get_spec` | `load_projects` + `spec_hash` | specs |
| `get_raw_spec` | `specs.load_spec_raw` + `spec_hash` | specs |
| `validate_spec` | `specschema.validate_spec` (the #131 JSON schema, on the *expanded* spec) | specs |
| `list_runs` | `runs.list_runs` (+ status/limit filter) | runs |
| `get_run` | `runs.get_run` | runs |
| `get_run_log` | `runs.get_run_log` (+ `lines` tail) | runs |
| `start_run` | `engine.start_priority_run` (trigger `mcp`) | runs |
| `queue_status` | `engine.pool_status` + `runs.load_records` | runs |
| `list_approvals` | `runs.pending_approvals` | approvals |
| `approve_run` | `chat._chat_resume(…, approve=True)` | approvals |
| `reject_run` | `chat._chat_resume(…, approve=False)` | approvals |
| `propose_spec_change` | spec-change proposal flow of the chat (full spec or patch + base `spec_hash`; validated by `specschema.validate_spec`) | spec-write |

**Approvals are opt-in.** `approve_run` and `reject_run` are offered (and callable) only with
`--allow-approvals` or `FABRIKANT_MCP_ALLOW_APPROVALS=1`. Approval is the human gate, and locally
the caller is a model that may be prompt-injected; it must not reach the gate by default.
`--read-only` (or `FABRIKANT_MCP_READONLY=1`) additionally hides `start_run` and wins over
`--allow-approvals`. Hidden tools are refused at call time, not merely unlisted.

**Spec writes are opt-in and approval-gated.** An operator agent that can start and approve runs but
cannot create or edit the loops they run is only half an operator, so spec-write tools exist
(`propose_spec_change`). They are offered and callable only with `--allow-spec-write` or
`FABRIK_MCP_ALLOW_SPEC_WRITE=1`; `--read-only` wins. A spec controls what runs, including tiers, shell
steps and sandbox/egress settings, so a prompt-injected agent with write access is a larger risk than
one that can only start runs. Therefore:

- the **default is propose-and-approve**: the change is validated against the JSON schema, refused if
  invalid or if its base `spec_hash` is stale, and then waits for a human approval through the same
  proposal and approval path the chat UI uses; nothing is written before that approval;
- a **direct write** (no human gate) is a further explicit opt-in (`--spec-write-direct` or
  `FABRIK_MCP_SPEC_WRITE_DIRECT=1`); it writes atomically, keeps a backup of the previous file and is
  audited with actor `mcp`;
- writes are confined to the projects directory, and secrets are never echoed back in results.

Every tool carries MCP **tool annotations** (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint=false`) so clients can prompt for the dangerous ones and auto-approve the read tools.

`approve_run`/`reject_run` reuse the **same atomic, audited resume path** the HTTP
`/api/approve` and `/api/reject` routes use (`_chat_resume`), so MCP approvals are
indistinguishable from UI approvals in the run record (except `actor=mcp`).

### Hardening

- **stdout is reserved for protocol messages.** The real stdout is kept for replies and
  `sys.stdout` points at stderr, so `print()` calls in the engine cannot corrupt the stream.
- **Failures are contained.** A failing request or tool yields a protocol error or an
  `isError` result; it never ends the long-lived process.
- **Conformance is the SDK's job**, and fabrik proves it with a contract test: the SDK's own client
  spawns `fabrik mcp` over stdio, lists tools (annotations present; opt-in tools hidden by default)
  and calls a read tool. This runs in CI.
- **Lifecycle:** orphaned runs are reaped at startup (like the HTTP server); on stdin EOF,
  in-flight run workers are drained for up to 10 minutes.
- **Inputs and outputs:** `limit`/`lines` are validated, `get_run_log` returns a bounded
  tail (flagged `truncated`), secret-looking keys (token, password, api key, ...) are redacted
  and the spec `_path` is dropped in `get_spec`/`get_raw_spec`. Approve/reject failures are
  tool errors, not success-shaped results.

### Tool results

Every `tools/call` returns both a human-readable `content[0].text` (pretty JSON) and a
machine-readable `structuredContent` (the raw object), so agents can parse either.

## Consequences

- New module `fabrik/mcp_server.py`; new CLI subcommand `mcp`; new console script
  `fabrik-mcp`.
- New core dependency: the official `mcp` SDK (see *Transport*); no change to the spec format, the engine, or the store.
- Agents can drive fabrik end-to-end (read specs → start a run → approve it) without
  HTTP.
- The MCP endpoint is **unauthenticated by design in the local profile** (stdio, local
  trust boundary, same as the CLI). Residual risk: a prompt-injected agent that has the
  server configured could start runs; approvals and spec writes are therefore opt-in (and spec writes
  are approval-gated by default). Remote exposure
  requires the hosted profile (see *Profiles*), not this server as it stands.

## Alternatives considered

- **Hand-rolled JSON-RPC over stdio (the original decision):** zero extra dependencies and about
  600 lines, but it tracks protocol revisions by hand, has no tool annotations or output schemas, and
  was never verified against a real client. **Rejected on 2026-11-23** in favour of the official SDK;
  reliable connection of coding agents outweighs the lean dependency tree.
- **Expose MCP over the existing HTTP server (a `/mcp` route):** couples the agent
  transport to the web server's lifecycle and auth; stdio is the MCP-native,
  process-local fit for a local factory. Rejected.
- **A read-only MCP server (no `start_run`/`approve_run`, no spec writes):** safer, but defeats the
  point — an agent that can only watch the factory is not an operator. Write tools are
  included and audited like the UI's; read-only is available as a flag, and the
  approval and spec-write tools are opt-in (see *Tools*).
