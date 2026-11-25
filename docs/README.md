# Documentation Index

**Start here:** [`roadmap.md`](roadmap.md) — the living plan: what fabrikant has
shipped (per ADR), what is in flight, and what is open.

Current documentation for the fabrikant framework. Architecture decisions are in
[`adr/`](adr/README.md). 
## Living

| Document | Role |
|---|---|
| [roadmap.md](roadmap.md) | The plan: P0→P3 phases with a status per item (shipped / in-flight / open / deferred / dropped), the first wins, and known contradictions. |
| [architecture.md](architecture.md) | Overview: principles, building blocks, where each topic is documented. |
| [configuration.md](configuration.md) | Env variables, spec sources, `vars`, loop templates, GitHub identity/signed commits, deployment profile. |
| [spec-reference.md](spec-reference.md) | Generated field reference of the spec (from the JSON schema). |
| [steps.md](steps.md) | All step types, `issue`/`review` details, `extra_context`, gates, approval, tiers, substitution. |
| [triggers.md](triggers.md) | Triggers, pause/disable, incident round trip, alerts (`notify`). |
| [runs.md](runs.md) | Run lifecycle, failure classes, verdict, retry, run store, session log. |
| [chat.md](chat.md) | Chat commands, LLM providers, named connections. |
| [api.md](api.md) | HTTP API routes. |
| [mcp.md](mcp.md) | MCP server on stdio (ADR 0064): client config, opt-in tools, spec proposals. |
| [auth.md](auth.md) | Local users, roles, OIDC login. |
| [security.md](security.md) | Security controls summary (see also [threat-model.md](threat-model.md)). |
| [cli.md](cli.md) | CLI command reference. |
| [operations.md](operations.md) | systemd units, auto-update, worker pool, parallel runs and worktrees. |
| [development.md](development.md) | Dev setup, tests, lint gate. |
| [backends.md](backends.md) | Agent backends (`opencode`, `claude_code`, `codex`, `copilot`): interface, transports, models, permissions, auth, sandbox/egress layers. |
| [ci.md](ci.md) | GitHub CI workflow (`test`, `package`, `browser` jobs; `release.yml` for tags), secrets handling, artifact-leak rules. |
| [plugins.md](plugins.md) | Installing fabrikant (pip/Docker), `fabrikant init`, writing step plugins (ADR 0069). |
| [releasing.md](releasing.md) | Release process: tag-driven PyPI + GHCR publish (ADR 0069/0070). |
| [relay.md](relay.md) | Relay tasks: spec config, how to write a brief, the session/audit flow, and how to intervene. |
| [sonarqube.md](sonarqube.md) | Local SonarQube setup, `sonar-scan.sh` usage (incl. PR mode), PR comments via fabrikant loops. |
| [threat-model.md](threat-model.md) | Threat model behind the security controls. |
| [screenshots/](screenshots/) | UI screenshots. |
