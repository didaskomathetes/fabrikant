# fabrik — the software factory

Define agentic development loops as YAML specs, run them locally, steer them
from the UI, adjust them via chat. One engine process (`fabrik serve`) watches
triggers and executes loops; the UI at `http://127.0.0.1:8321` shows
projects, loops, runs, live logs and a chat interface.

**Principles:**
- Local, 127.0.0.1, no cloud backend, no build step
- Python stdlib + PyYAML — one Python package (`src/fabrik/`) + a vanilla-JS UI
- **Python 3.11+** (modern typing: `str | None`, `tuple[str, bool]`;
  `requires-python` in `pyproject.toml`)
- Git projects run in **isolated worktrees** (the main checkout is never
  touched)
- **Never push to protected branches** (`main`/`master` = hard-blocked);
  all results go out as a branch + PR
- One project = one spec; one loop = ordered steps; one run = JSON + log
- **Autonomy with gates**: loops run fully on their own; only
  explicitly marked critical steps (`approval: true`) pause the run
  until a human approves

## Getting started

```bash
fabrik serve            # engine + UI + scheduler, 127.0.0.1:8321
fabrik list             # projects + loops
fabrik validate         # check specs (--offline [SPEC…]: without root check, e.g. CI)
fabrik run <project> <loop> [--note "…"]
fabrik runs [--project X]
fabrik runs show <run_id> [--project X] # single run: fields, steps, session log
fabrik runs export <run_id> [--project X]  # single run + session log as JSON
fabrik approve <run_id> [--project X]   # needs_approval → resume
fabrik reject  <run_id> [--project X]   # needs_approval → abort
fabrik chat "…"          # chat command / LLM copilot
fabrik alert test [--project X]          # test alert channels
```

**GitHub Apps (ADR 0010):** `scripts/gh-token-refresh.py` renews the 1h installation tokens —
without arguments the builder app (`BUILDER_APP_*` → `GITHUB_TOKEN`), with `--role reviewer` the
QA app (`REVIEWER_APP_ID`, `REVIEWER_APP_KEY_FILE`, `REVIEWER_INSTALLATION_ID` →
`GITHUB_REVIEWER_TOKEN`). One cron line each, every 30 min.

