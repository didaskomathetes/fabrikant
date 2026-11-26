# ADR 0005: opencode backend — dual support 1.x/2.x

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-17)

## Context

The `opencode` backend (SPIKE: `docs/spike-opencode.md`) was built against the
`opencode serve` HTTP API of **1.19**. opencode **2.x** changed the API in
breaking ways (verified 2026-10-17 against 2.0.32 + OpenAPI spec):

- **Auth:** HTTP basic auth is mandatory — the server generates a
  password if `OPENCODE_SERVER_PASSWORD` is not set (visible only in the
  server log).
- **Paths:** everything under `/api/*` (`/session` → `/api/session`,
  `prompt_async` → `prompt`, `abort` → `interrupt`, `/api/health` →
  `/api/info`).
- **Session create:** `permission` → `permissions`, rule fields
  `permission/pattern/action` → `action/resource/effect`, workdir via
  `location: {directory}` in the body (instead of `?directory=` per request),
  response in a `data` wrapper.
- **Prompt:** `{"parts": [...]}` → `{"text": "..."}`.
- **Messages:** paginated (`{data, cursor}`), flat shape
  (`type`, `content`, top-level `tokens/cost/finish/error/time`) instead of the
  `{info, parts}` envelope.

Existing deployments run on 1.x; 2.x is the current release branch.

## Decision

**One adapter, two dialects.** `OpenCodeBackend` detects the server
version lazily (cached): `GET /api/info` → 2.x, otherwise
`GET /api/health` → 1.x. Transport + shape normalization live in
a private `_ServeApi` class (dialect `1`/`2`); `poll()` stays
version-agnostic because 2.x messages are normalized into the 1.x envelope
(`{info, parts}`) (`_normalize_v2`).

- **Auth:** `FABRIKANT_AGENT_PASSWORD` (optional `FABRIKANT_AGENT_USERNAME`,
  default `opencode`), also as step override `password:`/`username:`.
  1.x harmlessly ignores the header. 2.x without a password → unhealthy with
  a hint in the error message.
- **Stayed stable** (hence a single `poll()` logic): `finish` values,
  tool `state.status` (`completed`/`error`/`running`), `tokens.input/output`,
  `cost` as a number.
- **Not** chosen: pinning the version (1.x is no longer developed),
  two separate adapters (duplicate status logic), a client SDK
  (a dependency for 5 endpoints).

## Consequences

- Deployment on 2.x: set `OPENCODE_SERVER_PASSWORD` on the serve process
  + `FABRIKANT_AGENT_PASSWORD` in `fabrikant.env` (same value).
- 2.x binds the workdir to the session (`location`) — `?directory=` per
  request no longer exists; irrelevant for fabrikant (session per step).
- When 1.x reaches EOL: remove dialect `1` + the related tests.
