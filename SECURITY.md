# Security policy

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
(**Security → Report a vulnerability** on this repository). Include the affected version, a description and, if
possible, a minimal reproduction. We aim to acknowledge reports within a few days.

## Scope

fabrikant runs agents that execute code on your machine. The security controls and the threat model
(prompt injection, secret exposure, egress, sandboxing) are documented in [docs/security.md](docs/security.md) and
[docs/threat-model.md](docs/threat-model.md). Reports about bypasses of those controls are especially welcome.

## Supported versions

Only the latest released version receives security fixes.
