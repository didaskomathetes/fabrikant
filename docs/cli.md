# CLI reference

`fabrikant` is the console script from `pyproject.toml`. From a source checkout use `uv run fabrikant ...` or
`python -m fabrikant ...`. `fabrikant <command> --help` is authoritative; global option:
`--profile homelab|cloud` (or `FABRIKANT_PROFILE`, see [configuration](configuration.md#deployment-profile-adr-0056)).

| Command | Purpose |
|---|---|
| `init [DIR]` | Scaffold a project directory (starter spec, `fabrikant.env`, plugin skeleton; ADR 0069) |
| `serve [--port N] [--host H]` | Engine + UI + scheduler on `127.0.0.1:8321` |
| `list` | Projects and loops |
| `validate [--offline] [SPEC...]` | Check specs; `--offline` skips the host-dependent `root` check (CI) |
| `project add <repo-path>` | Register a target repo that carries `.fabrikant/fabrikant.yaml` |
| `run <project> <loop> [--note "..."] [--var K=V]` | Start a run |
| `runs [--project X] [--limit N]` | List runs |
| `runs show <run_id> [--project X]` | One run: fields, steps, session log counts |
| `runs export <run_id> [--project X]` | One run + session log as JSON |
| `approve <run_id>` / `reject <run_id>` `[--project X]` | Resume or abort a `needs_approval` run |
| `pause` / `disable <project> [loop] [--until ISO]`, `resume <project> [loop]` | [Pause/disable](triggers.md#pause--disable-adr-0019) |
| `replay <run_id> [--project X] [--yes]` | Deterministic re-execution (ADR 0061) |
| `golden capture\|show`, `eval <project> <loop>` | Golden-run baselines and regression check (ADR 0065) |
| `chat "..."` | [Chat](chat.md) command or LLM copilot |
| `alert test [--project X]` | Test alert channels |
| `user add\|passwd\|role <name> [role]` | Local users ([auth](auth.md)) |
| `mcp [--read-only] [--allow-approvals]` | MCP server on stdio (ADR 0064); also `fabrikant-mcp` |
| `fetch <url> ...` | Research fetcher with injection scan (ADR 0060) |
| `agent-env` | Print the scrubbed env for a shared `opencode serve` ([backends](backends.md#security-layers-for-agent-processes)) |
| `egress start\|stop\|status` | Egress proxy for agent containers (ADR 0051) |
| `migrate-runs [--dry-run]`, `import-runs`, `retry-backfill` | One-off data migrations ([runs](runs.md)) |

Exit codes of `golden`/`eval`: 0 ok / no regression, 1 regression, 2 error or inconclusive.
