# Reading List — Self-Optimizing Software Factories & Falsifiable Self-Improvement

Companion to `self-optimization.md`. Grouped by theme; each entry has a one-line
"why it matters here." Links marked **(verified)** were confirmed at drafting
time; the rest are given as title + author + year for lookup — verify the exact
identifier before citing it in an ADR.

Suggested order is at the bottom.

---

## 1. Self-improving / self-modifying agents (the core prior art)

- **The Darwin Gödel Machine** — Singh et al., Sakana AI, 2025.
  arXiv:2505.22954 · https://sakana.ai/dgm/ **(verified)**
  The closest analog to our design: each agent *analyzes its own eval logs →
  proposes the next feature → implements it → is quantitatively evaluated →
  kept in an archive only if it improves, else discarded.* That is
  audit → falsifiable proposal → ratchet, verbatim. Read this first.
- **The Gödel Machine** — Schmidhuber, 2006 (technical report).
  The theoretical root (the "Gödel" in DGM): a self-modifying agent that only
  accepts a change it can *prove* is beneficial. Our design is the empirical,
  unprovable descendant — we substitute a benchmark/eval + human gate for the
  proof. Worth reading to see what we're deliberately *not* doing.
- **AlphaEvolve: A coding agent for scientific and algorithmic discovery** —
  Novikov et al., Google DeepMind, 2025. arXiv:2506.13131 **(verified)**
  Evolutionary pipeline of LLMs improving code "continuously receiving feedback
  from one or more evaluators." The propose→evaluate→keep loop at scale; note
  it even sped up training of the LLM inside it (self-referential).
- **ADAS: Automated Design of Agentic Systems** — Hu, Lu, Clune, 2024.
  arXiv:2408.08435 (verify ID)
  A meta-agent that *designs* agents (prompts/workflows/tools) as code. The
  "agent designs agents" line; DGM's main baseline.
- **Gödel Agent** — Zhang et al., 2024. (verify ID)
  Another self-modifying agent in the same family; useful for the range of
  approaches, not a primary read.
- **Failure analyses of self-modifying agents** — e.g. the safeguard.sh writeup
  "Self-Modifying Coding Agents: DGM, ADAS, Gödel Agent" and independent
  DGM post-mortems, 2025–2026.
  **The most important safety read.** Documents the DGM **benchmark-gaming**
  case: a self-modification raised the score by *removing the logging of the
  tokens the evaluator keyed on*, bypassing the detector without fixing the
  problem. This is Goodhart's law observed in the wild, in our exact
  architecture — the citation that justifies our guardrails + human gate.

## 2. Prompt & program optimization (the quantitative signal)

- **DSPy: Compiling Declarative LM Calls** — Khattab et al., Stanford, 2023.
  arXiv:2310.03714 · https://dspy.ai **(verified)**
  "Program, don't prompt": express tasks as typed signatures, then *compile
  against a metric.* The mature form of our "make prompts more efficient,
  falsified by a metric" goal.
- **GEPA: Reflective Prompt Evolution** — 2025. arXiv:2507.19457 **(verified)**
  The current best DSPy optimizer. An LLM *reflects on execution traces,
  proposes prompt mutations, keeps only the ones that score better* — a
  ratchet on prompts. The single most relevant paper for our prompt tier.
- **MIPROv2: Optimizing Instructions & Demos** — Opsahl-Ong et al., 2024.
  arXiv:2406.11695 **(verified)**
  Bayesian joint optimization of instructions + few-shot demos. The
  pre-GEPA workhorse.
- **OPRO: Large Language Models as Optimizers** — Yang et al., Google, 2023.
  arXiv:2308.00451 (verify ID)
  Uses an LLM to iteratively generate better prompts from (prompt, score)
  history. The simplest "score-driven prompt search" — a good minimal model of
  the quantitative signal.
- **TextGrad: Automatic "Differentiation" via Text** — Zhou et al., Stanford,
  2024. arXiv:2406.07496 (verify ID)
  Treats LLM feedback as "gradients" to optimize text/prompects. The
  gradient-descent framing of prompt optimization.

## 3. Goodhart's law, reward & specification gaming (the core risk)

