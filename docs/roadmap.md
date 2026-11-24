# Roadmap

Living plan for fabrikant. This document supersedes the phase plan of the
archived v1.1 design draft
([`archive/framework-v1-draft.md`](archive/framework-v1-draft.md)): it keeps the
draft's principles, its P0→P3 structure and its KPI definitions, reconciled
with what is actually shipped (ADRs + code) and what is in flight (PRs,
issues).

Statuses as of 2026-11-23.

**Status legend**

- **shipped** — implemented; pointer to the ADR (or issue) that decided it
- **in-flight** — open PR/branch; pointer included
- **open** — tracked as an issue; pointer included
- **deferred** — deliberately not scheduled
- **dropped** — not coming; one-line reason

## Principles (from the draft, still valid)

1. **The conductor is code, not an LLM.** Deterministic orchestration; LLMs
   only as workers in defined roles.
2. **Session must survive.** Durable event log (ADR 0002, ADR 0034); harness
   and sandbox are cattle, not pets.
3. **Verification before review.** Checks run before a human is involved;
   humans sit only at irreversible/accountability gates.
4. **Cross-model review.** Builder ≠ Reviewer (ideally a different vendor);
   a defect survives only if both miss it (ADR 0010, ADR 0003).
5. **Trust is built into the system.** Layered verification, observability,
   audit trail — including trust boundaries for external inputs (ADR 0008,
   ADR 0050).
6. **Don't overbuild.** Phase by phase; each phase must pay for itself.
7. **External input is data, not instruction.** User-controlled text
   (issues, webhooks, scraped pages) is the primary prompt-injection surface
   of a workflow factory.

## Factory KPIs (draft §5, the definitions fabrikant aims to compute)

- **Cycle time:** intent → merged artifact
- **Defect escape rate:** what is found after merge
- **Cost per change:** tokens + review time + CI
- **Replayability rate:** a run is reproducible from its inputs (needs the
  spec hash in the run record — shipped; see Phase 0)
- **Judge veto rate / course-correction rate**
- **Scale KPIs (P2):** queue depth, 429 rate, reaped jobs, budget hits

