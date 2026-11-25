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

Spec changes are **propose-and-approve**: the agent sends a patch (`loops`, `remove_loops`, `description`,
`max_parallel`) plus the `_hash` from `get_spec`; fabrikant validates it against the schema, refuses stale
hashes and stores a proposal. A human reviews and applies it with `fabrikant proposals list|show|apply|reject`
(atomic write, previous file kept as `<spec>.bak`, comments preserved). `apply` prints the patch and needs
`--yes` to proceed, since an applied patch can add shell steps. Only specs inside the projects dir can change.
`--read-only` affects only the MCP server; `fabrikant proposals` is the human side and is not gated by it.

Anything returned to the agent (`get_run`, `list_runs`, `get_run_log`, `list_approvals`) is scrubbed: values of
secret-looking keys are masked and secret patterns (bearer tokens, API keys, `key=value`, `scheme://user:pw@host`)
are redacted in strings. This is best-effort pattern matching, not a guarantee.
