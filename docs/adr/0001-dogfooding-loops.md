# ADR 0001: Fabrik dogfooding — Fabrik develops Fabrik

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-16)
**Decided by:** Dominic
**Amendment:** 2026-10-17 — model assignment decoupled: the invariant is
reviewer ≠ builder; concrete models are deployment details (they change quickly).
**Amendment:** 2026-10-19 — model choice is configuration (**ADR 0022**); replaces
the "Model" column and §3 paragraph 2.
**Amendment:** 2026-10-19 — `review-pr` switched to the native `on_pr` trigger
(§4): the cron + agent + state-file workaround is removed; dedup and
re-review triggering are handled by the engine.
**Context:** PR #10 (P0) and PR #11 (P1) are merged. The factory is production-ready enough
to develop itself. A production spec (CMS migration) serves as the template (triage →
human gate → develop → checks → PR).

## Decision

New project `projects/fabrik.fabrik.yaml` with six loops:

> "Model" column: roles, not bindings — models live in the spec (ADR 0022).

| Loop | Trigger | Tier | Model | Gate |
|---|---|---|---|---|
| `triage` | on_issue (`fabrik`) | T1 | (llm step) | before `apply-label` |
| `develop` | on_issue (`ready to develop`) | T2 | default (builder) | before `pr` |
| `refactor` | manual (`--var target=…`) | T2 | default (builder) | before `pr` |
| `refactor-sweep` | manual | T2 | default (builder) | end of run |
| `review-pr` | on_pr (`fabrik`) | T1 | reviewer (≠ builder, guiding principle 4) | — |
| `pr-reply` | cron `*/15` | T1 | builder (as `develop`) | — |

## Rationale for the details

### 1. Triage as T1 + step approval instead of T2
> **Replaced** (for the refine loop) by ADR 0006 and, for the gate, by
> **ADR 0017**: human gate only on `needs approval`/`close`.

T2 would put the tier gate at the **end** of the run — after the label is applied. That is
useless. Hence: T1 (no end gate) + `approval: true` on the `apply-label` step → the run pauses
**before** the label change. The human sees the classification (run record) and decides.
This is exactly "refine issues after creation, then human change".

### 2. Develop starts automatically after triage
> **Replaced by ADR 0017:** the refine gate is no longer the human gate for
> `develop`; `develop` is secured through `human_gate` + PR review (ADR 0010).

`develop` triggers on the label `ready to develop`, which triage creates (after approval).
No extra human step is needed — the triage gate is the human gate. Starting right away
without a human would be `tier: 1` without step approval; we keep T2 + PR approval because
AGENTS.md leaves critical decisions to the human. Labels are self-healing: triage's
`ensure-labels` step creates `needs approval` + `ready to develop` (`gh label create
--force`); both are also pre-created in the repo. A develop run before the first
triage run is a silent no-op (expected, not a defect).

### 2b. Git workflow: branch beforehand, commit + push after the checks
The worktree is **detached** on `origin/<branch>`. Hence: `git branch` (before the
agent) → agent edits → lint/test → `git commit` + `push` (`ship` step) → `pr` step
with an explicit `head:`. An `action:` field in the git step is a phantom (silently ignored —
the step only knows fetch/branch/commit/push), and a detached HEAD would produce
`head: HEAD` in the PR request (GitHub rejects it). Commit `add: ["."]` respects
`.gitignore` (state/, runs/ do not end up in the commit).

### 3. Cross-model review (guiding principle 4: builder ≠ reviewer)
The **invariant rule**: reviewer and builder must use different models.
The reviewer has fresh context and a different model family — defects that both
miss are the only ones that get through. Concrete model names are
deployment details and change quickly with the field — they live in the spec
(`projects/fabrik.fabrik.yaml`), not here.

> **Replaced by ADR 0022:** model/backend per step is spec configuration;
> `develop` and `pr-reply` share the builder model via a YAML anchor
> (`builder_model`), not via `FABRIK_AGENT_MODEL`.

`pr-reply` deliberately runs with the **builder model** (as `develop`, env
`FABRIK_AGENT_MODEL`): the replier fixes code on the PR branch and therefore needs
builder capacity. `review-pr` runs with a cheap, strong model via the Copilot CLI
(concrete model: spec, YAML anchor `reviewer_model`) — review quality at controlled cost.

### 4. `on_pr` trigger (formerly: cron + agent + state file)
**Originally (2026-10-16):** an `on_pr` trigger did not exist (→ issue). Instead of
writing engine code, we delegated the polling to the agent itself: cron started
the session, the agent checked `gh pr list` against a state file
(`state/reviewed-prs.json`) and ended with `nothing to do` if nothing was new.
That was the Squid-compliant version: no new substrate, just a prompt.

**Since 2026-10-19:** the native `on_pr` trigger exists (GitHub API polling like
`on_issue`: `repo`, `labels`, `poll`, `states`). `review-pr` now runs on
`on_pr` (label `fabrik`): the engine fires on new/updated PRs and sets
`{pr_number}`/`{pr_title}`/`{pr_head_ref}`/`{pr_body}` as variables. Dedup
(no PR twice) and re-review triggering (update → new run) are handled by the
engine — the state-file workaround logic has been removed from the prompt.
(`pr-reply` still uses `state/replied-comments.json` — comment dedup is
a different layer than PR dedup and stays on the agent side.)

### 5. `gh` CLI for issue/PR operations
A native `issue` step does not exist (→ issue). `gh` is already used in production
specs and available. Until the `issue` step exists, `shell` + `gh` remains the standard way.
**Token note:** shell steps run in the fabrik process env (`fabrik.env` →
`GITHUB_TOKEN`). Agent steps run in the **opencode-serve** process env —
`GITHUB_TOKEN` must be exported when `serve` starts (deployment prerequisite,
see Consequences). The engine does not inject env into agent sessions (`create_task` has no
env field).

### 6. Base branch `fabrik/v0.3`
Current main line of development (main is still at v0.2). When main becomes the
trunk line, we change `base:` in `develop`/`refactor`.

### 7. Refactoring as separate loops
`refactor` (targeted, manual with `--var target=…`) + `refactor-sweep` (finds code smells,
turns them into issues → feeds `refactor`). The boy-scout rule from AGENTS.md thus becomes
systematic: small improvements inline, larger ones as issues.

## Rejected alternatives
- **Build the `on_pr` trigger right away:** P2 mechanics (intake queue), overbuilding for the
  homelab proof. Cron + agent is enough.
- **Review step in the engine (P1 draft `review` step):** would be the clean way, but is
  itself an unimplemented feature (→ issue). The prompt-based review solves the same
  use case without an engine change.
- **One loop for everything:** a "super loop" (triage + dev + review + reply) would be
  untestable and unreadable. Separate loops = separate runs, separate audit trails.

## Consequences
- New prompts: `prompts/fabrik-{develop,refactor,refactor-sweep,review,pr-reply}.prompt.md`
- The reviewer model must be available in the deployment (Copilot CLI → `copilot` in PATH
  + `COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN`). Copilot withdraws models regularly
  — on `Model "…" is not available`, swap the model in the spec.
- `opencode serve` must be started with **`GITHUB_TOKEN` in the env** (agent steps
  call `gh`; the engine does not inject session env).
- `state/` is used (already gitignored).
- The dogfooding loop itself is part of the pipeline → its changes go through the
  normal gates (T2/PR approval). Self-improvement stays human-reviewed.
