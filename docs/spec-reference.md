# Spec reference

Machine-readable single source of truth: [`src/fabrikant/fabrikant-spec.schema.json`](../src/fabrikant/fabrikant-spec.schema.json).
This page is generated from it (`scripts/generate-spec-reference.py`) — do not edit by hand.

The schema checks the **structure** (types, enums, required fields, ranges) of the *expanded* spec
(after `vars:`/template/relay expansion). Semantic checks (backend registry,
cron-vs-retry intervals, alert route cycles, template parameters) additionally run in
`src/fabrikant/specs.py`. `fabrikant validate` runs both and warns about keys the schema doesn't know
(a typo or a key from a newer fabrikant version would otherwise silently have no effect).

Legend: **required** = mandatory, *italic* = optional. Types in ``code``.

## Top-Level

A project spec (`*.fabrikant.yaml` / `*.fabrikant.json`) defines loops that run on the project `root`. `loops` is the only required field; all other fields are optional.

| Field | Type | Status | Description |
|---|---|---|---|
| `name` | string | optional | Project name. Defaults to the file name without suffix. Must not contain '::' (lock-key separator). |
| `root` | string | optional | Path of the project root the loops operate on (~ and env vars are expanded). Required for validation; defaults to the repo root for specs in .fabrikant/fabrikant.yaml. |
| `description` | string | optional | Human-readable project description (UI, docs). |
| `env` | object | optional | Extra run-context variables (string values) available as {name} in all steps. |
| `max_parallel` | → `numeric` | optional | Maximum parallel runs of this project (integer >= 1, default 3, ADR 0028). |
| `github` | string | optional | GitHub repo as owner/name. Default repo for on_issue/on_pr triggers, issue/pr steps and alerts. |
| `hosting` | string | optional | Code-hosting provider name, e.g. github, gitlab, or a registered third-party provider. Inferred from the repo host if omitted. |
| `repo` | object | optional | Git repo of the project: every run gets a worktree detached on origin/<branch> (ADR 0028). |
| `github_env_file` | string | optional | Absolute path to a KEY=VALUE file with GitHub tokens (GITHUB_TOKEN, GITHUB_REVIEWER_TOKEN). |
| `commit_via` | `git` \| `api` | optional | How commits are made: local `git` (default) or signed commits via the GitHub API (ADR 0010 amendment). |
| `protected` | list of string | optional | Branch names the loops must not touch (legacy, informational — the engine does not enforce it). |
| `notify` | object | optional | Alert channels for run events (run_failed, run_exhausted, run_passed). |
| `notify_events` | list of `run_failed` \| `run_exhausted` \| `run_passed` | optional | Legacy top-level form of notify.events. |
| `extra_context` | list of → `context_source` | optional | Project-level prompt context sources for LLM steps (ADR 0008). Step-level extra_context supplements these. |
| `retry` | → `retry` | optional | Retry policy for failed runs (ADR 0036). max: 0 disables retry. Loop-level retry overrides the project-level block. |
| `vars` | object | optional | Spec-level variables substituted as {name} in all strings (ADR 0047). Keys must be identifiers and must not shadow reserved runtime variables (project, root, issue_number, ...). |
| `relay` | → `relay` | optional | Relay tasks (ADR 0037): long-running work as a chain of short audited sessions. Generates the loops relay-dispatch, relay-session and relay-audit. |
| `loops` | object | required | Loop name -> loop definition. At least one loop is required. |

## Loop

A loop: a chain of steps started by triggers. Concrete loops need steps; template loops (pre-expansion) use use:/with:/insert: instead (ADR 0047).

| Field | Type | Status | Description |
|---|---|---|---|
| `description` | string | optional | Human-readable loop description (UI). |
| `steps` | list of → `step` | required | Ordered step chain. Every step stops the run on failure by default. |
| `triggers` | list of → `trigger` | optional | What starts the loop (manual, cron, on_file, on_issue, on_pr, on_webhook, on_alert, on_slack). |
| `worktree` | boolean | optional | false = no git worktree, empty scratch dir per run (ADR 0020); only allowed without worktree-requiring steps (git, pr, agent, review, pr_review). |
| `tier` | integer | optional | Risk tier as spec data (1 = lowest, 4 = highest); mapped to gates/approvals by a single mapping function. |
| `retry` | → `retry` | optional | Retry policy for failed runs (ADR 0036). max: 0 disables retry. Loop-level retry overrides the project-level block. |
| `use` | string | optional | Template loop (pre-expansion): name of a <name>.loop.yaml template (ADR 0047). Expanded before validation. |
| `with` | object | optional | Template loop (pre-expansion): parameter values for the template. |
| `insert` | list of object | optional | Template loop (pre-expansion): extra steps inserted after named template steps. Cannot be combined with own steps. |

