# Changelog

All notable changes to fabrikant are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/). Releases are described in [docs/releasing.md](docs/releasing.md).

## [Unreleased]

### Added

- `claude_code` agent steps: `tools` is the hard limit on the available built-in tools (`--tools`);
  `allowed_tools` sets pre-approval rules (`--allowedTools`) and runs the CLI in `dontAsk` mode, denying calls
  that would prompt (calls needing no approval and `settings.json` approvals still run). Both keys accept a list
  or a comma-separated string; copilot's `tools` now accepts a list too.

### Security

- `claude_code` steps with `tools` or `allowed_tools` no longer load the working directory's project/local
  settings, `.mcp.json` or project/user hooks (`--setting-sources user --strict-mcp-config`, hooks disabled;
  administrator-managed hooks still apply): a PR under review can no longer run commands through its own
  `.claude/settings.json`. On hosts with an enterprise `managed-mcp.json` Claude Code refuses
  `--strict-mcp-config`, so such steps fail as a configuration error (`environment`).

## [0.1.0] - 2026-11-25

First public release.

### Added

- Local software factory: agentic development loops defined as YAML specs, run with gates, steered from a web UI
  or chat (see [README](README.md) and [docs/](docs/README.md)).
- Community files: code of conduct, issue and PR templates, Dependabot, CODEOWNERS.

[Unreleased]: https://github.com/didaskomathetes/fabrikant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/didaskomathetes/fabrikant/releases/tag/v0.1.0
