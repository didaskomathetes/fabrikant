# Security controls

Summary of the controls in the code. The map of trust boundaries and gaps is the [threat model](threat-model.md);
decisions are in the ADRs. Agent-process isolation (env allowlist, egress proxy, sandbox) is in
[backends.md](backends.md#security-layers-for-agent-processes).

- The server binds to **127.0.0.1 only by default**; other addresses only with `FABRIKANT_AUTH=local` or `oidc`
  ([auth](auth.md))
- The API is hardened against cross-origin: `POST` requests are accepted only from the own origin (no
  `Access-Control-Allow-Origin: *`), so that an arbitrary web page in the operator's browser can't fire shell
  steps (drive-by RCE). Optional: `FABRIKANT_TOKEN` enforces a bearer token for all `POST` endpoints
- Pushing to `main`/`master` is hard-refused (the spec's `protected:` list is legacy and informational only)
- Substituted branch/head names (`git`/`pr` step) are validated before git/API calls (`validate_branch_name`:
  allowlist `[A-Za-z0-9._/-]`, no `..`, no leading `-`, max length)
- No step can read outside `workdir`/`WEB_DIR` (path traversal check); `report.write` paths must stay inside the
  project root
- LLM steps inject only schema keys into the ctx (no overwriting of trusted variables — untrusted input stays
  data)
- Secrets are masked from logs **and** run records (env + `fabrikant.env` + spec env values); git error messages
  are redacted
- Webhook/alert intake is HMAC-signed with replay protection ([triggers](triggers.md))
- SonarQube: use only the cached token from `state/sonar-token.txt` (`state/` and `fabrikant.env` are
  gitignored); `SONAR_ADMIN_PASSWORD` serves exclusively to mint a fresh token — never write it to the repo,
  issues, commits or logs
- Spec changes from the LLM chat are written only after `apply`
- Agent containment: file tools confined to the workdir; shell access requires a non-root/container deployment
  for real isolation
