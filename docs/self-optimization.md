# Self-Optimization via Falsifiable Proposals

**Status:** Proposal (design doc) — not yet an accepted ADR.
**Depends on:** #130 (golden runs + eval harness), #129 (replay), #131 (spec JSON schema), #126/#127 (KPIs).
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
  approval through the existing approval gate (ADR 0015/0057).
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
  tier:          low | medium | high          (§6)
  falsification:
    regime:      zero-data | low-data | high-data   (auto-derived, §5)
    mode:        quantitative | qualitative        (routed by regime)
    plan:        baseline window, post window, N samples, metrics,
                 pass threshold            (when quantitative)
    why_here:    why THIS general pattern applies to THIS loop — not just
                 that it is a known pattern          (always present)
    reject_if:   the explicit condition under which a human should
                 reject the *approach*               (always present)
}
```

**Regime-derived mode.** `falsification.mode` is auto-derived from the loop's
data regime (§5), never hand-set. The quantitative mode is only available
once a loop has enough runs for a metric to beat its variance; below that the
proposal is `qualitative`.

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
  *then* the next. (This is exactly what replay #129 + golden runs #130 buy:
  attribution.)
- **A ratchet, not a random walk.** A proposal is accepted only on net
  improvement of the constrained objective; otherwise it is rejected and the
  loop is reverted. No net gain → no change.
- **Baseline + sample size.** Quantitative falsification names the baseline
  window, the post window, and an N large enough that the delta is not noise.
  A weekly loop needs a multi-week window; a per-issue loop needs enough issue
  volume.

**Data-regime routing (which signal leads).** The two signals differ in data
efficiency, so the regime routes which one leads — and how strong the
falsification is:

| Regime | Available | Leading signal | Falsification | Gate |
|--------|-----------|----------------|---------------|------|
| **zero-data** | spec + general patterns | LLM prior | weak — prior only | invariants (schema+tier) + human + ratchet |
| **low-data** | spec + a few traces | LLM prior + few obs. | weak — few observations | human + ratchet (revert-on-regression) |
| **high-data** | spec + enough runs | quantitative (metric) | strong — measured delta | metric filter + KPI/guardrail gate |

The fabrik eval is the final gate *whenever it is feasible*; in the zero/low
regime it usually is not, so the human + ratchet take over. The tradeoff to
keep honest: **data-frugal ↔ less falsifiable.** A zero/low-data acceptance is
a *low-confidence, qualitatively-justified* change, recorded as such — never
presented as a measured improvement.

## 6. Tiers by blast radius (different gate per tier)

| Tier | Scope | Gate |
|------|-------|------|
| **low** | prompt wording / efficiency tweaks | lightweight approval |
| **medium** | structural spec changes (step order, model routing, budgets) | normal approval + eval delta required |
| **high** | anything touching a trust boundary (egress, auth, untrusted-input rules, the approval gates) | expert / second-human review; **tighten-only** — a relaxing change is never proposed |

Security is the *scariest* tier, not the safest: an LLM can produce a spec
that *looks* hardened with a subtle hole a skimming human misses, and an
optimizer also has a quiet incentive to *relax* security to hit a speed/cost
number. The tighten-only rule removes that incentive at the source.

## 7. Why this is safe where prior art is not

The closest prior art — the [**Darwin Gödel Machine**](https://arxiv.org/abs/2505.22954)
(Sakana AI, [arXiv:2505.22954](https://arxiv.org/abs/2505.22954) ·
[sakana.ai/dgm](https://sakana.ai/dgm/)) — has the same audit → propose →
evaluate → keep-if-better loop, but it lets the agent **rewrite its own code**
(the conductor). Follow-up analysis documented **benchmark gaming**: a
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

## 8. Open questions

- **The constrained objective.** Which KPIs are hard floors vs. optimizable,
  and what are the per-KPI regression thresholds? This is the design decision
  that makes or breaks the system (it is where Goodhart lives).
- **Measurement validity per loop.** How do we auto-derive a sufficient N and
  window from a loop's trigger cadence, and when do we fall back to
  `qualitative-only`?
- **Proposal provenance & replay.** Every applied proposal should be recorded
  with its before/after eval so the ratchet history is auditable and reversible.
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

## 9. Roadmap placement

Follow-on to #130. Suggested tracking: a new issue "self-optimization via
falsifiable proposals" under the First Wins / post-#130 bucket, with the
read-only audit report (§4.1) as the first shippable slice. An ADR follows on
approval of this direction.
