# Self-Optimization via Falsifiable Proposals

**Status:** Proposal (design doc) — not yet an accepted ADR.
**Depends on:** #130 (golden runs + eval harness), #129 (replay, ADR 0061), #131 (spec JSON schema), #126/#127 (KPIs).
**Companion reading list:** [self-optimization-reading-list.md](self-optimization-reading-list.md)
**Last updated:** 2026-11-21.

## 1. Goal

Let a fabrik operator ask the factory to **audit its own runs and propose
improvements** — fewer errors, more efficient prompts, more secure or
performant specs — such that every proposal is **falsifiable** before it is
applied. The factory does not "learn" in the ML sense; it **ratchets**
forward, one validated step at a time, with a human at the irreversible gate.

This is a *proposal* system, not an *autonomous self-modification* system.
The distinction is the whole safety model (see §7).

## 2. Non-goals (deliberate)

- **The LLM never modifies the conductor.** Principle #1 ("the conductor is
  code, not an LLM") is preserved. The agent proposes changes to **specs,
  loops, and prompts** — data the conductor runs — never to fabrik's own
  orchestration code.
- **No autonomous application.** A proposal is never applied without human
  approval through the existing approval gate (ADR 0011 approval-request
  protocol; ADR 0059 `needs human` escalation).
- **No relaxation of trust boundaries.** Proposals that touch egress, auth,
  untrusted-input handling, or the approval gates themselves may only ever be
  proposed in a *tightening* direction, and require expert review (§6).

## 3. The core object: the falsifiable proposal

A proposal is a structured claim, not a bare diff. It carries its own
falsification criteria.

```
Proposal {
  id:            unique, immutable
  target:        loop / spec / step (and the specific field(s))
  change:        the concrete diff — validated by the spec schema (#131)
  hypothesis:    "this reduces X because …" (the causal claim)
  target_metric: the KPI it is meant to move (e.g. defect_escape ↓)
  guardrails:    KPIs that must NOT regress past a threshold, e.g.
                 { cycle_time: "+10%", judge_veto: "baseline+5%" }
  tier:          low | medium | high          (§6) — conductor-computed
  falsification:
    regime:      zero-data | low-data | high-data   (conductor-computed, §5)
    mode:        quantitative | qualitative        (routed by regime)
    plan:        baseline window, post window, N samples, metrics,
                 pass threshold            (when quantitative)
    why_here:    why THIS general pattern applies to THIS loop — not just
                 that it is a known pattern          (always present)
    reject_if:   the explicit condition under which a human should
                 reject the *approach*               (always present)
}
```

**Tier and regime are conductor-computed, not agent-set.** The agent may
*claim* a tier, but the conductor derives the effective tier deterministically
from the diff: which files and fields it touches. Trust-boundary fields
(egress, auth, untrusted-input rules, judge/eval/KPI-producing steps,
guardrail thresholds, the golden-run set) are tagged in the #131 spec schema;
touching any of them forces `high`. `regime` is computed from the loop's run
count, not from the agent's claim. This closes the self-labeling hole: an LLM
cannot call a trust-boundary change `low`.

**Regime-derived mode.** `falsification.mode` is derived by the conductor from
the loop's data regime (§5), never hand-set. The quantitative mode is only
available once a loop has enough runs for a metric to beat its variance; below
that the proposal is `qualitative`.

**Free hypotheses, paid validation.** The qualitative signal draws on the
LLM's *prior* — general, transferable patterns known from a huge corpus,
available even at zero runs. That knowledge is free; the validation that a
pattern applies *here* is not. A general pattern is a **prior, not evidence**:
it is the *modal* thing, and the modal thing can be wrong for this specific
loop. The `why_here` field exists so a proposal must argue applicability
rather than lean on familiarity — because the more a change *sounds* like a
known best practice, the *less* a human scrutinizes it (familiarity
short-circuits review; see §8).

**Acceptance rule.** A proposal is accepted only if, after the measurement
window, `target_metric` improved **and** every `guardrail` stayed within its
threshold. Improve the target but trip a guardrail → **auto-reject**, the
ratchet holds. This encodes the Goodhart defense in the object itself rather
than hoping for it in review.

**Honesty rule.** If a change cannot be measured with a statistically
meaningful window (low-frequency loop, one-off change), the proposal is
labeled `qualitative-only`. We do not fabricate a number; the human judgment
is the falsification, and the label says so.

