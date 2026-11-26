# ADR 0030: Authentication + authorization (local users, roles)

**Status:** Accepted (2026-10-21, merge #248; proposed 2026-10-20)
**Affects:** README principle "local, 127.0.0.1", security assumptions (issue #159). Complements ADR 0019 (roles).

## Context

So far only an optional shared bearer token (`FABRIKANT_TOKEN`) plus an Origin check protects the
`POST` routes; `GET` (runs, logs, specs) is open. A shared server lacks users,
roles and an audit of who started/approved what.

## Decision

- **Modes** (`FABRIKANT_AUTH`): `none` (default, backward compatible: GET open, POST as before
  Origin + optional token), `local` (users + roles). `oidc` is follow-up issue #160; the value
  is rejected until then.
- **Bind:** Default `127.0.0.1` (`--host` / `FABRIKANT_HOST`). A non-loopback bind with `none`
  is refused at startup (`auth.check_bind`). No TLS in the process: operate behind a reverse proxy,
  set `FABRIKANT_COOKIE_SECURE=1` there.
- **Users:** `state/users.json` (0600), password as `hashlib.scrypt` (n=2^14, r=8, p=1,
  16-byte salt, format `scrypt$n$r$p$salt$hash`), minimum length 8. Management via CLI
  `fabrikant user add|passwd|role`. An unknown user costs the same hashing time (no
  user oracle). No rate limit/lockout in this step (follow-up issue, reverse proxy).
- **Sessions:** server-side in memory (lost on restart), 256-bit random ID,
  absolute lifetime `FABRIKANT_SESSION_TTL` (default 8 h). Cookie `fabrikant_session`:
  `HttpOnly; SameSite=Strict; Path=/`. The role is read from the store on every request —
  role change/deletion take effect immediately; a password change ends the sessions (in the same process).
- **CSRF:** Every session has a token (response of `/api/login`, `/api/session`). Writing
  requests with session auth need the header `X-CSRF-Token`; the Origin check remains in addition.
  Token callers (no cookie) are exempt.
- **Roles** (ascending): `viewer` (all `GET`), `operator` (+ start runs, approve/reject,
  pause, chat), `admin` (+ LLM provider, branch switching, spec `apply` in chat; manage
  users via CLI). Per-project roles are deliberately not part of this step.
  Only the UI files, `/api/health`, `/api/login`, `/api/session`
  and the webhook (own HMAC auth, ADR 0027) remain public.
- **API token:** `FABRIKANT_TOKEN` counts as `admin` (name `token`) in both modes for CLI/automation.
  Per-user tokens: later.
- **Audit:** The run record gets `started_by`, `approved_by`, `rejected_by` (user name, `token`,
  `cli:<os-user>` or `webhook`; empty for trigger runs and mode `none` without a token).
  The pause/resume audit names `user:<name>`.

## Consequences

- Mode `none` changes nothing in the existing behaviour.
- Shared-server operation is possible, but without TLS/rate limit it only makes sense behind a reverse proxy.
- New routes must fit into `auth.required_role` (default: writing = `operator`).
