# fabrikant threat model

**Status:** DRAFT — not an ADR. A living document: update it when a trust boundary or
a control changes, and link the ADR that made the change. Individual decisions stay in
ADRs; this page is the map that connects them.

**Reviewed against:** ADRs 0008, 0010, 0026, 0030, 0050, 0051, 0054, 0060 (incl. its
stage-C amendment), 0064, 0065, 0066. Items marked *(verify)* were derived from ADR text
and not checked against the code.

## 1. What is being protected

| Asset | Why it matters |
|---|---|
| Forge credentials (GitHub/GitLab tokens, reviewer identity) | Merge/push rights are the factory's real-world effect |
| LLM provider keys | Spend and abuse |
| Target repositories and their secrets | Source of truth; may hold deploy credentials |
| The host (developer machine / homelab) | Agents execute code there unless sandboxed |
| Run store, specs, goldens | Integrity of audit trail and of "what passed" |
| Approval decisions | The human gate is the last control before an effect |
| Budget (tokens, CI minutes, API quota) | Runaway loops cost real money |

## 2. Who attacks, and how

fabrikant's core premise: **an agent is a process that reads untrusted input and can act.**
Design for the case where the model is fully controlled by an attacker.

| Actor | Capability | Typical vector |
|---|---|---|
| Content author (issue/PR text, web page, search result, dependency, README) | Puts instructions in text the agent reads | Prompt injection |
| Malicious or compromised upstream (repo code, packages, MCP client) | Runs code or sends tool calls | Supply chain, confused deputy |
| Other local user/process on the same machine | Reaches localhost ports, files, stdio servers | Unauthenticated local endpoints |
| Network attacker | Controls DNS/hosts the fetcher or agents contact | SSRF, DNS rebinding |
| The model itself (error, not malice) | Hallucinates a destructive step | Over-eager tool use |
| An authorised but careless operator | Approves without reading | Approval fatigue |

Out of scope: a compromised host kernel, a malicious fabrikant maintainer, physical access.

## 3. Trust boundaries

```
 untrusted input ──► [ fetcher / parsers ] ──► [ agent (model) ] ──► [ tools / forge / shell ]
 issues, PRs, web      scan = signal only        UNTRUSTED actor        effects happen here
 search results        (never grants)            ▲                      ▲
                                                 │ capability limits    │ human gate (approve)
 MCP client ──► [ fabrikant server ] ───────────────┴──────────────────────┘
 (stdio, local)   run store · specs · goldens · audit log
```

Boundaries that matter, in order of how much they carry:

1. **Agent ↔ credentials and network.** What an injected agent can reach.
2. **Agent ↔ effects.** What it can change without a human.
3. **Control plane ↔ its callers.** Who may start, approve, reject runs (HTTP, CLI, MCP).
4. **Fetcher ↔ internal network.** SSRF.
5. **Evaluator ↔ the thing evaluated.** Goldens and judges must not be editable by the
   change they judge.

## 4. Design rules

1. **Assume the scan missed.** Content scanners (injection patterns, quarantine) are
   *detection and telemetry*. They may route work (e.g. quarantine → human review). They
   must never *grant* anything: a `clean` verdict unlocks no capability the agent would
   not already have.
2. **Capabilities, not instructions.** Limits live in the platform (env allowlist,
   network, container, tool set), never in the prompt.
3. **No fetch plus write in one loop** without a human gate between them.
4. **The builder is not the reviewer** (ADR 0010); one identity must not both start and
   approve a run.
5. **Evaluators are not self-editable.** A change must not be able to re-baseline the
   golden or alter the judge that scores it in the same change.
6. **Every re-execution is metered.** Replay, eval and optimisation multiply spend and
   side effects; they need a cost/blast-radius cap before they run unattended.
7. **Fail closed.** Unparseable address, unknown role, malformed golden → deny/error,
   never "treat as empty".
8. **The deployment profile decides authentication.** Local single-user setups need no
   auth (the OS user is the identity); a hosted/cloud profile must have authentication,
   a per-caller identity and RBAC, and must refuse to start without them (the same
   principle as `auth.check_bind` in ADR 0030). One profile setting drives every
   control-plane surface (HTTP, MCP) so they cannot drift apart.

## 5. Existing controls

