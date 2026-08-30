# 20 — Research Findings That Change the Design

arXiv sweep, 2026-08-30, targeted at this project's open problems rather than a general survey.
Six findings matter. Two correct earlier docs, one exposes a hole in `19-feedback-and-improvement.md`,
and one upgrades the canvas from a design argument to a measured pattern.

---

## 1. The canvas is the blackboard architecture — and it is measured

I described it as "architecturally different" and admitted it was "a design argument, not a measured
result." It is in fact a named classical pattern with published numbers.

> "The blackboard architecture substantially outperforms strong baselines, achieving **13%–57%
> relative improvements** in end-to-end success… Our findings establish the blackboard paradigm as a
> **scalable and generalizable** communication framework for multi-agent systems."
> — [LLM-Based Multi-Agent Blackboard System, arXiv 2510.01285](https://arxiv.org/pdf/2510.01285v2)

Corroborated on cost: a blackboard MAS is "competitive with the SOTA static and dynamic MASs… and at
the same time manage to spend **less tokens**"
([arXiv 2507.01701](https://arxiv.org/html/2507.01701v1)). Also being used as an institutional
architecture for scientific workspaces ([MACC, arXiv 2603.03780](https://arxiv.org/html/2603.03780v1)).

**Design impact:** `15-extensibility.md` should call it what it is — a blackboard — and cite the
measurement. Naming it correctly also buys us fifty years of prior art instead of pretending we
invented it.

### But the "zero coordination" framing was too strong

> "We find that **adding additional collaborators can lower performance when coordination structure
> is absent.**" Their remedy: "collaboration scaffolding that combines shared group memory with
> simulated human-in-the-loop (HITL) gates, where selected actions require approval."
> — [arXiv 2606.18413](https://arxiv.org/html/2606.18413)

So a shared artifact space is not sufficient on its own. More agents on an unstructured board makes
things *worse*. The scaffolding that helps is shared memory **plus approval gates** — and we already
have both primitives (shared long-term memory; `DeferredToolResults.approvals`). They need wiring
together deliberately rather than assumed.

### And the blackboard is an attack surface

"Revisiting the Blackboard for Multi-Agent Safety, Privacy, and Security"
([arXiv 2510.14312](https://arxiv.org/html/2510.14312v1)) identifies the vectors: **misalignment,
malicious agents, compromised communication, data poisoning.** A shared canvas that any agent can
write to is a shared surface any compromised agent can poison. `guard/` must cover canvas writes,
not just tool grants.

---

## 2. ★ A hole in the self-improvement design

This is the most important finding and it directly threatens F16/F17.

> "**Memory evolution can convert one-time indirect injection into persistent compromise**, which
> suggests that defenses focused only on per-session prompt filtering are **not sufficient** for
> self-evolving agents."
> — [Persistent Control of Self-Evolving LLM Agents via Self-Reinforcing Injections, arXiv 2602.15654](https://arxiv.org/abs/2602.15654)

And the systems framing:

> "Untrusted content can be written into persistent agent state and **re-enter later sessions as an
> instruction**; the remaining systems question is how to preserve useful memory recall while
> preventing such state from justifying sensitive actions."
> — [Lineage-Guided Enforcement for LLM Agent Memory, arXiv 2605.14421](https://arxiv.org/abs/2605.14421)

**Our gap, precisely:** the scanner runs on **imported** skills. It does **not** run on
**agent-generated** ones. So the loop in `13-self-improvement.md` — distil a successful turn into a
skill — is a laundering path: poison a single turn, the agent writes it into a `draft` skill, the
curator promotes it on reuse, and the injection is now durable and *trusted because we generated it*.

Three consequences, all new requirements:

1. **Scan agent-generated skills with the same scanner as imported ones.** Provenance
   `agent_created` must not confer trust.
2. **Track lineage on memory and skills.** A skill distilled from a turn that consumed untrusted tool
   output inherits that taint. Lineage-guided enforcement is the named approach.
3. **Never let memory or a distilled skill justify a sensitive action** — specifically a
   credit-spending tool grant. Authority comes from the allowlist, never from remembered content.

This makes `guard/` (Phase 2) a hard prerequisite for `curator/` (Phase 7), which the roadmap
already ordered correctly by luck rather than by reasoning.

---

## 3. Guardrails beat guidance — a large-scale result that changes how we write rules

> "These findings expose a hidden reliability risk in the rapidly growing ecosystem of
> **community-authored rules and skills**, and they yield a clear principle for safer agent
> configuration: **constrain what agents must not do, rather than prescribing what they should.**"
> — [A Large-Scale Study of Rules, Skills, and Persistent Configuration for Coding Agents, arXiv 2604.11088](https://arxiv.org/abs/2604.11088)
> (also titled *Do Agent Rules Shape or Distort? Guardrails Beat Guidance*)

Two impacts:

- **Independent confirmation of our untrusted-skill thesis.** A large-scale study names
  community-authored skills as a *reliability* risk, not only a security one. `06-security.md` gets
  stronger, and so does the position in `17-standards-and-interop.md`.
- **Skill and rule authoring guidance flips.** Prefer prohibitions to prescriptions. A house style
  guide should say "never use these colours" more than "always do this." That is a concrete
  authoring rule for both our builtin skills and the correction→rule compiler.

Related: personality/persona specification is "the dominant behavioral lever," above model choice and
operational rules ([arXiv 2605.08463](https://arxiv.org/html/2605.08463v2)) — which supports
per-agent personas being the real differentiator in a crew, not per-agent models.

---

## 4. The correction-vs-cache tension has published answers

I left this unresolved: a compiled correction must be *enforced*, but a rule that changes per turn
breaks P10 (byte-identical cacheable prefix).

> "Every accepted review comment is codified as a **persistent behavioral rule**, progressively
> expanding the set of error classes the agent can self-detect."
> — [Self-Improving AI Coding Agents Through Accumulated Behavioral Rules, arXiv 2607.13091](https://arxiv.org/abs/2607.13091)

And the framing that names the whole idea:

> **"Compiled Memory: Not More Information, but More Precise Instructions for Language Agents"**
> — [arXiv 2603.15666](https://arxiv.org/pdf/2603.15666v1)

**Resolution:** rules **accumulate** rather than churn. An accumulated rule set is append-only and
therefore *stable within a turn* — it belongs in the static instruction prefix and stays cacheable.
P10 requires byte-identical *within one turn*, not across turns; a new rule invalidates the cache
once, at the moment it is added, and then amortises again. That is an acceptable cost and the tension
dissolves.

---

## 5. "No prior art" was wrong

In `19-feedback-and-improvement.md` I wrote that I found no prior art for feedback-driven agent
reputation without weight updates. There is:

- **[Self-Evolving Agent Memory With No Weight Updates via Population Broadcast, arXiv 2605.16233](https://arxiv.org/html/2605.16233)** — our exact constraint, named.
- **[A Survey of Self-Evolving Agents, arXiv 2507.21046](https://arxiv.org/abs/2507.21046)** — the
  canonical survey, organised as *what / when / how / where to evolve*. That is a better spine for
  `13-self-improvement.md` than the one I invented.
- **[Evolving Cognition and Elastic Memory Orchestration, arXiv 2603.09716](https://arxiv.org/abs/2603.09716v1)** — closed-loop cognition update "without external retraining."
- **[Multi-agent RAG with Evolving Orchestration and Agent Prompts, arXiv 2604.00901](https://arxiv.org/abs/2604.00901)** — "Role-Aware Prompt Evolution refines agent behaviors via credit assignment."

### The pattern worth stealing outright

> "A self-evolving agent-harness framework that **separates proposing changes from crediting them**:
> a language model diagnoses failures and proposes patches, while all sampling, measurement, and
> significance testing are owned by **deterministic code**, so every credited improvement is
> trustworthy by construction."
> — [Self-Evolving Agent Harnesses via Gated Semantic Quality-Diversity, arXiv 2607.13683](https://arxiv.org/html/2607.13683v1)

**LLM proposes, deterministic code credits.** That is exactly the right split for our curator, and it
is a cleaner statement of the same instinct behind "the agent must never see its own score." Adopt
the phrasing and the architecture.

---

## 6. Production reality check — mostly validates our choices

> "Production agents are typically built using **simple, controllable** approaches: **68% execute at
> most 10 steps** before requiring human intervention, **70% rely on prompting off-the-shelf models
> instead of weight tuning**, and **74% depend primarily on human evaluation**."
> — [Measuring Agents in Production, arXiv 2512.04123](https://arxiv.org/html/2512.04123v1)

| Our choice | Verdict |
|---|---|
| No weight tuning (hosted models) | ✅ 70% of production agents agree |
| Simplicity constraint C1 | ✅ "simple, controllable approaches" is the norm, not a compromise |
| Human eval in the loop | ✅ 74% depend primarily on it — supports tier-2 judge scores never gating |
| **`max_iterations = 20`** | ⚠️ **68% cap at ≤10 steps.** Our 20 is above the production norm; worth revisiting as a default |

---

## Changes to make — ✅ APPLIED 2026-08-30

All rows below were applied to the target docs in the same commit series. This table is kept as the
audit trail of what moved and why, not as outstanding work.

| Doc | Change |
|---|---|
| `15-extensibility.md` | Name the canvas a **blackboard**, cite 13–57%; add the "coordination structure is required" caveat and the poisoning surface |
| `19-feedback-and-improvement.md` | Remove the "no prior art" claim; adopt LLM-proposes/code-credits; resolve the P10 tension via accumulation |
| `13-self-improvement.md` | **Add: scan agent-generated skills; track lineage; memory must never justify a tool grant** |
| `06-security.md` | Add memory poisoning → persistent compromise; blackboard attack vectors |
| `03-skill-lifecycle.md` | Authoring guidance: prohibitions over prescriptions |
| `18-module-map-and-roadmap.md` | State that `guard/` (Phase 2) is a **hard prerequisite** for `curator/` (Phase 7), not merely earlier |
| `09-design-constraints.md` | Revisit the iteration cap against the ≤10-step production norm |

## Read before implementing

- [arXiv 2507.21046](https://arxiv.org/abs/2507.21046) — self-evolving agents survey, as the spine for `curator/`
- [arXiv 2605.14421](https://arxiv.org/abs/2605.14421) — lineage-guided memory enforcement, for `guard/` + `memory/`
- [arXiv 2607.13683](https://arxiv.org/html/2607.13683v1) — propose/credit separation, for `curator/`
- [arXiv 2604.11088](https://arxiv.org/abs/2604.11088) — guardrails beat guidance, for skill authoring
- [arXiv 2510.01285](https://arxiv.org/pdf/2510.01285v2) — blackboard measurement, for `canvas/`