## 4. The pipeline (sequenced so each stage is provable before the next)

1. **Read-only audit report** — the agent reads telemetry (run logs, KPIs,
   `FailureKind` from ADR 0035, step usage, cost rollups) and *describes*
   what is wrong. No proposals. Pure advisor. Ships first; zero write risk.
   The auditor is a **consumer of untrusted input** (ADR 0008/0050/0051): run
   logs contain issue text, PR comments, and other external content that can
   steer its proposals. It runs sandboxed with **no write credentials** —
   read-only telemetry access; proposals are emitted as data, never as
   commands.
2. **Constrained proposals + eval gate** — the agent emits tiered proposals.
   Each is validated by the schema (#131) **and** by a golden-run eval (#130)
   showing the constrained objective improved. Human approves. This is where
   "improve" becomes *measured* instead of *asserted*.
3. **Ratcheted, tiered self-optimization** — the control-system hygiene from
   §5 is in force. Only now is "the factory improves itself" true in a
   defensible sense.

**Cold start (zero-data) is a first-class entry point.** A brand-new loop with
no runs already gets qualitative proposals from the LLM's prior (general
patterns applied to the spec). Two rules keep this from becoming noise:

- **Prioritize.** Rank general-pattern proposals by how likely they are to
  matter for *this* loop's type (a `review` step and a `notify` step get
  different defaults), not by how "known" the pattern sounds.
- **Batch.** Group a new loop's cold-start proposals into a single reviewable
  set rather than N separate approvals, so a human isn't drowning in
  best-practice checkboxes.

## 5. Control-system hygiene (closed loops oscillate)

Self-optimization is a feedback loop; with LLM non-determinism in the loop it
will thrash without these rules:

- **Cooldown / hysteresis.** Do not re-optimize a loop immediately after a
  change; let it run N times and accumulate stable telemetry first.
- **One change at a time, attributable.** Apply a single proposal, measure,
  *then* the next. (This is exactly what replay (ADR 0061) + golden runs #130
  buy: attribution.)
- **A ratchet, not a random walk.** A proposal is accepted only on net
  improvement of the constrained objective; otherwise it is rejected and the
  loop is reverted. No net gain → no change.
- **Paired comparison, not before/after windows.** Before/after windows
  confound the change with time (model upgrades, issue mix, baseline drift).
  Prefer **paired A/B or shadow replay on identical inputs** via ADR 0061:
  replay the same golden runs under the old and new spec and compare outcomes
  on the *same* inputs, so the delta is the change, not the clock.
- **Name the test.** The quantitative gate specifies a named statistical test
  (e.g. paired bootstrap or permutation test on the paired run outcomes), not
  a bare "the number went down."
- **Correct for multiple comparisons.** Many proposals × many guardrails
  inflates false accepts; the pass threshold is corrected for the number of
  guardrails checked (e.g. Bonferroni or FDR).
- **Handle LLM run variance.** LLM outputs are stochastic; the paired design +
  resampling accounts for run-to-run variance, and a delta inside the variance
  band is a *no-change*, not an improvement.

### Rollback, provenance, and optimizer cost

- **Applied proposals are stored as a git commit to the `*.fabrik.yaml` spec
  plus a proposal record** (id, tier, regime, evidence, approver(s),
  before/after eval) kept with the run data, so the ratchet history is
  auditable and reversible.
- **Revert is a first-class operation.** The operator triggers it explicitly,
  or the conductor reverts **automatically** when a guardrail trips during
  the measurement window. The proposal record marks the revert, so the
  history shows the full accept→revert arc.
- **Cost cap on the optimizer itself.** Audits and eval loops spend tokens
  and CI time; the optimizer is a workload like any other. A budget cap
  (per audit, per measurement window, per project) is enforced via the #126
  cost rollups; a cap breach pauses optimization for that loop and surfaces
  it in the audit report.

**Data-regime routing (which signal leads).** The two signals differ in data
efficiency, so the regime routes which one leads — and how strong the
falsification is:

| Regime | Available | Leading signal | Falsification | Gate |
|--------|-----------|----------------|---------------|------|
| **zero-data** (0 runs) | spec + general patterns | LLM prior | weak — prior only | invariants (schema+tier) + human |
| **low-data** (≥ `MIN_BASELINE_RUNS`, default 5) | spec + a few traces | LLM prior + few obs. | weak — few observations | human + ratchet (revert-on-regression vs. baseline) |
| **high-data** (≥ graduation threshold, §8) | spec + enough runs | quantitative (metric) | strong — measured delta | metric filter + KPI/guardrail gate |

