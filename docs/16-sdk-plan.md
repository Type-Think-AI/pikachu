# 16 — Agent SDK: plan

Requested USP: **fast, scales, self-improving, easy multi-agent / custom agent creation, behaves
like a crew.** Researched against the field on 2026-08-30 before planning, per the rule in
`00-problem-statement.md`. The finding is uncomfortable and changes the plan.

---

## The uncomfortable part: four of the five are the most crowded claims in the space

### "Declaratively create custom agents" — CrewAI's core product

CrewAI defines an agent as **role, goal, backstory** in YAML (`agents.yaml`, `tasks.yaml`):

> "A CrewAI agent is not a tangle of system prompts that you tune by feel. It is three
> plain-language fields: a **role** (who this agent is), a **goal** (what it is trying to
> achieve), and a **backstory** (the context that shapes how it behaves)."
> — [spillwave.com CrewAI guide](https://spillwave.com/guides/crewai/part-02-agents-tasks-crew/)

That is the same shape as our `AgentSpec`. Shipped, documented, popular. **We would be
reimplementing their headline feature.**

### "Behaves like a crew" — CrewAI owns the word

"Crew" is CrewAI's product noun. Their crews run "sequential or hierarchical workflows" with
manager-worker delegation ([docs](https://docs.crewai.com/en/concepts/crews)). Using the term
positions us as a CrewAI clone.

### "Multiple agents" — Agno ships four orchestration modes, and it is already our dependency

Agno Teams: **coordinate** (leader delegates then synthesizes, the default), **collaborate** (all
members answer, leader synthesizes consensus), **route** (to one member), and a **task loop**
([docs](https://docs.agno.com/teams/delegation)). Also: *"Members can also be provided by callable
factories and resolved at run time"* — runtime member resolution already exists.

And their stated reason for teams is **the exact argument I made for crews**:

> "When the number of tools grows beyond what the model can handle… use a team of agents to spread
> the load."
> — [Agno multi-agent systems](https://docs-v1.agno.com/introduction/multi-agent-systems)

`api/pyproject.toml` already declares `agno>=2.5.17`. **If team modes are the requirement, we have
them installed today.**

### "No-code / easy agent creation" — a funded, crowded market

n8n, Zapier, Make, Langflow, Lindy, Taskade, Creatio AI Studio, OpenAI AgentKit, and
[inkeep/agents](https://github.com/inkeep/agents) ("Create AI Agents in a No-Code Visual Builder
or TypeScript SDK with full 2-way sync"). The no-code agent market is reported growing ~31% CAGR.

### "Fast" — Agno's own marketing axis

Agno benchmarks agent instantiation (3.2 µs, 5.2 KiB) and leads with it. Competing on speed means
competing on their chosen metric, which they measure and we do not. And per
`05-performance.md`, instantiation is noise against our 250–600 ms round trips.

**Conclusion: of the five requested USP elements, four are occupied and one is unwinnable as
stated.** Building the plan around them produces a worse CrewAI.

---

## What is actually still ours

Three things survive the audit. All three came out of the film/canvas framing, not the framework
framing.

### 1. The canvas as the coordination substrate

CrewAI coordinates via a **task graph plus delegation**. Agno coordinates via a **leader agent**.
Neither coordinates via a **shared immutable artifact graph**.

Our model (`15-extensibility.md`): agents never message each other, never hand off, have no
leader. They read and write artifacts on a canvas. Dependency is expressed by *reading an
artifact*, not by an edge someone wired. A revision is a new artifact with `parent` set.

That is architecturally different, not a reskin — and it is the right fit for film production,
where a real crew works off one board. **This is the claim to lead with.**

### 2. Self-improvement that is measurably net-positive

Everyone can bolt on auto-skill-generation. SkillsBench measured it across 7,308 trajectories:
**self-generated skills gave no benefit**; curated ones helped. So the differentiator is the
curation gate and lifecycle (`13-self-improvement.md`), and it is **provable** — SkillsBench's
three-arm design lets us beat the self-generated arm rather than assert we are better.

A framework that can show a benchmark number for its learning loop is rare. That is the second
claim.

### 3. End-user agent creation inside a product, not developer YAML

The distinction the market leaves open:

| | Who creates the agent | Where |
|---|---|---|
| CrewAI | a **developer** | YAML files in a repo |
| n8n / Zapier / Langflow | an **operator** | a separate visual product |
| **Pikachu** | the **end user of the app you built** | your product's own UI |

CrewAI's YAML is a developer artifact — it requires a checkout, an editor, a deploy. The no-code
builders are destination products, not SDKs you embed. "An SDK where *your users* create agents
inside *your* app" is narrower and genuinely less occupied. For a production house configuring its
own roster, that is the actual requirement.

---

## Plan

Ordered so each phase is independently useful and reversible.

### Phase 0 — Decide Agno vs Pydantic AI for teams *(blocking, do first)*

`agno>=2.5.17` is already installed and ships the four team modes. Pydantic AI is the chosen
framework everywhere else in these docs (C5: one framework only).

Two honest options:
- **Keep Pydantic AI and build only what the canvas model needs** — which is *not* team modes.
  Canvas coordination needs no leader and no delegation graph, so most of Agno's Teams surface is
  irrelevant to us.
- **Use Agno for multi-agent and Pydantic AI for the turn loop** — breaks C5, two frameworks, the
  thing we were consolidating away from.

Recommendation: **the first.** The canvas replaces orchestration, so we do not need team modes,
and C5 holds. But this must be an explicit decision, not a default.

### Phase 1 — `AgentSpec` + registry

Data-only agent definition (`14-multi-agent.md`), persisted, CRUD'd by the end user. Six fields.
Enforce `FIXED_ALLOWLIST ∩ toolsets` (P3) at construction. No routing yet — invoke by name.

**Done when:** a user creates "Script Writer" in the UI and invokes it by name.

### Phase 2 — Canvas artifact graph

The real backend work, and now on the critical path. Append-only artifact store: id, kind,
payload ref, provenance (prompt, model, cost, seed, **producing agent**), `parent`. Agents read by
id and write new nodes.

**Done when:** a storyboard agent produces frames by reading a script artifact it did not receive
as an argument.

### Phase 3 — Conservative routing

Trigger-match only, default-single, untriggered agents invocable by name only — KiroCrew's
`select_crew` rules verbatim (`14-multi-agent.md`).

**Done when:** an ambiguous request runs on one agent instead of fanning out.

### Phase 4 — Self-improvement loop

Plan → execute → distil → curate → promote (`13-self-improvement.md`), with the creation gate and
`draft/candidate/active` visibility.

**Done when:** SkillsBench's curated arm is beaten by our curated-auto arm, or we learn it is not.

### Phase 5 — Memory tiers

Short/mid/long per `13-self-improvement.md`, writes through an async queue, retrieval capped at
~3,150 tokens.

**Done when:** a designer and a marketer produce output consistent with the same brand facts.

---

## What not to claim

- **Not "fast."** Agno owns that axis and measures it. Our wins are cache-read pricing and a
  deleted threadpool hop — cost, not latency.
- **Not "crew."** CrewAI's word. Find another.
- **Not "no-code agent builder."** A crowded destination-product market we are not entering.
- **Not "declarative agents."** CrewAI shipped role/goal/backstory YAML already.

## What to claim

> **An SDK for products whose users build their own agent rosters, coordinating through a shared
> canvas rather than a task graph, with a learning loop that can show its benchmark.**

Longer, narrower, and survives someone who has read the CrewAI and Agno docs.

---

## Still unresolved

- Phase 0 is a real decision and nothing should be built before it is made.
- Whether the canvas coordination model actually holds for >3 agents is **unproven** — it is a
  design argument, not a measured result. Phase 2's done-condition is the first real test.
- `15-extensibility.md` had the user-tool price requirement removed on 2026-08-30 per direction;
  user tools are treated as free and unmetered until that is revisited.
