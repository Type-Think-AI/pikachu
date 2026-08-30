# 00 — Problem Statement

## What is NOT the problem

Three premises worth killing before they shape the roadmap.

### "Skills-first is our differentiator"

It is table stakes as of August 2026:

- **Microsoft Agent Framework** shipped Agent Skills for Python with a four-stage
  progressive disclosure pattern (advertise names → load instructions → read resources
  → run scripts).
- **Google ADK** ships `SkillToolset` with progressive disclosure.
- **Pydantic AI** has first-party Agent Skills docs.
- **`pydantic-ai-skills`** already implements the full agentskills.io spec — remote
  registries, script execution, runtime reload.
- **`openskills-sdk`** exists on PyPI as a standalone skills layer.
- There is an academic survey: arXiv 2602.12430, *Agent Skills for LLMs: Architecture,
  Acquisition, Security, and the Path Forward*.

A pitch of "an agent where skills are first-class" gets zero adoption. Microsoft,
Google and Pydantic all shipped that.

### "Our problem is framework overhead"

It is not. Agno's headline numbers (529× faster than LangGraph, 24× lower memory) measure
**agent instantiation**. PicX creates one agent per HTTP request against remote
DigitalOcean Postgres where every round trip is 250–600 ms. Instantiation at 3.2 µs vs
1.7 ms is noise. Chasing that benchmark is vanity. The real cost is per-turn token waste
and latency — see `05-performance.md`.

### "We need to build skill auto-creation"

We need to build it *with a curator*, or not at all. See `03-skill-lifecycle.md`:
unbounded skill accumulation is a named, published failure mode.

---

## What IS the problem

Three problems PicX is already forced to solve that the field is not. They only matter
together, and together they define a category: **an agent runtime for systems where
tools cost money and skills come from strangers.**

### P1 — Tool calls that spend real money

Every mainstream framework treats a tool call as a free function call. PicX tool calls
spend credits: Seedream 5 Lite 15, Seedream 4.5 18, NB2 Lite 20, Nano Banana 2 35,
GPT Image 2 53, NB Pro 53.

`api/app/groot/picx_tools.py` is the single charging point — charge → generate →
refund-on-failure (invariant P5). No framework offers this as a primitive, so everyone
building a paid-tool product rebuilds it, usually without the refund path.

**What the runtime must own:** a metered-tool protocol where cost, reservation, capture
and refund are first-class and centrally enforced, so no tool can spend without
accounting.

### P2 — Durable resume that does not double-charge

Generic durable execution is at-least-once by design. LlamaIndex's own durable-workflow
documentation states it plainly: *"Resume is at-least-once, and step side effects need to
be safe to repeat."*

A ₹35-credit image generation is **not** safe to repeat. DBOS, Restate, Kitaru, Temporal
and Agentspan all give replay; none of them know that step 7 already spent the user's
money.

PicX built this (`groot_runs`, `run_store.py`, migration `9d23a1195799`): checkpoint
after every tool result, cancel and resume without re-charging completed media. That is
the hard, unclaimed part.

**What the runtime must own:** checkpointing keyed to *paid side effects*, so resume
replays reasoning but never re-charges a captured spend.

### P3 — Skills written by strangers

Every shipping skills implementation assumes you authored the skill. PicX runs a public
catalog (`groot_skills`, `groot_user_skills`, 16 endpoints) where community `SKILL.md`
can be pulled mid-run by `find_skill` / `load_skill`.

The Lane I safety audit found two real escalation paths in our own system:

1. A community `SKILL.md` could self-grant credit-spending tools through its own
   `allowed_tools` frontmatter, because `produces_media` was derived from the skill's own
   declaration.
2. The terminal-toolset strip was normalization-dependent — `" TERMINAL "` survived the
   literal-string match and only died later at resolve.

Both are fixed; the scanner still misses paraphrased prompt injection, which is why
auto-approve on a clean scan remains unsafe and a human reviewer is required.

**What the runtime must own:** server-side intersection of a fixed allowlist with declared
toolsets, enforced by property tests, so a skill document can never widen its own
authority.

---

## Why the three compound

Any one alone is a library. Together they are a runtime:

- P1 without P2 → you charge, the process dies, the user paid for nothing.
- P2 without P1 → you replay safely but cannot tell which steps cost money.
- P1 + P2 without P3 → works until you let anyone publish a skill, then a stranger
  drains wallets.

PicX has to solve all three because it is a media marketplace. That is the moat, and it
generalises to any paid-tool domain.

---

## The market's pain list, audited honestly

The widely-cited developer pain points for agent frameworks, scored against what Pikachu
actually differentiates on. Claiming all twelve would be a pitch nobody believes; four is a
position you can defend.

