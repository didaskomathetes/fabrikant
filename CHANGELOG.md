# Changelog

All notable changes to fabrikant are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/). Releases are described in [docs/releasing.md](docs/releasing.md).

## [Unreleased]

### Added

- `fabrikant validate` warns about spec keys the schema doesn't know (`⚠ <project>: unknown spec key <path>: <key>`).
  The engine ignores them, so a typo or a key from a newer fabrikant version would otherwise silently have no
  effect. `x-` keys and keys a step plugin declares in its schema (literal or via `patternProperties`) are exempt
  (steps of a plugin without a key list or with open `additionalProperties` are not checked); the exit code is
  unchanged.
- The spec schema documents the backend step options `sandbox` (codex), `tools` (copilot) and `username`/`password`
  (opencode).
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

### Security

- `claude_code` steps with `tools` or `allowed_tools` no longer load the working directory's project/local
  settings, `.mcp.json` or project/user hooks (`--setting-sources user --strict-mcp-config`, hooks disabled;
  administrator-managed hooks still apply): a PR under review can no longer run commands through its own
  `.claude/settings.json`. On hosts with an enterprise `managed-mcp.json` Claude Code refuses
  `--strict-mcp-config`, so such steps fail as a configuration error (`environment`).

### Fixed

- Network blips (DNS, dropped connections) no longer fail a run whose work is done: GitHub/GitLab API calls and
  `git fetch`/`push`/`pull` retry transient errors with growing waits (about 2 minutes) before the step fails as
  `transient` ([run retry](docs/runs.md#run-retry-adr-0036)). A write POST (new issue, comment, GraphQL mutation) is
  replayed only if it never reached the server, so a lost answer cannot create a duplicate; git blob/tree uploads
  and GraphQL queries are always replayed.
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
- `scripts/pr-status.py`: `gh` calls ride out network blips (reads retry on any network error, writes only when no
  connection was made); a persisting network error exits with 75 (`transient`). Escalation counts verdicts and fix
  rounds only since the last escalation, so resuming an escalated PR grants a fresh budget.
- `scripts/ci-matrix-pytest.py`: pytest dying from a signal is a crash, not a red result — the entry is rerun once
  and a lasting crash exits with 75 (`transient`) instead of 1.

### Changed

- The source distribution contains only what the package build needs (`src/fabrikant`, `README.md`,
  `LICENSE` and project metadata). Docs, tests, CI and deployment files are no longer shipped; get them from
  the repository.

## [0.1.0] - 2026-11-25

First public release.

### Added

- Local software factory: agentic development loops defined as YAML specs, run with gates, steered from a web UI
  or chat (see [README](README.md) and [docs/](docs/README.md)).
- Community files: code of conduct, issue and PR templates, Dependabot, CODEOWNERS.

[Unreleased]: https://github.com/didaskomathetes/fabrikant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/didaskomathetes/fabrikant/releases/tag/v0.1.0
