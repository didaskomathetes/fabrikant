# Changelog

All notable changes to fabrikant are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/). Releases are described in [docs/releasing.md](docs/releasing.md).

## [Unreleased]

### Added

- `on_pr` trigger `reconcile: updated`: reconciles per observed PR state (`updated_at` at poll time), so pushes,
  comments, reviews and label changes fire a run, also across a server restart — one run per observed snapshot:
  several changes between two polls collapse into one run. New variable `{pr_updated_at}`.
- `on_pr` reconcile keys (`on_pr:#N@<sha>`, `on_pr:#N@u<stamp>`) are deduplicated at execution time too (a
  delayed queued copy no longer runs twice), and retry attempts carry the same key form as the retried run
  (`@u<stamp>` for `reconcile: updated`), so the next poll sees them. Plain `on_pr:#N` stays repeatable.
- Shell step markers: with the opt-in `done_marker: true`, a line `::fabrikant-done::` (exit 0) ends the run
  early and successfully (without the key the line is ordinary output, so test/lint output or echoed PR text
  cannot skip later gates); lines `::fabrikant-warning::<text>` report non-fatal problems (⚠ in the run log, `WARNING:` at the end of the output).

## [0.1.0] - 2026-11-25

First public release.

### Added

- Local software factory: agentic development loops defined as YAML specs, run with gates, steered from a web UI
  or chat (see [README](README.md) and [docs/](docs/README.md)).
- Community files: code of conduct, issue and PR templates, Dependabot, CODEOWNERS.

[Unreleased]: https://github.com/didaskomathetes/fabrikant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/didaskomathetes/fabrikant/releases/tag/v0.1.0
