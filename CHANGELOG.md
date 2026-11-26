# Changelog

All notable changes to fabrikant are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/). Releases are described in [docs/releasing.md](docs/releasing.md).

## [Unreleased]

### Added

- Loop option `exclusive_group`: loops of the same group never run at the same time for the same issue/PR (e.g. a
  review loop and a fix loop on one PR). A trigger run blocked this way is dropped like one over `max_parallel`
  (with `reconcile` it fires again on the next poll); queued manual runs and resumes wait. A run paused for
  approval keeps holding its issue/PR for the whole group.
- `claude_code` agent steps: `tools` is the hard limit on the available built-in tools (`--tools`);
  `allowed_tools` sets pre-approval rules (`--allowedTools`) and runs the CLI in `dontAsk` mode, denying calls
  that would prompt (calls needing no approval and `settings.json` approvals still run). Both keys accept a list
  or a comma-separated string; copilot's `tools` now accepts a list too.

## [0.1.0] - 2026-11-25

First public release.

### Added

- Local software factory: agentic development loops defined as YAML specs, run with gates, steered from a web UI
  or chat (see [README](README.md) and [docs/](docs/README.md)).
- Community files: code of conduct, issue and PR templates, Dependabot, CODEOWNERS.

[Unreleased]: https://github.com/didaskomathetes/fabrikant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/didaskomathetes/fabrikant/releases/tag/v0.1.0
