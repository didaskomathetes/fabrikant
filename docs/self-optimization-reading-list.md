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
- **A Self-Improving Coding Agent (SICA)** — Robeyns, Szummer, Aitchison,
  UBC / iGent, 2025. [arXiv:2504.15228](https://arxiv.org/abs/2504.15228)
  An agent with basic coding tools that *autonomously edits its own agent
  system code* (orchestrator + tools) and keeps the change only if a held-out
  benchmark improves (17%→53% on a SWE-Bench Verified subset). The purest
  "agent rewrites its own conductor" case — exactly the scope we deliberately
  *avoid* (we let the LLM touch specs, never the orchestration).
- **STOP: Self-Taught Optimizer: Recursively Self-Improving Code Generation** —
  Zelikman, Lorch, Mackey, Kalai, 2023 (COLM 2024).
  [arXiv:2310.02304](https://arxiv.org/abs/2310.02304)
  A scaffolding program that *rewrites its own improver code* (the LM stays
  frozen), scored by a utility on fixed tasks. Notable for **measuring
  sandbox-bypass attempts** — an explicit trust-boundary test we should copy as
  an audit signal: how often does a self-modification try to leave its
  boundary?
- **GPTSwarm: Language Agents as Optimizable Graphs** — Zheng, Men, Yang, Ju,
  ICML 2024. [arXiv:2402.16823](https://arxiv.org/abs/2402.16823)
  Treats the agent workflow as a graph and optimizes node prompts *and* edges
  (connectivity) against a fixed task. Bounded to graph + node data — the
  orchestrating search algorithm is not itself modified. A clean model of our
  "modify the workflow, not the conductor" boundary.
- **EvoAgent: Towards Automatic Multi-Agent Generation via Evolutionary
  Algorithms** — Yuan, Song, Chen, et al., NAACL 2025.
  [arXiv:2406.14228](https://arxiv.org/abs/2406.14228)
  Evolves agent *configurations* (skills/settings) by mutation/crossover/
  selection from a seed framework. The EA conductor is fixed; only agent data
  evolves. Weakest evaluator isolation here (task metric + an LLM quality
  check) — a contrast for why our judge must be immutable.
- **Reflexion: Language Agents with Verbal Reinforcement Learning** —
  Shinn et al., NeurIPS 2023. [arXiv:2303.11366](https://arxiv.org/abs/2303.11366)
  The single-agent self-improvement loop: convert task feedback into verbal
  self-reflections stored in episodic memory for the next trial. No code
  change, no separate judge — the baseline "improve by reflecting" pattern.
- **Self-Refine: Iterative Refinement with Self-Feedback** — Madaan et al.,
  NeurIPS 2023. [arXiv:2303.17651](https://arxiv.org/abs/2303.17651)
  generate → self-critique → refine, with the *same* model as generator,
  critic, and refiner. The anti-pattern for evaluation integrity: self-grading
  with no independent judge. Our evaluator-immutability rule is the direct
  answer to this.
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
- **FunSearch: Mathematical Discoveries from Program Search with LLMs** —
  Romera-Paredes et al., *Nature* 625:468–475, 2024.
  [nature.com/articles/s41586-023-06924-6](https://www.nature.com/articles/s41586-023-06924-6)
  Evolves candidate programs (heuristic code) with a **fixed, deterministic,
  non-LLM evaluator** while the LLM is frozen. The cleanest form of
  "evaluator immutability by construction" — the LLM proposes, a frozen judge
  scores, so Goodhart cannot reach the measurement. The target shape for our
  quantitative gate.
- **AlphaCodium: Code Generation — From Prompt Engineering to Flow
  Engineering** — Ridnik, Kredo, Friedman, Codium, 2024.
  [arXiv:2401.08500](https://arxiv.org/abs/2401.08500)
  A fixed multi-agent "flow" (coder/critic/debugger/optimizer) that optimizes
  the *solution* via execution feedback. Only the output program changes; the
  flow is fixed — but the judge is partly an LLM critic, a weaker integrity
  position than FunSearch.

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

## 4. Safety & trust-boundary enforcement for self-improving systems

The newest and most directly on-point line: how do you make a self-improving
runtime's safety *falsifiable* rather than self-graded? Two 2026 preprints
formalize essentially the same envelope fabrik proposes; keep both close.

- **Falsifiable Release Gates for Self-Improving Systems: Standing Invariants
  at Scale** — Soni, 2026. [arXiv:2607.13070](https://arxiv.org/abs/2607.13070)
  Every new capability must pass a **pre-declared, machine-checkable
  acceptance suite** while a fixed set of standing invariants is preserved
  across every gate: a **non-self-modifiable gate** ("the improver cannot edit
  its own acceptance criteria, so the judge stays fixed relative to the thing
  it judges"), acceptance monotonicity, one-cycle reversibility, eval-
  freshness, a **reward-hacking floor** (decide on external verified
  correctness, never self-reported confidence), and a residency invariant.
  Machine-checked (TLA+) and carried through six further releases without the
  safety core weakening. This is fabrik's §7.1 (formal envelope) with a working
  precedent — and the strongest citation for our "constrained objective"
  decision.
- **The Unfireable Safety Kernel: Execution-Time AI Alignment for AI Agents and
  Other Escapable AI Systems** — Dobrin & Chmiel, 2026.
  [arXiv:2606.26057](https://arxiv.org/abs/2606.26057)
  A compiled deterministic kernel mediates what self-modifications are
  permitted; the agent runtime is treated as untrusted by construction
  (process separation, pre-action enforcement on a structurally-only path,
  fail-closed, externalized signed evidence). Against a live self-improving
  world model: across 1,000 self-modifications, all 704 attempts on the
  safety-critical core were refused, no escape. The architectural form of our
  "the LLM never touches the conductor."
- **Asimov Safety Architecture (ASA)** — IETF draft, 2025.
  [datatracker.ietf.org](https://datatracker.ietf.org/doc/html/draft-baysal-asimov-safety-architecture-00)
  Separates a reasoning model from a stateless, zero-context judge/gate LLM;
  operator-defined ground rules **must not be modifiable at runtime** by the
  reasoning model ("operator sovereignty"). A second, explicit dual-gate
  trust-boundary pattern — the industry-facing echo of tighten-only.

**Positioning note.** On the two axes that matter most — *can it modify its own
conductor/evaluator* and *does a human approve each change* — prior art splits
cleanly: the RSI research line (DGM, ADAS, SICA, STOP, Clune) self-modifies
autonomously with no per-change human; the commercial factories (Factory.ai,
Devin, Jules, Codex, §6) have a human gate but do not self-optimize. fabrik is
the intersection of both — data-only self-modification *and* a human approver
of record *and* an immutable judge — and the two 2026 preprints above are the
closest formal prior art to that intersection.

## 5. Open-endedness & evolutionary search (the ratchet / archive pattern)

- **AI-GAs: AI-generating algorithms, an alternate paradigm for producing
  general artificial intelligence** — Clune, 2019.
  [arXiv:1905.10985](https://arxiv.org/abs/1905.10985)
  The archive-of-solutions pattern DGM uses: keep diverse, non-dominated
  solutions so later improvements can build on earlier "stepping stones." The
  reason our ratchet is an *archive*, not a single best-so-far pointer.
- **OMNI: Open-Endedness via Novelty Search** — Zhang, Lehman, Stanley, Clune,
  ICLR 2024. [arXiv:2306.01711](https://arxiv.org/abs/2306.01711)
  The mature open-endedness engine behind DGM/ADAS: an ever-growing archive of
  novel, learnable solutions. The theoretical reason "keep diverse stepping
  stones" beats hill-climbing one KPI.
- **Open-Endedness is Essential for ASI** — Hughes et al., DeepMind, 2024.
  [arXiv:2406.04268](https://arxiv.org/abs/2406.04268)
  The argument that bounded, single-objective optimization plateaus; open-
  endedness (indefinite novelty) is what keeps improving. Caveat: in this line
  the *reward itself* can be self-generated — a known open safety risk that
  reinforces our "constrained objective" open question.
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

## 6. The "software factory" category & autonomous SWE

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
- **Cognition Devin · Google Jules · OpenAI Codex (cloud agent)** —
  [cognition.ai](https://cognition.ai) (2024) ·
  [jules.google](https://jules.google) (2025) ·
  [openai.com](https://openai.com/index/introducing-codex/) (2025)
  The commercial coding-agent category: all three generate code in a sandbox
  and hand off a PR to a **human reviewer** — the human gate is the
  approver-of-record, but none of them self-optimizes its own agent. They are
  the "worker" side of the factory with a human gate, and the counterpoint
  that a gate alone is not self-improvement.
- **Software Product Lines: Architecture and Process** — Clements & Northrop,
  2001 (Addison-Wesley).
  The academic origin of the term "software factory" (productivity via
  reusable, configurable assets) — useful for positioning fabrik against the
  pre-LLM meaning.

## 7. Feedback-loop stability (the oscillation angle)

- **Control-theory basics applied to agentic loops** — any short treatment of
  feedback control, hysteresis, and deadbands.
  Our §5 (cooldown, one-change-at-a-time, ratchet) is applied control theory:
  a closed loop with a stochastic actor oscillates without damping. Read for
  the vocabulary (hysteresis, deadband, gain) to name the knobs we'll tune.

## 8. fabrik's own foundations (internal)

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
4. **Falsifiable Release Gates** + **Unfireable Safety Kernel** (§4) — the
   safety envelope made falsifiable; the newest on-point line.
5. **Open-endedness / Clune** (§5) — why the ratchet is an archive.
6. **Factory.ai** + the commercial coding agents (§6) — the category + the
   human-gate precedent.
7. **fabrik ADRs** (§8) — the invariants the design must respect.

The two make-or-break decisions in `self-optimization.md` §8 map onto this
list: the **constrained objective** is a §3 question (Goodhart) with a §4
answer (falsifiable gates + a reward-hacking floor), and the **data-regime
threshold** is a §2 question (when does a metric beat its variance).
