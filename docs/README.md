# Documentation Index

**Start here:** [`roadmap.md`](roadmap.md) — the living plan: what fabrik has
shipped (per ADR), what is in flight, and what is open.

Current documentation for the fabrik framework. Architecture decisions are in
[`adr/`](adr/README.md). Historical and one-off documents live in
[`archive/`](archive/) (each carries a pointer note at the top).

## Living

| Document | Role |
|---|---|
| [roadmap.md](roadmap.md) | The plan: P0→P3 phases with a status per item (shipped / in-flight / open / deferred / dropped), the first wins, and known contradictions. |
| [backends.md](backends.md) | Agent backends (`opencode`, `claude_code`, `codex`, `copilot`): interface, transports, models, permissions, auth. |
| [ci.md](ci.md) | GitHub CI workflow (`test`, `browser` jobs), secrets handling, artifact-leak rules. |
| [relay.md](relay.md) | Relay tasks: how to write a brief, the session/audit flow, and how to intervene. |
| [sonarqube.md](sonarqube.md) | Local SonarQube setup, `sonar-scan.sh` usage, PR comments via fabrik loops. |
| [screenshots/](screenshots/) | UI screenshots. |

## Historical (archive)

| Document | Role |
|---|---|
| [archive/framework-v1-draft.md](archive/framework-v1-draft.md) | Historical v1.1 framework design draft (2026-10); the origin of the [roadmap](roadmap.md), which supersedes it as a plan. |
| [archive/review-pr10-pr11.md](archive/review-pr10-pr11.md) | One-off code review of PRs #10/#11 (2026-10); findings addressed in later versions. |
| [archive/spike-opencode.md](archive/spike-opencode.md) | Completed spike of `opencode serve` as agent backend (2026-10), incl. pitfalls and API references; referenced by [backends.md](backends.md). |
| [archive/investigations/2026-10-ai-software-factory/](archive/investigations/2026-10-ai-software-factory/README.md) | Completed desk research on AI software factory patterns, measured against fabrik (audited, 2026-10). |