**Regime boundaries are named constants, conductor-computed.** `zero-data` =
0 completed runs. `low-data` = ≥ `MIN_BASELINE_RUNS` (default 5) completed
runs — the minimum for "regression" to mean anything: the ratchet compares
post-change runs against those baseline runs on the loop's primary outcome
(failure rate, judge verdicts) and reverts if they are worse. Below 5 runs
there is no baseline, so the loop is `zero-data` and the revert-on-regression
claim does not apply. `high-data` = enough runs that the metric beats its
variance (the graduation threshold is the §8 open question).

The fabrik eval is the final gate *whenever it is feasible*; in the zero/low
regime it usually is not, so the human + ratchet take over. The tradeoff to
keep honest: **data-frugal ↔ less falsifiable.** A zero/low-data acceptance is
a *low-confidence, qualitatively-justified* change, recorded as such — never
presented as a measured improvement.

## 6. Tiers by blast radius (different gate per tier)

| Tier | Scope | Gate |
|------|-------|------|
| **low** | prompt wording / efficiency tweaks | single approval, diff + loop-specific evidence shown |
| **medium** | structural spec changes (step order, model routing, budgets) | **two reviewers** + eval delta required |
| **high** | anything touching a trust boundary (egress, auth, untrusted-input rules, the approval gates) | expert / second-human review; **tighten-only** — a relaxing change is never proposed |

Tier is conductor-computed from the diff (§3), so the table is a property of
the change, not of the agent's self-assessment.

Security is the *scariest* tier, not the safest: an LLM can produce a spec
that *looks* hardened with a subtle hole a skimming human misses, and an
optimizer also has a quiet incentive to *relax* security to hit a speed/cost
number. The tighten-only rule removes that incentive at the source.

### Evaluator immutability (the optimizer cannot game the judge)

The DGM gaming case (§7) is not prevented by guardrails alone: a proposal can
still touch steps, prompts, and budgets in ways that move the *measurement*
instead of the *outcome* — weaken a judge step, drop a review step, change
what telemetry a KPI reads. Therefore:

- **Judge/eval/KPI-producing steps and the golden-run set are immutable to
  proposals** — touching them forces `high` tier, and even then only in a
  *strengthening* direction (tighten-only, as for trust boundaries).
- **A proposal cannot lower a guardrail threshold in the same change** that
  claims to improve a guarded KPI. Threshold changes are their own proposal,
  `high` tier, and never bundled.
- **Tiering includes a "reduces observability" check:** any diff that removes
  a log, metric, review step, or telemetry source is at least `medium`, and
  `high` if the removed signal feeds a guardrail.

### The human gate is only as strong as the approver's information

A gate that presents a familiar-sounding diff will rubber-stamp it. Concrete
mitigations, in force from the first tier:

