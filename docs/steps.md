# Step types

Built-in types are listed in `config.STEP_TYPES`; third-party types register through the `fabrikant.steps`
entry point ([plugins.md](plugins.md)). Field-level reference: [spec-reference.md](spec-reference.md).

| Type     | Fields                                                        | Behavior |
|----------|---------------------------------------------------------------|----------|
| `shell`  | `cmd`, `workdir`, `env`, `timeout` (default 600 s), `name`, `failure_kind` | Run `sh -c`; exit ≠ 0 aborts the loop. `failure_kind` (`outcome`\|`transient`\|`environment`) names the [failure class](runs.md#failure-classes-adr-0035) for exit ≠ 0, e.g. `outcome` for test/lint steps. Fixed exit codes take precedence: 126/127 and 78 (`EX_CONFIG`) → `environment`, 75 (`EX_TEMPFAIL`) → `transient` — this is how a script reports the class itself; death by signal (OOM kill, SIGTERM; exit > 128) stays `unknown`. Allowed on shell steps only |
| `git`    | `fetch`, `branch`, `track`, `commit: {add, message}`, `push` | Git op; `push` blocks on protected branches; `commit` on a clean tree is a no-op |
| `pr`     | `repo: owner/name`, `base`, `head`, `title`, `body`          | PR via the GitHub API (token from `GITHUB_TOKEN` / `FABRIKANT_ENV_FILE` / `~/.config/fabrikant/fabrikant.env`). An open PR for `head` → reused; `head` without commits ahead of `base` → no PR, the run ends successfully, follow-up steps `skipped` (the tier gate still applies) |
| `agent`  | `backend` (e.g. `opencode`), `connection`, `prompt`, `prompt_file`, `workdir`, `endpoint`, `model`, `variant` (`opencode` only: model variant from the opencode config, e.g. reasoning effort), `permissions`, `timeout`, `github_identity` — or legacy: `cmd`, `prompt` | Agent task via a pluggable [backend](backends.md) (session + prompt + polling) or an agent CLI. Without `backend`/`cmd` = cleanly skipped. `github_identity: dev\|reviewer` (CLI backends only) sets the role's `GH_TOKEN`/`GITHUB_TOKEN` in the agent process — `gh` (and `git` with `gh` as credential helper) then runs as the builder or QA app instead of as the local user. A `gh` wrapper at the front of the PATH reads the token fresh from `FABRIKANT_ENV_FILE` on every call, so runs with `timeout` > 1h survive the 1h installation token |
| `report` | `text`, `write` (optional: file path, relative to the project root) | Message to the run log and/or a report file |
| `http`   | `url`, `method`, `headers`, `body`, `expect: {status}`, `emit: {var: body\|status}` | HTTP request; the response/status becomes available as a variable for following steps |
| `llm`    | `prompt`, `schema: {var: type}`, `untrusted: [vars]`, `provider`, `model`, `temperature`, `extra_context` | Structured LLM call; the JSON is validated against the schema, keys become variables. Schema value: a type or `{type, enum, default}` — `enum` = whitelist, `default` makes the key optional. `provider` pins a configured LLM provider (otherwise the active one), `model` overrides the provider model. `untrusted` marks external content as data (prompt-injection protection). Per call, `usage` (tokens + model + provider) is written to the run record (tracing building block for cost exporters) |
| `notify` | `message`, `slack`, `webhooks`                                | Notification as a step; without channels = log only |
| `state`  | `action` (`get`\|`set`\|`add`), `key`, `value` (substitution allowed), `file` (default `state/<project>.json`) | Persistent state across runs (JSON, atomic write). Emits `state_value` as a variable. For "already processed" counters, round caps, last-seen in cron loops. See [examples](#state-examples) |
| `alert_route` | `routes: {category: loop}`, `fallback` | Deterministic routing `{category}` → hotfix loop (no LLM); unmapped → `fallback` or no hotfix. Targets (`none` or another loop of the project) and route cycles are checked at spec load. See [Incident round trip](triggers.md#incident-round-trip) |
| `verify` | `checks: [{type, ...}]` (each check a normal step def: `shell`\|`http`\|...) | Check-list semantics: all checks are run, output per check goes into the run record. All green → `pass`; one red → `fail` with check name + output. **Caution:** sub-checks have full step semantics — `llm` checks mutate the ctx, `state` checks persist. LLM judges / screenshot diffs may be added later as check types |
| `issue`  | `action` (`create`\|`edit`\|`label`\|`close`\|`list`\|`block`), `repo`, `number` (except `create`; default `{issue_number}`), `title`, `body`, `labels`, `remove_labels`, `state`, `comment`, `append`, `unless_contains`, `only_if`, `skip_if_empty` | Native GitHub issue operation (REST, no shell/`gh`, no LLM); see [`issue` step](#issue-step) |
| `review` | `model` (required), `backend`/`endpoint` (as `agent`), `target` (optional), `prompt_file` (optional; resolved as for `agent`) | Cross-model cold critic (ADR 0003): agent session with a **different model** than the builder step (engine check). The reviewer answers with a JSON verdict (`approved`\|`changes_requested` + findings, optional `acceptance` — AC verification `[{criterion, status: met\|unmet\|unverifiable, evidence}]`, ADR 0013; invalid → no verdict) → lands in the run record (`step.review`). `changes_requested` → run `fail`. `target` default: output of the last agent step. See [example](#review-example) |
| `pr_review` | `repo`, `pr`, `model` (required), `backend`, `max_rounds` (default 2), `max_findings` (default 5), `fix_backend`/`fix_model`/`fix_variant` (default: last agent step), `fix_timeout` (default 1800 s), `fix_checks: [cmd]`, `fix_check_timeout` (default 900 s) | LLM review of the PR diff + GitHub review; on `changes_requested` the builder (backend/model/variant of the last `agent` step) fixes and pushes. `fix_checks` (e.g. lint + test) run after the fix **before** commit/push; a red check aborts the step (`outcome`), nothing is pushed |
| `pr_merge` | `repo`, `pr`, `method` (`squash`\|`merge`\|`rebase`), `update_branch` (default `false`) | Merge a PR. `update_branch: true`: if the PR is behind the base, first `update-branch` (merge the base into the PR) and wait for mergeability; conflict = step red (`outcome`). The update merge commit is **not** tested again (trade-off: otherwise the PR would be stranded behind a moving `main`) |
| `human_gate` | `repo`, `pr` (default `{pr_number}`), `adr_paths`, `max_files` (default 40), `max_lines` (default 1500), `keywords`, `llm` (default `true`), `provider`, `model`, `max_tokens`, `timeout` | Criticality check on the PR diff (ADR 0010): heuristics (ADR paths, diff size, keywords) plus an LLM opinion, fail-closed. Critical → the run pauses (`needs_approval`), otherwise the loop continues autonomously. The pass is persisted, so a resume doesn't repeat the check |
| `escalation` | `repo`, `item` (default `{pr_number}`, else `{issue_number}`), `label` (default `needs human`) | Escalation to a human (ADR 0059): if the item carries the label, the run pauses with a kind (`blocked`\|`stalled`\|`exhausted`\|`scope`\|`access`) and the reason from the last `fabrikant-escalate` comment. Approve = resume (label removed), reject = give up (label stays). The label is set by `scripts/pr-status.py` (`escalate`, `escalation-check --escalate`) |

## `issue` step

Deterministic issue operations over the GitHub REST API (ADR 0018). All string fields are `{vars}`-substituted
and go to the API as a JSON payload — never through a shell, so untrusted text (LLM output, issue body) is safe.

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

**Auth:** `GITHUB_TOKEN` (env or `FABRIKANT_ENV_FILE`, developer identity as with `pr`). The token ends up in no
step result; logs and run records are additionally protected by secret masking (`[REDACTED]`). Error → step
`fail` with the HTTP status.

## `review` example

```yaml
# Builder (e.g. opencode with model A) …
- {type: agent, name: build, backend: opencode, endpoint: "…", model: "model-a", prompt: "…"}
# … then the cold critic with model B (must be ≠ A, otherwise the step fails)
- {type: review, name: critique, backend: opencode, endpoint: "…", model: "model-b"}
# … only after that the PR step
```

The reviewer sees only the review target (fresh context, no bias). Unreadable verdict output fails the step — no
silent pass. The same applies to an invalid `acceptance` field (ADR 0013). In the `pr_review` step the ACs of the
linked issue go into the reviewer's prompt as untrusted data; at least one `unmet` criterion forces
`changes_requested` (bounded fix rounds), `unverifiable` doesn't block but is traceable in the review text and
in the run record.

## `extra_context` (all LLM nodes)

Additional prompt context for `llm`, `agent`, `review`, `pr_review` (review and fix prompt) and `human_gate`. At
project level it applies to all these steps; a step adds to it (same `name` → the step wins),
`extra_context: false` turns it off.

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

Exactly one source per entry: `file` (relative to the step workdir — `workdir:` on the step, otherwise the run's
—, must not leave it), `cmd` (`sh -c` in the step workdir, stdout only, `timeout` default 300 s; placeholders
only for trusted values as in the `shell` step) or `value` (template). **The default is untrusted**: the content
is marked as data, as with `untrusted:`. `trusted: true` only for repo-owned files and tool output — issue text
or PR comments stay untrusted. `<untrusted`/`<context` tags in the content are neutralized (`&lt;`) so text can't
close or fake a block. Sources are reloaded on every prompt build (`pr_review`: per round). A source that can't
be loaded doesn't abort the step; the block names the reason, the log warns. Budget: `max_chars` per source
(default 50,000 characters), all sources of a prompt together at most 120,000 characters — in spec order, what
doesn't fit is truncated or omitted (log warning). Checks in a `verify` step get the project context like
top-level steps. The loop templates `develop` and `review-pr` pass it through to their LLM steps via the
parameters `builder_context`/`review_context`.

For the `sonar-scan.sh --pr` quality report used above see [sonarqube.md](sonarqube.md#pr-mode-quality-report-in-the-review).

## `state` examples

```yaml
# Remember the last processed PR state (cron loop, idempotent)
- {type: state, name: last, action: get, key: last_pr_head}
- {type: state, name: mark, action: set, key: last_pr_head, value: "{pr_head_sha}"}

# Round cap: increment a counter, the loop stops when >= 5
- {type: state, name: bump, action: add, key: rounds, value: "1"}

# Maintain a list (non-numeric value → append)
- {type: state, name: seen, action: add, key: seen_issues, value: "{issue_number}"}
```

The state lives under `state/<project>.json` in the project root (default) — deliberately **deterministic** (no
agent access), so cron loops cleanly recognize repeated runs. Path traversal outside the project root is blocked.

## Gates, approval and tiers

**`gate`** (per step, default `true`): controls what a step failure does. `gate: true` → the loop aborts
immediately (run = `fail`). `gate: false` → the failure is noted, the loop continues, the run ends as `fail` at
the end. Useful for "best-effort" steps (e.g. optional cleanup or notification steps) that shouldn't block the
rest of the loop.

Every step can have `approval: true` → the run pauses **before** this step with status `needs_approval`. Approve
via `fabrikant approve <run_id>`, `POST /api/approve` or the button in the UI. Rejecting aborts the run. This
keeps the human completely out of the loop — stepping in only for the critical actions.

**Conditional gate** (ADR 0017): `approval_when: {label: [needs approval, close]}` pauses only if the run
variables match (AND across variables, OR across values; missing variable → no gate). Can't be combined with
`approval: true`. An unmet `approval_when` doesn't replace the tier gate.

**Reason:** `approval_reason: "{reasoning}"` (a template with run variables, e.g. from a previous `llm` step)
appears in the approval context (UI, chat, log) as the **why** — next to the **what** (action derived from the
spec) and the satisfied **condition**. If a referenced variable is missing, the line is omitted.

**Risk tiers** (per loop: `tier: 1-4`): T1 runs through automatically after green checks; T2+ ends with a
synthetic tier approval step (`needs_approval`), even if no single step has `approval: true`. The mapping lives
in one function (`tier_policy`) — P1/P2 extend it instead of scattering logic.

**Locking** (ADR 0020): all loops of a project share one git worktree and therefore run one after another
(project lock). A loop with `worktree: false` instead gets an empty workdir per run and its own lock — it runs
in parallel to all other loops, but never twice. Allowed only without `git`/`pr`/`agent`/`review`/`pr_review`
steps (spec validation). If the spec changes between lock selection and run, the run falls back to the project
lock; scratch cleanup errors land in the run log.

## Substitution

In `cmd`/`env`/`text`/`prompt`: `{project}`, `{root}`, `{workdir}`, `{branch}`, `{head}`, `{loop}`, `{trigger}`,
`{run_id}`, `{fabrikant_home}` (fabrikant checkout, e.g. for `{fabrikant_home}/scripts/pr-status.py` from other
projects) + all `env:` variables of the spec. Trigger variables are listed in [triggers.md](triggers.md).
