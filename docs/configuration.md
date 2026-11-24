# Configuration

Field-level reference of the spec: [spec-reference.md](spec-reference.md) (generated from
[`fabrikant-spec.schema.json`](../src/fabrikant/fabrikant-spec.schema.json), the single source of truth that
`fabrikant validate` checks against). Template for the env file: [`fabrikant.env.example`](../fabrikant.env.example).

## Environment variables

| Variable | Meaning |
|---|---|
| `FABRIKANT_HOME` | Home of runs, state and session log. Default: the directory holding `fabrikant.env` when you start from it (see `fabrikant init`), else `~/.local/share/fabrikant` ([plugins.md](plugins.md#install)) |
| `FABRIKANT_DATA` | Alternative data dir; specs are looked up in `FABRIKANT_DATA/projects` |
| `FABRIKANT_ENV_FILE` | Path to the env file with `GITHUB_TOKEN` (default `~/.config/fabrikant/fabrikant.env`) |
| `GITHUB_TOKEN` | GitHub API token (developer identity) |
| `FABRIKANT_TOKEN` | Optional bearer token for the API `POST` routes ([auth](auth.md)) |
| `FABRIKANT_WORKERS` | Worker pool size for trigger runs (default 4) |
| `FABRIKANT_WORKTREES` | Worktree root, see [operations](operations.md#parallel-runs-and-worktrees-adr-0028) |
| `FABRIKANT_PROJECTS_PATH` | Extra spec search path, colon-separated |
| `FABRIKANT_LLM_PROVIDERS`, `FABRIKANT_LLM_<NAME>_{URL,MODEL,KEY}` | LLM providers ([chat](chat.md#llm-copilot)) |
| `FABRIKANT_AGENT_*`, `FABRIKANT_SANDBOX`, `FABRIKANT_EGRESS_*` | Agent backends ([backends](backends.md)) |
| `FABRIKANT_AUTH`, `FABRIKANT_OIDC_*`, `FABRIKANT_SESSION_TTL` | [Authentication](auth.md) |
| `FABRIKANT_RUN_STORE`, `FABRIKANT_DB_URL`, `FABRIKANT_SECRET_KEY` | [Run store](runs.md#run-store-adr-0034), [connections](chat.md#connections-adr-0052) |
| `FABRIKANT_URL` | Base URL used by the relay dispatcher (default `http://127.0.0.1:8321`) |
| `FABRIKANT_PROFILE` | [Deployment profile](#deployment-profile-adr-0056) |

## Minimal spec

```yaml
name: my-app
description: "Drupal 7→11 migration"
root: ~/src/my-app
max_parallel: 3                             # concurrent runs (default 3)
repo:
  branch: master
  subpath: drupal11
github: acme/my-app                         # for on_issue + github_issue alerts

notify:                                     # alert channels on (default: run_failed)
  slack: https://hooks.slack.com/services/…
  webhooks:                                 # generic: Datadog, CloudWatch, …
    - {name: datadog, url: https://http-intake.logs.datadoghq.com/v1/input?api_key=…}
  github_issue: true                        # on run_failed: open an issue in the repo
  issue_labels: [fabrikant]
  events: [run_failed]                      # also: [run_failed, run_passed]; run_failed includes run_exhausted

loops:
  migrate:
    description: "Drupal changes as branch + PR"
    triggers:
      - {type: on_issue, labels: [migration], poll: 300}   # new issue → loop
    steps:
      - {type: git, name: prepare, action: prepare, message: "fabrikant: …"}
      - {type: agent, name: changes, prompt_file: prompts/….md}
      - {type: shell, name: checks, cmd: "bash scripts/run_checks.sh"}
      - {type: pr, name: pr, base: master, title: "…", approval: true}
```

Step types: [steps.md](steps.md). Triggers and alerts: [triggers.md](triggers.md).

## Spec sources, `vars` and loop templates (ADR 0047)

**Where specs live** (first hit wins; the same `name:` in two sources is reported as an error by
`fabrikant validate`):

1. `FABRIKANT_PROJECTS_PATH` — colon-separated like `PATH` (folders with `*.fabrikant.yaml` or a repo with `.fabrikant/fabrikant.yaml`)
2. `FABRIKANT_DATA/projects` (without `FABRIKANT_DATA`: `<home>/projects`)
3. Target repos with `.fabrikant/fabrikant.yaml`, registered via `fabrikant project add <repo-path>` (without `name:` the repo folder applies, without `root:` the repo)
4. The bundled examples in `projects/`

**Paths:** `prompt_file` and `scripts/...` in shell commands are resolved relative to the spec file, then relative
to the installation (`prompts/...`, `scripts/...` of the factory — so keep script names unique).

**`vars:`** — `{name}` applies throughout the spec (type-preserving if the string is only `{name}`). Other `{x}`
stay runtime variables (`{issue_number}`, `{root}`, ...; forbidden as `vars` names). `vars` may reference other
`vars` (`a: "{b}"`); cycles are an error.

**Loop templates** (`src/fabrikant/templates/<name>.loop.yaml`; bundled: `develop`, `review-pr`, `escalation`):
`use:` + `with:`. Required parameters (`null` in the template) come from `with` or `vars`; further loop keys
override the template, `insert: [{after: <step>, steps: [...]}]` inserts steps (not together with own `steps:`).
Unknown `with` keys (neither `params` nor placeholders in the template) are an error. A second project (repo
`acme/app`):

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

If the spec lives as `.fabrikant/fabrikant.yaml` in the repo, the prompts sit next to it
(`.fabrikant/develop.prompt.md`). `projects/fabrikant.fabrikant.yaml` is itself an instance (repo name only in
`vars`).

## GitHub identity and commits

**GitHub Apps (ADR 0010):** `scripts/gh-token-refresh.py` renews the 1h installation tokens — without arguments
the builder app (`BUILDER_APP_*` → `GITHUB_TOKEN`), with `--role reviewer` the QA app (`REVIEWER_APP_ID`,
`REVIEWER_APP_KEY_FILE`, `REVIEWER_INSTALLATION_ID` → `GITHUB_REVIEWER_TOKEN`). One cron line each, every 30
minutes, or the `deploy/gh-token-refresh*.{service,timer}` units, or the `token-refresh` loop while the server
runs.

**Tokens per project** (GitHub-specific implementation detail, no ADR): `github_env_file: <path>` in a spec
replaces `FABRIKANT_ENV_FILE` for that project: runs, trigger polls, retry attempts and their subprocesses (shell
steps, agent `gh`) read `GITHUB_TOKEN`/`GITHUB_REVIEWER_TOKEN` only from this file; global `GITHUB_TOKEN` env
values don't apply there (shell steps get `GH_TOKEN`/`GITHUB_TOKEN` = the project's builder token; subprocesses
recognize the override via `FABRIKANT_PROJECT_ENV_FILE`). Use it for a different app installation (other org) or
a PAT as reviewer. Refresh via cron with an env override, e.g.
`FABRIKANT_ENV_FILE=<path> BUILDER_INSTALLATION_ID=<id> scripts/gh-token-refresh.py`.

**Signed commits:** `commit_via: api` in a spec (default `git`) doesn't push with `git push` but rebuilds the
local commits through the Git Data API (`src/fabrikant/apicommit.py`). GitHub signs them itself for the builder
app ("Verified", author = app bot) — for repos with `required_signatures`, without keys on the host. Applies to
`git` steps (`push: true`), the fix rounds of `pr_review` and relay pushes. Agents that push themselves call
`python -m fabrikant.push [--force]` instead of `git push` (the route per project is selected via
`FABRIKANT_COMMIT_VIA`). Limits: no root commit; the local author/timestamp are lost.

## Deployment profile (ADR 0056)

`--profile homelab|cloud` (global, before the subcommand) or `FABRIKANT_PROFILE`; default `homelab`, CLI beats
env. At startup the profile selects the adapter per port (`fabrikant.profiles`): `homelab` → local store,
`cloud` → placeholder (`NotImplementedError`, until the follow-up issues land). New ports/adapters are
registered in `profiles._build_default_registry`; the business logic doesn't know the profile name (guard test
in `tests/test_profiles.py`).