- The approver sees **the diff plus the loop-specific evidence** (`why_here`,
  baseline numbers, the named test's result) — not just the pattern name.
- **`medium` requires a second reviewer**, so no applied change rests on one
  skim.
- **Track approval rate and time-to-approve per approver** as a
  rubber-stamp signal: a sustained ~100% instant-approval rate is itself a
  finding the auditor reports back.

## 7. Why this is safe where prior art is not

The closest prior art — the [**Darwin Gödel Machine**](https://arxiv.org/abs/2505.22954)
(Sakana AI, [arXiv:2505.22954](https://arxiv.org/abs/2505.22954) ·
[sakana.ai/dgm](https://sakana.ai/dgm/)) — has the same audit → propose →
evaluate → keep-if-better loop, but it lets the agent **rewrite its own code**
(the conductor). The paper's own analysis documents **objective hacking**: a
self-modification raised the score by removing the logging of the tokens the
evaluator keyed on, bypassing the detector without fixing the problem. That is
Goodhart's law observed in the wild, in this exact architecture.

Related lines: [**AlphaEvolve**](https://arxiv.org/abs/2506.13131) (DeepMind,
[arXiv:2506.13131](https://arxiv.org/abs/2506.13131)) — evolutionary code
improvement with evaluators; [**DSPy**](https://dspy.ai) (Stanford) — compiling
prompts against a metric ([GEPA](https://arxiv.org/abs/2507.19457) /
[MIPROv2](https://arxiv.org/abs/2406.11695)), the mature form of our
"efficient prompts" goal; [**Factory.ai**](https://factory.ai) — the commercial
"software factory" category, which ships **human approval checkpoints** as a
first-class feature.

The full set, with a "why it matters here" note and a suggested reading order,
is in the [companion reading list](self-optimization-reading-list.md).

fabrik's deliberate differences:

1. **Modify the workflows, never the conductor.** The LLM proposes changes to
   specs/loops/prompts (data), never to fabrik's orchestration code. This
   keeps principle #1 intact and removes the highest-risk surface.
2. **Guardrails are acceptance criteria**, not an afterthought — the direct
   answer to the DGM gaming failure.
3. **A human is the approver of record** for every applied change (ADR 0030
   audit is preserved: the human, not the agent, is who approved it).
4. **Trust-boundary changes are tighten-only** and expert-reviewed.

### 7.1 Formal verification of the safety envelope (optional hardening)

The safety argument above rests on the conductor enforcing a set of invariants
(acceptance rule, deterministic tiering, evaluator immutability, ratchet
monotonicity, tighten-only). Those invariants are **pure functions over data
records** — exactly what a theorem prover can check. As an optional
hardening tier, the acceptance/tiering/ratchet logic can be mirrored in a
formal specification (e.g. Lean) with a small set of theorems:

- no proposal that lowers a guardrail threshold can be accepted;
- touching a trust-boundary field forces `high` tier;
- the ratchet is monotonic (accepted states form a chain under the
  improvement order);
- tighten-only: no accepted diff relaxes a trust boundary.

Scope, stated honestly: this proves the **envelope**, not the **improvement**.
An LLM's effect on outcomes is an empirical claim — no prover can establish
that it is a theorem; it stays falsified by eval + ratchet + human gate. The
position is: *we cannot prove the improvement, but we can prove the boundary
that contains the risk.* Practical shape: the Python implementation remains
the executable spec; a CI differential test checks that the Lean model and
the Python behavior agree on a generated corpus, closing the "code matches
model" gap. This is a maintenance cost, so it is a ratchet-compatible upgrade,
not a dependency: the ADR stands on the empirical gate + tested invariants,
and the proof strengthens the claim later.

## 8. Open questions

- **The constrained objective.** Which KPIs are hard floors vs. optimizable,
  and what are the per-KPI regression thresholds? This is the design decision
  that makes or breaks the system (it is where Goodhart lives).
- **Measurement validity per loop.** How do we auto-derive a sufficient N and
  window from a loop's trigger cadence, and when do we fall back to
  `qualitative-only`?
- **Data-regime classification.** How do we auto-classify a loop's regime
  (zero / low / high) from trigger cadence × run count, and at what run-count
  does a loop *graduate* from qualitative to quantitative gating? This is the
  concrete threshold that decides when a proposal earns a measured claim.
- **Familiarity vs. scrutiny.** The `why_here` field is our mitigation for the
  risk that a "known best practice" gets approved on authority rather than
  evidence. How do we make a human actually push on a familiar-sounding
  proposal? (Counterweight: surface the loop-specific evidence, not just the
  pattern name.)
- **Scope of "prompt efficiency."** Do we adopt a DSPy-style metric compiler
  for the prompt tier, or keep it to LLM-proposed rewrites validated by eval?
  Current lean: use the fabrik eval harness as the source of truth; treat
  DSPy as a reference implementation (or a drop-in for the prompt tier only),
  contingent on the fabrik eval proving too sparse to gate on alone.
- **Typed `Proposal`.** Per AGENTS.md, the proposal object becomes a typed
  dataclass (with the JSON schema from #131 as its wire format) in the ADR —
  not a dict poked at procedurally. The conductor-computed fields (`tier`,
  `regime`) are set by the conductor, not by the constructing agent.

## 9. Roadmap placement

Tracking issue: **#176** ("Self-optimization via falsifiable proposals"),
follow-on to #130, with the read-only audit report (§4.1) as the first
shippable slice.

**Promotion to an ADR** happens when all of the following hold:

1. The make-or-break decisions in §8 are resolved (constrained objective,
   data-regime graduation threshold).
2. The read-only audit report (§4.1) has shipped and produced at least one
   real audit a human acted on.
3. This direction is explicitly approved in review.

The ADR will contain the typed `Proposal` model, the conductor-computed
tiering rules, the evaluator-immutability invariants, and the statistical
gate specification.
