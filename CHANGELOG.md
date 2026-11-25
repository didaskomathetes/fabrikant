# Changelog

All notable changes to fabrikant are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/). Releases are described in [docs/releasing.md](docs/releasing.md).

## [Unreleased]

### Added

- `fabrikant validate` warns about spec keys the schema doesn't know (`⚠ <project>: unknown spec key <path>: <key>`).
  The engine ignores them, so a typo or a key from a newer fabrikant version would otherwise silently have no
  effect. `x-` keys and keys a step plugin declares in its schema are exempt (steps of a plugin without a schema
  are not checked); the exit code is unchanged.
- The spec schema documents the backend step options `sandbox` (codex), `tools` (copilot) and `username`/`password`
  (opencode).

## [0.1.0] - 2026-11-25

First public release.

### Added

- Local software factory: agentic development loops defined as YAML specs, run with gates, steered from a web UI
  or chat (see [README](README.md) and [docs/](docs/README.md)).
- Community files: code of conduct, issue and PR templates, Dependabot, CODEOWNERS.

[Unreleased]: https://github.com/didaskomathetes/fabrikant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/didaskomathetes/fabrikant/releases/tag/v0.1.0