Cost per change, cycle time, defect escape and judge veto are computed
from the run store since first win 1 (issues #126, #127; shipped). The
replayability rate rests on replay (ADR 0061); the scale KPIs are still
open (P2).

## Phase 0 — General purpose in the existing engine

**Status: shipped.**

| Draft item | Status | Pointer |
|---|---|---|
| Step types `http`, `llm`, `notify` | shipped | `src/fabrikant/steps/` |
| Step type `issue` (native GitHub issue ops) | shipped | ADR 0018 |
| Variable store + `[a-zA-Z0-9_]+` substitution, `vars`, loop templates | shipped | ADR 0047 |
| Risk tier as spec data field + single mapping function | shipped | `tier` in specs, single mapping function |
| Untrusted-input rules (marking, read-only research agent, deterministic triage mapping) | shipped | ADR 0008, ADR 0050 |
| Stale-run check (~20 lines) | shipped | `FABRIKANT_STALE_HOURS` |
| Spec hash in the run record | shipped | `sessionlog.py` (`spec_hash`) |
| Token masking in logs | shipped | `secrets.py`, ADR 0026 (alert `log_tail`) |
| Triggers `manual`, `cron`, `on_file`, `on_issue`, `on_pr` | shipped | `scheduler.py`, ADR 0014, ADR 0023 |
| `on_webhook` explicitly *not* in P0 (unauthenticated server = footgun) | shipped later | ADR 0027 (HMAC + tunnel-CORS) |
| 2 example workflows as proof (research, admin) | shipped | dogfooding loops, ADR 0001 |
| `verify` / `file` steps (deferred from P0 to P1 in the draft) | `verify` shipped, `file` dropped | `steps/verify.py` (issue #117); `file`: no use case appeared |

## Phase 1 — Agent backends + verification

**Status: shipped, mostly.**

| Draft item | Status | Pointer |
|---|---|---|
| SPIKE: `opencode serve` (resume, cost, health, workdir) | shipped (spike done) | [`archive/spike-opencode.md`](archive/spike-opencode.md) |
| `AgentBackend` interface freeze incl. `resume`/`health`/`budget` | **reconciled:** never frozen as a base class — the shipped interface is implicit (no base class) and shaped by what backends support | [backends.md](backends.md); per-session `resume` does not exist, recovery is run-level (ADR 0036) |
| `opencode` backend (primary, HTTP, session API) | shipped | ADR 0005 (dual 1.x/2.x) |
| One-shot CLI backends (`claude_code`, `codex`, `copilot`) | shipped (beyond the draft's single `cli` backend) | [backends.md](backends.md) |
| `review` step — cross-model cold critic | shipped | ADR 0003, `steps/review.py` |
| `verify` step (check-list semantics) | shipped | `steps/verify.py` (issue #117) |
| `file` step (only if P0 use cases need it) | dropped | no use case appeared |
| Session log in SQLite, defined as the end schema | shipped, partly superseded | ADR 0002 → ADR 0034 (typed run model + relational store) |
| No new agent-step spec keys in P0 | shipped | `backend`/`model`/`endpoint` in the agent step |
| Model routing: cost follows difficulty, critique on a different model | shipped | ADR 0022 (model choice is config), ADR 0003 |
| `continue_on_fail` for non-blocking steps (P1 follow-up of the "no `gate` flag" rule) | dropped | never needed; every step stops the run by default |
| `state` step (persistent state across runs) | shipped (beyond draft) | `steps/state.py` (issue #113) |

## Phase 2 — Control/worker separation + enterprise mechanics

**Status: in progress.** The draft's P2 is a 6–12-month scope; fabrikant took
its mechanics one by one, mostly on the homelab profile.

| Draft item | Status | Pointer |
|---|---|---|
| Worktree per run instead of project lock (`max_parallel`, shared agent state) | shipped | ADR 0028 |
| Worktree-free loops with own lock and scratch workdir | shipped (beyond draft) | ADR 0020 |
| Typified run model + relational storage (SQLite / Postgres) | shipped for SQLite; Postgres backend open | ADR 0034, issue #43 (tracking: #48) |
| Remaining state (pause, users, agent state) into the run store | open | issue #41 |
| Queue semantics: priority, max-retry, DLQ | partial: retry with backoff + storage of exhausted jobs; full queue/DLQ open | ADR 0036, issue #111 |
| Failure classification (`FailureKind`) | shipped | ADR 0035 |
| Concurrency coordination: reconcile dedup, queued triggers, paused runs | shipped | ADR 0043 (ADR status stale, see below) |
| Reaper (heartbeats, max job duration) | open | issue #109 |
| Budget service (cost caps, circuit breaker) | open | issue #108 |
| Admission control (per-repo/provider caps, 429-aware retry) | open | issue #107 |
| Idempotency (`safe_to_rerun`, idempotency keys) | open | issue #106 |
| Sandbox per run (Docker) — the draft's "real jump" | partial | ADR 0054 (in revision), issue #9; `opencode` one-shot in its own container shipped (PR #125); follow-ups #6, #7, #3, #2, #4, #5, #8 |
| `on_webhook` with auth | shipped early | ADR 0027; replay protection shipped (ADR 0058, PR #144, issue #60) |
| `on_slack` trigger (beyond draft) | shipped | ADR 0040 |
| `on_datadog` trigger (beyond draft) | open | issue #98 |
| Approval service: async with SLA + escalation | partial: queued approvals and needs-human escalation shipped; full service open | ADR 0015; ADR 0059 (PR #122, PR #124); issue #104 (interface: #67) |
| Auth + roles, audit who started/approved what | shipped | ADR 0030; OIDC: ADR 0046 |
| Relay tasks — long-running work as a chain of short audited sessions | shipped (beyond draft) | ADR 0037, [relay.md](relay.md) |
| Alert dedup by fingerprint, `log_tail` redaction | shipped | ADR 0025, ADR 0026 |
| Egress default-deny: agent internet access only via allowlist proxy | shipped | ADR 0051; per-loop network profiles open (issue #119, folded into ADR 0060 access profiles: #138) |
| Deployment profiles (`homelab` \| `cloud`) with adapter registry | shipped | ADR 0056 |
| Named connections (LLM providers, agent backends in the UI) | shipped | ADR 0052 |
| Run launcher port/adapter (execution substrate, `FABRIKANT_LAUNCHER`) (beyond draft) | shipped | ADR 0057, PR #147 |
| Hosting provider port/adapter (GitHub, GitLab) + extensible registry (beyond draft) | shipped; GitLab write-flow verification open | ADR 0062, ADR 0063, PR #172; issue #171 |
| Research fetcher + access profiles per loop/run, one-time grants (beyond draft) | partial | ADR 0060; stages 1–2 shipped (PR #181, issue #180), amendment draft PR #184; stages 3–5 open: #138, #139, #140 (also #119) |
| Threat model (beyond draft) | shipped (draft) | PR #182 |
| Usage: cached-token split + billing mode (beyond draft) | shipped | PR #167 |
| CI quality gates: coverage gate, Biome JS lint, `pip-audit` (beyond draft) | shipped | PR #175 |

## Phase 3 — Cloud deployment + governance

**Status: mostly open, gated on approval.**

| Draft item | Status | Pointer |
|---|---|---|
| Cloud deployment profile (ECS/GKE, autoscale with hard cap, Vault, non-human identities, audit export) | open (needs approval) | issue #102 |
| Multi-project / multi-team + cost dashboards | open (needs approval) | issue #101 (cost rollup first: issue #126) |
| Incident round-trip (alert → triage → hotfix-PR) | shipped | ADR 0045 |
| ACP backend (OpenHands via SDK/ACP) | deferred — observe, not adopt | ADR 0044 |
| `hermes` backend for non-dev workflows | dropped | no demand; watch only (ADR 0044) |

## First wins (strategy 2026-11-17)

The current priorities, ahead of the open P2/P3 items. Status as of 2026-11-23.

**1. Observability & KPIs** — shipped

- Per-run cost rollup (cost-per-change) from `step_usage` — issue #126 (shipped, #142)
- Factory KPIs from the run store: cycle time, defect escape, judge veto — issue #127 (shipped, #143/#168)
- Trace view per run in the web UI (steps, durations, usage, agent events) — issue #128 (shipped, #173)

**2. Reproducibility & evals**

- Replay: deterministic re-execution of a run from pinned inputs — issue #129 (shipped, #170, ADR 0061)
- Golden runs + eval harness ("this pipeline change did not regress my
  loops" as a provable statement) — issue #130 (core shipped, #177, ADR 0065; gaps: goldens are sidecar files
  instead of run-store marks, CLI is `fabrikant eval <project> <loop>`, opt-in CI
  workflow still in `docs/ci/golden-eval.yml`, no user-facing docs)

**3. Spec as a standard**

- JSON schema for `*.fabrikant.yaml`, single source of truth for validation
  and docs — issue #131 (shipped, #169)
- fabrikant as an MCP server (runs, specs, approvals reachable from any
  MCP-capable agent) — issue #132 (shipped, #178, ADR 0064, incl. the
  `validate_spec` tool; ADR 0064 amendment in draft PR #183)
- Grow the template library (≥5 documented, versioned templates) — issue #133 (not started)

## Known contradictions & stale ADRs

Tracked for follow-up; **not** fixed by this branch. ADR content is only
changed with explicit approval (see `adr/README.md`).

**Shipped features whose ADR was never accepted** (index status out of date;
ADR 0049 and ADR 0051 have since been accepted and dropped from this list):

| ADR | Feature | Evidence in code |
|---|---|---|
| 0037 | Relay tasks (status: Draft) | `src/fabrikant/relay/`, `prompts/relay/`, [relay.md](relay.md) |
| 0038 | Live log (SSE) + transcript in run modal | `server.py` (`text/event-stream`) |
| 0039 | Convergent PR review rounds | `steps/pr_review.py` |
| 0043 | Reconcile dedup (queued triggers, paused runs) | `engine.py` (`_safe_run`), `runs.py` |
| 0050 | Agent env allowlist | `agentenv.py` |

**Cross-reference & consistency issues:**

- ADR 0030 says it "supplements ADR 0019 (Rollen)", but ADR 0019
  (pause/resume/disable) defines no roles.
- ADR 0024 assumes the reviewer is the personal account (human PRs share
  one account). ADR 0010's amendment (2026-11-03: identities per project,
  signed API commits) and ADR 0030's user-based run attribution post-date
  it; ADR 0024 was never amended.
- ADR 0002 is "partly replaced by ADR 0034", and both stores run in
  parallel: `sessionlog.py` still receives agent events while the
  relational store grows (issues #48, #41).
- The draft's §2 P1 interface (frozen `AgentBackend` base class with
  `create_session`/`run`/`resume`/`health`/`budget`) was never adopted;
  the shipped interface is implicit and different — see
  [backends.md](backends.md).
- Issue #134 tracks the language migration (docs/ADRs/issues → English);
  ADR translations (#162, #163), backend message catalogs (#164) and source
  comments/docstrings (#165) are merged; newer modules (hosting, MCP server,
  replay, golden) still contain German strings and comments. Until it is
  done, this roadmap and any new content stay in English.

## Backlog highlights

Open issues beyond the phase tables and first wins (all in the issue
tracker):

- **Sandbox hardening:** fail-closed without Docker (#6), sandbox by
  default (#7); token/cost usage across all backends (#1) shipped (#142)
- **Spec & tooling:** provider abstraction / IssueTracker + CodeHost ports
  (#86; the CodeHost side shipped as the hosting provider port, #172), Jira adapter (#85), deterministic loops with native
  issue/sonarqube steps (#84), settings & connections UI (#81)
- **UI:** graph view for loops (#73), visual loop editor (#72),
  chat sessions (#77), project export/import (#71)
- **Language:** output language per project (#25), UI i18n follow-ups
  (#24, #74), English README (#23), full migration (#134; remainder after PRs #162–#165)
- **Self-optimization:** falsifiable improvement proposals — design
  proposal merged (#174), implementation tracked in issue #176
- **Refactors:** typed `AgentPollResult` (#19), typed run model
  continuation (#18), server dispatch table (#54)
