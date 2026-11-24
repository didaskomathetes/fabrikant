# Migrating from fabrik to fabrikant

See [ADR 0070](adr/0070-rename-to-fabrikant.md). Existing installs keep working after the upgrade
(legacy `FABRIK_*` variables, `*.fabrik.yaml` specs, `fabrik.env` and `state/fabrik.db` are still read,
with a deprecation warning); new names win when both exist.

1. `pip uninstall fabrik && pip install fabrikant` (Docker: `ghcr.io/<owner>/fabrikant`).
2. Run `fabrikant` instead of `fabrik`; MCP clients: `fabrikant-mcp`.
3. Rename at your pace: `FABRIK_*` → `FABRIKANT_*`, `fabrik.env` → `fabrikant.env`,
   `*.fabrik.yaml` → `*.fabrikant.yaml`, `.fabrik/fabrik.yaml` → `.fabrikant/fabrikant.yaml`.
4. systemd: replace `deploy/fabrik*.service|timer` with the `fabrikant*` units and `daemon-reload`.
5. Plugins: entry-point groups are `fabrikant.steps` / `fabrikant.hosting_provider`; `import fabrikant.sdk`.
6. Webhook senders: send `X-Fabrikant-*` headers (the old `X-Fabrik-*` ones are still accepted).
7. Target repos: relay refs moved from `refs/fabrik/relay/*` to `refs/fabrikant/relay/*`; finish or
   discard running relay tasks before upgrading.
8. Per-user files outside the repo are not migrated automatically: move `~/.config/fabrik/` to
   `~/.config/fabrikant/` (GitHub App keys, `deploy.env`) and update paths in your unit drop-ins.
