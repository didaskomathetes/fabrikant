# ADR 0022: Model choice is configuration, not architecture

**Status:** Accepted (2026-10-19); personal name replaced by GitHub handle 2026-11-26
**Deciders:** didaskomathetes
**Supersedes:** ADR 0001 §3, paragraph 2 (binding of `pr-reply`/`review-pr` to
concrete models or to `FABRIKANT_AGENT_MODEL`) and the "Model" column of the loop
table in ADR 0001. The invariant builder ≠ reviewer (ADR 0001 §3, ADR 0003)
applies unchanged.

## Context

ADR 0001 laid down how the loops are bound to models: `pr-reply` via the env
variable `FABRIKANT_AGENT_MODEL`, `review-pr` via the Copilot CLI. Models and
backends change quickly. Copilot withdraws models, new generations arrive, prices
change. Every switch therefore ran against the text of an ADR, although it
changes no architecture. Example: `develop`/`pr-reply` switch from opencode (env
default) to `claude_code` with an explicit step model.

## Decision

- **Builder ≠ reviewer remains an architecture decision** (guiding principle 4,
  ADR 0001 §3, ADR 0003): builder and reviewer always use different models. Which
  models these are is configuration — that they are different is not.
  `review`/`pr_review` check this at runtime (cross-model check, fails on the same
  model); the `review-pr` loop (agent step) keeps the invariant via the anchor
  `reviewer_model`. Lifting the invariant needs a new ADR.
- **Model and backend per step are configuration.** They live in the spec
  (`projects/*.fabrikant.yaml`) or in `fabrikant.env`, not in ADRs. A switch needs
  neither an ADR nor ADR approval.
- **ADRs name roles, not models:** builder, reviewer, classifier. Which role uses
  which model is determined by the spec.
- **Roles share a model via YAML anchors**, like a local variable: in
  `projects/fabrikant.fabrikant.yaml`, `develop` and `pr-reply` use
  `*builder_backend`/`*builder_model`, `develop.review` and `review-pr` use
  `*reviewer_model`.

## Consequences

- Model switches are normal spec changes. They go through the usual PR gates, but
  without ADR approval.
- ADR 0001 §3 is kept as the rationale for the roles. Concrete bindings there are
  historical.
- Deployment prerequisites (CLI in PATH, tokens) are documented in
  `docs/backends.md`, not in the ADR.
