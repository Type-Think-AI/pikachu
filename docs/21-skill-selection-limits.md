# 21 — Skill Selection Limits, and Why Partitions Are a Correctness Mechanism

Verified against arXiv abstracts 2026-08-30. Four papers checked directly; all four exist and are
correctly identified. Two were mischaracterised in the summary that surfaced them, and the most
consequential fact was not the headline.

---

## ★ The biggest finding: ClawHavoc

Buried in the SoK paper, and it is the most important sentence in this entire sweep:

> "…grounded by a case study of the **ClawHavoc campaign in which nearly 1,200 malicious skills
> infiltrated a major agent marketplace, exfiltrating API keys, cryptocurrency wallets, and browser
> credentials at scale.**"
> — [SoK: Agentic Skills — Beyond Tool Use in LLM Agents, arXiv 2602.20867](https://arxiv.org/abs/2602.20867)
> (Jiang, Li, Deng, Ma, Wang, Wang, Yu — 24 Feb 2026, **cs.CR**)

Our entire security posture — untrusted skills, the scanner, P3 confinement — has been argued from
first principles as a *hypothetical* threat model. It is not hypothetical. It happened, at a
marketplace, at a scale of ~1,200 skills, and the payload was exactly what we predicted: credential
exfiltration.

**Impact:** `06-security.md` stops being a defensive chapter justified by reasoning and becomes one
justified by an incident. `17-standards-and-interop.md`'s central claim — that the standards define
no trust or provenance verification and that this is the gap — now has a body count. This is the
strongest single piece of evidence for the USP that exists in our research.

The same paper names **"trust-tiered execution"** as an established pattern, which is what our
allowlist tiers are. We should use that name.

### And it corroborates the SkillsBench result independently

> "…anchored by recent benchmark evidence that **curated skills can substantially improve agent
> success rates while self-generated skills may degrade them.**"

That is a second independent source for the finding that gates F16 behind F17. Evidence strength on
"do not auto-generate skills without a curation gate" moves from **moderate → strong**.

---

## The phase transition — real, but hedged much harder than advertised

[When Single-Agent with Skills Replace Multi-Agent Systems and When They Fail, arXiv 2601.04748](https://arxiv.org/abs/2601.04748)
— Xiaoxiao Li, single author, 8 Jan 2026 (v2 14 Jan), **25 pages, self-described "technical report."**

What the abstract actually establishes:

> "A multi-agent system can be **compiled into an equivalent single-agent system**, trading
> inter-agent communication for skill selection… Rather than degrading gradually, **selection accuracy
> remains stable up to a critical library size, then drops sharply**, indicating a phase transition
> reminiscent of capacity limits in human cognition. Furthermore, we find evidence that **semantic
> confusability among similar skills, rather than library size alone**, plays a central role."

### Read the hedges

The paper says "our **preliminary** experiments suggest," "we **propose** that," "we find
**evidence** that," "our **initial** results… support this hypothesis." Single author, technical
report, not peer-reviewed.

**Claims in the summary I could NOT verify from the abstract:** that tests used 3–4 skills; that the
threshold is 10+; the Hick's Law attribution; and "stronger model for top-level routing, cheaper
model for execution." Those may well be in the 25-page body — but they are not established by
anything I have read, and they should not be quoted as findings until someone reads the PDF.

**Evidence strength: moderate at best.** The mechanism is plausible, the direction is almost
certainly right, and the specific numbers are unconfirmed.

### Why we act on it anyway

The design response is cheap and low-regret, and the failure mode it predicts is *silent* — selection
accuracy degrades without an error. A cheap guard against a silent failure is worth taking on
moderate evidence. That reasoning should be explicit rather than us pretending the evidence is
stronger than it is.

---

## What this changes: partitions become a correctness mechanism

This is the genuinely new design insight and it reframes `14-multi-agent.md`.

Our design already had per-agent **skill partitions**, justified organisationally — a colourist
should not see storyboard skills because that is how a production house works. Under this paper, the
partition is doing something else entirely: **it keeps each agent's selectable skill set below the
confusability threshold.**

> The reason to split one agent into several is not organisational tidiness. It is to stop skill
> selection from silently degrading.

Consequences:

1. **Multi-agent stays opt-in, but gains a principled trigger.** Instead of "use multi-agent if you
   feel like it," we can tell the user *when*: when one agent's skill library grows past the point
   where descriptions start overlapping. That is a measurable, explainable recommendation.
2. **The agent partition *is* the hierarchical routing layer** the paper recommends. Top level =
   which agent (a human picks, or the canvas implies it). Second level = `find_skill` within that
   agent's partition. We get hierarchy for free from a design we chose for other reasons.
3. **`find_skill` retrieval was already the right call.** We never put the full skill library in
   context — retrieval narrows first. That is the "metadata-driven progressive disclosure" pattern
   the SoK paper names as one of its seven. We are accidentally aligned; worth knowing it is
   deliberate now.

### New feature this implies: confusability warning at authoring time

The paper's sharpest practical claim is that **semantic confusability, not count**, drives the
collapse. Confusability is directly measurable — cosine distance between skill description
embeddings, which we already compute for `find_skill`.

So: when a user creates or imports a skill whose description is too close to an existing one in the
same partition, **warn at authoring time**. Cheap, deterministic, no LLM call, and it attacks the
actual cause rather than the proxy (count). This is a small feature with a real basis and it belongs
in `curator/`.

It also gives us a tier-2 metric worth tracking: **max pairwise description similarity per
partition**, as a leading indicator before selection accuracy drops.

---

## A correction: 2606.11435 is not a router

The summary described this as "SkillRouter / SkillOrchestra work… they route by comparing full skill
content (not just names/descriptions), and learn from past success/failure trajectories."

Actual paper: **"Agent Skill Evaluation and Evolution: Frameworks and Benchmarks"** — Ding, Zhou,
Jin, Tong, Zhou, Metaxas, 9 Jun 2026. It is a **survey**, not a routing method. It categorises skill
evolution into **four paradigms — execution feedback, trajectory distillation, compression,
reinforcement learning** — and analyses **six skill-centric benchmark categories**, identifying gaps
in coverage and metric richness. Code: [github.com/Cassie07/AgentSkill_Survey](https://github.com/Cassie07/AgentSkill_Survey).

Still useful — the four-paradigm split is a cleaner spine for `13-self-improvement.md` than what I
wrote, and note that **reinforcement learning is one of the four and is unavailable to us**, which
narrows our options to three. But the "routes by full skill content" claim is unverified and should
not be cited.

---

## The theory paper is worth taking seriously

[Agentifying Agentic AI, arXiv 2511.17332](https://arxiv.org/abs/2511.17332) — **Virginia Dignum and
Frank Dignum**, Nov 2025 (v2 Feb 2026), 10 pages, CC-BY. These are established AAMAS figures, not
newcomers, which is why this one carries weight disproportionate to its length.

> "The conceptual tools developed within the Autonomous Agents and Multi-Agent Systems (AAMAS)
> community, such as **BDI architectures, communication protocols, mechanism design, and
> institutional modelling**, provide precisely such a foundation… a path toward agentic systems that
> are not only capable and flexible, but also **transparent, cooperative, and accountable**."

Why it matters concretely: our "production house" with named roles is, in the AAMAS sense, an
**institution** with **roles** — and both have formal definitions with decades of theory behind them.
Right now our `AgentSpec` is a prompt template with six fields. Grounding "role" in institutional
modelling would give us a principled answer to questions we currently hand-wave:

- What does an agent's role *permit* and *oblige*, distinct from what it *can* do? (Our allowlist
  answers "can"; we have no notion of "ought.")
- **Whose correction wins when two users of one house disagree?** That is a mechanism-design and
  institutional question, and it is currently listed as unresolved in `19-feedback-and-improvement.md`.
  This literature is where the answer lives.

Not a blocker for Phase 1. The right reading order is: build the seam, then read Dignum before
designing multi-agent roles properly in Phase 6.

---

## Changes to make — ✅ APPLIED 2026-08-30

All rows below were applied to the target docs in the same commit series. Kept as the audit trail.

| Doc | Change |
|---|---|
| `06-security.md` | **ClawHavoc**: ~1,200 malicious skills, real marketplace, credential exfiltration. Threat model is now evidenced, not hypothesised. Adopt "trust-tiered execution" |
| `17-standards-and-interop.md` | The provenance gap has a real incident attached — strengthen the USP claim with it |
| `14-multi-agent.md` | **Partitions are a correctness mechanism, not just organisation.** Agent = top-level cluster in hierarchical routing. Add the "when to split" trigger |
| `03-skill-lifecycle.md` | Confusability warning at authoring time; cite the seven design patterns and the representation × scope taxonomy |
| `13-self-improvement.md` | Re-spine on the four evolution paradigms; note RL is unavailable to us; second source now supports the F17 gate |
| `12-evaluation.md` | New tier-2 metric: max pairwise description similarity per partition |
| `19-feedback-and-improvement.md` | Multi-user attribution is a mechanism-design question — point at Dignum |
| `09-design-constraints.md` | Constraint: a single agent's partition should stay small enough to avoid the confusability cliff |

## Read the PDFs before implementing

- [2602.20867](https://arxiv.org/abs/2602.20867) — **highest priority.** ClawHavoc detail, the seven
  design patterns, trust-tiered execution. This is our security chapter's evidence base.
- [2601.04748](https://arxiv.org/abs/2601.04748) — confirm the actual threshold and the hierarchical
  routing method before we quote numbers.
- [2606.11435](https://arxiv.org/abs/2606.11435) — the four paradigms, for `curator/`.
- [2511.17332](https://arxiv.org/abs/2511.17332) — before Phase 6 role design.
