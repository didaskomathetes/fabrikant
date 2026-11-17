# Report — AI software factory patterns, for fabrik

## Executive summary
Research from Cloudflare, The Pragmatic Engineer, aihero.dev and all-ai.de (Reddit was blocked, see below). The findings agree on a few points. Agents work best in short loops with fresh context and a file as the handoff. Quality depends on feedback loops the agent can run itself, and review is the bottleneck, so it deserves the most design effort. Cost and cache behaviour need telemetry from the start. Unattended runs need sandboxes, iteration caps and a human at the merge gate. Fabrik already covers isolation, approval gates, dual identity, cross-model review and alert dedup. The gaps are mostly measurement (cost, cache, outcome metrics), context discipline, review structure and decomposition rules.

## Patterns
- [F6](findings.md#f6) — Skip re-reviews when nothing new changed, and make timeouts and cancellations explicit
- [F7](findings.md#f7) — Shadow-mode canary and randomized splits for changing agent harness or model
- [F9](findings.md#f9) — Task-scoped consent: grant the narrowest subset of a requested scope
- [F13](findings.md#f13) — Research, design, plan: compress context into Markdown between fresh sessions
- [F15](findings.md#f15) — Slow loops: nightly agents that open PRs for a human to merge
- [F19](findings.md#f19) — Build your own pipeline rather than depend on a big agent framework
- [F20](findings.md#f20) — Close the loop: design so the agent can verify its own work
- [F22](findings.md#f22) — Running 5–10 agents in parallel needs planned, queued work
- [F23](findings.md#f23) — A single model gateway enables spend tracking, security checks and audit
- [F25](findings.md#f25) — Code review is the bottleneck: pre-review with AI, risk scores and evidence
- [F28](findings.md#f28) — The Ralph loop: same prompt, agent picks the next task, explicit completion signal
- [F30](findings.md#f30) — A committed progress file plus git history carries state across fresh context windows
- [F34](findings.md#f34) — Sandbox unattended runs so a runaway agent cannot touch the host
- [F36](findings.md#f36) — Use progressive disclosure for agent instructions instead of one big file
- [F37](findings.md#f37) — Self-improving harness: an editor proposes changes, tested against the baseline run
- [F42](findings.md#f42) — Hand off by file: portable, references instead of copies, secrets redacted
- [F44](findings.md#f44) — Slice tickets vertically, size each to one fresh context, and use expand-migrate-contract for wide refactors

## Concepts
- [F1](findings.md#f1) — Per-turn cost drift: measure cost per review by tier, not in aggregate
- [F2](findings.md#f2) — Most agent spend is context re-read, so prompt-cache prefix stability is a design constraint
- [F8](findings.md#f8) — A cheaper reviewer is only good if findings are checked against author actions
- [F11](findings.md#f11) — Benchmark-trained models can degrade a codebase over time
- [F12](findings.md#f12) — The "dumb zone": keep context utilization well under the window limit
- [F16](findings.md#f16) — Three ways to run a software factory, with measured productivity claims
- [F17](findings.md#f17) — Don't optimise model cost early; use cheap models for simple steps once scale justifies it
- [F18](findings.md#f18) — "Token harder" versus "token smarter"
- [F24](findings.md#f24) — Tiered spend allocation with manager sign-off instead of hard caps
- [F27](findings.md#f27) — Measure agent output by PR type instead of PR velocity
- [F29](findings.md#f29) — Start human-in-the-loop, then go unattended, and always cap iterations
- [F31](findings.md#f31) — Feedback loops are the quality lever, and a failing check should block the commit
- [F38](findings.md#f38) — Keep task-specific lessons in a library, not in the shared base configuration
- [F39](findings.md#f39) — Juniors need to learn to verify AI code, not only write code

## Prompts
- [F4](findings.md#f4) — Inline the reference index so sessions stop spending their first turns reading files
- [F21](findings.md#f21) — Review the prompt and plan, not only the code
- [F32](findings.md#f32) — Prioritise risky and architectural tasks first, and give humans those
- [F33](findings.md#f33) — Tell the agent what quality bar the repository has
- [F40](findings.md#f40) — Review on two independent axes: standards and spec, never blended
- [F41](findings.md#f41) — Repo documentation outranks a generic smell baseline; no spec means say so

## Error scenarios
- [F3](findings.md#f3) — Per-repo content in front of the shared prompt silently killed cache hits
- [F5](findings.md#f5) — Don't give sub-agents tools the server will reject
- [F10](findings.md#f10) — Unread, unreviewed agent-written code failed within months
- [F14](findings.md#f14) — Sycophantic "You're absolutely right" replies signal a poisoned trajectory
- [F26](findings.md#f26) — CI load explodes with agents; move cheap checks into the inner loop
- [F35](findings.md#f35) — Oversized AGENTS.md files cost tokens on every request and make the agent worse
- [F43](findings.md#f43) — Handoff files kept in temp directories vanish between sessions

## Gaps in fabrik
Each gap is a suggestion for review; the fabrik baseline was taken from the README and the ADR index (October 2026).

1. **Cost and cache telemetry per run and per tier.** Track spend per review/build by model tier and watch the cache-hit rate, since per-repo content ahead of the shared prompt silently killed caching. Supports: [F1](findings.md#f1), [F2](findings.md#f2), [F3](findings.md#f3), [F23](findings.md#f23), [F24](findings.md#f24).
2. **Shadow-mode canary for model or harness changes.** Fabrik has model choice as config (ADR 0022) but no way to compare a new model against the current one on the same work. Run it in shadow or with a randomised split. Supports: [F7](findings.md#f7), [F8](findings.md#f8), [F37](findings.md#f37).
3. **Context budget and poisoned-trajectory detection.** Cap context use well below the window, and treat sycophantic replies or repeated failures as a signal to restart from a handoff. Supports: [F12](findings.md#f12), [F14](findings.md#f14), [F13](findings.md#f13).
4. **Handoff artifacts with a reference-not-copy rule, kept durable.** Standardise a research/plan/progress file between sessions, link to specs and ADRs, redact secrets, and keep scratch state off temp directories. Supports: [F13](findings.md#f13), [F30](findings.md#f30), [F42](findings.md#f42), [F43](findings.md#f43).
5. **Iteration caps and explicit completion signals for unattended loops.** Supports: [F28](findings.md#f28), [F29](findings.md#f29), [F31](findings.md#f31).
6. **Role-scoped tools and credentials.** Give each agent role only tools the server will accept and the narrowest scope of access. Supports: [F5](findings.md#f5), [F9](findings.md#f9), [F34](findings.md#f34).
7. **Structured reviewer prompt: two axes, evidence required.** Split review into standards and spec, require a cited rule or spec line per finding, and check reviewer findings against what the author did. Supports: [F40](findings.md#f40), [F41](findings.md#f41), [F8](findings.md#f8), [F21](findings.md#f21), [F25](findings.md#f25).
8. **Planner decomposition rules.** Vertical (tracer-bullet) tickets sized to one fresh context, explicit blocking edges, human approval of the breakdown, risky work first. Supports: [F44](findings.md#f44), [F32](findings.md#f32), [F22](findings.md#f22), [F13](findings.md#f13).
9. **Risk score and skip-rereview logic.** Score diffs by risk to route human attention, and skip a re-review when nothing new changed. Supports: [F25](findings.md#f25), [F6](findings.md#f6), [F10](findings.md#f10).
10. **Instruction-file size lint and progressive disclosure.** Warn when AGENTS.md or CLAUDE.md files grow large, and split detail into on-demand files. Supports: [F35](findings.md#f35), [F36](findings.md#f36), [F4](findings.md#f4), [F33](findings.md#f33).
11. **CI load and outcome metrics.** Move cheap checks into the agent's inner loop to protect shared CI, and measure output by PR type and rework instead of PR count. Supports: [F26](findings.md#f26), [F27](findings.md#f27), [F44](findings.md#f44), [F16](findings.md#f16).
12. **Human verification skills and gates.** Keep humans reading plans and diffs for risky changes, since unread agent code failed within months. Supports: [F10](findings.md#f10), [F11](findings.md#f11), [F39](findings.md#f39), [F21](findings.md#f21).

## Sources used
- https://blog.cloudflare.com/obsessive-inference-optimization/
- https://blog.cloudflare.com/task-based-oauth-consent/
- https://newsletter.pragmaticengineer.com/p/context-engineering-with-dex-horthy
- https://newsletter.pragmaticengineer.com/p/the-creator-of-clawd-i-ship-code
- https://newsletter.pragmaticengineer.com/p/ubers-ai-infra-from-the-inside
- https://www.aihero.dev/a-complete-guide-to-agents-md
- https://www.aihero.dev/skills-code-review
- https://www.aihero.dev/skills-handoff
- https://www.aihero.dev/skills-to-tickets
- https://www.aihero.dev/tips-for-ai-coding-with-ralph-wiggum
- https://www.all-ai.de/news/news26/google-harness-start
- https://www.all-ai.de/news/news26/microsoft-praise-programmierer

## Sources that could not be accessed
- Reddit (r/ExperiencedDevs search JSON and old.reddit.com search page): HTTP 403 on 2026-10-21 in session 1 and again in the single permitted retry in session 2 (scripts/s1-09, s2-01). Not bypassed. No findings from Reddit.
- Pragmatic Engineer "why-ramp-built-inspect": paid-only audience, not used.

## Further leads
Not researched; notes only.

- https://developers.cloudflare.com/ — Cloudflare docs for Agents SDK / Workers AI, possibly relevant to agent runtime patterns (found via https://blog.cloudflare.com/obsessive-inference-optimization/)
- https://ghuntley.com/loop — "Everything is a ralph loop", loop-engineering pattern (found via https://newsletter.pragmaticengineer.com/p/context-engineering-with-dex-horthy)
- https://github.com/humanlayer/12-factor-agents — principles for reliable LLM applications; primary source for agent-pipeline design (found via https://newsletter.pragmaticengineer.com/p/context-engineering-with-dex-horthy)
- https://martinfowler.com/articles/harness-engineering.html — harness engineering article (found via https://newsletter.pragmaticengineer.com/p/context-engineering-with-dex-horthy)
- https://martinfowler.com/ — Martin Fowler's site; harness engineering and agentic development writing (found via https://newsletter.pragmaticengineer.com/p/context-engineering-with-dex-horthy)
- https://www.aihero.dev/skills — Matt Pocock's agent skill set (grill-me, handoff, code-review, to-tickets, triage); candidate prompt structures for planner/reviewer agents (found via https://www.aihero.dev/)
- https://www.anthropic.com/engineering — Anthropic engineering blog; the article on long-running agent harnesses is referenced by Pocock (found via https://www.aihero.dev/tips-for-ai-coding-with-ralph-wiggum)
- https://www.humanlayer.dev/blog/skill-issue-harness-engineering-for-coding-agents — HumanLayer on harness engineering for coding agents (found via https://newsletter.pragmaticengineer.com/p/context-engineering-with-dex-horthy)
- https://www.langchain.com/blog/improving-deep-agents-with-harness-engineering — LangChain harness engineering post (found via https://newsletter.pragmaticengineer.com/p/context-engineering-with-dex-horthy)
