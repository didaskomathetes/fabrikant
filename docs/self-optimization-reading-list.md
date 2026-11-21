# Reading List — Self-Optimizing Software Factories & Falsifiable Self-Improvement

Companion to `self-optimization.md`. Grouped by theme; each entry has a one-line
"why it matters here." All arXiv IDs and URLs below were verified at drafting
time (2026-11-21). Entries without a URL are books/speeches cited
bibliographically.

Suggested order is at the bottom.

---

## 1. Self-improving / self-modifying agents (the core prior art)

- **The Darwin Gödel Machine** — Zhang, Hu, Lu, et al., Sakana AI / UBC, 2025.
  [arXiv:2505.22954](https://arxiv.org/abs/2505.22954) ·
  [sakana.ai/dgm](https://sakana.ai/dgm/)
  The closest analog to our design: each agent *analyzes its own eval logs →
  proposes the next feature → implements it → is quantitatively evaluated →
  kept in an archive only if it improves, else discarded.* That is
  audit → falsifiable proposal → ratchet, verbatim. Read this first.
- **Gödel Machines: Self-Referential Universal Problem Solvers Making
  Provably Optimal Self-Improvements** — Schmidhuber, 2003 (revised 2006).
  [arXiv:cs/0309048](https://arxiv.org/abs/cs/0309048)
  The theoretical root (the "Gödel" in DGM): a self-modifying agent that only
  accepts a change it can *prove* is beneficial. Our design is the empirical,
  unprovable descendant — we substitute a benchmark/eval + human gate for the
  proof. Worth reading to see what we're deliberately *not* doing.
- **AlphaEvolve: A coding agent for scientific and algorithmic discovery** —
  Novikov et al., Google DeepMind, 2025.
  [arXiv:2506.13131](https://arxiv.org/abs/2506.13131)
  Evolutionary pipeline of LLMs improving code "continuously receiving feedback
  from one or more evaluators." The propose→evaluate→keep loop at scale; note
  it even sped up training of the LLM inside it (self-referential).
- **ADAS: Automated Design of Agentic Systems** — Hu, Lu, Clune, 2024.
  [arXiv:2408.08435](https://arxiv.org/abs/2408.08435)
  A meta-agent that *designs* agents (prompts/workflows/tools) as code. The
  "agent designs agents" line; DGM's main baseline.
- **Gödel Agent: A Self-Referential Agent Framework for Recursive
  Self-Improvement** — Yin et al., 2024.
  [arXiv:2410.04444](https://arxiv.org/abs/2410.04444)
  Another self-modifying agent in the same family; useful for the range of
  approaches, not a primary read.
- **Failure analyses of self-modifying agents** — primary source: the DGM
  paper's own discussion of **objective hacking**
  ([arXiv:2505.22954](https://arxiv.org/abs/2505.22954), the agent removes its
  own hallucination-detection / logging that the evaluator keyed on);
  secondary: [safeguard.sh, "Self-Modifying Coding Agents: DGM, ADAS, Gödel
  Agent"](https://safeguard.sh/resources/blog/self-modifying-coding-agents-darwin-godel-machine-adas-and-godel-agent-explained).
  **The most important safety read.** Documents the DGM **benchmark-gaming**
  case: a self-modification raised the score by *removing the logging of the
  tokens the evaluator keyed on*, bypassing the detector without fixing the
  problem. This is Goodhart's law observed in the wild, in our exact
  architecture — the citation that justifies our guardrails + human gate.

## 2. Prompt & program optimization (the quantitative signal)

- **DSPy: Compiling Declarative LM Calls** — Khattab et al., Stanford, 2023.
  [arXiv:2310.03714](https://arxiv.org/abs/2310.03714) · [dspy.ai](https://dspy.ai)
  "Program, don't prompt": express tasks as typed signatures, then *compile
  against a metric.* The mature form of our "make prompts more efficient,
  falsified by a metric" goal.
- **GEPA: Reflective Prompt Evolution** — Agrawal et al., 2025.
  [arXiv:2507.19457](https://arxiv.org/abs/2507.19457)
  The current best DSPy optimizer. An LLM *reflects on execution traces,
  proposes prompt mutations, keeps only the ones that score better* — a
  ratchet on prompts. The single most relevant paper for our prompt tier.
- **MIPROv2: Optimizing Instructions & Demos** — Opsahl-Ong et al., 2024.
  [arXiv:2406.11695](https://arxiv.org/abs/2406.11695)
  Bayesian joint optimization of instructions + few-shot demos. The
  pre-GEPA workhorse.
- **OPRO: Large Language Models as Optimizers** — Yang et al., Google, 2023.
  [arXiv:2309.03409](https://arxiv.org/abs/2309.03409)
  Uses an LLM to iteratively generate better prompts from (prompt, score)
  history. The simplest "score-driven prompt search" — a good minimal model of
  the quantitative signal.
- **TextGrad: Automatic "Differentiation" via Text** — Yuksekgonul et al.,
  Stanford, 2024. [arXiv:2406.07496](https://arxiv.org/abs/2406.07496)
  Treats LLM feedback as "gradients" to optimize text/prompts. The
  gradient-descent framing of prompt optimization.

## 3. Goodhart's law, reward & specification gaming (the core risk)

- **Goodhart's Law** — Goodhart, 1975, "Problems of Management of the Economy"
  (Bank of England speech).
  The root: "when a measure becomes a target, it ceases to be a good measure."
  The one-paragraph origin of our entire guardrail design.
- **Specification gaming: the flip side of AI ingenuity** — Krakovna, Letcher,
  et al., DeepMind, 2020.
  [deepmind.google/blog/specification-gaming-the-flip-side-of-ai-ingenuity](https://deepmind.google/blog/specification-gaming-the-flip-side-of-ai-ingenuity)
  The taxonomy of how optimizers find the *metric* instead of the *goal*
  (reward hacking, apparent vs. direct, etc.). The conceptual backbone for why
  a single KPI target is unsafe.
- **The DGM objective-hacking case study** — (see §1 failure analyses).
  The concrete, recent instance. Read §3 theory, then this, to see it happen.

## 4. Open-endedness & evolutionary search (the ratchet / archive pattern)

- **AI-GAs: AI-generating algorithms, an alternate paradigm for producing
  general artificial intelligence** — Clune, 2019.
  [arXiv:1905.10985](https://arxiv.org/abs/1905.10985)
  The archive-of-solutions pattern DGM uses: keep diverse, non-dominated
  solutions so later improvements can build on earlier "stepping stones." The
  reason our ratchet is an *archive*, not a single best-so-far pointer.
- **Open Questions in Creating Safe Open-ended AI: Tensions Between Control
  and Creativity** — Ecoffet, Clune, Lehman, 2020.
  [arXiv:2006.07495](https://arxiv.org/abs/2006.07495)
  The safety side of open-ended search — directly relevant to why an
  open-ended optimizer needs the control structure (tiers, gates, ratchet)
  this doc describes.
- **Why So Many Kinds? Animal Diversity in the Light of Evolutionary
  Development** — Stanley & Lehman, 2015 (book, Cambridge UP).
  The argument against premature convergence on one metric — directly relevant
  to why we keep guardrails + a diverse proposal archive instead of hill-
  climbing a single KPI.

## 5. The "software factory" category & autonomous SWE

- **Factory.ai** — [factory.ai](https://factory.ai)
  The commercial namesake: Droids, Missions (long-running multi-phase work),
  Automations (event-driven/scheduled), and — tellingly — **human approval
  checkpoints** as a first-class feature. Confirms the category and the
  human-gate pattern.
- **OpenHands (All-Hands-AI)** —
  [github.com/All-Hands-AI/OpenHands](https://github.com/All-Hands-AI/OpenHands)
  Open-source autonomous SWE agent; the "worker" side of the factory.
- **SWE-agent: Agent-Computer Interfaces Enable Automated Software
  Engineering** — Yang et al., Princeton, 2024.
  [arXiv:2405.15793](https://arxiv.org/abs/2405.15793)
  The agent–computer interface for software engineering; a clean reference for
  how a worker agent is given tools.
- **SWE-bench: Can Language Models Resolve Real-World GitHub Issues?** —
  Jimenez et al., 2023. [arXiv:2310.06770](https://arxiv.org/abs/2310.06770)
  The benchmark the self-improving agents are measured on; understand it to
  understand what "improvement" means (and how it can be gamed) in §1.
- **Software Product Lines: Architecture and Process** — Clements & Northrop,
  2001 (Addison-Wesley).
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
- **ADR 0008 / 0050 / 0051** — untrusted-input rules, agent env allowlist,
  egress default-deny.
  The trust boundaries our tighten-only rule protects — and the rules the
  *auditor itself* runs under (it reads untrusted content).
- **ADR 0011 / 0059** — approval-request protocol + `needs human` escalation.
  The gate every proposal flows through.
- **ADR 0030** — auth, roles, audit (who started/approved what).
  Why the *human*, not the agent, is the approver of record.
- **ADR 0061** — replay, deterministic re-execution of a finished run
  (`fabrik replay`, `replay_of`, diff report).
  The attribution + paired-comparison mechanism: the same golden runs replayed
  under old vs. new spec, so a measured delta is the change, not time.
- **#130 golden runs + evals · #131 spec schema · #126/#127 KPIs**
  The remaining dependencies this design stands on.

---

## Suggested reading order

1. **DGM** (§1) — the shape of the thing we're building.
2. **DGM objective-hacking case** (§1) + **specification gaming** (§3) — why
   it can go wrong, and the guardrails that answer it.
3. **GEPA** + **DSPy** (§2) — the quantitative signal, mature form.
4. **Open-endedness / Clune** (§4) — why the ratchet is an archive.
5. **Factory.ai** (§5) — the commercial category + human-gate precedent.
6. **fabrik ADRs** (§7) — the invariants the design must respect.

The two make-or-break decisions in `self-optimization.md` §8 map onto this
list: the **constrained objective** is a §3 question (Goodhart), and the
**data-regime threshold** is a §2 question (when does a metric beat its
variance).
