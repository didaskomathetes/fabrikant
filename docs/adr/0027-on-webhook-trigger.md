# ADR 0027: `on_webhook` trigger (HMAC, tunnel CORS)

**Status:** Accepted (2026-10-20, merge #189; proposed 2026-10-20; changed by ADR 0028; extended by ADR 0058)
**Complements:** `docs/framework-v1-draft.md` §1.3 (cut from P0, here P2).

## Context

The server binds to 127.0.0.1 and checks only Origin/an optional bearer token. A
tunnel (cloudflared) would make it public: `POST /api/runs` without an
`Origin` header (curl) passes the Origin check and starts shell steps.

## Decision

- **Route:** `POST /api/webhook/<project>/<loop>`; the loop needs an
  `on_webhook` trigger (`secret_env`, optional `map`).
- **Auth:** HMAC-SHA256 over the raw body in the header `X-Fabrikant-Signature:
  sha256=<hex>`, compared via `hmac.compare_digest`. Missing/wrong signature,
  unknown project/loop, no trigger or unset secret → uniformly
  **401** (no existence oracle); a run never starts. The route is exempt from
  Origin/`FABRIKANT_TOKEN` (callers are servers; HMAC replaces them).
- **Secret handling:** The spec names only the env name (`secret_env`), never the
  value. A separate secret per loop. No replay protection (timestamp/nonce) in
  this step — deliberately left out, follow-up issue if needed.
  *(Addendum 2026-11-18: replay protection is available since ADR 0058 as opt-in
  `replay_protection: true`; the omission now applies only to
  triggers without the flag.)*
- **Payload→variables:** `map: {variable: "dot.path"}` (dict keys, list index).
  Invalid JSON/not an object → 400, missing field → 422. Values are untrusted
  (§1.4), non-strings are serialized as JSON. Body max. 1 MB (413).
- **One code path:** The start goes through `FabrikantHandler._start_run` — the same
  function as `POST /api/runs` ("start a run with initial variables"); trigger
  `on_webhook`, response with `run_id`, 409 if the project lock is held.
- **CORS:** `FABRIKANT_WEBHOOK_ORIGIN` (e.g. `https://hooks.example.com`). Only on the
  webhook route and only for an exactly equal `Origin` is
  `Access-Control-Allow-Origin` set (+ preflight `OPTIONS`). Never `*`.
- **Tunnel exposure:** Requests with `Host` = tunnel host reach only
  `/api/webhook/*` (everything else 404). The tunnel must pass the original host
  through (cloudflared default). Without `FABRIKANT_WEBHOOK_ORIGIN` there is no
  block and no CORS; the operator must then not open the tunnel.

## Consequences

Webhooks are usable only after setting secret and origin. A secret leak
allows runs for exactly this loop (tier/approval gates still apply).
