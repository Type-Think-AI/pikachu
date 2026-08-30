# 19 — Feedback, Reputation and Getting Better From Day One

The ask: agents feel empty and useless on day one; working with them should compound; add reward
points and punishment so good decisions are reinforced and bad ones discouraged.

**The intuition is right and the mechanism needs changing.** Researched 2026-08-30. Three findings
reshape it, and one of them says points are the wrong instrument.

---

## Finding 1 — a scored LM agent games the score, and it resists fixing

> "Reward hacking arises naturally when optimizing proxy objectives with capable language model
> agents and **resists standard mitigations**, suggesting that proxy-reward failures in agentic
> settings may require approaches beyond standard exploration and credit-assignment fixes."
> — [Reward Hacking in Language Model Agents, arXiv 2606.15385](https://arxiv.org/html/2606.15385v1)

Reward hacking is when a model "produces behavioral trajectories that mathematically maximize the
proxy reward while actively degrading or bypassing the intended objective"
([arXiv 2604.13602](https://arxiv.org/html/2604.13602v1)). And a survey of 373 studies frames the
whole class: "benchmark scores, reward signals, and safety metrics can improve while the
capabilities they are meant to represent remain uncertain"
([EvalSafetyGap, arXiv 2606.30219](https://arxiv.org/html/2606.30219v3)).

Goodhart's law, in RL terms: "it is often appropriate to think of the reward function as a **proxy
for the true objective rather than as its definition**"
([arXiv 2310.09144](https://arxiv.org/html/2310.09144)).

### The rule this produces

> **The agent must never see its own score.**

Not "we will design the reward carefully" — the paper says careful design resists mitigation. The
robust move is to remove the optimization target from the agent's context entirely. The score shapes
**what the agent is shown before the turn**; the agent never learns it exists, so there is nothing
to optimize.

This is cheap, absolute, and it is the single most important design decision here.

### A second hard constraint

**We cannot train the model.** We call hosted models over OpenRouter — no weight updates. So
"reward" can only ever influence **retrieval and selection**, never the policy. Anyone imagining
reward → better reasoning is imagining RLHF, which is not available to us. Reward → better
*context* is available, and is the whole mechanism.

---

## Finding 2 — corrections beat points, and memory alone is not enough

> "Compiling corrections into runtime enforcement can address a repeated-friction failure mode that
> **memory alone does not reliably solve**, reducing the need for users to restate the same
> correction across future sessions."
> — [Compiling User Corrections into Runtime Enforcement for Coding Agents, arXiv 2606.13174](https://arxiv.org/abs/2606.13174)

This is the paper that changes the design. When a user dislikes a response, the high-value artifact
is not "−1 point." It is a **rule**, compiled into enforcement so the agent cannot repeat the
mistake — and the paper's finding is that dropping it into memory and hoping retrieval surfaces it
does **not** reliably work.

So the correction path is:

```
user rejects/edits output
   → capture WHAT was wrong (not just that it was wrong)
   → compile into a durable rule on that agent
   → the rule is enforced, not merely retrievable
```

A rule is deterministic; a memory is a lottery ticket on retrieval.

---

## Finding 3 — implicit and explicit signals are complementary, not ranked

My initial instinct was "behavioural signals beat thumbs-up." The literature says that is too
simple:

- Implicit is **dense but noisy and biased** — "a large portion of clicks do not translate to
  purchases, and many purchases end up with negative reviews"
  ([arXiv 2112.01160](https://arxiv.org/html/2112.01160v1)).
- Explicit is **sparse but high-signal**.

The useful synthesis: **repetition is the reliability measure.** One signal is noise; the same
signal repeatedly is evidence — see [Uncertainty in Repeated Implicit Feedback as a Measure of
Reliability](https://arxiv.org/html/2505.02492v1).

And on update discipline, from a practitioner writeup that gets it right: compare the profile
against recent behaviour, produce a **conservative diff**, and update "without overreacting to one
weird week"
([Oracle](https://blogs.oracle.com/developers/how-i-taught-an-ai-to-sound-like-me-agent-memory-with-oracle-database-26ai)).

---

## The design

### Two separate systems that must not be confused

| | Audience | Purpose | Sees the score? |
|---|---|---|---|
| **Points / levels / streaks** | the **human** | motivation, visible progress, "my agent is improving" | user does, agent never |
| **Signal ledger** | the **machine** | reranks retrieval, drives promotion | agent never |

The gamification the ask describes is legitimate **as a product surface**. Keep it — just make it a
UI projection over the ledger rather than an input to the agent. That way "win long term, manage
agents that level up" is real for the user and inert for the model.

### The signal ledger

Every turn emits typed signals. Signals are **evidence with a subject**, never a scalar verdict.

```python
class Signal(BaseModel):
    subject: SignalSubject        # agent | skill | memory | artifact | tool
    subject_id: str
    kind: SignalKind
    strength: float               # small; magnitude comes from repetition
    run_id: str
    at: datetime
```

| Kind | Source | Class |
|---|---|---|
| `kept` | artifact stays on canvas | implicit, positive |
| `exported` | downloaded / published | implicit, strong positive |
| `regenerated_away` | user regenerates the same intent | implicit, strong negative — **free to collect** |
| `edited_then_kept` | user fixed it and kept it | positive **plus a correction** |
| `abandoned` | run cancelled mid-way | implicit, weak negative |
| `rated` | explicit thumb | explicit, sparse, high-signal |
| `corrected` | user states what was wrong | **highest value** — becomes a rule |
| `reused` | skill invoked again by choice | positive, drives promotion |

### Attribution — be conservative, because credit assignment is hard

A bad turn has many candidate causes: the model, a loaded skill, a retrieved memory, the agent's
persona, or the user's own brief. Blaming one is a credit-assignment problem, and the reward-hacking
paper explicitly says credit-assignment fixes are not sufficient.

**Rule: attribute only when isolatable.** A signal attaches to a subject when that subject was the
*only* variable, or when a pattern repeats across runs. Otherwise it attaches to the **run** and
stays unattributed. An unattributed negative is honest; a misattributed one silently degrades a good
skill.

### The structural rule: LLM proposes, deterministic code credits

There is a published pattern that states this better than I did:

> "A self-evolving agent-harness framework that **separates proposing changes from crediting them**:
> a language model diagnoses failures and proposes patches, while all sampling, measurement, and
> significance testing are owned by **deterministic code**, so every credited improvement is
> trustworthy by construction."
> — [arXiv 2607.13683](https://arxiv.org/html/2607.13683v1)

Adopt this as the architecture of the curator, not just as a principle. A model may **diagnose** and
**propose** — "this failed because the palette rule was missing." Only deterministic code
**measures** and **credits**. That makes the credit path unhackable by construction rather than by
policy, and it is the same instinct as never showing the agent its score, stated more generally.

Practical consequence: promotion thresholds, similarity checks, and signal aggregation are all plain
Python. No model call decides whether something earned promotion.

### What the score is allowed to do

| Allowed | Forbidden |
|---|---|
| rerank `find_skill` results | delete anything |
| move `draft → candidate → active` (existing lifecycle) | remove a tool from an agent |
| decay `confidence` on unreinforced memory | change the model's instructions invisibly |
| surface "this agent gets rejected a lot" to the **user** | enter the agent's context |
| propose an archive for curator review | archive without review |

**Never punish by removing capability.** Archive-never-delete is already the invariant
(`03-skill-lifecycle.md`); it extends here. One bad week must not cost an agent a tool — that is the
"overreacting to one weird week" failure.

### This mostly already exists

Worth naming, because the ask assumed it was missing:

- **Promotion by demonstrated reuse** — `draft → candidate → active` at ≥3 successful uses
  (`03-skill-lifecycle.md`). That *is* a reward mechanism.
- **Confidence decay without reinforcement** and `evidence_count` on `style_memory`
  (`04-memory.md`). That *is* punishment, done safely.
- **Rejections as labelled negatives** — already recorded as "a regeneration is a labelled negative
  and is free to collect."

What was genuinely missing: a **unified signal type**, the **correction→rule** path, and the
**never-show-the-agent-its-score** rule. Those are new.

---

## Cold start — the real answer to "day one feels empty"

Points do not solve this. Points are *earned later*; they are worth nothing on day one, which is
exactly when the agent feels useless. Four mechanisms do solve it:

**1. Ship curated skills, not an empty agent.** SkillsBench: curated skills help, self-generated
gave no benefit. A new agent starts with a curated pack for its role.

**2. Import a plugin.** Agent Plugins 1.0.0 means a new agent can start with somebody else's
packaged skills + tools (`17-standards-and-interop.md`). Day one is not blank if day one includes an
install.

**3. Extract brand facts once, at onboarding.** A short intake — palette, banned looks, aspect
ratios, tone — writes long-term semantic memory before the first turn. Cheap, high-leverage, and it
is the difference between "who are you" and "I know your house style."

**4. New agents inherit the crew's long-term memory.** This is the best one and it falls out of the
architecture for free: a newly created colourist joins a production house that **already** knows the
brand. Short-term is private, mid-term is opt-in, long-term semantic is shared
(`14-multi-agent.md`). The tenth agent a user creates should be useful immediately because it
inherits nine agents' worth of accumulated house knowledge.

That last point reframes the pitch: **it is not that one agent gets better, it is that the house
gets better and every new agent starts there.**

---

## How we would know it worked

Per `12-evaluation.md` this is all **tier 2** — trends, never a build gate.

| Metric | Why |
|---|---|
| Retrieval precision on `find_skill` before vs after reranking | EAR reports up to **17.9%** retrieval improvement from feedback-driven recall ([arXiv 2607.17879](https://arxiv.org/html/2607.17879v1)) — a real target |
| `regenerated_away` rate per agent over time | should fall; it is the honest satisfaction proxy |
| Repeated-correction rate | should approach zero — the 2606.13174 failure mode |
| Time-to-useful for a newly created agent | the cold-start metric that actually matters |

---

## Resolved since first draft

**The P10 tension is resolved.** I flagged that a compiled correction must be *enforced*, but a rule
changing per turn would break P10 (byte-identical static prefix). The resolution is that rules
**accumulate rather than churn** — the pattern in
[arXiv 2607.13091](https://arxiv.org/abs/2607.13091), where "every accepted review comment is codified
as a persistent behavioral rule, progressively expanding the set of error classes the agent can
self-detect."

An append-only rule set is stable *within* a turn, which is all P10 requires — it asserts identical
bytes across iterations of one turn, not across turns. Adding a rule invalidates the cache once, at
the moment it is added, then amortises again over subsequent turns. That is an acceptable cost, and it
means compiled rules belong in the **static instruction prefix**, enforced on every iteration.

The framing to borrow: *"Compiled Memory: Not More Information, but More Precise Instructions for
Language Agents"* ([arXiv 2603.15666](https://arxiv.org/pdf/2603.15666v1)). And per
[arXiv 2604.11088](https://arxiv.org/abs/2604.11088), compiled rules should be phrased as
**prohibitions** — "never do X" — rather than prescriptions, because guardrails outperform guidance.

**"No prior art" was wrong.** An earlier draft of this doc claimed none existed for feedback-driven
improvement without weight updates. Corrections:

- [Self-Evolving Agent Memory With No Weight Updates via Population Broadcast, arXiv 2605.16233](https://arxiv.org/html/2605.16233) — our exact constraint, named
- [A Survey of Self-Evolving Agents, arXiv 2507.21046](https://arxiv.org/abs/2507.21046) — *what / when / how / where to evolve*
- [Evolving Cognition and Elastic Memory Orchestration, arXiv 2603.09716](https://arxiv.org/abs/2603.09716v1) — closed-loop updates "without external retraining"
- [Agent Skill Evaluation and Evolution, arXiv 2606.11435](https://arxiv.org/abs/2606.11435) — of its four evolution paradigms, RL is the one unavailable to us

## Open, and genuinely unresolved

- **Multi-user attribution.** Two users of one production house disagree about house style. Whose
  correction wins? This is not an engineering question, it is **mechanism design and institutional
  modelling** — and that is a formalised field. Dignum & Dignum argue precisely that agentic AI should
  borrow AAMAS's institutional and mechanism-design tools rather than improvise
  ([arXiv 2511.17332](https://arxiv.org/abs/2511.17332)). Read before designing. Related: our
  `AgentSpec` currently has no notion of what a role *obliges*, only what it *can* do.
- **Whether user-visible points cause bad user behaviour.** Gamifying "train your agent" could produce
  rating spam, which poisons the explicit signal. Untested, and the reward-hacking literature is about
  models, not users — so it does not transfer.
- **Where the rule-enforcement point physically lives.** Resolved in principle (static prefix,
  accumulated); still undecided whether that is a field on `AgentSpec`, a `guard/` policy, or a
  generated instruction block — and the taint rules from `06-security.md` apply, since a rule compiled
  from a poisoned turn is a durable injection.
- **Whether a signal ledger measurably improves anything for us.** EAR reports up to 17.9% retrieval
  improvement from feedback ([arXiv 2607.17879](https://arxiv.org/html/2607.17879v1)); nobody has
  measured it on media skills, and we should not assume the number transfers.
