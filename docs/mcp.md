# MCP server (ADR 0064)

`fabrikant mcp` (or `fabrikant-mcp`) is a local stdio [MCP](https://modelcontextprotocol.io) server built on
the official Python SDK. It gives an MCP-capable agent (Claude Desktop, opencode, a coding agent) the
factory: list/inspect/validate specs, list runs and logs, start runs, queue status. No authentication:
the OS user is the identity (local profile); every action is audited as actor `mcp`. Secret-looking spec
values are redacted.

```json
{ "mcpServers": { "fabrikant": { "command": "fabrikant", "args": ["mcp"], "env": { "FABRIKANT_DATA": "/path/to/data" } } } }
```

(opencode: `"mcp": { "fabrikant": { "type": "local", "command": ["fabrikant", "mcp"] } }`. Without a global install:
`"command": "uv", "args": ["run", "--project", "/path/to/fabrikant", "fabrikant", "mcp"]`.)

Risky tools are opt-in (not even listed otherwise); `--read-only` wins over all of them:

| Flag (env) | Adds |
|---|---|
| `--read-only` (`FABRIKANT_MCP_READONLY=1`) | removes `start_run` and every opt-in tool |
| `--allow-approvals` (`FABRIKANT_MCP_ALLOW_APPROVALS=1`) | `approve_run`, `reject_run` |
| `--allow-spec-write` (`FABRIKANT_MCP_ALLOW_SPEC_WRITE=1`) | `propose_spec_change`, `list_spec_proposals` |
| `--spec-write-direct` (`FABRIKANT_MCP_SPEC_WRITE_DIRECT=1`, needs spec write) | `write_spec_change` (no human approval) |

Spec changes are **propose-and-approve**: the agent sends a patch (`loops`, `remove_loops`, `description`,
`max_parallel`) plus the `_hash` from `get_spec`; fabrikant validates it against the schema, refuses stale
hashes and stores a proposal. A human reviews and applies it with `fabrikant proposals list|show|apply|reject`
(atomic write, previous file kept as `<spec>.bak`, comments preserved). Only specs inside the projects dir can
change.