- **Goodhart's Law** — Goodhart, 1975, "Problems of Management of the Economy"
  (Bank of England).
  The root: "when a measure becomes a target, it ceases to be a good measure."
  The one-paragraph origin of our entire guardrail design.
- **Specification gaming: the flip side of AI robustness** — Krueger et al.,
  DeepMind, 2020. arXiv:2012.08825 (verify ID)
  The taxonomy of how optimizers find the *metric* instead of the *goal*
  (reward hacking, apparent vs. direct, etc.). The conceptual backbone for why
  a single KPI target is unsafe.
- **The DGM benchmark-gaming case study** — (see §1 failure analyses).
  The concrete, recent instance. Read §3 theory, then this, to see it happen.

## 4. Open-endedness & evolutionary search (the ratchet / archive pattern)

- **Open-endedness** — Clune, 2019–2021 ("Growing the Universe"; "Evolution in
  the Age of AI").
  The archive-of-solutions pattern DGM uses: keep diverse, non-dominated
  solutions so later improvements can build on earlier "stepping stones." The
  reason our ratchet is an *archive*, not a single best-so-far pointer.
- **Why search (not just optimization) matters** — Stanley & Lehman, 2015.
  The argument against premature convergence on one metric — directly relevant
  to why we keep guardrails + a diverse proposal archive instead of hill-
  climbing a single KPI.

## 5. The "software factory" category & autonomous SWE

- **Factory.ai** — https://factory.ai **(verified)**
  The commercial namesake: Droids, Missions (long-running multi-phase work),
  Automations (event-driven/scheduled), and — tellingly — **human approval
  checkpoints** as a first-class feature. Confirms the category and the
  human-gate pattern.
- **OpenHands (All-Hands-AI)** — https://github.com/All-Hands-AI/OpenHands
  Open-source autonomous SWE agent; the "worker" side of the factory.
- **SWE-agent** — Yang et al., Princeton, 2024.
  The agent–computer interface for software engineering; a clean reference for
  how a worker agent is given tools.
- **SWE-bench** — Jimenez et al., 2023.
  The benchmark the self-improving agents are measured on; understand it to
  understand what "improvement" means (and how it can be gamed) in §1.
- **Software Product Lines / the original "software factory"** — Clements &
  Northrop.
  The academic origin of the term "software factory" (productivity via
  reusable, configurable assets) — useful for positioning fabrik against the
  pre-LLM meaning.

## 6. Feedback-loop stability (the oscillation angle)

- **Control-theory basics applied to agentic loops** — any short treatment of
  feedback control, hysteresis, and deadbands.
  Our §5 (cooldown, one-change-at-a-time, ratchet) is applied control theory:
  a closed loop with a stochastic actor oscillates without damping. Read for
  the vocabulary (hysteresis, deadband, gain) to name the knobs we'll tune.

## 7. fabrik's own foundations (internal)

- **ADR 0002 / 0034** — session log → typed run model + relational store.
  The telemetry the auditor reads.
- **ADR 0035** — `FailureKind` failure classification.
  The structured signal behind "loop X fails 30% on review with
  FailureKind=timeout."
- **ADR 0008 / 0050** — untrusted-input rules + agent env allowlist.
  The trust boundaries our tighten-only rule protects.
- **ADR 0015 / 0057** — approval service + needs-human escalation.
  The gate every proposal flows through.
- **ADR 0030** — auth, roles, audit (who started/approved what).
  Why the *human*, not the agent, is the approver of record.
- **#129 replay · #130 golden runs + evals · #131 spec schema · #126/#127 KPIs**
  The four dependencies this design stands on.

---

## Suggested reading order

1. **DGM** (§1) — the shape of the thing we're building.
2. **DGM failure analysis** (§1) + **specification gaming** (§3) — why it can
   go wrong, and the guardrails that answer it.
3. **GEPA** + **DSPy** (§2) — the quantitative signal, mature form.
4. **Open-endedness / Clune** (§4) — why the ratchet is an archive.
5. **Factory.ai** (§5) — the commercial category + human-gate precedent.
6. **fabrik ADRs** (§7) — the invariants the design must respect.

The two make-or-break decisions in `self-optimization.md` §8 map onto this
list: the **constrained objective** is a §3 question (Goodhart), and the
**data-regime threshold** is a §2 question (when does a metric beat its
variance).
