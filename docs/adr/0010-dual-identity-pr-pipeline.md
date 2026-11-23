# ADR 0010: Dual-identity PR pipeline (builder ≠ reviewer, human only when critical)

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-18); amendment 2026-10-27 (PR #364): concrete
identities are configuration, not architecture — the invariant "two
different identities" is the decision.
Amendment 2026-11-03 (PR #453): identities can be overridden per project
(`github_env_file`); optional GitHub-signed commits (`commit_via: api`).
Addendum 2026-11-04 (ADR 0013 stage 2): AC verification in `pr_review`
(structured `acceptance` verdict, `unmet` → `changes_requested`).

## Context

Goal: a PR pipeline without a human in the loop — except for critical decisions
(ADR change, large refactoring/architecture changes, data loss, GDPR, …).

**Core problem: review processes must come from a different identity than
the PR author.** A PR **cannot** be approved by the same identity
that created it (user OR app — GitHub rule, self-approval is
rejected). The same applies to review events in general (APPROVE /
REQUEST_CHANGES, merge approval): they must come from an identity that is
separate from the authorizing identity. Fully automatic approval
therefore necessarily needs **two different identities**.

## Decision

**Identity split: builder and reviewer are always two different
identities.**

- The **builder identity** (developer) works in the worktree, pushes the
  branch, creates the PR and **merges** (squash) after approval.
- The **reviewer identity** (QA) reviews the PR (APPROVE /
  REQUEST_CHANGES) — visible as its own identity, never the builder's.
- **The two identities must be different** — that is the
  architectural decision. Review processes (approval, changes request,
  merge approval) only work if they come from an identity that is
  separate from the PR author; an identity that approves its own PRs
  is rejected by the host.

**Concrete identities are configuration, not architecture** (like
model choice, ADR 0022): which identities builder and reviewer concretely
are — GitHub apps, PATs, users, on another host (e.g. GitLab)
other bot identities — is determined by the project config (spec/`fabrik.env`),
not by this ADR. Another project on GitLab uses other identities;
the principle (two different identities, review separate from the
author) stays the same.

*Historical bindings (configuration, not part of the decision):*
- 2026-10-18: agent-host app (builder) + personal PAT (reviewer).
- 2026-10-27 (PR #364): two GitHub apps — `fabrik-builder` (builder) +
  `fabrik-reviewer` (reviewer), both with 1h installation tokens.

**Example: token handling (GitHub implementation, config):**
A concrete example of how the two identities are mapped in the project
config:
- `GITHUB_TOKEN` = builder installation token (developer). Renewed externally via
  `scripts/gh-token-refresh.py` (cron) every ~30 min and written to
  `FABRIK_ENV_FILE` (default `/root/.hermes/.env`) — the file
  is re-read per API call, no restart needed.
- `GITHUB_REVIEWER_TOKEN` = reviewer installation token (QA). Also valid
  for 1h, renewed via `scripts/gh-token-refresh.py --role reviewer` (cron).
- `gitops._gh_token(role)` with `role="dev" | "reviewer"`.

**New steps:**
- `pr_review`: LLM review of the PR diff (cross-model, ADR 0003 contract) +
  GitHub review via the reviewer token (`APPROVE` / `REQUEST_CHANGES`).
  `changes_requested` → bounded fix rounds (developer agent fixes in the worktree,
  pushes, re-review; default `max_rounds: 2`). If it is still
  `changes_requested` afterwards → step fails.
- `pr_merge`: squash merge via the developer token after approval.
- `human_gate`: criticality check on the PR diff. Heuristics (ADR paths,
  diff size, keywords) **and** LLM opinion. Critical → run pauses
  (`needs_approval`), a human approves via `fabrik approve`. Not critical →
  continues automatically. Statically "always human" stays `approval: true` in the
  loop spec (existing semantics).

**Engine:** steps can pause dynamically — step result
`{"needs_approval": true, "approval": {...}}` → the run goes into
`needs_approval` (analogous to the static `approval: true`, but decided at
runtime). Resume executes the step again; the step remembers the
state via a ctx variable (`human_gate_passed`) that is persisted via
`record["vars"]`.

## Example loop (fully automatic, human only when critical)

```yaml
loops:
  auto-dev:
    description: Issue → PR → review → merge (human only when critical)
    tier: 1  # T1: no synthetic tier gate at the end
    triggers:
      - on_issue
    steps:
      - name: develop
        type: agent
        prompt: "Solve the issue {issue_title}: {issue_body}"
      - name: push
        type: git
        branch: "fabrik/auto/{issue_number}"
        commit: {message: "fabrik: {issue_title}"}
        push: true
      - name: open-pr
        type: pr
        repo: owner/name
        title: "fabrik: {issue_title} (#{issue_number})"
      - name: review
        type: pr_review
        repo: owner/name
        model: <different model than develop>
        max_rounds: 2
      - name: gate
        type: human_gate
        repo: owner/name
      - name: merge
        type: pr_merge
        repo: owner/name
        method: squash
```

**on_pr trigger note:** a `REQUEST_CHANGES` review changes the PR's `updated_at`
→ an `on_pr` trigger fires. Fix rounds deliberately run INSIDE
the `pr_review` step (bounded), not via on_pr re-runs — on_pr loops
should be filtered by `labels` so that review updates are not interpreted as
new work triggers.

## Amendment 2026-11-03 (PR #453): per-project identities, signed commits

The invariant (builder ≠ reviewer) remains. Two extensions, both opt-in:

- **`github_env_file: <abs. path>` (project spec):** token file for this project
  only (another app installation/org, PAT as reviewer). Runs, trigger polls,
  retry attempts and all subprocesses (shell, agent `gh`, relay CLI) then read
  `GITHUB_TOKEN`/`GITHUB_REVIEWER_TOKEN` **only** from this file; global
  token env values do not apply there (they belong to a different identity).
  Without the option everything stays as above (env → `FABRIK_ENV_FILE`).
- **`commit_via: git|api` (project spec, default `git`):** repos with
  `required_signatures` reject the locally created, unsigned bot commits
  (squash merge does not help). With `api`, fabrik rebuilds every missing commit via
  the Git Data API, and GitHub signs it for the builder app. Trade-off:
  author/timestamp of the local commits are lost (author = app bot), no
  root commits. Secured by a tree-SHA comparison per commit and a
  non-fast-forward check as with `git push`.

Rejected: a signing key on the host (key management, contradicts the
app identity) and a global token per process (cannot serve multiple
installations).

## Addendum 2026-11-04 (ADR 0013, stage 2): AC verification in `pr_review`

`pr_review` verifies the acceptance criteria of the linked issue: the
AC section goes into the prompt for the reviewer as **untrusted data**, the verdict
carries per criterion `acceptance: [{criterion, status: met|unmet|unverifiable,
evidence}]` (verdict contract, ADR 0003 addendum) and lands in the run record
(`step.review`).

- **≥ 1 `unmet` → `changes_requested`** — deterministic in the engine
  (even against an `approved` verdict from the reviewer); each unmet criterion
  is added as a high-severity finding so that the bounded fix rounds
  (`max_rounds`) address it. No new loop.
- **`unverifiable` does not block**, but is traceable in the GitHub review text and in the
  run record.
- Invalid `acceptance` JSON is rejected (no verdict → step fails),
  not silently ignored.

## Consequences

+ Fully automatic cycle: PR → review → approval → merge, without a human
+ Human only when critical (heuristics + LLM, deterministically traceable
  in the run record: `step.approval` with reasons)
+ Approval identity = human → GitHub branch protection ("require 1 review")
  becomes a hard merge gate that GitHub itself enforces
+ An app cannot approve its own PRs → self-approval is structurally impossible
- A second token (`GITHUB_REVIEWER_TOKEN`) in the environment; the reviewer app
  needs Contents:read + Pull requests:read/write
- The builder app needs Contents:read/write + Pull requests:read/write + Issues:read/write
- Fix rounds are LLM work: they cost tokens, bounded via `max_rounds`
- `human_gate` LLM call per PR: one additional call (activated provider)

## Alternatives (rejected)

- **PAT creates, app approves:** works, but machine PRs carry the
  personal identity as author (worse audit story).
- **Review only locally (existing `review` step), no GitHub review:**
  no visible PR approval → branch protection cannot be used as a merge gate,
  no audit trail in GitHub.
- **Unbounded fix rounds:** risk of an endless loop (reviewer and builder
  ping-ponging) → bounded + run fail as the way out.
- **Criticality only static (`approval: true`):** misses unexpectedly
  critical changes (e.g. the agent "forgets" to touch an ADR).