| Control | Boundary | Source | Status |
|---|---|---|---|
| Untrusted values reach shell steps via `env:`, not inline substitution | 2 | ADR 0008 | Accepted |
| Dual identity: builder ≠ reviewer | 2 | ADR 0010 | Accepted |
| Pattern-based secret redaction in alert log tails | 1 | ADR 0026 | Accepted |
| Local users, roles (`viewer`/`operator`/`admin`), CSRF, loopback bind, audit fields | 3 | ADR 0030 | Accepted; default mode `none` |
| Agent env allowlist — agents do not inherit `os.environ` | 1 | ADR 0050 | ADR says Proposed; shipped (`agentenv.build_agent_env`, used by the one-shot backends) |
| Egress default-deny via allowlist proxy; internal sandbox network | 1 | ADR 0051 | Accepted, implemented |
| Per-run agent container (cap-drop, read-only, limits) | 1, 2 | ADR 0054 | Opt-in; image/host-exec points open |
| Fetcher: approval by domain/URL, SSRF guard, scan stages A/B/C (C = in-process detector; scan is signal only, a verdict never grants capability), quarantine, ledger | 4 | ADR 0060 | Accepted (2026-11-19) |
| MCP server: stdio only; `--read-only` wins over every opt-in; approve/reject (`--allow-approvals`) and spec-write proposals (`--allow-spec-write`) are off by default; secret-looking values masked; spec changes only via human-applied proposals. Other mutating tools (e.g. `start_run`) are on by default unless `--read-only` | 3 | ADR 0064 | Accepted (amended 2026-11-23) |

## 6. Known gaps and residual risk

Ordered by how much damage they allow.

1. **MCP control plane: auth must follow the deployment profile.**
   - *Local (stdio):* no auth is acceptable; the caller is whoever spawned the process.
     Residual risk: a prompt-injected agent that has the server configured can start
     runs and approve its own. Mitigations: `--read-only`, and either keep
     `approve_run`/`reject_run` off unless explicitly enabled, or forbid the client that
     started a run from approving it.
   - *Hosted/cloud:* stdio does not apply. Needs an authenticated HTTP transport, one
     identity per client, RBAC reusing the ADR 0030 roles (`viewer` read tools,
     `operator` start/approve/reject, `admin` the rest), the real identity in the audit
     actor instead of the constant `mcp`, and a refusal to start without auth.
   - *Today:* only the local profile exists; the hosted profile is not built. Approve/reject
     are opt-in (`--allow-approvals`), and spec changes go through proposals a human applies.
2. **Auth mode defaults to `none`.** Safe only while bound to loopback and used by one
   person. Any shared deployment must set `local`/`oidc`.
3. **Fetcher auto-approves URLs that came from search results.** The search engine is
   therefore part of the attack surface (SEO'd page → approved fetch). Read-only egress
   plus an injected page still permits exfiltration through URLs and queries (accepted
   residual in ADR 0060). *Needed:* approve for the fetch only, never for links followed
   from fetched content; per-session byte/rate caps on outbound traffic.
4. **Replay and eval re-execute real side effects.** There is no dry-run or recorded-I/O
   mode, so an eval can comment on PRs or notify. Gated only by a confirmation prompt.
5. **Goldens sit next to the spec.** A change to the pipeline can re-baseline its own
   evaluator in the same PR.
6. **No budget enforcement yet** (issue #108). Re-executing features can multiply spend.
7. **Several security ADRs are still Proposed or Draft** although the features ship, so
   later ADRs rely on foundations whose status says otherwise.
8. **Secret lineage is not tracked** — which run touched which credential is not
   recorded.
9. **Versioning is undefined** for specs and golden files; a migrated spec against an
   old golden reads as a spurious regression.

## 7. Where truth lives

| Data | Location | Consequence |
|---|---|---|
| Specs, goldens, applied proposals | Git | Reviewable, tamper-evident |
| Run records and logs | Local run store | A golden's `baseline_run` id exists only on the machine that made it; CI cannot resolve it |
| Approvals / audit actor | Run record | Only as good as the identity recorded |
| Credentials | Environment / env files | Must not reach agent processes (ADR 0050) |

## 8. Open questions for the owner

- ~~Single-user or shared MCP?~~ Decided: it depends on the profile — no auth locally,
  authentication + identity + RBAC when hosted (rule 8). Open: do approve/reject ship
  disabled by default in the local profile?
- Should replay/eval default to recorded I/O with live re-execution as an explicit mode?
- Where should the budget cap live (per run, per project, per day), and what happens at
  the cap (stop, queue, ask)?
- Does the fetcher belong in the core product or in a separate profile with its own
  threat model?

## 9. Keeping this current

Update this file in the same PR whenever a change adds a caller of the control plane,
a new place content enters the system, a new way to execute code, or a new store of
credentials. Review the gaps list each release and turn items into issues.
