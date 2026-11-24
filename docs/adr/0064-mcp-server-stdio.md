# ADR 0064: MCP Server — fabrik as a stdio Endpoint for MCP-Capable Agents

**Status:** Accepted (2026-11-21); proposed on the `feat/mcp-server` branch
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

### Transport: hand-rolled stdio, zero extra dependencies

The server speaks **MCP protocol `2025-03-26`** over **stdio** using
newline-delimited **JSON-RPC 2.0** (one JSON object per line on stdin/stdout). It is
implemented directly in `fabrik/mcp_server.py` — **no `mcp` package, no new runtime
dependency** — to match the "local software factory" ethos (self-hosted, zero extra
deps, always available). The only stdlib used is `json`/`sys`.

- Entry points: `fabrik mcp` (CLI subcommand) and the `fabrik-mcp` console script.
- The process is **long-lived**: the MCP client starts it and keeps the pipe open,
  exchanging lines. Run/approval actions spawn worker threads inside this process
  exactly like the HTTP server does, so the process must stay alive while a triggered
  run executes. If the client kills it, the next fabrik server start reaps the
  orphaned runs (`reap_orphaned_runs`).

### Supported methods

| Method | Response | Notes |
|---|---|---|
| `initialize` | protocolVersion, capabilities, serverInfo | returns `2025-03-26` |
| `notifications/initialized` | none | notification (no `id`) |
| `ping` | `{}` | |
| `tools/list` | tool array (name, description, inputSchema) | 12 tools |
| `tools/call` | `{content, structuredContent, isError}` | dispatches to a handler |
| unknown | JSON-RPC error `-32601` | |
| malformed line | JSON-RPC error `-32700` | parse error |

Notifications (requests without `id`) produce no response. A tool that raises a
domain error returns an **`isError: true`** result (not a JSON-RPC error) so the agent
sees the message in-band.

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

`approve_run`/`reject_run` reuse the **same atomic, audited resume path** the HTTP
`/api/approve` and `/api/reject` routes use (`_chat_resume`), so MCP approvals are
indistinguishable from UI approvals in the run record (except `actor=mcp`).

### Tool results

Every `tools/call` returns both a human-readable `content[0].text` (pretty JSON) and a
machine-readable `structuredContent` (the raw object), so agents can parse either.

## Consequences

- New module `fabrik/mcp_server.py`; new CLI subcommand `mcp`; new console script
  `fabrik-mcp`.
- No new runtime dependency; no change to the spec format, the engine, or the store.
- Agents can drive fabrik end-to-end (read specs → start a run → approve it) without
  HTTP.
- The MCP endpoint is **unauthenticated by design** (stdio, local trust boundary,
  same as the CLI). Remote exposure is out of scope and would go through the HTTP
  API's auth (ADR 0030), not this server.

## Alternatives considered

- **The `mcp` Python package (FastMCP):** fastest to write, but adds a runtime
  dependency and pulls a web-framework stack for a 100-line protocol. Rejected to keep
  the dependency surface flat.
- **Expose MCP over the existing HTTP server (a `/mcp` route):** couples the agent
  transport to the web server's lifecycle and auth; stdio is the MCP-native,
  process-local fit for a local factory. Rejected.
- **A read-only MCP server (no `start_run`/`approve_run`):** safer, but defeats the
  point — an agent that can only watch the factory is not an operator. Write tools are
  included and audited like the UI's.
