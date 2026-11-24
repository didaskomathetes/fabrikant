# Architecture overview

fabrikant defines agentic development loops as YAML specs, runs them locally and lets you steer them from a web
UI or a chat. One engine process (`fabrikant serve`) watches triggers and executes loops; the UI at
`http://127.0.0.1:8321` shows projects, loops, runs, live logs and a chat interface. Decisions live in
the [ADRs](adr/README.md); the plan is in the [roadmap](roadmap.md).

## Principles

- Local (127.0.0.1), no cloud backend, no frontend build step.
- **Python 3.11+** (`requires-python` in `pyproject.toml`). Runtime dependencies: PyYAML, ruamel.yaml, jsonschema,
  SQLAlchemy + Alembic (run store), cryptography (connection secrets). The UI is vanilla JS. One Python package
  (`src/fabrikant/`).
- The conductor is deterministic code, not an LLM; agents are replaceable workers ([backends](backends.md)).
- Git projects run in **isolated worktrees**; the main checkout is never touched ([operations](operations.md#parallel-runs-and-worktrees-adr-0028)).
- **Never push to protected branches** (`main`/`master` are hard-blocked); results go out as a branch + PR.
- One project = one spec; one loop = ordered steps; one run = a record in the run store + a log ([runs](runs.md)).
- **Autonomy with gates:** loops run on their own; only steps marked `approval: true` (or tier 2+ loops) pause
  the run until a human approves ([steps](steps.md#gates-approval-and-tiers)).

## Building blocks

| Concept | Where to read |
|---|---|
| Spec format, sources, `vars`, loop templates | [configuration.md](configuration.md), [spec-reference.md](spec-reference.md) |
| Step types (`shell`, `git`, `pr`, `agent`, `llm`, `review`, `issue`, ...) | [steps.md](steps.md) |
| Triggers (cron, `on_issue`, `on_pr`, webhook, alert, Slack), pause/disable, alerts | [triggers.md](triggers.md) |
| Agent backends and sandboxing | [backends.md](backends.md) |
| Runs, failure classes, retry, run store, session log | [runs.md](runs.md) |
| Relay tasks (multi-session work) | [relay.md](relay.md) |
| Chat, LLM providers, connections | [chat.md](chat.md) |
| HTTP API | [api.md](api.md) |
| Authentication, roles, OIDC | [auth.md](auth.md) |
| Security controls and threat model | [security.md](security.md), [threat-model.md](threat-model.md) |

## Components

The diagram in the [README](../README.md#how-it-fits-together) maps to code as follows. Each substrate-specific
piece sits behind a port, so deployments (local, server, cluster) differ only in which adapter is registered.

| Component | Role | Code |
|---|---|---|
| Scheduler | cron, file, issue/PR and Slack triggers | `scheduler.py`, `webhook.py` |
| Conductor | deterministic loop engine, approval gates, retries | `engine.py`, `approval.py` |
| Run launcher (port) | executes run work; in-process worker pool today, k8s/ECS later | `launcher/` |
| Runner | steps in an isolated git worktree | `steps/`, `gitops.py` |
| Agent backend (port) | replaceable CLI/server agents | `backends/` |
| Sandbox | optional per-run agent container (`FABRIKANT_SANDBOX=1`) | `sandbox.py` |
| Egress proxy | default-deny allowlist for agent traffic (ADR 0051) | `egress.py` |
| Run store | JSON or SQL backend, session logs | `runstore.py`, `runstore_sql.py` |
| Code host (port) | GitHub, GitLab or registered provider | `hosting/` |

## Usage in practice

fabrikant runs several private projects in production, for example a CMS migration (issue-to-PR loop with
approval gate and failure alerts), a game project (lint/test loops) and a meeting pipeline. Their specs stay
with each project (`FABRIKANT_DATA` or `.fabrikant/fabrikant.yaml` in the repo).