*GitHub-specific (implementation detail, no ADR; moves into the GitHub adapter with the
provider ports from PR #437):*

**Tokens per project:** `github_env_file: <path>` in a spec replaces `FABRIK_ENV_FILE` for that
project: runs, trigger polls, retry attempts and their subprocesses (shell steps, agent `gh`)
read `GITHUB_TOKEN`/`GITHUB_REVIEWER_TOKEN` only from this file; global
`GITHUB_TOKEN` env values don't apply there (shell steps get `GH_TOKEN`/`GITHUB_TOKEN` =
the project's builder token; subprocesses recognize the override via `FABRIK_PROJECT_ENV_FILE`).
For a different app installation (other org) or a PAT as reviewer. Refresh via cron with an env
override, e.g. `FABRIK_ENV_FILE=<path> BUILDER_INSTALLATION_ID=<id> scripts/gh-token-refresh.py`.

**Signed commits (GitHub):** `commit_via: api` in a spec (default `git`) doesn't push with `git push`
but rebuilds the local commits through the Git Data API (`src/fabrik/apicommit.py`). GitHub
signs them itself for the builder app ("Verified", author = app bot) — for repos with
`required_signatures`, without keys on the host. Applies to `git` steps (`push: true`), the
fix rounds of `pr_review` and relay pushes. Agents that push themselves call
`python -m fabrik.push [--force]` instead of `git push` (the route per project is selected via
`FABRIK_COMMIT_VIA`). Limits: no root commit; the local author/timestamp are lost.

`fabrik` is the console script from `pyproject.toml` (after `uv sync`).
Without installation: `python -m fabrik …` (from the project root).

Environment variables: `GITHUB_TOKEN` (GitHub API), `FABRIK_ENV_FILE` (path to the
`.env` file with `GITHUB_TOKEN`, default `/root/.hermes/.env`), `FABRIK_TOKEN`
(optional: bearer token for the API `POST` routes), `FABRIK_WORKERS`
(worker pool size, default 4), `FABRIK_LLM_PROVIDERS` +
`FABRIK_LLM_<NAME>_{URL,MODEL,KEY}` (LLM providers, see below).

### Local operation (systemd user units)

Permanent operation runs as a user service instead of a foreground `fabrik serve`, so that
scheduler/cron triggers survive closing the terminal and reboots. Only one
`serve` process at a time (see "Single node" under Runs).

The units in `deploy/` contain no checkout paths. They read `FABRIK_HOME`
(the `fabrik/` directory of the checkout, same meaning as in
`config.py`) from **one** file per machine and switch there via
`/bin/sh -c 'cd "$FABRIK_HOME" && exec …'` (systemd doesn't expand
variables in `WorkingDirectory=`). If the variable is missing, the unit aborts with
"FABRIK_HOME missing".

```bash
# ~/.config/fabrik/deploy.env — create once per machine
mkdir -p ~/.config/fabrik
echo "FABRIK_HOME=$PWD/fabrik" > ~/.config/fabrik/deploy.env   # from the repo root
# example content: FABRIK_HOME=/home/<user>/quark-playground/fabrik
```

```bash
cp fabrik/deploy/fabrik.service fabrik/deploy/fabrik-autoupdate.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fabrik.service fabrik-autoupdate.timer
journalctl --user -u fabrik -u fabrik-autoupdate -f      # logs
```

systemd starts without a shell profile: agent CLIs outside `/usr/bin`
(`copilot` via nvm, `claude`, `opencode`) are otherwise missing from `PATH` → agent steps
fail with "CLI '…' not in PATH". Set it per machine via a drop-in:

```ini
# ~/.config/systemd/user/fabrik.service.d/path.conf
[Service]
Environment=PATH=%h/.nvm/versions/node/<version>/bin:%h/.local/bin:%h/.opencode/bin:/usr/local/bin:/usr/bin:/bin
```

Don't forget `enable`, or nothing starts after a reboot (`systemctl --user is-enabled
fabrik`); without a login you also need `loginctl enable-linger`.

**Auto-update after merges** (`scripts/fabrik-autoupdate.py`, timer every 5 min):
pulls `main` from `origin` with `--ff-only` (only on branch `main`; with
local commits/conflicts it aborts with exit 1, nothing is overwritten),
runs `uv sync` if `pyproject.toml`/`uv.lock` changed and restarts
`fabrik.service` if anything under `fabrik/` has changed since the start commit
(`state/serve-commit`, written via `ExecStartPre`).
Restarts happen only when idle — if `running`/`queued` runs exist, it is deferred to
the next tick (a restart would orphan them as `error`/`interrupted`;
auto-retry kicks in if configured, ADR 0036).
`needs_approval` runs don't block.

## Development

`uv` project (lock file: `uv.lock`); pytest, ruff, pyright and bandit as the dev group:

```bash
uv sync                 # venv + dependencies (runtime + dev)
uv run pytest           # unit/integration tests
uv run pytest -m "not browser and not perf" -n auto   # parallel (pytest-xdist), much faster
uv run ruff format .    # format
bash scripts/lint.sh    # gate: ruff check + format --check, pyright (types), bandit (SAST, medium and up)
```

**Browser tests (UI):** `tests/test_ui_browser.py` starts an isolated
fabrik server + Chromium (Playwright) and checks the interactive UI behavior
(scrollbars, run modal, clickable loop specs, chat overlay). Once:
`uv run playwright install chromium`. To skip: `uv run pytest -m "not browser"`.

**CI:** `.github/workflows/fabrik-ci.yml` (PRs + pushes to `main`, only for changes under
`fabrik/`) has two independent jobs: `test` (`uv sync --frozen`, `scripts/lint.sh`
(ruff, pyright, bandit), `pytest -m "not browser and not perf" -n auto`, then `pytest -m perf`
serially) and `browser` (Playwright + Chromium, `pytest -m browser`). Python 3.11+, uv cache keyed on `uv.lock`.

**Worker pool:** trigger runs execute on a bounded pool of daemon workers
(`FABRIK_WORKERS`, default 4). For local development / initial testing:
`FABRIK_WORKERS=1` → all runs serially on a single agent worker
(no parallel LLM sessions). Daemon threads: Ctrl+C (or SIGTERM/SIGHUP) ends the
process immediately without waiting for running runs; running shell steps and one-shot agents
are terminated together with their process group.

**Parallel runs (ADR 0028):** each run works in its own git worktree
(`<WORKTREES_DIR>/<project>/<run_id>`; `FABRIK_WORKTREES`, default
`~/.local/state/fabrik/worktrees/<installation>` — deliberately outside the checkout so
IDEs don't index the run checkouts, ADR 0047 addendum), which is cleaned up at the end (or on reject, and after
a crash on the next server start); a run in `needs_approval`
keeps it for the resume. `max_parallel` (project spec, default 3) limits
concurrent runs per project; specs without `repo:` run without isolation in `root` and
are therefore fixed at `max_parallel: 1`. Trigger runs over the limit are dropped,
manual/chat runs and approved resumes are queued (ADR 0015) and wait for
a free slot. `worktree: false` loops (ADR 0020) don't occupy a slot. At most one active run
runs per loop + issue/PR. `state/replied-comments.json` and
`state/reviewed-prs.json` are merged under lock into `state/agent/<project>/`.
Operation: exactly **one** fabrik process per project (see ADR 0028).
True parallelism needs `FABRIK_WORKERS` > 1.

## Spec format (`*.fabrik.yaml`)

**Vollständige Feld-Referenz:** [docs/spec-reference.md](docs/spec-reference.md) — generiert aus
[`src/fabrik/fabrik-spec.schema.json`](src/fabrik/fabrik-spec.schema.json) (Single Source of Truth;
`fabrik validate` prüft dagegen).

```yaml
name: kyudo-neandertal
description: "Drupal 7→11 migration"
root: /root/kyudo-neandertal
max_parallel: 3                             # concurrent runs (default 3)
repo:
  branch: master
  subpath: drupal11
github: didaskomathetes/kyudo-neandertal   # for on_issue + github_issue alerts

notify:                                     # alert channels on (default: run_failed)
  slack: https://hooks.slack.com/services/…
  webhooks:                                 # generic: Datadog, CloudWatch, …
    - {name: datadog, url: https://http-intake.logs.datadoghq.com/v1/input?api_key=…}
  github_issue: true                        # on run_failed: open an issue in the repo
  issue_labels: [fabrik]
  events: [run_failed]                      # also: [run_failed, run_passed]; run_failed includes run_exhausted

loops:
  migrate:
    description: "Drupal changes as branch + PR"
    triggers:
      - {type: on_issue, labels: [migration], poll: 300}   # new issue → loop
    steps:
      - {type: git, name: prepare, action: prepare, message: "fabrik: …"}
      - {type: agent, name: changes, prompt_file: prompts/….md}
      - {type: shell, name: checks, cmd: "bash scripts/run_checks.sh"}
      - {type: pr, name: pr, base: master, title: "…", approval: true}
```

### Spec sources, `vars` and loop templates (ADR 0047)

**Where specs live** (first hit wins; the same `name:` in two sources is reported as an error by `fabrik validate`):

1. `FABRIK_PROJECTS_PATH` — colon-separated like `PATH` (folders with `*.fabrik.yaml` or a repo with `.fabrik/fabrik.yaml`)
2. `FABRIK_DATA/projects` (without `FABRIK_DATA`: the installation's `projects/`)
3. Target repos with `.fabrik/fabrik.yaml`, registered via `fabrik project add <repo-path>` (without `name:` the repo folder applies, without `root:` the repo)
4. the bundled examples in `projects/`

**Paths:** `prompt_file` and `scripts/…` in shell commands are resolved relative to the spec file, then relative to the
installation (`prompts/…`, `scripts/…` of the factory — so keep script names unique).

**`vars:`** — `{name}` applies throughout the spec (type-preserving if the string is only `{name}`). Other `{x}` stay
runtime variables (`{issue_number}`, `{root}`, …; forbidden as `vars` names). `vars` may reference other `vars`
(`a: "{b}"`); cycles are an error.

**Loop templates** (`templates/<name>.loop.yaml`; bundled: `develop`, `review-pr`): `use:` + `with:`.
Required parameters (`null` in the template) come from `with` or `vars`; further loop keys override the template,
`insert: [{after: <step>, steps: […]}]` inserts steps (not together with own `steps:`). Unknown `with` keys
(neither `params` nor placeholders in the template) are an error. A second project (repo `acme/app`):

```yaml
name: app
root: ~/src/app
github: acme/app
vars: {repo: acme/app, builder_model: claude-sonnet-5-5, reviewer_model: gpt-5.6-luna}
loops:
  develop:
    use: develop
    with: {prompt_file: develop.prompt.md, lint_cmd: make lint, test_cmd: make test}
  review-pr: {use: review-pr, with: {prompt_file: review.prompt.md}}
```

If the spec lives as `.fabrik/fabrik.yaml` in the repo, the prompts sit next to it (`.fabrik/develop.prompt.md`).
`projects/fabrik.fabrik.yaml` is itself an instance (repo name only in `vars`).

**Migrating private specs:** `kyudo-neandertal`, `gods-last-garden` and `ai-lab-merchant-assistant` (including
`prompts/kyudo-*`, `prompts/ailab-*`) are no longer in the repo. Fetch them from the history and put them into `FABRIK_DATA`
(or into the target repo as `.fabrik/fabrik.yaml`):

```bash
mkdir -p "$FABRIK_DATA/projects/prompts"
for f in kyudo-neandertal gods-last-garden ai-lab-merchant-assistant; do
  git show 5ab9351:fabrik/projects/$f.fabrik.yaml > "$FABRIK_DATA/projects/$f.fabrik.yaml"
done
for f in ailab-address ailab-develop ailab-pr-reply ailab-review kyudo-develop; do
  git show 5ab9351:fabrik/prompts/$f.prompt.md > "$FABRIK_DATA/projects/prompts/$f.prompt.md"
done
fabrik validate
```

### Step types

| Type     | Fields                                                        | Behavior |
|----------|---------------------------------------------------------------|----------|
| `shell`  | `cmd`, `workdir`, `env`, `timeout` (default 600 s), `name`, `failure_kind` | Run `sh -c`; exit ≠ 0 aborts the loop. `failure_kind` (`outcome`\|`transient`\|`environment`) names the [failure class](#failure-classes-adr-0035) for exit ≠ 0, e.g. `outcome` for test/lint steps. Fixed exit codes take precedence: 126/127 and 78 (`EX_CONFIG`) → `environment`, 75 (`EX_TEMPFAIL`) → `transient` — this is how a script reports the class itself; death by signal (OOM kill, SIGTERM; exit > 128) stays `unknown`. Allowed on shell steps only |
| `git`    | `fetch`, `branch`, `track`, `commit: {add, message}`, `push` | Git op; `push` blocks on protected branches; `commit` on a clean tree is a no-op |
| `pr`     | `repo: owner/name`, `base`, `head`, `title`, `body`          | PR via the GitHub API (token from `GITHUB_TOKEN` / `FABRIK_ENV_FILE` / `/root/.hermes/.env`). An open PR for `head` → reused; `head` without commits ahead of `base` → no PR, the run ends successfully, follow-up steps `skipped` (the tier gate still applies) |
| `agent`  | `backend` (e.g. `opencode`), `prompt`, `workdir`, `endpoint`, `model`, `variant` (`opencode` only: model variant from the opencode config, e.g. reasoning effort), `permissions`, `timeout`, `github_identity` — or legacy: `cmd`, `prompt` | Agent task via a pluggable backend (session + prompt + polling) or an agent CLI. Without `backend`/`cmd` = cleanly skipped. `github_identity: dev\|reviewer` (CLI backends only) sets the role's `GH_TOKEN`/`GITHUB_TOKEN` in the agent process — `gh` (and `git` with `gh` as credential helper) then runs as the builder or QA app instead of as the local user. A `gh` wrapper at the front of the PATH reads the token fresh from `FABRIK_ENV_FILE` on every call, so runs with `timeout` > 1h survive the 1h installation token |
| `report` | `text`, `write` (optional: file path, relative to the project root) | Message to the run log and/or a report file |
| `http`   | `url`, `method`, `headers`, `body`, `expect: {status}`, `emit: {var: body\|status}` | HTTP request; the response/status becomes available as a variable for following steps |
| `llm`    | `prompt`, `schema: {var: type}`, `untrusted: [vars]`, `provider`, `model`, `temperature`, `extra_context` | Structured LLM call; the JSON is validated against the schema, keys become variables. Schema value: a type or `{type, enum, default}` — `enum` = whitelist, `default` makes the key optional. `provider` pins a configured LLM provider (otherwise the active one), `model` overrides the provider model. `untrusted` marks external content as data (prompt-injection protection). Per call, `usage` (tokens + model + provider) is written to the run record (tracing building block for cost exporters) |
| `notify` | `message`, `slack`, `webhooks`                                | Notification as a step; without channels = log only |
| `state`  | `action` (`get`\|`set`\|`add`), `key`, `value` (substitution allowed), `file` (default `state/<project>.json`) | Persistent state across runs (JSON, atomic write). Emits `state_value` as a variable. For "already processed" counters, round caps, last-seen in cron loops |
| `alert_route` | `routes: {category: loop}`, `fallback` | Deterministic routing `{category}` → hotfix loop (no LLM); unmapped → `fallback` or no hotfix. Targets (`none` or another loop of the project) and route cycles are checked at spec load. See [Incident round trip](#incident-round-trip) |
| `verify` | `checks: [{type, ...}]` (each check a normal step def: `shell`\|`http`\|…) | Check-list semantics: all checks are run, output per check goes into the run record. All green → `pass`; one red → `fail` with check name + output. **Caution:** sub-checks have full step semantics — `llm` checks mutate the ctx, `state` checks persist. LLM judges / screenshot diffs may be added later as check types |
| `issue`  | `action` (`create`\|`edit`\|`label`\|`close`\|`list`\|`block`), `repo`, `number` (except `create`; default `{issue_number}`), `title`, `body`, `labels`, `remove_labels`, `state`, `comment`, `append`, `unless_contains`, `only_if`, `skip_if_empty` | Native GitHub issue operation (REST, no shell/`gh`, no LLM); see [`issue` step](#issue-step) |
| `review` | `model` (required), `backend`/`endpoint` (as `agent`), `target` (optional), `prompt_file` (optional; resolved as for `agent`) | Cross-model cold critic (ADR 0003): agent session with a **different model** than the builder step (engine check). The reviewer answers with a JSON verdict (`approved`\|`changes_requested` + findings, optional `acceptance` — AC verification `[{criterion, status: met\|unmet\|unverifiable, evidence}]`, ADR 0013; invalid → no verdict) → lands in the run record (`step.review`). `changes_requested` → run `fail`. `target` default: output of the last agent step |
| `pr_review` | `repo`, `pr`, `model` (required), `backend`, `max_rounds` (default 2), `max_findings` (default 5), `fix_backend`/`fix_model`/`fix_variant` (default: last agent step), `fix_timeout` (default 1800 s), `fix_checks: [cmd]`, `fix_check_timeout` (default 900 s) | LLM review of the PR diff + GitHub review; on `changes_requested` the builder (backend/model/variant of the last `agent` step) fixes and pushes. `fix_checks` (e.g. lint + test) run after the fix **before** commit/push; a red check aborts the step (`outcome`), nothing is pushed |
| `pr_merge` | `repo`, `pr`, `method` (`squash`\|`merge`\|`rebase`), `update_branch` (default `false`) | Merge a PR. `update_branch: true`: if the PR is behind the base, first `update-branch` (merge the base into the PR) and wait for mergeability; conflict = step red (`outcome`). The update merge commit is **not** tested again (trade-off: otherwise the PR would be stranded behind a moving `main`) |
| `escalation` | `repo`, `item` (default `{pr_number}`, else `{issue_number}`), `label` (default `needs human`) | Escalation to a human (ADR 0057): if the item carries the label, the run pauses with a kind (`blocked`\|`stalled`\|`exhausted`\|`scope`\|`access`) and the reason from the last `fabrik-escalate` comment. Approve = resume (label removed), reject = give up (label stays). The label is set by `scripts/pr-status.py` (`escalate`, `escalation-check --escalate`) |

### `issue` step

Deterministic issue operations over the GitHub REST API (ADR 0018). All
string fields are `{vars}`-substituted and go to the API as a JSON payload —
never through a shell, so untrusted text (LLM output, issue body) is safe.

| Field | Applies to | Meaning |
|-------|------------|---------|
| `action` | all | `create` \| `edit` \| `label` \| `close` \| `list` \| `block` |
| `repo` | all | `owner/name` (required) |
| `number` | `edit`, `label`, `close`, `block` (`list`: omitted) | Issue number; default: variable `issue_number` |
| `title` | `create` (required), `edit` | Title |
| `body` | `create`, `edit` | Body; with `edit` + `append: true` appended to the existing body |
| `labels` | `create`, `label` | Add labels (list or `"a,b"`); GitHub creates unknown ones |
| `remove_labels` | `label` | Remove labels (a missing label is not an error) |
| `state` | `edit`, `list` | `open` \| `closed` (`list`: which issues, default `open`) |
| `comment` | `close` | Reason, posted as a comment before closing |
| `state_reason` | `close` | `completed` \| `not_planned` \| `duplicate`; empty = GitHub default `completed` |
| `duplicate_of` | `close` | `#N`: close as a duplicate of #N; issues blocked by the duplicate are blocked by #N instead (ADR 0031) |
| `emit`, `inherit_labels` | `create` | Target variable for the new number (default `issue_number`); take over labels from the list that issue `number` carries (e.g. priority) |
| `append`, `unless_contains` | `edit` | Append to the body; no-op if the marker is already in the body (idempotency) |
| `only_if` | all | `{var: value\|[values]}` — no-op unless every variable has one of the values |
| `skip_if_empty` | all | `[vars]` — no-op if all the named variables are empty |
| `labels`, `limit`, `emit` | `list` | Filter, upper bound (default 100, most recently updated first), target variable (default `issues`): one line `#N [labels] title — body excerpt` per issue (ADR 0031) |
| `blocked_by` | `block` | `"#12, #14"` → native GitHub "blocked by" dependencies (ADR 0031); missing/closed issues and PRs are skipped, max. 5; sets `blockers` |

`create` sets `issue_number` and `issue_url` for follow-up steps.

```yaml
- {type: issue, action: create, repo: o/r, title: "refactor: {t}", body: "{b}", labels: [refactor]}
- {type: issue, action: label, repo: o/r, number: "{issue_number}", labels: [ready], remove_labels: [needs approval]}
- {type: issue, action: close, repo: o/r, number: "{issue_number}", comment: "Duplicate of #65"}
```

**Auth:** `GITHUB_TOKEN` (env or `FABRIK_ENV_FILE`, developer identity as with `pr`). The
token ends up in no step result; logs and run records are additionally protected by
secret masking (`[REDACTED]`). Error → step `fail` with the HTTP status.

### `review` example

```yaml
# Builder (e.g. opencode with model A) …
- {type: agent, name: build, backend: opencode, endpoint: "…", model: "model-a", prompt: "…"}
# … then the cold critic with model B (must be ≠ A, otherwise the step fails)
- {type: review, name: critique, backend: opencode, endpoint: "…", model: "model-b"}
# … only after that the PR step
```

The reviewer sees only the review target (fresh context, no bias).
Unreadable verdict output fails the step — no silent pass. The same applies
to an invalid `acceptance` field (ADR 0013). In the `pr_review` step the
ACs of the linked issue go into the reviewer's prompt as untrusted data;
at least one `unmet` criterion forces `changes_requested` (bounded
fix rounds), `unverifiable` doesn't block but is traceable in the review text and in the
run record.

### `extra_context` (all LLM nodes)

Additional prompt context for `llm`, `agent`, `review`, `pr_review` (review and
fix prompt) and `human_gate`. At project level it applies to all these steps; a step
adds to it (same `name` → the step wins), `extra_context: false` turns it off.

```yaml
extra_context:                       # project level
  - {name: agents_md, file: AGENTS.md, trusted: true}
loops:
  develop:
    steps:
      - name: review
        type: pr_review
        extra_context:               # step level, adds to agents_md
          - {name: quality, cmd: "bash scripts/sonar-scan.sh --pr {pr_number}", trusted: true}
          - {name: threads, cmd: "python3 scripts/pr-status.py threads {pr_number} --repo o/r"}
```

Exactly one source per entry: `file` (relative to the step workdir — `workdir:` on the step, otherwise
the run's —, must not leave it), `cmd` (`sh -c` in the step workdir, stdout only,
`timeout` default 300 s; placeholders only for trusted values as in the `shell` step)
or `value` (template). **The default is untrusted**: the content is marked as
data, as with `untrusted:`. `trusted: true` only for repo-owned files and tool output — issue text
or PR comments stay untrusted. `<untrusted`/`<context` tags in the content are
neutralized (`&lt;`) so text can't close or fake a block.
Sources are reloaded on every prompt build (`pr_review`: per round). A source that
can't be loaded doesn't abort the step; the block names the reason, the log warns.
Budget: `max_chars` per source (default 50,000 characters), all sources of a prompt
together at most 120,000 characters — in spec order, what doesn't fit is truncated or
omitted (log warning). Checks in a `verify` step get the project context like
top-level steps. The loop templates `develop` and `review-pr` pass it through to their LLM steps via the parameters
`builder_context`/`review_context`.

### `sonar-scan` PR mode (quality report in the review, advisory)

`bash scripts/sonar-scan.sh` analyzes `main` and checks the quality gate (loop `sonar-scan`,
daily). With `--pr <n> [--post] [--base main]` it scans the current workdir
(PR head) under its own key `<projectKey>:pr-<n>` instead — the Community Edition can't do
PR analysis, so the main analysis stays untouched. A `coverage.xml` newer than the
HEAD commit is reused (the develop `test` step produces it). Instead of the gate,
`scripts/sonar-pr-report.py` writes a deterministic Markdown report to stdout:
Sonar issues only on the changed lines, lint (ruff check + ruff format + mypy) on the
changed lines, coverage of the changed lines (`diff-cover`), key figures; last line
`<!-- fabrik-quality sha=<head> -->`. `--post` creates it as a single
PR comment or updates it. As an `extra_context` source (`trusted: true`) it lands
in every `pr_review` prompt; `review-pr` reads the comment. Logs go to stderr.

### `state` examples

```yaml
# Remember the last processed PR state (cron loop, idempotent)
- {type: state, name: last, action: get, key: last_pr_head}
- {type: state, name: mark, action: set, key: last_pr_head, value: "{pr_head_sha}"}

# Round cap: increment a counter, the loop stops when >= 5
- {type: state, name: bump, action: add, key: rounds, value: "1"}

# Maintain a list (non-numeric value → append)
- {type: state, name: seen, action: add, key: seen_issues, value: "{issue_number}"}
```

The state lives under `state/<project>.json` in the project root (default) —
deliberately **deterministic** (no agent access), so cron loops
cleanly recognize repeated runs. Path traversal outside the project root
is blocked.

**`gate`** (per step, default `true`): controls what a step failure does.
`gate: true` → the loop aborts immediately (run = `fail`). `gate: false` → the
failure is noted, the loop continues, the run ends as `fail` at the end.
Useful for "best-effort" steps (e.g. optional cleanup or
notification steps) that shouldn't block the rest of the loop.

Every step can have `approval: true` → the run pauses **before** this step
with status `needs_approval`. Approve via `fabrik approve <run_id>`,
`POST /api/approve` or the button in the UI. Rejecting aborts the run.
This keeps the human completely out of the loop — stepping in only for
the critical actions.

Conditional gate (ADR 0017): `approval_when: {label: [needs approval, close]}`
pauses only if the run variables match (AND across variables, OR across
values; missing variable → no gate). Can't be combined with `approval: true`.
An unmet `approval_when` doesn't replace the tier gate.

Reason for the approval: `approval_reason: "{reasoning}"` (a template with
run variables, e.g. from a previous `llm` step) appears in the
approval context (UI, chat, log) as the **why** — next to the **what** (action
derived from the spec) and the satisfied **condition**. If a referenced
variable is missing, the line is omitted.

**Risk tiers** (per loop: `tier: 1-4`): T1 runs through automatically after green checks;
T2+ ends with a synthetic tier approval step (`needs_approval`),
even if no single step has `approval: true`. The mapping lives in one
function (`tier_policy`) — P1/P2 extend it instead of scattering logic.

**Locking** (ADR 0020): all loops of a project share one git worktree and
therefore run one after another (project lock). A loop with `worktree: false` instead gets
an empty workdir per run and its own lock — it runs in parallel
to all other loops, but never twice. Allowed only without `git`/`pr`/`agent`/
`review`/`pr_review` steps (spec validation). If the spec changes between
lock selection and run, the run falls back to the project lock; scratch cleanup
errors land in the run log.

Substitution in `cmd`/`env`/`text`/`prompt`: `{project}`, `{root}`,
`{workdir}`, `{branch}`, `{head}`, `{loop}`, `{trigger}`, `{run_id}`,
`{fabrik_home}` (fabrik checkout, e.g. for `{fabrik_home}/scripts/pr-status.py` from
other projects) + all `env:` variables of the spec.

### Agent backends (P1)

`agent` steps dispatch via a pluggable backend interface (SPIE) —
the conductor stays deterministic, the agent is replaceable:

| Backend    | Setup                                                        |
|------------|--------------------------------------------------------------|
| `opencode` | Host: `opencode serve --port 8945` + `fabrik.env`: `FABRIK_AGENT_ENDPOINT`, `FABRIK_AGENT_MODEL` (format `provider/model`); 2.x additionally `FABRIK_AGENT_PASSWORD`. With `FABRIK_SANDBOX=1`: no `serve` — one `opencode run --standalone` per session inside the sandbox container, model as `<LLM connection>/<model>` (e.g. `llama-cpp/Qwen3.8-27B-GGUF`); the provider config is derived from the connection (http endpoint, secret only if set), whose host must be on the egress allowlist. Step `permissions` are restrictive: listed patterns apply, all others of the same permission are `deny` |
| (legacy)   | `cmd:` with any agent CLI (e.g. `claude -p {prompt_file}`) |

Security layers for agent processes (can be enabled individually, see the ADRs):

- **Agent env allowlist (ADR 0050):** agent subprocesses don't inherit `os.environ` —
  only the allowlist (`agentenv.py`, extendable via `FABRIK_AGENT_ENV_EXTRA`) plus
  variables passed explicitly per step. For the shared `opencode serve` process:
  `fabrik agent-env` prints the scrubbed env (systemd `EnvironmentFile` / Docker
  `--env-file`).
- **Egress default-deny (ADR 0051):** agent containers attach to `fabrik-sandbox`
  (no direct internet access); all egress goes through a Tinyproxy with a
  domain allowlist (`fabrik egress start`; allowlist: builtins + optional `FABRIK_EGRESS_ALLOWLIST_FILE`/
  `FABRIK_EGRESS_ALLOWLIST`, validated). Denied requests in the proxy log are an injection signal.
  Known limit: the shared `opencode serve` process runs unsandboxed on the host with
  full network access and isn't covered by the proxy (only agents in `fabrik-sandbox`).
- **Per-run agent sandbox (ADR 0054, under revision):** with `FABRIK_SANDBOX=1`, every
  one-shot CLI agent run executes in a hardened Docker container (`--cap-drop ALL`,
  no-new-privileges, resource caps, read-only root fs, egress only via the ADR 0051 proxy).
  The worktree is not mounted: the state goes in as a git bundle, the result comes
  back as a bundle; fabrik checks it and applies the diff to the worktree. The image needs the
  agent CLI and git; template with claude-code, codex, copilot, gh, Python/uv:
  `docker build -t fabrik/agent-sandbox:latest deploy/agent-sandbox` (versions via
  `--build-arg`, base images pinned by digest; fabrik starts it with `--pull never`).
  A GitHub token only enters the container with `github_identity`; without it all
  token variables are empty (also for projects with `github_env_file`).

The `opencode` backend (details: `docs/spike-opencode.md`, 1.x/2.x dual support:
ADR 0005):
- Supports opencode serve **1.x and 2.x** — the server version is detected
  automatically (`/api/info` vs. `/api/health`). 2.x requires
  HTTP basic auth: `OPENCODE_SERVER_PASSWORD` on the server +
  `FABRIK_AGENT_PASSWORD` in `fabrik.env` (same value)
- One session per step (workdir via `?directory=` (1.x) or `location`
  (2.x) → work happens in the step workdir, one serve process serves many projects)
- Prompt + polling of the message API (no SSE needed);
  done/failed detection via `finish` + substance check (completed tool or
  final text) — empty/wrong messages (e.g. LLM 500) → step `fail`, no
  30-minute hang
- Token/cost accounting per run (from the message API)
- Health gate before dispatch; timeout → `abort`/`interrupt`; an exception
  mid-run aborts the session (no orphaning)
- Robust against transient backend/transport errors (ADR 0012): if
  `poll()`/`transcript()` throws (e.g. `opencode serve` down/restart), the
  step keeps polling with backoff — **within the step timeout** — instead of killing
  the run. A persistent error until the timeout → `fail` with the backend cause
- Containment: file tools (`edit`) are confined to the session workdir
  (`external_directory` = ask → deny when headless); `bash "*` is deliberately broad —
  real isolation (non-root user, container, VM) is a **deployment
  prerequisite**, not a config option. Override: `step.permissions`
- Security: agent steps are T2+ — the conductor trusts only the
  deterministic step outcome, not agent prose; untrusted inputs
  (issue bodies) are data in the prompt

### Triggers

- **manual** — UI button, CLI, API, chat
- **cron** — 5 fields (`min hour dom mon dow`, `*`, `*/n`, `a-b`, lists).
  `on_start: true` additionally fires once after server start (or on the first
  tick after a resume) — e.g. for `token-refresh`, so that after a server pause
  no loop starts with an expired 1h token. Until this run is done (max.
  2 min), the project's other triggers (cron, file, issue and PR polls) wait
- **on_file** — fires when a file in the glob (relative to `root`) changes
  and has been stable for `debounce` seconds
- **on_issue** — polls GitHub (`repo` from spec or trigger, optional
  `labels`), starts the loop for every new issue. With `reconcile: true`
  (ADR 0014) it instead fires for every open issue for which the loop
  has never had a run — survives restarts; max. `max_per_poll`
  (default 2) runs per poll, oldest first. Optional `priority_labels`
  (ADR 0021, e.g. `[p0, p1, p2, p3]`): issues with a higher priority label
  first, unlabeled last. `skip_blocked: true` (ADR 0031, only with
  `reconcile`): issues with unresolved GitHub "blocked by" dependencies don't
  start, but on the first poll after the last blocker was closed as `completed`
  (`duplicate`/`not_planned` keep blocking).
  `exclude_labels` (also on **on_pr**, ADR 0057, e.g. `[needs human]`): items with one of
  these labels do not start; once the label is removed they fire again
- **on_pr** — polls GitHub PRs (`repo` from spec or trigger, optional
  `labels` (AND semantics), `poll` in seconds, `states: [open|closed]`
  (default `[open]`)), starts the loop for new/updated PRs
  (updated_at change) and on state changes (e.g. open→closed if
  `closed` is in `states`). Dedup: a PR fires only on change —
  polling the same PR again doesn't trigger a second run. Variables:
  `{pr_number}`, `{pr_title}`, `{pr_head_ref}`, `{pr_body}`.
  `reconcile: true` (ADR 0011): also fires for existing PRs, but only
  if no previous `on_pr` run exists (status irrelevant; restart-proof)
  (reconciling controller). `reconcile: head` (ADR 0023): like `true`, but
  per head SHA (`on_pr:#N@<sha>`) — every new push fires exactly once, a
  discarded trigger (lock busy, restart) is caught up on the next poll.
  Additional variable: `{pr_head_sha}`.

### Pause / Disable (ADR 0019)

State per project and loop: `active` | `paused` (triggers don't fire,
manual runs allowed) | `disabled` (additionally no manual runs).
Project state beats loop state; running runs finish. Optional
`until` (ISO 8601, without a time zone = UTC) → automatic resume. Persisted in
`state/run-states.json` (not in the spec); every change is in the audit log
`state/run-state-changes.jsonl` (who, when, from, to, until).

```bash
fabrik pause <project> [loop] [--until 2026-01-31T18:00]
fabrik disable <project> [loop]
fabrik resume <project> [loop]
```

Chat: `pause|resume|disable <project> [loop]`; UI: buttons per project/loop;
API: `POST /api/projects/<p>/state`. An emergency stop for all projects is not
part of v1. Access: role `operator` (see "Authentication and roles").

- **on_webhook** — `POST /api/webhook/<project>/<loop>` (ADR 0027). Auth via
  HMAC-SHA256 of the raw body in the header `X-Fabrik-Signature: sha256=<hex>`; wrong/
  missing → `401`. Payload→variables via `map` (dot paths; invalid JSON `400`,
  missing field `422`). CORS only for `FABRIK_WEBHOOK_ORIGIN` (tunnel origin, never
  `*`); over this host only the webhook route is reachable.
  ```yaml
  triggers:
    - type: on_webhook
      secret_env: MYAPP_HOOK_SECRET   # env name, never the value
      map: {issue_number: issue.number, title: issue.title}
  ```

- **on_alert** — `POST /api/alert/<project>/<loop>` (ADR 0045): Sentry/CloudWatch intake.
  Auth as with `on_webhook` (HMAC in `X-Fabrik-Signature`; Sentry additionally
  `Sentry-Hook-Signature`), otherwise `401`. Variables: `alert_source|title|message|level|service|url|fingerprint`.
  CloudWatch state ≠ `ALARM` → `200 ignored`, no run. Reachable via the tunnel host.

- **on_slack** — polls Slack channels (ADR 0040) and starts exactly one run per new message
  matching the pattern (dedup via `ts` in
  `state/slack/`). Variables: `{alert_text}`, `{alert_channel}`, `{alert_ts}`.
  `alert_text` is untrusted (any Slack user in the channel can set it):
  in `llm` steps only via `untrusted: [alert_text]`, in `shell` steps only via
  `env:` — never inline in `cmd` (ADR 0008). Token only via an env variable
  (`token_env`), never in the spec; only channels on the `channels` whitelist are read.
  ```yaml
  triggers:
    - type: on_slack
      token_env: SLACK_BOT_TOKEN      # env name, never the value
      channels: [C0123ALERTS]         # whitelist (channel/DM IDs)
      pattern: "^(ALERT|FIRING)"      # optional, regex
      poll: 60
      max_pages: 20                   # optional, history pages (of 100) per channel and poll
      max_alert_chars: 4000           # optional, longer alert_text → "… [gekürzt, N Zeichen]" (truncated, N characters)
  ```
  If `max_pages` is reached, the poll processes the newest messages and catches up
  on the older gap in the following polls (warning in the log) — no alert
  is lost, only the order across polls is then not chronological.
  `pattern` always checks the full text, truncation happens only afterwards.

| Trigger | Source | Mechanism | Auth |
|---|---|---|---|
| `on_issue` | GitHub issues | polling | GitHub token |
| `on_webhook` | HTTP sender | push | HMAC-SHA256 |
| `on_slack` | Slack channels/DMs | polling | bot token (env) |

### Relay tasks (ADR 0037)

For tasks that don't fit into a single run (historical import, migration, analysis over days).
An issue with the label `fabrik-relay` is the assignment. It runs in many short sessions, the
handoff file lives on the branch `fabrik/relay/<N>`. On `done`, an independent,
initially blind audit checks the result. Enable per project:

```yaml
relay:
  allowed: [alice]             # required: only these logins may approve briefs (label + body)
  forbid: [infrastructure/**]  # paths that sessions never commit
  builder: {model: …}          # sessions (github_identity dev)
  reviewer: {model: …}         # audit (github_identity reviewer, read-only)
  bot_login: my-builder-app    # required: read the status comment only from this identity; commit author
  retry: {max: 3, backoff: [5m, 30m, 2h], "on": [interrupted, transient], max_wait: 24h}  # like run retry (ADR 0036); off by default
  trail_archive: {type: git-ref}  # after audit pass, archive the trail + remove it from the branch (git-ref | local)
  # optional: repo, base, label, dispatch (cron), max_session_minutes, verify_timeout,
  #           audit_timeout, audit: false
```

From this fabrik generates the loops `relay-dispatch` (cron, no agent), `relay-session` and
`relay-audit`. Brief template and flow: [docs/relay.md](docs/relay.md). A human unblocks
(checkpoint, stall, changed `## Verify`) by removing the label `relay: blocked`.
The dispatcher needs `FABRIK_URL` (default `http://127.0.0.1:8321`) and, if applicable, `FABRIK_TOKEN`
for `POST /api/runs`.

**Trail archive** (ADR 0037, addendum 2026-11-03): with `trail_archive`, `audit-publish` stores the trail
(`.fabrik/relay/<N>/`) immutably after `pass` (`git-ref`: hidden ref
`refs/fabrik/relay/<N>/audit-<k>`; `local`: `FABRIK_DATA/relay-archive/`), checks the archive against
the trail hash and only then removes the trail from the branch with a delete commit. The PR diff and merge
thus contain only the actual change. The verdict is bound to the hash (last line of
`AUDIT.md`, status comment). If a later session or a repeated audit needs the trail, it is
restored from the archive (hash check, otherwise the step fails). Without `trail_archive`
the trail stays on the branch; the status comment warns as soon as a PR from the branch exists.

### Incident round trip

Alert → triage → hotfix → verify → PR (ADR 0045). Boundary: further intake sources are
#33 (`on_slack`) and #34 (`on_datadog`); the ACP backend status is in ADR 0044 (watching).

```yaml
loops:
  triage:
    worktree: false
    triggers:
      - {type: on_alert, source: sentry, secret_env: SENTRY_HOOK_SECRET}  # or cloudwatch
    steps:
      - name: triage            # read-only: classification only, alert fields untrusted
        type: llm
        prompt: "Classify the alert {alert_title}: {alert_message}"
        untrusted: [alert_title, alert_message]
        schema:
          category: {type: string, enum: [bug, infra, noise, unknown], default: unknown}
      - {name: route, type: alert_route, routes: {bug: hotfix}}   # unknown/noise → no hotfix
  hotfix:                       # runs as its own run with alert_* + category
    steps: [agent-fix, verify, pr]
```

Sources: **Sentry** — internal integration/webhook to the URL, client secret as
`secret_env`. **CloudWatch** — alarm → SNS → relay (Lambda/API Gateway) that signs the body with
HMAC (`X-Fabrik-Signature: sha256=<hex>`) and forwards it; the SNS envelope is
unpacked. The hotfix run is subject to the normal tier/approval gates.

### Alerts (`notify`)

After every run whose status is in `events`:
- **slack** — Slack webhook URL (string or `{url: …}`)
- **webhooks** — arbitrary HTTP POST targets (Datadog HTTP intake, CloudWatch,
  custom endpoints); payload: `projekt, loop, status, lauf, trigger, message, log_tail`,
  on `run_failed` additionally `failure_kind` (also in the text and issue title; `environment`
  highlighted, ADR 0035)
- **github_issue** — on `run_failed`, create an issue in the `github` repo
  (title + log tail + label). **Dedup:** the same failure pattern (project/loop +
  failed step + error text without run/session IDs, i.e. UUIDs and
  hex tokens with a-f; decimal numbers — even long ones like timestamps — are
  kept) → as long as the issue is open, it is commented on instead of creating a
  new one. The lookup paginates over all open issues with the
  `issue_labels` (AND-combined — after a label change old
  issues no longer match). Tuning constants in `config.py`:
  `ALERT_OPEN_ISSUES_PAGE` (max. 100), `ALERT_LOOKUP_MAX_PAGES`,
  `ALERT_FP_LEN`, `ALERT_REASON_CAP` (details: ADR 0025)
- **log_tail policy:** known secrets (env/token files) are already masked in the log;
  in addition `secrets.redact_text` redacts bearer/API/AWS keys,
  private key blocks and `key=`/`token=` assignments to `[REDACTED]` (switch
  `config.ALERT_LOG_REDACTION`, on by default; on a redaction error fail-safe:
  unredacted + warning). Heuristic — still keep the targets restricted
  (ADR 0025, ADR 0026)

Test: `fabrik alert test [--project X]` or `alert test` in the chat.

## Chat (UI + CLI + API)

The chat interface builds, changes and debugs loops — via commands or via the
LLM copilot.

**Commands:** `help`, `list`, `show <project>`, `lauf <project> <loop> [note]` (start a run),
`runs [project]`, `log <run_id>`, `approve <run_id>`, `reject <run_id>`,
`alert test [project]`, `apply <project>`

**LLM copilot:** everything that isn't a command goes to the **active**
LLM provider (OpenAI-compatible). Providers are configured in `fabrik.env`
and switched in the UI (select at the top right, `POST /api/llm`):

- **Named providers** (any number, e.g. Ollama + local llama.cpp):
  `FABRIK_LLM_PROVIDERS=ollama,llama-cpp` +
  `FABRIK_LLM_OLLAMA_URL`/`_MODEL`/`_KEY` and
  `FABRIK_LLM_LLAMA_CPP_URL`/`_MODEL`/`_KEY`
  (hyphens in the name → underscores in the env prefix).
- **CLI providers** (`_CLI` instead of `_URL`, auth = the CLI's login, no key):
  `FABRIK_LLM_CLAUDE_CLI=claude` / `FABRIK_LLM_COPILOT_CLI=copilot` + `_MODEL`.
  Runs without tools and without repo instructions (plain text answer);
  `temperature`/`max_tokens` are ignored.
- **Legacy** (one endpoint, becomes provider `default`): `FABRIK_LLM_URL`,
  `FABRIK_LLM_MODEL`, optionally `FABRIK_LLM_KEY`.

The active provider is persisted in `state/active_llm` (survives
restarts); the default is the first configured provider. `llm` steps can
pin a provider via `provider:` and override the model via `model:`.
The LLM sees the current specs + the latest failed runs; if it proposes a
spec change, it is validated and waits for `apply <project>` (nothing
is written without consent). While an LLM call is running, the chat shows
animated dots + a **✕ cancel** button (`POST /api/chat/cancel`); the
request ends immediately; an HTTP call finishes in the background, a
CLI is killed. The model dropdown sits in the chat's title bar.

## API

| Route | Method | Description |
|-------|--------|-------------|
| `/api/projects` | GET | Projects + loops + last run + github/notify |
| `/api/schedule` | GET | Schedule view: all cron loops with next fire times, last run, state + scheduled one-time overrides; `queue` = the actual queue (running/waiting/approvals, worker pool, slot context) |
| `/api/schedule/onetime` | POST `{"project","loop","when","note"?}` | Schedule a one-time run (`when`: `15:05` · `in 10 min` · ISO-8601); replaces an existing entry per loop; the scheduler fires it (disabled → dropped, paused → runs) |
| `/api/schedule/onetime` | DELETE `{"project","loop"}` | Remove a scheduled one-time run |
| `/api/projects/<name>/spec` | GET | Full (expanded) spec + `_hash` (optimistic lock) |
| `/api/projects/<name>/spec?raw=1` | GET | Raw, unexpanded spec (vars verbatim, `use:`/`insert:` un-inlined, all loop keys) + `_hash` — draft source for the UI graph editor |
| `/api/projects/<name>/spec` | PUT `{"loops"?,"remove_loops"?,"description"?,"max_parallel"?,"base_hash"?}` | Spec edit (graph view): merge + delete loops per loop, set top-level fields; `base_hash` ≠ current hash → 409. Validation + atomic write, comments preserved |
| `/api/projects/<name>/branches` | GET | Available branches + current branch (ADR 0004) |
| `/api/projects/<name>/branch` | POST `{"branch"}` | Switch the target branch (override, ADR 0004) |
| `/api/projects/<name>/state` | POST `{"state","loop"?,"until"?}` | Set `active`/`paused`/`disabled` for a project (or loop) (ADR 0019) |
| `/api/runs?project=&limit=` | GET | Run list (newest first) |
| `/api/runs?project=&q=&from=&to=&status=&failure_kind=&loop=&cursor=&limit=` | GET | History: free text (run ID, loop, trigger, status, note, step summaries; not the log), date range, status/failure class (comma-separated)/loop; cursor-paginated → `{runs, next_cursor}` (ADR 0034, 0035) |
| `/api/runs` | POST `{"project","loop","note"}` | Start a run; if the project is running, it is queued (202, `queued: true`, record status `queued`) and starts with priority over trigger runs (ADR 0015). The response contains `run_id` |
| `/api/run/<id>` | GET | Run record (steps, output) + `cost`: token/cost sum from the step usages, broken down per step and backend; `unpriced_steps` > 0 = the backend reported no cost (the sum is a lower bound) |
| `/api/kpis/costs?project=&from=&to=` | GET | Cost rollup per project (runs, tokens, `cost_usd`, per backend) over the period (`from`/`to` as in the history, `to` as a date = the whole day) + total; the run list carries `cost` per row (issue #126) |
| `/api/kpis?project=&from=&to=` | GET | Factory KPIs (framework-v1-draft §5) per project/loop + total: cycle time (start → accept, successful runs: avg/median/max), judge veto rate (runs with a review and `changes_requested`), defect escape (proxy: an outcome failure after an earlier successful run of the same loop = escaped; runs with a red `verify`/veto/outcome failure otherwise = caught). Also per group: `failed` (status `fail`/`error`), `runtime_s` (count/sum/mean/max over all finished runs) and `cost` (tokens, `cost_usd`, `unpriced_steps` as in `/api/kpis/costs`). Rates are `null` without data. The same values as Prometheus gauges `fabrik_kpi_*{project,loop}` in `GET /api/metrics`; KPI panel in the web UI (issue #127) |
| `/api/run/<id>/log?lines=N` | GET | Raw run log as plain text (tail via `lines`) |
| `/api/run/<id>/stream?project=` | GET | Live log + status changes as server-sent events (`log`/`status`/`end`); reconnect via `Last-Event-ID` (byte offset); secrets redacted (ADR 0038) |
| `/api/run/<id>/transcript?after=&limit=` | GET | Agent turns (prompt, tool calls, answers) from the SessionLog, paginated (ADR 0038) |
| `/api/chat` | POST `{"message","project","req_id"}` | Chat (command or LLM); answers with `req_id` |
| `/api/chat/cancel` | POST `{"req_id"}` | Cancel a running chat request (UI button) |
| `/api/approve` / `/api/reject` | POST `{"project","run_id"}` | Approval gate; the response contains `resumed` (bool) |
| `/api/llm` | GET | LLM providers (never the key) + active provider |
| `/api/llm` | POST `{"provider"}` | Set the active LLM provider (persisted) |
| `/api/health` | GET | Liveness |

All `POST` routes check the origin (own host) and — if `FABRIK_TOKEN`
is set — the bearer token; otherwise `403`. With `FABRIK_AUTH=local`,
**all** `/api/` routes (including `GET`) check session or token according to the role (`401`/`403`);
additionally `/api/login` (POST), `/api/logout` (POST), `/api/session` (GET).

### Connections (ADR 0052)

LLM providers and agent backends can be maintained as **named connections** in the UI (⚙ at the top right,
writing only as `admin`). Env configuration (`FABRIK_LLM_*`, `FABRIK_AGENT_*`) keeps applying and appears
read-only (`env`); a UI connection with the same name overrides it.

- An agent step selects via `connection: <name>`; without it, the connection marked as default applies
  (if its backend matches the step). Precedence: step fields > `connection:` > default > env.
- Secrets (API key, server password) are stored Fernet-encrypted in the `connections` table of the
  state DB (`FABRIK_DB_URL`) and are never returned via the API. Key: `FABRIK_SECRET_KEY`,
  otherwise `state/secret.key` (0600, generated automatically). Without the key, stored secrets
  are unusable — back up the key separately from the DB.

```yaml
steps:
  - {type: agent, name: changes, connection: hetzner-opencode, prompt_file: prompts/develop.md}
```

### Authentication and roles (ADR 0030)

`FABRIK_AUTH=none` (default): as before, loopback bind only. `FABRIK_AUTH=local`:
login with local users (`state/users.json`, scrypt), session cookie
(HttpOnly, SameSite=Strict, `FABRIK_SESSION_TTL` seconds, default 28800) and
a CSRF header `X-CSRF-Token` on writing requests. Roles: `viewer` (read),
`operator` (+ start runs, approve/reject, pause, chat), `admin`
(+ LLM providers, branch switch, spec `apply`). `FABRIK_TOKEN` stays as an
API token (= admin, without CSRF) for CLI/automation. Binding to a
non-loopback address (`fabrik serve --host 0.0.0.0` / `FABRIK_HOST`) without
`local` is refused; TLS via a reverse proxy (+ `FABRIK_COOKIE_SECURE=1`).

```bash
export FABRIK_AUTH=local
fabrik user add anna operator        # the password is prompted for (or --password-stdin)
fabrik user passwd anna
fabrik user role anna admin
```

Run records contain `started_by`, `approved_by`, `rejected_by`.

#### OIDC login (`FABRIK_AUTH=oidc`, ADR 0046)

Sign-in via a central IdP (authorization code flow + PKCE, `state`/`nonce`, ID token validation
via JWKS). Needs the extra: `pip install 'fabrik[oidc]'` (`uv sync --extra oidc`); without it
`fabrik serve` aborts with a message. Register the redirect URI `https://<host>/api/oidc/callback`
at the IdP (https required, `http` only for loopback) and create a confidential client with a secret.
The `state` is additionally bound via a short-lived cookie (`fabrik_oidc_state`, `SameSite=Lax`) to the
browser that started the login (protection against login CSRF). Logging out ends only the
fabrik session, not the one at the IdP; the UI doesn't automatically redirect to the IdP again afterwards.

| Variable | Meaning |
|---|---|
| `FABRIK_OIDC_ISSUER` | Issuer URL (discovery: `<issuer>/.well-known/openid-configuration`) |
| `FABRIK_OIDC_CLIENT_ID` / `_CLIENT_SECRET` | Client credentials |
| `FABRIK_OIDC_REDIRECT_URI` | must be registered at the IdP |
| `FABRIK_OIDC_ROLE_MAP` | `group=role,…` (roles `viewer`/`operator`/`admin`; the highest wins) |
| `FABRIK_OIDC_GROUPS_CLAIM` | Groups claim (default `groups`) |
| `FABRIK_OIDC_SCOPES` | Default `openid profile email` |

Anyone not in a mapped group is rejected (403). Examples:

```bash
# Keycloak (group mapper "Group Membership" → claim "groups", full group path off)
export FABRIK_OIDC_ISSUER=https://kc.example.com/realms/acme
export FABRIK_OIDC_CLIENT_ID=fabrik FABRIK_OIDC_CLIENT_SECRET=...
export FABRIK_OIDC_ROLE_MAP="fabrik-admins=admin,fabrik-ops=operator"

# AWS Cognito (issuer = user pool: https://cognito-idp.<region>.amazonaws.com/<pool-id>)
export FABRIK_OIDC_ISSUER=https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_AbCdEf
export FABRIK_OIDC_GROUPS_CLAIM=cognito:groups
export FABRIK_OIDC_ROLE_MAP="fabrik-admins=admin,fabrik-ops=operator"

# Entra ID (enable the groups claim in the app registration; groups arrive as object IDs)
export FABRIK_OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
export FABRIK_OIDC_GROUPS_CLAIM=groups
export FABRIK_OIDC_ROLE_MAP="11111111-aaaa-...=admin,22222222-bbbb-...=operator"
```

`POST /api/runs` returns `run_id` **before** the worker starts — the UI
(`web/app.js`) then polls via `GET /api/run/<id>` (avoids the
start race in which the new run wasn't yet visible in the history).
`POST /api/approve`/`/api/reject` returns `resumed` (bool): `true` only if
a resume worker was started for the run (e.g. after an approval gate);
`false` if the run is already running or finished. Integrators
should read `resumed` defensively (`if (r && r.resumed)`) — older clients
without the field stay compatible (additive API change).

## Runs

`runs/<project>/<id>.json` (machine-readable: status, steps, docs) +
`runs/<project>/<id>.log` (human-readable, live). Step output is capped at
200 KB. Status: `running`, `pass`, `fail`, `needs_approval`,
`rejected`. The raw `.log` is served via `GET /api/run/<id>/log`
and shown on demand in the run panel via the **📜 log** button (toggle between
the formatted record view and the raw log).

**Single node:** on start, `fabrik serve` sets every `running` record to `error`
(restart recovery, `needs_approval` stays). Two server processes sharing a
`runs/` directory are therefore not supported.

Agent steps write their **conversation** to the log: the prompt (input,
truncated), the assistant texts and tool calls (live, per completed
message) as well as the final text (output). This makes it traceable after a run
what the agent was given, what it did and what it answered.

### Failure classes (ADR 0035)

Failed runs (`fail`/`error`) and steps carry `failure_kind`; the UI shows
it as a badge (loop list, history, run modal), the history filters by it.

| Class | Meaning | Examples |
|---|---|---|
| `interrupted` | aborted from outside | server restart (reaper), dead-man's switch |
| `transient` | external dependency briefly gone | exit 75 (`EX_TEMPFAIL`), network error, HTTP 429/5xx (except 501/505), `git fetch` without network, agent backend unreachable, hanging session |
| `environment` | environment/configuration broken — needs a human | exit 126/127/78 (`EX_CONFIG`), missing file, invalid step configuration, project/loop unknown, HTTP 401/403, token missing |
| `outcome` | genuine negative result | tests red (`failure_kind: outcome` on the step), review `changes_requested`, merge rejected |
| `unknown` | not classified (default, also legacy data) | everything else |

Classification happens at the source (`src/fabrik/failures.py`): steps report the
class in the result or raise typed errors; text patterns exist only as a
fallback for foreign CLI output (agent CLIs, `git`). With `transient`, a
run can carry `retry_after` (UTC), e.g. from `Retry-After`. The class doesn't change
the flow behavior; auto-retry builds on it (ADR 0036).

### Verdict (business result)

The run status says whether the machinery ran; a step can still report a negative
*result* — e.g. `relay-audit`: `audit-publish` runs green and publishes `fail`. A
shell step reports this with a dedicated output line `::fabrik-verdict::<word>` (a–z, `_`, `-`,
max. 20 characters). The line disappears from output and summary and lands as `verdict` in the
step record; the run's verdict is that of the last step that reported one. The UI shows it
as a badge next to the status (list, history, run detail, step): `pass`/`done` green, `fail`/`blocked`
red, otherwise neutral. Flow, retry and alerts stay untouched. Relay reports `pass`/`fail` (audit)
or the handoff status (`active`/`blocked`/`done`, session).

### Run retry (ADR 0036)

Failed runs are optionally repeated automatically — one **retry chain** with backoff per
`(project, loop, trigger)`. Off by default;
enabled via a `retry:` block at project or loop level (the loop replaces
the project block completely, `max: 0` turns it off):

```yaml
retry:
  max: 3                      # additional attempts after the first run
  backoff: [5m, 30m, 2h]      # wait before attempt 1..n (the last one keeps applying)
  on: [interrupted, transient]  # only these classes are retried
  max_wait: 24h               # upper bound for a reported `retry_after`
```

Only classes in `on` (always a subset of `interrupted`/`transient`)
trigger a retry; `unknown`/`outcome`/`environment` end the chain.
Every attempt passes all gates again (approval, blockers) and fetches fresh
trigger variables from GitHub. If the chain exhausts the budget, the job is
listed in the UI under **📦 Shelved** (class, last error, attempt count)
with the actions **↻ retry now** (run with the full budget; without a free slot
it waits with priority like other manual runs) and
**⊘ discard**. A running cron tick is skipped as long as the chain
is `pending`; `reconcile` still counts every run as done.
A closed issue or a merged/closed PR ends the chain
(`dismissed`) — unless the `on_pr` trigger itself fires on closed PRs
(`states: [.., closed]`); a restart during an attempt consumes it (`interrupted`).
As long as the chain is `pending`, there is no `run_failed`; the final
failure arrives as `run_exhausted` — also with `events: [run_failed]`.

**One-off inventory reconciliation** (`fabrik retry-backfill`, idempotent): creates a chain for
triggers whose *entire* runs failed (inventory from before `retry` was introduced) — if the class
of the last run is in `on` of the loop's policy → `pending` (starts on the next scheduler tick after the backoff;
a `retry_after` of the last run counts, capped at `max_wait`), every other
class → `exhausted` (lands in the "Shelved" list for review). The
old runs don't count against the budget — the first attempt is attempt 1 with the
full budget. Whether the trigger still exists (issue closed, PR merged) is not checked by the
reconciliation: the attempt queries GitHub on the first tick and then files the
chain as `dismissed`. Existing chains and groups with
non-failed runs stay untouched.

### Run store (ADR 0034)

Runs are read and written only via the run store (`fabrik.runstore.STORE`);
the backend is selected by `FABRIK_RUN_STORE`:

- `sql` (default): relational schema (SQLAlchemy Core + Alembic) under `FABRIK_DB_URL`,
  default `sqlite:///$FABRIK_HOME/state/fabrik.db` (WAL, `busy_timeout`,
  foreign keys). Tables for runs, steps, usage, review results, verify checks,
  approvals, run variables and the session log (`run_events`, `run_artifacts`,
  `run_checkpoints`); the DB enforces status, required fields and foreign keys. The
  schema migrates to `head` automatically on first access (`src/fabrik/migrations`).
  Run search is an indexed `SELECT`. Run logs (`runs/<project>/<id>.log`)
  stay files.
- `json`: the former storage `runs/<project>/<id>.json` (fallback, tests).

Existing run JSON and `sessionlog.db` are taken over by `fabrik migrate-runs` (first
`--dry-run`: writes nothing and lists every record that can't be mapped or that differs,
with run ID and reason; idempotent). After a real import, the imported run JSON
is moved to `state/runs-archive/` (never deleted; layout
like `runs/`) — broken or differing records stay in `runs/`. Sessions
without a run record (e.g. test leftovers) are counted, not imported.

In the SQL store, runs are no longer readable via `cat`/`jq`: `fabrik runs show <run_id>`
shows a single run (fields, steps, session log counts), `fabrik runs export
<run_id>` prints record + session log as JSON.

### Session log (SQLite, ADR 0002)

In the SQL store (default) the session log lives in the run store (see above); what
follows describes the JSON operation.

Next to the run JSONs there is an append-only event store:
`$FABRIK_HOME/sessionlog.db` (override: `FABRIK_SESSIONLOG`), WAL mode.
Tables: `sessions` (one row per run), `events`
(`run_start`/`step_start`/`step_done`/`tool_call`/`message`/`cost`/
`approval`/`run_end`), `artifacts`, `checkpoints` (only `pass` steps).

The run JSON stays the single source of truth for the engine — the log is
a durable, queryable projection (raw data: tokens + cost from the agent,
no price table). Agent steps additionally land tool-call events per
newly seen tool. Existing P0 runs are imported via
`fabrik import-runs` (idempotent, `source='import'` — native sessions
win). Log errors never fail a run (best effort).

## SonarQube scan

`scripts/sonar-scan.sh` scans the code (pysonar) and checks the quality gate
(daily on main, loop `sonar-scan`; PR analysis via the community branch plugin,
loop `sonar-pr`). The daily main scan additionally cleans up orphaned
legacy PR projects (`scripts/sonar-pr-cleanup.py`, ADR 0053): before native
PR analysis (#524), a separate project
`quark-playground:fabrik:pr-<n>` was created per PR. All `…:pr-<n>` projects whose PR
is no longer open on GitHub are listed and deleted. Fail-safe:
undeterminable PR status → the project stays; a cleanup failure is
only a warning and doesn't change the scan's exit code.
`scripts/sonar-pr-cleanup.py --dry-run` shows what would be deleted.

## Deployment profile (ADR 0056)

`--profile homelab|cloud` (global, before the subcommand) or `FABRIK_PROFILE`; default `homelab`,
CLI beats env. At startup the profile selects the adapter per port (`fabrik.profiles`):
`homelab` → local store, `cloud` → placeholder (`NotImplementedError`, until the follow-up issues
land). New ports/adapters are registered in `profiles._build_default_registry`; the
business logic doesn't know the profile name (guard test in `tests/test_profiles.py`).

## Security

- The server binds to **127.0.0.1 only by default**; other addresses only with `FABRIK_AUTH=local` or `oidc`
- The API is hardened against cross-origin: `POST` requests are accepted only from the own
  origin (no `Access-Control-Allow-Origin: *`), so that an
  arbitrary web page in the operator's browser can't fire shell steps
  (drive-by RCE). Optional: `FABRIK_TOKEN` enforces a bearer token for all
  `POST` endpoints.
- Pushing to `main`/`master` (or the `protected:` list) is hard-refused
- Substituted branch/head names (`git`/`pr` step) are validated before git/API calls
  (`validate_branch_name`: allowlist `[A-Za-z0-9._/-]`, no `..`, no
  leading `-`, max length)
- No step can read outside `workdir`/`WEB_DIR` (path traversal check);
  `report.write` paths must stay inside the project root
- LLM steps inject only schema keys into the ctx (no overwriting of
  trusted variables — untrusted input stays data)
- Secrets are masked from logs **and** run records (env + `fabrik.env` +
  spec env values); git error messages are redacted
- SonarQube: use only the cached token from `state/sonar-token.txt`
  (`state/` and `fabrik.env` are gitignored); `SONAR_ADMIN_PASSWORD` serves
  exclusively to mint a fresh token — never write it to the repo, issues,
  commits or logs
- `send` loops (irreversible, e.g. Signal) are `manual`-only
- Spec changes from the LLM chat are written only after `apply`
- Agent containment: file tools confined to the workdir; shell access requires
  a non-root/container deployment for real isolation

### Label migration (`ready to develop` → `ready for development`)

Relabel existing issues once (needs `gh`, create the label first):

```bash
gh label create "ready for development" --repo OWNER/REPO || true
for n in $(gh issue list --repo OWNER/REPO --label "ready to develop" --state all --limit 500 --json number -q '.[].number'); do
  gh issue edit "$n" --repo OWNER/REPO --add-label "ready for development" --remove-label "ready to develop"
done
gh label delete "ready to develop" --repo OWNER/REPO --yes   # optional, afterwards
```

## Projects in use

- `kyudo-neandertal` (private, `FABRIK_DATA`) — Drupal 7→11 migration (lint=CI checks, migrate=issue→PR
  with approval gate, `github_issue` alerts on failures)
- `echochamber` — meeting pipeline (lint/smoke, worktree on the M3 branch)
- `gods-last-garden` (private, `FABRIK_DATA`) — Godot FPS (lint=import, test=headless battery)

## Roadmap

- [x] `agent` steps with the opencode backend (P1; SPIKE: `docs/spike-opencode.md`)
- [x] SQLite session log (final schema, ADR 0002; `fabrik import-runs`)
- [x] `review` step: cross-model cold critic (ADR 0003)
- [ ] More agent backends (claude/codex) via the same interface
- [ ] Loop dependencies (loop A must be green before loop B)
- [x] Bounded worker pool for trigger runs (`FABRIK_WORKERS`; local = 1)
- [ ] Spec editor in the UI (direct, without chat)
