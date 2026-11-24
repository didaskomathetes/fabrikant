# Authentication and roles (ADR 0030)

`FABRIKANT_AUTH=none` (default): loopback bind only. `FABRIKANT_AUTH=local`: login with local users
(`state/users.json`, scrypt), session cookie (HttpOnly, SameSite=Strict, `FABRIKANT_SESSION_TTL` seconds, default
28800) and a CSRF header `X-CSRF-Token` on writing requests. Roles: `viewer` (read), `operator` (+ start runs,
approve/reject, pause, chat), `admin` (+ LLM providers, branch switch, spec `apply`). `FABRIKANT_TOKEN` stays as
an API token (= admin, without CSRF) for CLI/automation. Binding to a non-loopback address
(`fabrikant serve --host 0.0.0.0` / `FABRIKANT_HOST`) without `local` or `oidc` is refused; TLS via a reverse
proxy (+ `FABRIKANT_COOKIE_SECURE=1`).

```bash
export FABRIKANT_AUTH=local
fabrikant user add anna operator        # the password is prompted for (or --password-stdin)
fabrikant user passwd anna
fabrikant user role anna admin
```

Run records contain `started_by`, `approved_by`, `rejected_by`.

## OIDC login (`FABRIKANT_AUTH=oidc`, ADR 0046)

Sign-in via a central IdP (authorization code flow + PKCE, `state`/`nonce`, ID token validation via JWKS). Needs
the extra: `pip install 'fabrikant[oidc]'` (`uv sync --extra oidc`); without it `fabrikant serve` aborts with a
message. Register the redirect URI `https://<host>/api/oidc/callback` at the IdP (https required, `http` only for
loopback) and create a confidential client with a secret. The `state` is additionally bound via a short-lived
cookie (`fabrikant_oidc_state`, `SameSite=Lax`) to the browser that started the login (protection against login
CSRF). Logging out ends only the fabrikant session, not the one at the IdP; the UI doesn't automatically
redirect to the IdP again afterwards.

| Variable | Meaning |
|---|---|
| `FABRIKANT_OIDC_ISSUER` | Issuer URL (discovery: `<issuer>/.well-known/openid-configuration`) |
| `FABRIKANT_OIDC_CLIENT_ID` / `_CLIENT_SECRET` | Client credentials |
| `FABRIKANT_OIDC_REDIRECT_URI` | must be registered at the IdP |
| `FABRIKANT_OIDC_ROLE_MAP` | `group=role,...` (roles `viewer`/`operator`/`admin`; the highest wins) |
| `FABRIKANT_OIDC_GROUPS_CLAIM` | Groups claim (default `groups`) |
| `FABRIKANT_OIDC_SCOPES` | Default `openid profile email` |

Anyone not in a mapped group is rejected (403). Examples:

```bash
# Keycloak (group mapper "Group Membership" → claim "groups", full group path off)
export FABRIKANT_OIDC_ISSUER=https://kc.example.com/realms/acme
export FABRIKANT_OIDC_CLIENT_ID=fabrikant FABRIKANT_OIDC_CLIENT_SECRET=...
export FABRIKANT_OIDC_ROLE_MAP="fabrikant-admins=admin,fabrikant-ops=operator"

# AWS Cognito (issuer = user pool: https://cognito-idp.<region>.amazonaws.com/<pool-id>)
export FABRIKANT_OIDC_ISSUER=https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_AbCdEf
export FABRIKANT_OIDC_GROUPS_CLAIM=cognito:groups
export FABRIKANT_OIDC_ROLE_MAP="fabrikant-admins=admin,fabrikant-ops=operator"

# Entra ID (enable the groups claim in the app registration; groups arrive as object IDs)
export FABRIKANT_OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
export FABRIKANT_OIDC_GROUPS_CLAIM=groups
export FABRIKANT_OIDC_ROLE_MAP="11111111-aaaa-...=admin,22222222-bbbb-...=operator"
```
