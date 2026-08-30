# 14 — Crews: user-defined agents with scoped skills

## Correction to the first draft of this doc

The first version designed **ephemeral task delegation** — spawn a child for a subtask, get a
result back, discard it. That is the wrong shape.

What is actually wanted is a **crew**: persistent, *named*, user-created agents each holding a
curated skill set. "One graphical designer and one social-media marketer" are roles that exist
before any task arrives, not workers conjured per subtask. That changes the design, the caching
arithmetic, and the reason for existing.

Two directives recorded, because they override earlier reasoning in this repo:

- **Cost is explicitly deprioritised.** "If one agent takes time and costs more, that is fine. We
  do not look at cost. We need functionality." The cost arithmetic below is kept as information,
  not as an objection.
- **Multi-agent is opt-in.** Default is one agent for everything. A crew exists only because a
  user configured one.

---

## The real motivation is skill scoping, not parallelism

This is the argument that makes crews worth building, and it is not about speed.

`find_skill` returns `MAX_FIND_RESULTS=5`. At forty skills the top five are probably right. At
four hundred — a realistic public catalogue plus a user's installs — they are noise, and every
turn silently degrades. That is library drift at the *roster* level.

**A named agent is a retrieval scope.** A designer agent searching only design skills gets five
good hits. One generalist agent searching everything gets five mediocre ones. The partition is
the feature; parallelism is a side effect.

This also matches how a user actually thinks about it: they do not want "an agent that can do
anything," they want a designer and a marketer, because that is how they'd delegate to people.

### The partition is a correctness mechanism, and there is evidence

*Added 2026-08-30.* The paragraph above argued this from retrieval quality. There is a stronger and
more specific reason, and it changes how we present the feature.

