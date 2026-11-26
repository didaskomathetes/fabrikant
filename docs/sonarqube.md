# SonarQube (local)

`compose.yaml` starts SonarQube Community **with the
[Community Branch Plugin](https://github.com/mc1arke/sonarqube-community-branch-plugin)**
(image `mc1arke/sonarqube-with-community-branch-plugin`): branch and PR analysis plus PR decoration on GitHub,
which the Community Edition lacks.

```bash
docker compose up -d        # http://localhost:9000, first login admin/admin
```

**Pin the version:** plugin version = SonarQube minor (e.g. plugin 26.5 ↔ SonarQube 26.5.x). SonarQube cannot
downgrade its database. On upgrade (`SONARQUBE_TAG`) also delete the volume `fabrikant_sonarqube_extensions`,
because it holds a copy of the plugin JAR.

## Scanning

`scripts/sonar-scan.sh` (tests + coverage → analyzer reports → pysonar → quality gate):

| Call | Analysis |
|---|---|
| `bash scripts/sonar-scan.sh` | main branch (loop `sonar-scan`, daily) |
| `SONAR_BRANCH=auto bash scripts/sonar-scan.sh` | current git branch, locally before pushing |
| `SONAR_PR=42 bash scripts/sonar-scan.sh` | PR #42 (run `gh pr checkout 42` first) |
| `SONAR_PR=auto bash scripts/sonar-scan.sh` | PR of the current branch |

`SONAR_REPORT_FILE=<path>` additionally writes the result (gate, metrics, open issues) as JSON, even when the gate
is red.

**Analyzer reports:** before the scan, `scripts/lint.sh --reports .fabrikant/sonar/lint` writes the findings of
Ruff, Bandit (SAST) and Pyright; Sonar shows them as external issues (they count as new issues for the quality
gate). Sonar-Python imports Ruff and Bandit natively (`sonar.python.{ruff,bandit}.reportPaths`); it doesn't know
Pyright, so `scripts/sonar_issues.py` converts to the generic issue format (`sonar.externalIssuesReportPaths`,
rule IDs `<tool>:<rule>`, e.g. `pyright:reportOptionalMemberAccess`; it also converts ShellCheck output via
`--shellcheck`).
Report findings don't fail the scan — the gate for that is plain `scripts/lint.sh` (CI, the `lint` step of
develop/refactor). "New code" of a branch/PR is computed against the analysis of the target branch, which is why
`sonar-scan` runs daily on `main`.

## PR comments (loops)

Without the GitHub Checks permission, via the existing GitHub apps (ADR 0010):

- **`sonar-pr`** (`on_pr`, poll every 30 min, debounced per head SHA: pushes within one interval produce a single
  scan of the latest state): PR analysis (`SONAR_PR`, head checkout in the script) → `scripts/sonar_report.py`
  renders report and comment from the API responses (template, no LLM) → `scripts/gh-upsert-comment.sh` posts it
  as the **reviewer app** (`pull_requests: write` is enough). Exactly one comment per PR (marker
  `<!-- fabrikant-sonar -->`), updated on every push. A red gate is commented first, then the run fails
  (`outcome`).
- **`token-refresh`** (every 30 min + `on_start`): renews the 1 h tokens of both apps while the fabrikant server
  is running.

The comment is a regular PR comment, not a review: it sets no verdict and doesn't block `review-pr`. `pr-reply`
sees it once per PR as a new comment.

### Alternative: plugin decoration

The branch plugin can decorate PRs itself (check run with annotations + summary comment). That needs a GitHub app
with **Checks: Read & write**; without it the check run fails and the comment is missing entirely. Setup:
`scripts/sonar-github-setup.sh` (documented in the script header). If you bind a project this way, disable
`sonar-pr`, otherwise both comment.

## Other projects

`sonar-scan.sh` scans any repo via environment: `SONAR_PROJECT_DIR` (worktree, e.g. `.`), `SONAR_PROJECT_KEY`,
`SONAR_SCANNER` (`{fabrikant_home}/.venv/bin/pysonar` if the project doesn't have pysonar as a dependency),
`SONAR_SCANNER_ARGS` (`-Dsonar.…`, replaces `[tool.sonar]`), `SONAR_TEST_CMD` + `SONAR_COVERAGE_PATHS`,
`SONAR_LINT_CMD` (writes `ruff.json`, `bandit.json`, `pyright-sonar.json` to `$SONAR_LINT_DIR`; without it, no
import for foreign projects). For uv monorepos with a CI test matrix, `scripts/ci-matrix-pytest.py --cov-dir D`
(`--all` = whole matrix, otherwise only packages affected by the diff) writes `D/coverage-<n>.xml`. If pytest dies
from a signal (e.g. SIGSEGV), the entry is rerun once; if it still crashes and nothing is red, the script exits
with 75 (`EX_TEMPFAIL` → failure class `transient`, run retry) instead of 1.

Example: the loops `sonar-main` (daily) and `sonar-pr` (opt-in via the PR label `sonar`, debounced, comment only
after approval) in a production spec (`FABRIKANT_PROJECTS_PATH`). `gh-upsert-comment.sh` recognizes its own comment
by the token's login (PAT) or the bot type (app token).

If the project ships its own scan script (own `sonar-project.properties`, linter imports), the loop calls it as
`SONAR_SCANNER` with `SONAR_TEST_CMD=true` and an empty `SONAR_COVERAGE_PATHS`. The script must pass its arguments
(`-Dsonar.branch.name` / `-Dsonar.pullrequest.*`) through to the scanner.

## PR mode: quality report in the review

`bash scripts/sonar-scan.sh --pr <n> [--post] [--base main]` scans the current workdir (PR head) under its own
key `<projectKey>:pr-<n>` (advisory; the main analysis stays untouched). A `coverage.xml` newer than the HEAD
commit is reused (the develop `test` step produces it). Instead of the gate, `scripts/sonar-pr-report.py` writes a
deterministic Markdown report to stdout: Sonar issues only on the changed lines, lint on the changed lines,
coverage of the changed lines (`diff-cover`), key figures; last line `<!-- fabrikant-quality sha=<head> -->`.
`--post` creates it as a single PR comment or updates it. As an `extra_context` source (`trusted: true`, see
[steps.md](steps.md#extra_context-all-llm-nodes)) it lands in every `pr_review` prompt; `review-pr` reads the
comment. Logs go to stderr.

## Orphaned legacy PR projects (ADR 0053)

The daily main scan also cleans up orphaned legacy PR projects (`scripts/sonar-pr-cleanup.py`): before native PR
analysis (#524), a separate project `<org>:fabrikant:pr-<n>` was created per PR. All `...:pr-<n>` projects whose
PR is no longer open on GitHub are listed and deleted. Fail-safe: undeterminable PR status → the project stays; a
cleanup failure is only a warning and doesn't change the scan's exit code.
`scripts/sonar-pr-cleanup.py --dry-run` shows what would be deleted.
