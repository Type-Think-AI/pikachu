# 15 — Extensibility: what a user creates

The frame: a production house making a film on a canvas. Roles like planner, script writer,
storyboard artist, shot designer, colourist. **Those are examples, not a schema** — every
production house works differently, so Pikachu ships no fixed roster. The user builds theirs.

Three extension points, in ascending order of danger: **agents**, **skills**, **tools**.

---

## Coordination is the canvas, not a protocol

The key architectural consequence of "there is a canvas, so there is no multiple team worker":

**Agents never talk to each other. They read and write artifacts on a shared canvas.**

```
   script writer ──writes──►  ┌─────────────────────┐
                              │      CANVAS         │
   storyboard   ──reads───►   │  immutable,         │  ◄──reads──  shot designer
                ──writes──►   │  addressable        │
                              │  artifacts with     │
   colourist    ──reads───►   │  provenance         │
                              └─────────────────────┘
```

This is how an actual film crew works — everyone works off the same board — and it dissolves the
problem that makes multi-agent frameworks unmanageable:

- **No handoff protocol.** Dependency is expressed by *reading an artifact*, not by a graph edge.
  The storyboard agent needs a script, so it reads the script artifact. Nobody wired an edge.
- **No shared mutable state.** Artifacts are immutable and addressable. A revision is a *new*
  artifact with `parent` set, so the canvas is an append-only graph, not a mutable blackboard.
- **No agent-to-agent messaging.** Which means no protocol to version, no deadlock, no ordering
  bugs.
- **Dropping an artifact from context is lossless** — the id restores it. Already established in
  `02-architecture.md` as the restorability rule; the canvas is that rule at product scale.

The canvas is therefore the *only* coordination mechanism we build. It replaces the graph we
refused, and it is also the UI the user already has.

**One consequence worth designing for:** an artifact needs to record *which agent* produced it, on
top of the existing prompt/model/cost/seed/parent provenance. On a film canvas "who made this
frame" is a real question, and it is also how you evaluate an agent's output quality later.

### This pattern has a name, and numbers

*Added 2026-08-30.* What is described above is the **blackboard architecture** — a coordination
pattern from classical multi-agent systems, not something we invented. Calling it by its name buys us
decades of prior art and, more usefully, published measurements:

> "The blackboard architecture substantially outperforms strong baselines, achieving **13%–57%
> relative improvements in end-to-end success**… Our findings establish the blackboard paradigm as a
> **scalable and generalizable** communication framework for multi-agent systems."
> — [arXiv 2510.01285](https://arxiv.org/pdf/2510.01285v2)

Corroborated on cost: a blackboard MAS is "competitive with the SOTA static and dynamic MASs… and at
the same time manage to spend **less tokens**" ([arXiv 2507.01701](https://arxiv.org/html/2507.01701v1)).

**Ours is an append-only blackboard**, and that distinction matters rather than being pedantry. The
classical blackboard is shared *mutable* state; ours is immutable artifacts with `parent` pointers.
That removes the overwrite vector — no agent can silently rewrite another's work — while keeping the
coordination property that makes the pattern effective.

### Correction: "no coordination needed" was too strong

The claim that a shared artifact space *replaces* coordination overstates it:

> "We find that **adding additional collaborators can lower performance when coordination structure
> is absent.**" The remedy is "collaboration scaffolding that combines **shared group memory** with
> simulated **human-in-the-loop gates**, where selected actions require approval."
> — [arXiv 2606.18413](https://arxiv.org/html/2606.18413)

So more agents on an unstructured board is actively worse, not merely no better. The scaffolding that
helps is shared memory **plus approval gates** — and we have both primitives already, unwired:
crew-shared long-term memory (`14-multi-agent.md`) and `DeferredToolResults.approvals`
(`02-architecture.md`). Wiring them together is a Phase 5 requirement, not an optional refinement.

### And it is an attack surface

A shared space every agent writes to and reads from carries the blackboard threat model:
**misalignment, malicious agents, compromised communication, data poisoning**
([arXiv 2510.14312](https://arxiv.org/html/2510.14312v1)). Append-only removes overwrite attacks but
not injection — a poisoned artifact that another agent reads is still poison. `guard/` must cover
**canvas reads**, not only tool grants. See `06-security.md`.

---

## Extension point 1 — Agents

Declarative, roughly six fields, no code. Per `14-multi-agent.md`:

```python
class AgentSpec(BaseModel):
    name: str                    # "Script Writer"
    description: str             # also the routing signal
    prompt: str                  # the persona — its own file
    model: str | None = None     # None → crew default
    skills: list[str] = []       # skill ids, never pasted bodies
    toolsets: list[str] = []     # narrowing only
    triggers: list[str] = []     # empty → invocable by name only, never auto-routed
```

A user creating an agent picks a name, writes a persona, ticks some skills, ticks some tools.
That is the whole surface. **The runtime enforces `FIXED_ALLOWLIST ∩ toolsets`** — a spec can
only ever narrow.

---

## Extension point 2 — Skills

"How can we create these skills?" Three paths, all landing in the same store:

| Path | Who writes it | Trust |
|---|---|---|
| **Author** | user writes a `SKILL.md` in the UI | untrusted-ish — their own workspace |
| **Import** | pulled from the public catalogue | **untrusted** — written by a stranger |
| **Distil** | the agent proposes one from a successful turn | gated, see `13-self-improvement.md` |

The format is already fixed and small — `name`, `description`, a markdown body, and a declared
toolset list. A user authoring one is writing instructions, not code.

What the runtime does to every skill regardless of path:

1. **Parse** the frontmatter and body.
2. **Scan** for injection. The scanner catches pattern-matched attacks and **misses paraphrased
   ones**, which is why auto-approve on a clean scan stays unsafe for anything public.
3. **Adapt** — foreign skills get `toolsets=[]` forced, and `bash`/`terminal`/`read_file`/`browser`
   stripped into a recorded `removed_tools` list.
4. **Confine** — `FIXED_ALLOWLIST ∩ declared`, evaluated per step by `.filtered()`. A skill can
   never widen its own authority (P3). This was a real hole once: `produces_media` was derived
   from the skill's own frontmatter, letting a document self-grant a credit-spending tool.

For a film crew the high-value authored skills are things like a house style guide, a shot-naming
convention, a director's visual grammar. Those are exactly the things a production house has and a
generic framework cannot ship.

---

## Extension point 3 — Tools, via MCP

"The user is also able to connect their custom widget." This is the highest-risk extension point
and it needs a standard boundary rather than a bespoke plugin API.

**Use MCP.** Pydantic AI already ships `MCPToolset` (`pydantic_ai.mcp`), so a user-connected tool
is an MCP server and we inherit a specified transport, schema and discovery model instead of
inventing one.

```python
toolsets = [
    base_toolset,
    MCPToolset(user_server_url).filtered(gate),   # same gate as everything else
]
```

Three rules for a user-connected tool:

**1. A user tool is untrusted input in a privileged position** — same posture as a stranger's
skill. It goes through the same `.filtered()` gate and the same allowlist intersection.

**2. Its output is untrusted data, never instructions.** Tool results must never be interpreted
as commands. This is already an invariant in `picx_tools.py`; it extends to every MCP tool.

**3. It must return handles, not payloads.** An oversized MCP tool return persists in history as
a `ToolReturnPart` and is re-sent on every later model request. Enforce a size cap rather than
trusting the tool to behave.

> **Deferred (2026-08-30):** an earlier draft required a user-connected tool to *declare a price*
> before it could reach credit-spending toolsets, so the Meter could reserve for it. Dropped for
> now as not important at this stage. Worth revisiting only if user tools that call paid APIs
> become common — until then a user tool is simply treated as free and unmetered.

---

## Why a crew beats one big agent here

The pitch — "one Pikachu is powerful, a group is more powerful" — holds for three concrete
reasons, none of which is speed:

**1. Retrieval scope.** `find_skill` returns 5. A script-writer agent searching writing skills
gets five good hits; a generalist searching everything gets five mediocre ones.

**2. Persona fidelity.** A script writer's prompt and a colourist's prompt genuinely conflict. One
system prompt holding both dilutes both. Separate agents keep each persona sharp — and this is the
reason a *human* production house has separate roles too.

**3. Cache economics.** A named agent's prefix is stable across every invocation, so 20 calls cost
`1.25 P + 19 × 0.10 P` instead of `20 × 1.25 P` for ephemeral workers. Cost is deprioritised, but
this direction happens to be the cheap one anyway.

---

## Open questions this raises

- **Does a user-created agent get to pick its model?** Per-agent choice interacts with the cache
  threshold — picking Gemini 3.x silently disables caching at our prefix size. Constrain the picker
  or surface the consequence.
- **How does a user's agent get evaluated?** A production house will make bad agents. Per-agent
  output-quality tracking (which artifacts got kept vs regenerated) is the honest signal, and it
  needs the artifact→agent provenance field above.
- **MCP protocol version.** `MCPToolset` exists; which revision it speaks, and whether user
  servers must match, is unverified and needs a docs pass before we publish a connector guide.
- **Canvas as a durable store.** The canvas is currently a UI concept. Making it the coordination
  substrate means it becomes a first-class artifact graph with its own schema — that is a real
  piece of backend work, not a rendering change.