Skill selection does not degrade gracefully as a library grows. It "remains stable up to a critical
library size, **then drops sharply**," and the driver is "**semantic confusability among similar
skills, rather than library size alone**"
([arXiv 2601.04748](https://arxiv.org/abs/2601.04748)). The suggested remedy is hierarchical
organisation of the skill set.

Two consequences:

**1. We already built the hierarchy, for different reasons.** Top level = which agent (chosen by the
user, or implied by the canvas). Second level = `find_skill` within that agent's partition. That is
precisely the hierarchical routing the paper recommends, obtained free from a decision we made on
organisational grounds. Retrieval-before-context is also the "metadata-driven progressive disclosure"
pattern named in [arXiv 2602.20867](https://arxiv.org/abs/2602.20867).

**2. Multi-agent gains a principled trigger.** Multi-agent stays **opt-in** — that is unchanged and
non-negotiable. But we can now answer *when* instead of leaving it to taste:

> **Split an agent when its partition's skill descriptions start overlapping in meaning** — measured
> as max pairwise cosine similarity between descriptions (C7, `09-design-constraints.md`), not as a
> skill count.

That is measurable, explainable to a user in one sentence, and it fires *before* quality drops rather
than after. It is also honest about the mechanism: you are not splitting for tidiness, you are
splitting because one agent can no longer reliably tell its own skills apart.

**Evidence caveat:** that paper is a single-author, self-described technical report with
"preliminary" experiments, and the specific threshold numbers in circulation are not in its abstract.
The mechanism is plausible and the guard is cheap; the numbers are unconfirmed. We act on it because
the failure it predicts is **silent** — wrong skill selected, no error raised.

---

## Prior art: how KiroCrew defines an agent

Read from `/Users/yash/.kiro/agents/alex.json`. It is declarative JSON plus a separate markdown
prompt — no graph, no DSL, no code:

```json
{
  "name": "Alex",
  "description": "Backend & AI Engineer: builds APIs, CLIs, SDKs, MCP servers…",
  "model": "auto",
  "tools":        ["code", "grep", "shell", "web_search", "write", "@kirocrew-core"],
  "allowedTools": ["fs_read", "fs_write", "code", "grep"],
  "resources": [
    "file://.kiro/steering/**/*.md",
    "skill://cloudflare",
    "skill://mcp-development",
    "skill://picx-generation-api"
  ],
  "includeMcpJson": false
}
```

Four things to copy:

1. **The skill set is a list of URIs, not code.** A user edits a list; they do not write an
   orchestrator.
2. **`tools` and `allowedTools` are separate** — what the agent *can* reach vs what runs without
   asking. That separation is exactly what a metered runtime needs (a paid tool is reachable but
   never auto-approved).
3. **The prompt lives in its own file** (`alex-prompt.md`), so persona is editable without
   touching config.
4. **`description` doubles as the routing signal.** No separate router model needed.

### Routing discipline — how KiroCrew avoids becoming multi-agent by accident

`select_crew`'s own rules, which answer the "it should not always turn into multi-agent" concern
directly:

> Pick a crew **ONLY** when its triggers clearly and specifically match the task with high
> confidence. If no crew is a strong match, do **NOT** route — fall back to the default. Crews
> without triggers are omitted from the roster and are never auto-selected.

That is the behaviour to implement verbatim: **default-single, route only on a confident trigger
match, and an agent with no triggers is never auto-selected** (it can still be invoked by name).
Conservative routing is what keeps a one-agent product from silently becoming a distributed
system.

---

## The Pikachu shape

An agent spec is data. Building one is constructing a Pydantic AI `Agent` from it.

```python
class AgentSpec(BaseModel):
    name: str
    description: str                  # also the routing signal
    prompt: str                       # or a path
    model: str | None = None          # None → crew default
    skills: list[str] = []            # skill ids — never pasted bodies
    toolsets: list[str] = []          # narrowing only, never widening
    triggers: list[str] = []          # empty → never auto-routed
```

Enforcement of the partition uses what Pydantic AI already provides — no new machinery:

```python
allowed = FIXED_ALLOWLIST & set(spec.toolsets)      # P3: narrow only

agent = Agent(
    OpenRouterModel(spec.model or crew.default_model),
    instructions=[spec.prompt, *resolved_skill_bodies],   # static → cacheable prefix
    toolsets=[base.filtered(lambda ctx, td: td.name in allowed)],
    model_settings=OpenRouterModelSettings(openrouter_cache_instructions='1h'),
)
```

`.filtered()` re-evaluates per step against `RunContext`, so a skill loaded mid-run still cannot
widen the partition. The **P3 invariant holds per agent** — a designer agent cannot reach video
generation because a skill asked nicely.

### Invocation is a tool call, not a topology

```python
result = await crew.delegate(
    agent="designer",
    task="three carousel panels, brand palette",
    context=ContextSlice(style=True, episodic=False),
)
```

One direction. No agent-to-agent messaging, no supervisor node, no shared blackboard. **Still
five concepts** — `delegate` is a tool, and the crew is configuration.

---

## State transfer between agents

Unchanged from the first draft, because this part was right. Context crosses by **declared
slice**, mirroring KiroCrew's `include_memory` / `include_lessons` / `include_project`
(and `spawn_list` renders `ctx-withheld:` so the scoping is visible). Default-deny.

| Tier | Crosses to another agent? |
|---|---|
| Short-term (working) | **never** — it is that run's own history |
| Mid-term (episodic) | opt-in, and only the retrieved slice |
| Long-term semantic (brand, palette, banned looks) | **usually yes** — this is what makes a designer and a marketer produce consistent output |
| Long-term procedural (skills) | as a **skill id**, never a pasted body |

That third row is the one that makes a crew feel coherent rather than like two strangers. The
marketer writing copy about an image the designer produced needs the same brand facts.

Two invariants borrowed from KiroCrew's typed failures:

- **A child that cannot restore its context must fail, not proceed.** Their `resume_failed`
  "never executes context-free." A context-free agent holding a spending tool is the worst case.
- **Never interrupt an agent holding an open reservation.** `spawn_steer` distinguishes
  `interrupt` from `follow_up`; for a metered agent `follow_up` is the default, because
  interrupting between `reserve` and `capture` orphans a spend.

---

## Caching: persistent agents reverse the earlier objection

The first draft argued fan-out defeats prompt caching. **That holds for ephemeral children and
not for a named crew**, and the difference is worth stating.

An ephemeral child has a prefix that exists for one task: one cache write, no reads, pure loss.

A **named agent's prefix is stable across every invocation** — its persona, its skill set, its
tool schemas do not change between calls. So a designer agent invoked twenty times in an hour
pays one write and nineteen reads against a `'1h'` TTL:

```
ephemeral, 20 one-shot children :  20 × 1.25 P  = 25.00 P
named agent, 20 invocations     :  1.25 P + 19 × 0.10 P = 3.15 P
```

**The crew model is the cache-friendly one.** Per-agent prefixes are exactly the kind of stable
prefix caching is designed for — arguably more so than a single generalist agent whose skill body
changes per turn.

Cost is deprioritised per direction, so this is recorded as a happy consequence rather than a
justification.

---

## What we build, and what we still refuse

**Build:**
- `AgentSpec` as data; a crew is a list of specs plus a default.
- User-facing agent creation — name, prompt, skill picks, triggers.
- Conservative routing: default-single, confident-trigger-only, untriggered agents invocable by
  name only.
- Per-agent skill partition enforced by `.filtered()`; P3 holds per agent.
- `delegate()` as a single tool call with an explicit `ContextSlice`.
- Parent run owns the credit envelope; delegated agents draw against it atomically and settle per
  agent (3 of 5 succeed → capture 3, refund 2).

**Refuse:**
- Supervisor/router graphs, handoff protocols, agent-to-agent messaging.
- Shared mutable state. State moves as a declared slice or not at all.
- A pasted skill body crossing an agent boundary. Ids only, resolved server-side.
- Automatic multi-agent. If routing is not confident, one agent does the work.
- A toolset that widens. `FIXED_ALLOWLIST & spec.toolsets`, always.

---

## Open, and needed before implementation

- **Routing mechanism.** KiroCrew uses `description` + `triggers` matched by the model. A cheap
  classifier is the alternative. Trigger-matching is simpler and already proven; start there.
- **Does a user-created agent get to pick its model?** Per-agent model choice interacts with the
  cache threshold — a user picking Gemini 3.x for their agent silently loses caching
  (`02-architecture.md`). Either constrain the picker to 1,024-floor models or surface the
  consequence.
- **Untrusted agent specs.** A user-created agent is a new authority surface. The spec declares
  skills and toolsets, so it must go through the same `FIXED_ALLOWLIST ∩ declared` intersection as
  a skill — a spec must never be able to grant what a skill cannot (`06-security.md`).