| Pain | What everyone means | What it becomes when tool calls cost money | Ours? |
|---|---|---|---|
| **State management** | consistency across long workflows | state that must not **re-charge** on resume | ✅ **yes** (P9) |
| **Retries & failure handling** | timeouts, partial execution, retry loops | a wrong retry **double-charges a customer**; "outcome unknown" ≠ failed | ✅ **yes** |
| **Infinite / expensive loops** | burns tokens | burns **dollars**; `cost_limit` stops at one `run()` | ✅ **yes** |
| **Tool permissions** | what can *my* agent do | what can a **stranger's skill** do | ✅ **yes** (P3) |
| **Observability** | trace tokens, latency, cost | **cost per artifact, with provenance** | 🟡 partly |
| **Human-in-the-loop** | pause for approval | approve *before spending 53 credits* — approval gains a **reason** | 🟡 partly |
| **Testing / evals** | non-deterministic behaviour | invariants that are assertions, not scores | 🟡 partly |
| **Debugging** | why did it take that path | unchanged | ❌ inherited |
| **Framework complexity** | large abstraction stack | unchanged | ❌ commodity |
| **Breaking changes** | APIs churn, forced migrations | unchanged | ❌ inherited |
| **Deployment** | demo → reliable production | unchanged | ❌ **we don't solve this** |
| **Framework choice** | too many frameworks | unchanged | ❌ **we make it worse** |

### The four we own, and why

**State that must not re-charge.** Every durable engine gives exactly-once *orchestration* and
at-least-once *side effects* — they say so themselves. P9 (resume never re-captures a captured
reservation) is the invariant none of them can offer, because none of them know which step cost
money.

**Retries measured in currency.** `ToolReturnPart.outcome == 'interrupted'` is the case the
whole category fumbles: not failed, not succeeded. Auto-refund loses money on work that
succeeded; auto-retry charges twice. Correctness here is a billing property, not a reliability
one — and a 429 without `retry-after` is a spend cap, not a transient.

**Budget that outlives a run.** `UsageLimits.cost_limit` exists and Pydantic AI's own docs say
it "does not cover money, a period longer than one run, a per-tenant share of a shared
allowance, or a counter that several worker processes agree on." That sentence is the feature
spec for our Meter.

**Permissions against a hostile author.** Everyone else's tool-permission story assumes you
wrote the tool. Ours assumes a stranger did, and confines it by server-side intersection of a
fixed allowlist with what the skill declares.

### The three we partly own

Observability and evals we mostly *inherit* — Pydantic AI plus Logfire plus `pydantic-evals` is
already strong. What is ours is narrower and real: **artifact provenance** (prompt, model, cost,
seed, parent) and the `CostReserved` / `ArtifactProduced` events, which no general tracer emits
because no general tracer has a spend to show. On HITL, `DeferredToolResults.approvals` is
theirs; giving the approval a **price** is ours.

### The five we should stop claiming

Debugging, framework complexity, and breaking changes are **inherited, not built** — the V1
no-breaking-changes-until-V2 pledge is Pydantic AI's moat and we are a beneficiary, not the
author. Being small is a virtue but Agno and smolagents are small too; it is table stakes, not a
position.

Deployment we genuinely do not solve. We are a library; getting a service to production is the
consumer's problem.

And framework choice we actively **worsen** — we are one more framework on the pile. The only
honest mitigation is a sharp enough scope that the choice is obvious: if your tool calls are
free, do not use this.

### The USP, in one line

> **Every problem on that list gets worse when the tool call costs money, and Pikachu is the
> runtime whose correctness is measured in currency.**

### On speed

Speed is **not** the USP and claiming it invites an easy refutation. Instantiation benchmarks
(Agno's 3.2 µs) are noise against our 250–600 ms round trips to remote Postgres — see
`05-performance.md`. The real gains are a deleted threadpool hop and ~84% off the prefix via
cache-read pricing. Both are **cost** wins that read as speed; neither makes us the fastest
framework, and we should not say we are.

---

## Claims — now resolved

Both load-bearing claims have been checked against primary sources, and one needed
sharpening.

### "No mainstream framework offers a metered-tool primitive" — holds, but state it correctly

The *problem* is well documented. Temporal's own docs use our exact example: activities are
at-least-once, and "without idempotence, this could cause **duplicate charges in payment
processing**." DBOS states that steps are at-least-once while only transactions are
exactly-once. Restate ships `ctx.uuid()` for minting stable downstream keys plus request-level
`idempotency-key` dedup. Stripe defines the receiver contract, 24-hour dedup window included.

So nobody is unaware. What none of them provide is the **accounting**: a credit ledger,
reserve/capture/refund states, a cross-run and cross-worker budget, and reconciliation of the
unknown-outcome case. Every engine hands you the pattern and leaves it as homework — the same
thing Pydantic AI's spend doc says about money.

**Corrected claim:** not "we discovered double-charging," but "we ship the prescribed pattern
as a runtime primitive with the ledger attached." Weaker on novelty, far stronger as a product
claim, and defensible against anyone who reads the Temporal docs.

### "Untrusted third-party skill execution is unsolved" — holds, and now has a benchmark

No shipping skills implementation assumes a hostile author; all of Microsoft's, Google's and
Pydantic's docs are about *your* skills. Our own audit found two real escalation paths, and the
scanner still misses paraphrased injection.

Adjacent evidence that strengthens the wider skills case:
[SkillsBench](https://arxiv.org/abs/2602.12670) measured 7,308 trajectories across three
conditions and found **self-generated skills offered no benefit** while curated skills helped
inconsistently. Curation is the differentiator, not generation — see `03-skill-lifecycle.md`.
