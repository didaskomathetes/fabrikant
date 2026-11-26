# Changelog

All notable changes to fabrikant are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/). Releases are described in [docs/releasing.md](docs/releasing.md).

## [Unreleased]

### Added

- `scripts/pr-status.py`: new subcommands `review-check` (did the reviewer actually post a marker review for the
  head?), `sync-base` (keep a PR on its base via `update-branch`, conflict label), `reply-context` / `post-replies`
  (host-side gate and posting for the pr-reply loop: reply drafts with `re=` markers, checked issue references,
  follow-up issues labelled `fabrikant-followup`) and `issue-candidates` (issue lookup list for sandboxed agents).
  `post-review --sha` lets the host build the verdict marker from a `verdict` field; forged fabrikant markers in the
  body are stripped. `new-comments` gains `--ignore-bots`/`--keep-bot`, skips machine-reviewer receipts and treats
  comments named in reply markers as answered (the `--state` file is optional). Non-fatal problems are reported on
  stderr as `::fabrikant-warning::<text>`.
- `scripts/sonar_issues.py`: converts Pyright and ShellCheck JSON into one SonarQube generic issue report (rule IDs
  `<tool>:<rule>`); replaces `scripts/pyright_sonar.py`.
- `claude_code` agent steps: `tools` is the hard limit on the available built-in tools (`--tools`);
  `allowed_tools` sets pre-approval rules (`--allowedTools`) and runs the CLI in `dontAsk` mode, denying calls
  that would prompt (calls needing no approval and `settings.json` approvals still run). Both keys accept a list
  or a comma-separated string; copilot's `tools` now accepts a list too.
- `on_pr` trigger `reconcile: updated`: reconciles per observed PR state (`updated_at` at poll time), so pushes,
  comments, reviews and label changes fire a run, also across a server restart — one run per observed snapshot:
  several changes between two polls collapse into one run. New variable `{pr_updated_at}`.
- `on_pr` reconcile keys (`on_pr:#N@<sha>`, `on_pr:#N@u<stamp>`) are deduplicated at execution time too (a
  delayed queued copy no longer runs twice; like for `on_issue`/`on_slack`, the record check now runs while
  holding the run claim, closing the gap where the earlier run finished between check and claim), and retry attempts carry the same key form as the retried run
  (`@u<stamp>` for `reconcile: updated`), so the next poll sees them. Plain `on_pr:#N` stays repeatable.
- Shell step markers: with the opt-in `done_marker: true`, a line `::fabrikant-done::` (exit 0) ends the run
  early and successfully (without the key the line is ordinary output, so test/lint output or echoed PR text
  cannot skip later gates); lines `::fabrikant-warning::<text>` report non-fatal problems (⚠ in the run log, `WARNING:` at the end of the output).

### Fixed

- `scripts/pr-status.py`: `gh` calls ride out network blips (reads retry on any network error, writes only when no
  connection was made); a persisting network error exits with 75 (`transient`). Escalation counts verdicts and fix
  rounds only since the last escalation, so resuming an escalated PR grants a fresh budget.
- `scripts/ci-matrix-pytest.py`: pytest dying from a signal is a crash, not a red result — the entry is rerun once
  and a lasting crash exits with 75 (`transient`) instead of 1.

## [0.1.0] - 2026-11-25

First public release.

### Added

- Local software factory: agentic development loops defined as YAML specs, run with gates, steered from a web UI
  or chat (see [README](README.md) and [docs/](docs/README.md)).
- Community files: code of conduct, issue and PR templates, Dependabot, CODEOWNERS.

[Unreleased]: https://github.com/didaskomathetes/fabrikant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/didaskomathetes/fabrikant/releases/tag/v0.1.0