## Step

One step of a loop. `type` selects the step kind; the per-type fields are listed under that type. Common fields (name, gate, approval, ...) apply to all types.

| Field | Type | Status | Description |
|---|---|---|---|
| `type` | string | required | Step kind: a built-in (shell, git, pr, agent, report, http, llm, notify, state, verify, review, pr_review, pr_merge, issue, human_gate, alert_route, escalation) or a plugin registered via the `fabrikant.steps` entry point (ADR 0069); membership is checked at load time. |
| `name` | string | optional | Step name (run log, insert anchors, reports). |
| `gate` | boolean | optional | false = a failure of this step does NOT stop the run (default true). |
| `approval` | boolean | optional | true = pause the run for human approval before this step (ADR 0015). |
| `approval_when` | object | optional | Conditional approval (ADR 0017): mapping variable -> value or list of values; pauses only when the variable matches. Not combinable with approval: true. |
| `approval_reason` | string | optional | Template for the approval request text ({variables} allowed). |
| `failure_kind` | `transient` \| `environment` \| `outcome` | optional | Failure class (ADR 0035) for non-zero exit codes; shell steps only. |
| `extra_context` | list of → `context_source` | optional | Step-level prompt context sources for LLM steps (ADR 0008); false disables the project-level context for this step. |
| `cmd` | string | optional | shell: command (sh -c in the workdir; {variables} substituted). |
| `timeout` | → `numeric` | optional | Step timeout in seconds (>= 0; shell, http, llm, agent). |
| `env` | object | optional | shell: extra environment variables for the command. |
| `branch` | string | optional | git: branch to create/checkout (template allowed). |
| `commit` | object | optional | git: commit the worktree changes (add = pathspecs, message = commit message). |
| `fetch` | boolean | optional | git: fetch before checkout. |
| `push` | boolean | optional | git: push the branch after commit. |
| `track` | boolean | optional | git: track the branch. |
| `protected` | boolean | optional | git/pr_review: refuse to touch protected branches. |
| `repo` | string | optional | pr/pr_review/pr_merge/issue/human_gate/escalation: GitHub repo as owner/name (default: project-level github:). |
| `title` | string | optional | pr: PR title. issue (create/edit): issue title. Templates allowed. |
| `body` | string | optional | http: request body (template allowed). |
| `head` | string | optional | pr: head branch (default: the run branch). |
| `base` | string | optional | pr: base branch (default: project base branch). |
| `labels` | string / list of string | optional | pr/issue: labels to set (list or comma-separated string). |
| `prompt` | string | optional | agent/llm: prompt text (template allowed). agent: exactly one of prompt/prompt_file. |
| `prompt_file` | string | optional | agent/review: prompt file path (relative to the project root). |
| `backend` | string | optional | agent/review/pr_review: agent backend name (default: installation default; must be a known backend). |
| `model` | string | optional | agent/llm/review/pr_review: model name. review/pr_review: required (must differ from the builder model, ADR 0003). |
| `endpoint` | string | optional | agent: backend endpoint override. |
| `connection` | string | optional | agent: named connection (ADR 0052) providing the backend credentials; unknown names are a warning, not an error (host state). |
| `variant` | string | optional | agent: backend variant. |
| `sandbox` | string | optional | agent with backend codex: `codex exec --sandbox` mode (default: FABRIKANT_CODEX_SANDBOX or workspace-write). |
| `tools` | string / list of string | optional | agent with backend copilot: `copilot --allow-tool` value (default: FABRIKANT_COPILOT_TOOLS, otherwise --allow-all-tools). |
| `username` | string / number | optional | agent with backend opencode: server basic-auth user (default: FABRIKANT_AGENT_USERNAME or opencode). |
| `password` | string / number | optional | agent with backend opencode: server basic-auth password (default: FABRIKANT_AGENT_PASSWORD; prefer the env var or a connection over a password in the spec). |
| `billing` | `api` \| `subscription` \| `unknown` | optional | llm/agent: billing kind (api = real cost, subscription = list-price equivalent only, unknown); overrides backend auto-detection. |
| `github_identity` | `dev` \| `reviewer` | optional | agent: GitHub identity (token) the agent uses for pushes/PRs. |
| `workdir` | string | optional | agent: working directory inside the worktree. |
| `permissions` | list of string | optional | agent: tool permissions for the session. |
| `retries` | → `numeric` | optional | agent: number of session restarts after the first attempt (integer >= 0, default 2). |
| `stale_after` | → `numeric` | optional | agent: seconds without progress before the session counts as stale (>= 0, default 900). |
| `text` | string | optional | report: report text (template allowed); logged, or written with write:. |
| `write` | string | optional | report: file path (inside the project root) to write the report to. |
| `url` | string | optional | http: request URL (template allowed). Required. |
| `method` | string | optional | http: HTTP method (default GET); pr_merge: squash \| merge \| rebase (default squash). |
| `headers` | object | optional | http: request headers. |
| `expect` | object | optional | http: expected response. |
| `emit` | string / object | optional | http: response parts to store as variables (variable -> body\|status). issue: name of the variable to store the result in (create: issue number, list: issue lines; default issue_number / issues). |
| `system` | string | optional | llm: system prompt. |
| `temperature` | → `numeric` | optional | llm: sampling temperature (>= 0). |
| `max_tokens` | → `numeric` | optional | llm: max completion tokens (integer >= 1). |
| `provider` | string | optional | llm: LLM provider (named connection, ADR 0052). |
| `schema` | object | optional | llm: JSON schema the response must match. |
| `untrusted` | list of string | optional | llm: variable names whose values are marked as untrusted data in the prompt (prompt-injection protection, Leitprinzip 7). |
| `message` | string | optional | notify: message text (template allowed). |
| `slack` | string / object | optional | notify: Slack webhook URL (string or {url: ...}). |
| `webhooks` | list of object | optional | notify: generic webhook targets (Datadog, CloudWatch, ...). |
| `action` | string | optional | state: get\|set\|add on the state file. issue: create\|edit\|label\|close\|list\|block. (Other step types ignore the field; the per-type enum is enforced below.) |
| `key` | string | optional | state: state key (required). |
| `value` | None | optional | state: value to set/add (any JSON type). |
| `file` | string | optional | state: state file path (default: project state dir). |
| `checks` | list of → `step` | optional | verify: check-list of steps that must all pass (issue #117). |
| `target` | string | optional | review: review target (default: the worktree diff). |
| `max_rounds` | → `numeric` | optional | pr_review: max review/fix rounds (integer >= 0; 0 = review only, ADR 0039). |
| `max_findings` | → `numeric` | optional | pr_review: max findings per round (integer >= 1, ADR 0039). |
| `fix_backend` | string | optional | pr_review: backend for the fix rounds. |
| `fix_model` | string | optional | pr_review: model for the fix rounds. |
| `fix_variant` | string | optional | pr_review: backend variant for the fix rounds. |
| `fix_timeout` | → `numeric` | optional | pr_review: fix agent timeout in seconds (integer >= 1). |
| `fix_check_timeout` | → `numeric` | optional | pr_review: fix check timeout in seconds (integer >= 1). |
| `fix_checks` | list of string | optional | pr_review: shell commands that must pass after a fix round. |
| `fix_commit` | boolean | optional | pr_review: commit fix rounds to the PR branch. |
| `pr` | string | optional | pr_review/pr_merge: PR number or owner/name#number. |
| `update_branch` | boolean | optional | pr_merge: update the branch before merging. |
| `number` | string | optional | issue: issue number (for edit/label/close/block; default: the triggering issue). |
| `remove_labels` | list of string | optional | issue: labels to remove. |
| `inherit_labels` | list of string | optional | issue (create): whitelist of labels inherited from the referenced issue (e.g. priority). |
| `limit` | → `numeric` | optional | issue: max items for list (integer >= 1, default 100). |
| `append` | boolean | optional | issue (edit): true = append the body to the existing one instead of replacing it. |
| `unless_contains` | string | optional | issue (edit + append): skip when the current body already contains this marker (idempotency). |
| `state` | `open` \| `closed` | optional | issue (edit/list): issue state (list default: open). |
| `comment` | string | optional | issue (close): comment posted before closing. |
| `state_reason` | string | optional | issue (close): close reason (completed \| not_planned \| duplicate; templates allowed, default: completed). |
| `duplicate_of` | string | optional | issue (close): close as duplicate of #N and re-hang blocker relations onto #N (ADR 0031). |
| `blocked_by` | string | optional | issue (block): issue references like '#12, #14' that block this issue (max 5, ADR 0031). |
| `prerequisite` | string | optional | issue (block): newly created prerequisite issue reference, always added to the blockers. |
| `only_if` | object | optional | issue: condition mapping variable -> value or list of values; the step is a no-op when any variable is missing or has none of the values (AND over variables, OR over values). |
| `skip_if_empty` | string / list of string | optional | issue: variable name(s); the step is a no-op when all of them are empty. |
| `max_files` | → `numeric` | optional | human_gate: diff heuristic — more changed files makes the gate critical (integer >= 1, default 40). |
| `max_lines` | → `numeric` | optional | human_gate: diff heuristic — more +/- lines makes the gate critical (integer >= 1, default 1500). |
| `keywords` | list of string | optional | human_gate: keywords that make the gate critical (default: GDPR/data-loss/migration terms). |
| `adr_paths` | list of string | optional | human_gate: paths whose touch always makes the gate critical (default: docs/adr). |
| `llm` | boolean | optional | human_gate: use an LLM critique in addition to the heuristics. |
| `routes` | object | optional | alert_route: alert category -> target loop (or none) (ADR 0045). Required; targets must be loops of this project and the route graph must be acyclic. |
| `fallback` | string | optional | alert_route: target loop for unknown categories. |
| `item` | string | optional | escalation: the item to escalate (issue/PR reference, template allowed). |
| `label` | string | optional | escalation: label to apply (default: needs human, ADR 0059). |
| `grant_rounds` | → `numeric` | optional | escalation: fix rounds granted on approval (integer >= 0). |

## Trigger

What starts a loop. `type` selects the trigger kind; the per-type fields are listed under that type.

| Field | Type | Status | Description |
|---|---|---|---|
| `type` | `manual` \| `cron` \| `on_file` \| `on_issue` \| `on_pr` \| `on_webhook` \| `on_alert` \| `on_slack` | required | Trigger kind. |
| `cron` | string | optional | cron: 5-field cron expression (local time). |
| `on_start` | boolean | optional | cron: fire once when the scheduler starts (default false). |
| `glob` | string | optional | on_file: glob pattern relative to the project root (default *). |
| `debounce` | → `numeric` | optional | on_file: seconds the newest mtime must be stable before firing (>= 0, default 60). |
| `repo` | string | optional | on_issue/on_pr: GitHub repo as owner/name (default: project-level github:; one of the two is required). |
| `labels` | list of string | optional | on_issue: label filter (AND semantics). |
| `priority_labels` | list of string | optional | on_issue: labels used to sort candidates (first match wins, ADR 0021). |
| `exclude_labels` | list of string | optional | on_issue/on_pr: issues/PRs with any of these labels do not start the loop (ADR 0059). |
| `poll` | → `numeric` | optional | on_issue/on_pr/on_slack: poll interval in seconds (>= 0). |
| `reconcile` | boolean / = `head` | optional | on_issue: fire for every matching open issue without a run record (ADR 0014). on_pr: true/false or 'head' (per head SHA, ADR 0023). |
| `skip_blocked` | boolean | optional | on_issue with reconcile: true: skip issues with open 'blocked by' dependencies (ADR 0031). |
| `max_per_poll` | → `numeric` | optional | on_issue with reconcile: max runs started per poll (integer >= 1). |
| `states` | list of `open` \| `closed` | optional | on_pr: PR states to react to (default: open). |
| `secret_env` | string | optional | on_webhook/on_alert: name of the environment variable holding the shared HMAC secret (required). |
| `map` | object | optional | on_webhook: variable -> JSON path in the payload. |
| `replay_protection` | boolean | optional | on_webhook: require timestamp + nonce in the signature (replay protection, issue #60). |
| `source` | `sentry` \| `cloudwatch` | optional | on_alert: alert source (required). |
| `token_env` | string | optional | on_slack: name of the environment variable holding the bot token (required; the token itself never goes into the spec). |
| `channels` | list of string | optional | on_slack: non-empty whitelist of channel IDs like C0123ABC (required). |
| `pattern` | string | optional | on_slack: regex the alert text must match. |
| `max_pages` | → `numeric` | optional | on_slack: history pages per channel and poll (integer >= 1, default 20). |
| `max_alert_chars` | → `numeric` | optional | on_slack: alert_text is truncated beyond this (integer >= 1, default 4000). |

## Retry block

Retry policy for failed runs (ADR 0036). max: 0 disables retry. Loop-level retry overrides the project-level block.

| Field | Type | Status | Description |
|---|---|---|---|
| `max` | → `numeric` | optional | Number of ADDITIONAL attempts after the first run (integer >= 0, default 3). |
| `backoff` | list of → `duration` | optional | Wait before attempt 1..n; the last value applies to all further attempts (default 5m, 30m, 2h). |
| `on` | list of `interrupted` \| `transient` | optional | Failure kinds (ADR 0035) that may be retried (default: interrupted, transient). |
| `max_wait` | string | optional | Cap for a reported retry_after (default 24h). |

## extra_context entry

One prompt context source (extra_context entry): exactly one of file/cmd/value (ADR 0008). Untrusted by default; trusted: true only for repo-owned files and tool output.

| Field | Type | Status | Description |
|---|---|---|---|
| `name` | string | required | Variable name of the context block. |
| `file` | string | optional | File path relative to the step workdir (must not escape it). |
| `cmd` | string | optional | Shell command (sh -c) in the step workdir; only stdout counts. |
| `value` | string | optional | Template with {variables}. |
| `trusted` | boolean | optional | true = content is instruction; default false = content is marked as data (prompt-injection protection). |
| `timeout` | → `numeric` | optional | cmd timeout in seconds (>= 0, default 300). |
| `max_chars` | → `numeric` | optional | Per-source character cap (integer >= 1, default 50000). |

## Relay block

Relay tasks (ADR 0037): long-running work as a chain of short audited sessions. Generates the loops relay-dispatch, relay-session and relay-audit.

| Field | Type | Status | Description |
|---|---|---|---|
| `repo` | string | optional | GitHub repo as owner/name (default: project-level github:). |
| `allowed` | list of string | required | GitHub logins allowed to file relay tasks (briefs are agent instructions). |
| `bot_login` | string | required | Pinned bot identity for status comments (required). |
| `base` | string | optional | Base branch (default main). |
| `label` | string | optional | Issue label marking relay tasks (default: relay task). |
| `forbid` | list of string | optional | Labels that forbid a task. |
| `dispatch` | string | optional | Cron expression of the dispatcher loop (default */15 * * * *). |
| `builder` | → `agent_role` | optional | Backend/model of a relay role. |
| `reviewer` | → `agent_role` | optional | Backend/model of a relay role. |
| `max_session_minutes` | → `numeric` | optional | Session length cap from the brief (integer >= 1, default 35). |
| `verify_timeout` | → `numeric` | optional | Verify timeout in seconds (integer >= 1, default 300). |
| `audit_timeout` | → `numeric` | optional | Audit agent timeout in seconds (integer >= 1, default 1200). |
| `audit` | boolean | optional | Enable the audit loop (default true). |
| `retry` | → `retry` | optional | Retry policy for failed runs (ADR 0036). max: 0 disables retry. Loop-level retry overrides the project-level block. |
| `trail_archive` | object | optional | Where the session trail is archived before the branch goes (ADR 0037). |

## Step types: required fields

| Type | Required fields |
|---|---|
| `shell` | `cmd` |
| `http` | `url` |
| `llm` | `prompt` |
| `state` | `key` |
| `verify` | `checks` |
| `review` | `model` |
| `pr_review` | `repo`, `model` |
| `alert_route` | `routes` |

## Trigger types: required fields

| Type | Required fields |
|---|---|
| `on_webhook` | `secret_env` |
| `on_alert` | `source`, `secret_env` |
| `on_slack` | `token_env`, `channels` |

_Generated from `src/fabrikant/fabrikant-spec.schema.json` — please make changes in the schema and re-run this script._
