# 13 — Self-Improvement

The headline capability: the agent plans a task, decomposes it, learns from the result, writes
a skill when the result is worth keeping, and gets better over time.

This doc specifies the loop. It deliberately does **not** re-specify the skill lifecycle
(`03-skill-lifecycle.md`) or the memory store (`04-memory.md`) — it wires them together.

---

## The one thing that decides whether this works

**SkillsBench measured self-generated skills at no benefit** across 7,308 trajectories, while
curated skills helped ([arXiv 2602.12670](https://arxiv.org/abs/2602.12670)). So the loop below
is not "generate skills." It is **generate, then make them earn their place.** Remove the
earning half and the published expectation is zero improvement.

That is also the differentiator. Bolting auto-generation onto an agent is a weekend. Shipping
the curation that makes it net-positive is the hard part — and it is why Hermes' own curator is
87 KB of which most is `_classify_removed_skills`, `_reconcile_classification`,
`_render_report_markdown`. **The hard part of self-improvement is auditing what the machine
decided, not getting it to decide.**

---

## Where this sits in the published taxonomy

Added 2026-08-30. The field has a taxonomy and we should use its vocabulary rather than our invented
stage names. Skill evolution splits into **four paradigms: execution feedback, trajectory
distillation, compression, and reinforcement learning**
([Agent Skill Evaluation and Evolution, arXiv 2606.11435](https://arxiv.org/abs/2606.11435) —
a survey, with [code](https://github.com/Cassie07/AgentSkill_Survey)).

| Paradigm | Us |
|---|---|
| **Execution feedback** | ✅ the signal ledger — `19-feedback-and-improvement.md` |
| **Trajectory distillation** | ✅ the DISTIL step below |
| **Compression** | ⚠️ this is skill consolidation, and we ship it **off** (`CONSOLIDATE=False`) |
| **Reinforcement learning** | ❌ **unavailable to us** — hosted models over OpenRouter, no weight updates |

Three of four, and the missing one is missing structurally rather than by choice. Worth stating
plainly: **anything in this literature that requires gradient updates does not apply to us**, which
rules out a large fraction of published self-improvement methods and is the reason our mechanism has
to work through retrieval instead.

The best structural pattern found, and one to adopt directly: **separate proposing changes from
crediting them** — "a language model diagnoses failures and proposes patches, while all sampling,
measurement, and significance testing are owned by **deterministic code**, so every credited
improvement is trustworthy by construction"
([arXiv 2607.13683](https://arxiv.org/html/2607.13683v1)). That is exactly the right split for the
curator, and it is the same instinct as never showing the agent its own score.

For a broader spine, [A Survey of Self-Evolving Agents (arXiv 2507.21046)](https://arxiv.org/abs/2507.21046)
organises the field as *what / when / how / where to evolve*.

## The loop

```
   ┌──────────────────────────────────────────────────────────────┐
   │                                                              │
   ▼                                                              │
① PLAN ──► ② EXECUTE ──► ③ DISTIL ──► ④ CURATE ──► ⑤ PROMOTE ────┘
   decompose    run the      candidate    idle-time    demonstrated
   the task     turn         skill?       review       reuse
   │            │            │            │            │
   │            │            │            │            └─ long-term
   │            │            │            └─ auxiliary model, never in a turn
   │            │            └─ gated (below) — most turns produce nothing
   │            └─ short-term memory
   └─ short-term memory
```

### ① PLAN — decompose the task into steps

New capability, and the one piece not already designed. Maps to the OTel GenAI **`plan`**
operation span (`plan {gen_ai.agent.name}`, kind INTERNAL), so it is observable without
inventing telemetry.

Rules, because planning is where token cost balloons:

- **Plan once per turn, not per iteration.** The plan is part of the stable prefix
  (`02-architecture.md`), so it is written once and cached, not regenerated each loop.
- **Plan only when the task warrants it.** A single-tool request does not need a plan; emitting
  one costs a model call and adds context for no gain. Gate on: multi-step intent, or a skill
  whose body declares steps.
- **The plan is a hint, not a contract.** The model may deviate. A rigid plan that the executor
  must follow turns a flexible agent into a brittle workflow engine — and we explicitly rejected
  graph orchestration in `09-design-constraints.md` C1.
- **Plans are cheap to store and valuable later.** A plan that produced a good outcome is the
  raw material for a skill (③). Store it in mid-term memory with the outcome attached.

### ② EXECUTE — the turn loop

Unchanged. See `02-architecture.md`. Every tool result, cost, and artifact lands in short-term
memory; the durable ledger checkpoints after each one.

### ③ DISTIL — should this become a skill?

Runs at turn end, cheap, no model call for the rejection path. The gate from
`03-skill-lifecycle.md`, restated because it is the load-bearing part:

1. The turn **succeeded** — artifact produced, not refunded.
2. It was **non-trivial** — multi-step, or a real recipe.
3. It is **not a near-duplicate** — embedding similarity below threshold. The single most
   important anti-drift check.
4. It is **parameterisable** — generalises past the literal prompt.

Fail any one and record the rejection with its reason. **Most turns should produce no skill.**
A system that writes a skill per turn is the failure mode, not the feature. The rejection log
is the tuning signal for the gate itself.

Pass all four → write as `draft`. Drafts are **invisible to `find_skill`**.

#### Security requirements on this step — non-optional

The distil step is a **privilege laundering path** and was under-specified in the first draft of this
doc. A one-time injection that reaches a turn can be written into a `draft`, promoted on reuse, and
thereafter carries our own provenance. The literature is explicit that per-session filtering does not
cover this: "memory evolution can convert one-time indirect injection into persistent compromise"
([arXiv 2602.15654](https://arxiv.org/abs/2602.15654)).

| # | Requirement |
|---|---|
| 1 | **Scan agent-generated skills with the same scanner as imported ones.** `agent_created` provenance grants no trust. |
| 2 | **Track lineage.** A skill distilled from a turn that consumed untrusted tool output or a foreign skill body inherits that taint. **Taint blocks promotion** — a tainted draft can exist but can never reach `candidate`. |
| 3 | **A distilled skill can never widen authority.** P3 holds across the memory boundary: the allowlist is the only source of tool grants. No remembered fact, retrieved style memory, or generated skill may justify a spend. |

See `06-security.md` for the threat detail. Consequence for the roadmap: `guard/` is a **hard
prerequisite** for `curator/`, not merely earlier.

### ④ CURATE — the idle-time review

Adopt Hermes' five invariants wholesale; they are scar tissue, not preference:

- Only touches **agent-created** skills.
- **Never auto-deletes — only archives, recoverably.**
- **Pinned skills bypass every auto-transition.** The user's override the machine cannot argue with.
- Runs on the **auxiliary model**, never the main session's prompt cache.
- **Inactivity-triggered**, never mid-turn.

Defaults: `INTERVAL_HOURS=168`, `MIN_IDLE_HOURS=2`, `STALE_AFTER_DAYS=30`,
`ARCHIVE_AFTER_DAYS=90`, **`CONSOLIDATE=False`** — even the authors do not trust automatic skill
merging on by default.

The curator also **proposes rather than mutates** for anything public: a patched version goes to
the existing moderation queue, because a stranger-visible catalogue entry must not change without
a human (`06-security.md`).

### ⑤ PROMOTE — earning the retrieval set

`draft → candidate` on first reuse. `candidate → active` at ≥3 successful uses above a success
threshold. **Only `candidate` and `active` are visible to `find_skill`.**

That single rule is what bounds library drift: the retrieval set grows with *demonstrated
value*, not with volume. At 4,000 auto-generated near-duplicates, `MAX_FIND_RESULTS=5` returns
noise and every turn silently degrades.

Improvement writes a **new version**, never mutates the old one — `version`,
`parent_version`, immutable bodies, revert as a single pointer update. If improving a skill can
lose the version that worked, users disable the feature.

---

## Memory: short / mid / long

The requested three tiers are a **lifetime** axis. `04-memory.md` uses a **content-type** axis.
They are orthogonal, and both are useful — here is the mapping, so we have one vocabulary
instead of two:

| Requested | Existing tier | Lifetime | Where it lives | Written |
|---|---|---|---|---|
| **Short-term** | Working | one run | `groot_runs`, in-process | synchronously, it *is* the run |
| **Mid-term** | Episodic | per user, decaying | Postgres + pgvector | **async queue, never the request path** |
| **Long-term** | Semantic + Procedural | durable | Postgres + pgvector; Procedural **is** the skill library | curator only, on promotion |

Three consequences worth stating explicitly:

**Long-term is not one thing.** *Semantic* is facts about the user (palettes, brands, banned
looks, aspect ratios). *Procedural* is how to do things — and that is the skill library, not a
second store. Do not build two. That mistake was already made once here: three coexisting skill
systems, with `groot_skills` declared canonical.

**Writes are queued, always.** A dropped memory write degrades quality. A memory write that
fails a paid generation is unacceptable. Reads are synchronous and capped; writes are enqueued.

**Promotion is the only path upward.** Mid → long happens in the curator: cluster episodic rows
by embedding similarity, promote repeated patterns into semantic memory with an
`evidence_count`, decay confidence on anything unreinforced, **archive rather than delete**.
Identical discipline to the skill lifecycle, because it is the identical failure mode.

### The retrieval budget is not negotiable

| Tier | Cap |
|---|---|
| Long-term semantic / style | 8 items / 400 tok |
| Mid-term episodic | 3 items / 600 tok |
| Procedural (skill find) | 5 results / 150 tok |
| Procedural (skill body) | 1 / 2,000 tok |

~3,150 tokens per turn regardless of how much the agent has learned. **Self-improvement must not
make the agent more expensive per turn** — if learning inflates the prompt, the feature pays for
itself in the wrong currency. This is the number that keeps the loop honest.

### The domain tier that nobody else has

For a visual agent the highest-value long-term memory is aesthetic, derived from **behaviour not
statements** — which generations the user kept, downloaded, boarded, or regenerated away from. A
regeneration is a labelled negative and is free to collect. See `style_memory` in
`04-memory.md`.

---

## How we will know it worked

Per `12-evaluation.md`, all of this is **tier 2** — scored, tracked as a trend, and it must
never gate a build. The claim to prove:

- **SkillsBench**, three arms: our curated-auto pipeline vs curated vs self-generated. Beating
  the self-generated arm is the publishable result.
- **SkillLearnBench** scores skill quality, trajectory, and outcome separately — a direct
  evaluation of the curator rather than of skill use.

If we cannot beat the self-generated arm, we learn that before shipping rather than after.

---

## Naming

The Pokémon-power naming (`thunderbolt`, `voltage`) belongs to the **product surface** — UI copy,
docs voice, mascot, CLI aliases — and **not** to the library's public API. Reasoning and the
concrete split are in `08-naming.md`. The short version: a published package's method names are
the one thing you can never rename, and `agent.run()` tells a developer what it does while
`agent.thunderbolt()` does not.

The **evolution** metaphor is the exception and is actively recommended: it is a common English
word, staged progression is not protectable, and it already maps onto the lifecycle —
`draft` = just hatched, `candidate` = evolving, `active` = evolved, `archived` = dormant.
