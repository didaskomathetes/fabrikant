# Changelog

All notable changes to fabrikant are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/). Releases are described in [docs/releasing.md](docs/releasing.md).

## [Unreleased]

### Fixed

- Network blips (DNS, dropped connections) no longer fail a run whose work is done: GitHub/GitLab API calls and
  `git fetch`/`push`/`pull` retry transient errors with growing waits (about 2 minutes) before the step fails as
  `transient` ([run retry](docs/runs.md#run-retry-adr-0036)).
- A failed step no longer hides its cause behind a noisy tail: the run log shows a failure-aware excerpt (windows
  around pytest failures, tracebacks, crashes and verdict lines, plus the tail; cuts are stated), and a failed
  shell step's summary names the last failure-marker line instead of the last stdout line, which could be a
  green partial result ([failure summary](docs/runs.md#failure-summary-and-log-excerpt)). Over-long output of a
  failed shell step keeps the windows around failure markers, its start and its end in the run record instead of
  only the start.
- An agent session that exhausts the model's context window is no longer retried with the same input: the run log
  warns to rescope the work and the step fails as `outcome` instead of `unknown`/`transient`
  ([backends](docs/backends.md)).
- A failed `codex` session reports its `ERROR:` lines (e.g. a spend cap or auth error) as the failure text instead of
  the first 300 characters of stderr, which were mostly banner and prompt
  ([codex](docs/backends.md#codex-openai-codex-cli)).

## [0.1.0] - 2026-11-25

First public release.

### Added

- Local software factory: agentic development loops defined as YAML specs, run with gates, steered from a web UI
  or chat (see [README](README.md) and [docs/](docs/README.md)).
- Community files: code of conduct, issue and PR templates, Dependabot, CODEOWNERS.

[Unreleased]: https://github.com/didaskomathetes/fabrikant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/didaskomathetes/fabrikant/releases/tag/v0.1.0
