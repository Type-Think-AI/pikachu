# Pikachu — PRD

**Status:** design complete, no code. **Codename** internal only (`docs/08-naming.md`).
**Last updated:** 2026-08-30.

**Repo assumption:** Pikachu lives inside the parent `picx-studio` repo at `pikachu/` for now.
Extraction to its own repo is a later phase, not a precondition — the module boundaries and the
"no PicX types outside `tools/`" rule are what make extraction cheap when we want it
(`docs/10-module-api.md`). *If the intent was a separate repo from day one, say so and this
changes.*

---

## 1. What we are building

A Python **agent SDK** that runs the open agent-interoperability standards and supplies the
permission layer those standards deliberately omit.

> **Position.** Agent Plugins 1.0.0 — maintained by Amazon, Cursor, Microsoft, OpenAI, Vercel and
> Google — states it defines "no permission model, no sandboxing requirements, no trust or
> provenance verification." Pikachu implements the standards **and** fills that list.

Not a chatbot framework. Not a no-code builder. Not an orchestration graph. A library you embed in
a product whose **users** build their own agent rosters.

## 2. Who it is for

| # | Persona | Job | Primary? |
|---|---|---|---|
| P1 | **Product builder** | embeds the SDK in their app so their users get agents | ✅ primary |
| P2 | **End user of that app** | a production house configuring "Script Writer", "Storyboard" | ✅ primary |
| P3 | **Skill / plugin author** | packages instructions + tools to distribute | secondary |
| P4 | **OSS adopter** | wants metered/confined agents without our stack | secondary |

P2 is the one the market underserves: CrewAI's agents are developer YAML, and no-code builders are
destination products rather than embeddable SDKs (`docs/16-sdk-plan.md`).

## 3. Reference use case

A film production house on a canvas. Roles are theirs to define — planner, script writer,
storyboard, shot designer, colourist. Agents coordinate by **reading and writing artifacts on the
canvas**, never by messaging each other (`docs/15-extensibility.md`).

## 4. Features

Priority: **A** = required for first usable release · **B** = needed for the full pitch ·
**C** = later.

### Core runtime

| ID | Feature | Pri | Notes |
|---|---|---|---|
| F1 | Turn runtime on Pydantic AI | **A** | `PydanticAIBackend` behind the existing one-method seam |
| F2 | **Guard — permission & confinement layer** | **A** | ★ the USP. `FIXED_ALLOWLIST ∩ declared` (P3), scanner, `removed_tools` |
| F3 | Event stream output | **A** | extends Pydantic AI's events; `ArtifactProduced` is ours |
| F4 | Cacheable prompt assembly | **A** | static instructions, byte-identical prefix (P10) |

### Standards conformance

| ID | Feature | Pri | Notes |
|---|---|---|---|
| F5 | Agent Skills (agentskills.io) | **A** | read + write, progressive disclosure honoured |
| F6 | Agent Plugins 1.0.0 | **A** | fixed locations, explicit transports, **independent component failure** |
| F7 | MCP 2026-07-28 client | **A** | stateless, `server/discover`, `resultType`, MRTR |
| F8 | OAuth 2.1 as MCP client | **A** | 401 → RFC 9728 metadata → PKCE + RFC 8707 resource indicator; CIMD not DCR |
| F9 | Discovery — ARD + AI Catalog | **B** | "what is available for this task?" |
| F10 | A2A — signed Agent Cards | **C** | **cross-boundary only**, never internal coordination |
| F11 | WebMCP bridge | **C** | we already have a conformance harness |
| F12 | MCP server mode | **C** | client first; being a server is a separate surface |

### Agents and crews

| ID | Feature | Pri | Notes |
|---|---|---|---|
| F13 | `AgentSpec` + registry | **A** | six declarative fields, user-created at runtime, no code |
| F14 | Conservative routing | **B** | trigger-match only, default-single, untriggered = by-name only |
| F15 | Canvas artifact graph | **A** | append-only; provenance incl. **producing agent** |

### Learning and memory

| ID | Feature | Pri | Notes |
|---|---|---|---|
| F16 | Self-improvement loop | **B** | plan → execute → distil → curate → promote |
| F17 | Creation gate | **B** | ships **with** F16 or not at all — see §7 |
| F18 | Memory tiers short/mid/long | **B** | writes queued, retrieval capped ~3,150 tok |

### Operations

| ID | Feature | Pri | Notes |
|---|---|---|---|
| F19 | Durable runs | **B** | checkpoints keyed to paid effects; resume never re-captures (P9) |
| F20 | Telemetry — OTel GenAI | **B** | conventions moved repos; all `gen_ai.*` still Development |
| F21 | Eval harness | **B** | `pydantic-evals`; tier-1 invariants gate CI, judge scores never do |
| F22 | Metered tools | **C** | deprioritised by direction; kept as a Protocol so nothing depends on credits |
| F23 | Plugin distribution / install | **C** | Agent Plugins defines none — **this is a supply-chain surface, not a download** |

## 5. Non-goals

Explicitly out, to stop scope creep:

- **Orchestration graphs** — no supervisor, router, handoff protocol, or shared mutable blackboard.
  The canvas replaces them.
- **A no-code destination product.** We are an embeddable SDK; n8n/Zapier/Langflow are not our lane.
- **Being the fastest framework.** Agno owns that axis and measures it; our wins are cache-read
  pricing and a deleted threadpool hop (`docs/05-performance.md`).
- **Multi-framework support.** Pydantic AI only (C5). One backend plus a test double.
- **A2A for internal coordination.** Cross-boundary only.
- **Deciding the public name.** Deferred to the brand track.

## 6. Success criteria

Measurable, not aspirational. Each is a pass/fail.

| # | Criterion |
|---|---|
| S1 | `RunUsage.cache_hit_ratio` > 0 on the default model — today it is 0 because the prefix is below Gemini 3.x's floor |
| S2 | A hostile skill, a hostile plugin **and** a hostile MCP server are refused by the *same* code path, proven by property test |
| S3 | A third-party plugin loads with one deliberately broken `mcp.json` entry skipped and reported, skills still working |
| S4 | An end user creates an agent in the UI and invokes it, with no code change and no deploy |
| S5 | A storyboard agent produces frames from a script artifact it was never passed as an argument |
| S6 | Our curated-auto arm beats SkillsBench's self-generated arm — or we record that it does not |
| S7 | `pip install` works in a clean venv; `grep -r "picx" --include=*.py` hits only `tools/picx_media/` |

## 7. Constraints that shape the build

- **Simplicity is a hard constraint.** Five concepts — agent, skill, tool, run, memory. No DSL. "If
  explaining a feature needs a diagram, it is too complex for v1" (`docs/09-design-constraints.md`).
- **F17 gates F16.** Two independent sources: SkillsBench measured self-generated skills at **no
  benefit** across 7,308 trajectories while curated ones helped, and a Feb-2026 SoK reports curated
  skills improve success rates "while self-generated skills **may degrade them**"
  ([arXiv 2602.20867](https://arxiv.org/abs/2602.20867)). Evidence is **strong**, not indicative —
  shipping creation without curation is shipping against the published consensus.
- **F16 cannot ship before `guard/` covers agent-authored artifacts.** The distil step is a privilege
  laundering path: memory evolution "can convert one-time indirect injection into persistent
  compromise" and per-session filtering is explicitly insufficient
  ([arXiv 2602.15654](https://arxiv.org/abs/2602.15654)). Agent-generated skills must go through the
  same scanner as imported ones, carry lineage/taint, and never widen a tool grant.
- **`guard/` is built early, not last.** Retrofitting a permission model onto five modules that
  already grant capability is how this goes wrong — and a six-vendor TSC deferring it is evidence
  that bolting it on later is hard.
- **Verification before design.** This session produced repeated corrections from guessed API names
  (`history_processors`→`ProcessHistory`, `Usage`→`RunUsage`,
  `request_tokens_limit`→`input_tokens_limit`). Four new specs, four new chances to repeat it.

## 8. Dependencies and risks

| Risk | Impact | Mitigation |
|---|---|---|
| `MCPToolset` may not speak 2026-07-28 | blocks F7/F8 | **Phase 0 verification, before any code** |
| Agent Plugins schema unread (only the announcement) | F6 field-level conformance wrong | read `plugin.schema.json` in Phase 0 |
| Per-agent model choice silently kills caching | S1 fails invisibly | constrain the picker to 1,024-floor models, or surface it |
| Five specs version independently | breakage on any rev | compatibility matrix + a rev policy — **not yet written** |
| Third-party plugins have no provenance standard | trust decision unmade | F23 must not ship before it is made — and ClawHavoc (~1,200 malicious marketplace skills) is what the gap costs |
| Canvas degrades with more agents if coordination structure is absent | F15 succeeds at 2 agents, silently worsens at 4 | the blackboard pattern itself is measured (13–57% gain, [arXiv 2510.01285](https://arxiv.org/pdf/2510.01285v2)) but scaffolding is required: wire shared memory + approval gates in Phase 5 |
| Skill-selection accuracy collapses on semantic confusability | F5/F13 degrade **silently** — wrong skill, no error | track max pairwise description similarity per partition; warn at authoring time (C7) |
| `max_iterations = 20` exceeds the production norm | unnecessary cost and drift per turn | 68% of production agents cap at ≤10 ([arXiv 2512.04123](https://arxiv.org/html/2512.04123v1)); defend the cap with the observed distribution |

## 9. Open decisions

Blocking, and none are mine to make:

1. **Repo shape** — stay in `picx-studio/pikachu/`, or extract now? (assumed: stay)
2. **Default model** — must move off Gemini 3.x or S1 is unreachable.
3. **Public name** — now more urgent: publishing conformance claims means appearing in other
   people's compatibility tables under some name.
4. **Trust policy for third-party plugins** — gates F23.

## 10. What is deliberately not in this repo yet

**No module directories have been created.** The map in `docs/18-module-map-and-roadmap.md` is a
target. Empty scaffolding that nothing imports is dead weight and makes the tree harder to reason
about — directories get created by the phase that puts real code in them.
