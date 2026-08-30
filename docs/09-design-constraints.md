# 09 — Design Constraints

Four stated priorities, in the owner's words: **performance, skills, visuals, simple** —
plus **ecosystem support** and **Pydantic AI only, no other framework**.

These are constraints, not aspirations. Each is written so it can be checked in review.

---

## C1 — Simple, because complexity has a real cost here

Direct quote: *"go simple because I don't know very complex tools."*

The most useful constraint in this document. A framework its own author cannot reason about
will not be maintained and will not be adopted. Concrete implications:

- **One agent per turn.** No orchestration graphs, no supervisor/worker topologies, no handoff
  protocols. The existing contract is already a single method — keep it that way. *Refined by
  `14-multi-agent.md`:* a user may define several named agents, but they never talk to each other
  and only one runs a given turn. Coordination is the canvas (`15-extensibility.md`), and per C7 the
  reason to have more than one is selection accuracy, not topology.
- **No DSL.** Configuration is plain Python and Pydantic models. If defining a skill
  requires learning a new notation, we failed.
- **Five concepts, total:** agent, skill, tool, run, memory. If a sixth is proposed, one has
  to go or it needs a written justification.
- **Good defaults over options.** Every knob is a decision pushed onto the user. Ship
  opinions; expose the knob only when a real case demands it.
- **Boring code.** No metaclasses, no dynamic attribute magic, no clever auto-registries.
  Explicit imports, plain functions.

**Review test:** if explaining a feature needs a diagram, it is too complex for v1.

## C2 — Performance means token waste and latency, not benchmarks

See `05-performance.md`. The measured costs are the skill body re-sent every iteration
(~13.7 KB per 20-call turn), prompt caching that transmits nothing, a blocking call
offloaded to a worker thread, and 250–600 ms round trips to remote Postgres.

**Explicitly not a goal:** winning instantiation benchmarks. Agno's 3.2 µs figure is noise
against a 250–600 ms round trip. Do not pick or defend a design on that basis.

**Review test:** every performance claim ships with a number from
`scripts/measure_turn_cost.py` or `token_ledger.py`, measured before and after.

## C3 — Skills are the product surface

See `03-skill-lifecycle.md`. Creation, versioning and self-improvement ship **with**
curation, because unbounded accumulation is the published *library drift* failure mode.

**Review test:** no skill-creation feature merges without its lifecycle counterpart.

## C4 — Visuals are first-class, not decoration

This is the content/visual vertical (`01-positioning.md`), so the runtime treats media as a
real output type rather than text with a URL in it:

- An artifact carries **provenance**: prompt, model, cost, parent artifact, seed.
- A skill carries **example output**. `groot_skills.cover_image_url` already exists — a
  skill catalogue for a visual tool must lead with artwork, not a text list. Both chosen
  references (youmind.com/skills, higgsfield.ai/supercomputer) are art-led galleries.
- Cost is **visible before spend**, because every generation debits credits.
- Rejected outputs are signal, not waste — they feed style memory (`04-memory.md`).

**Review test:** can a user see what a skill produces before running it? If not, the visual
surface is incomplete.

## C5 — Pydantic AI only

Explicit instruction: Pydantic AI, no other framework.

- One backend implementation in `backends/`, plus `FakeBackend` for tests. No adapter zoo,
  no pluggable-framework abstraction layer.
- `GrootAgentBackend` stays as the **test seam and migration path**, not because we intend
  to support many frameworks.
- Consequence to act on: `api/pyproject.toml` declares **both** `agno>=2.5.17` and
  `hermes-agent>=0.19.0`, both installed. Both go once the port is green.

**Review test:** exactly one third-party agent framework in the dependency list.

## C6 — Ecosystem support beats novelty

Ranked above novelty deliberately. What we get for free by not being clever:

| Need | Comes from |
|---|---|
| Model access incl. OpenRouter | first-party provider |
| Durable execution | DBOS / Restate / Kitaru / Temporal integrations |
| Agent Skills | first-party, plus `pydantic-ai-skills` |
| Type safety | Pydantic, already in the stack |
| API stability | V1 since Sept 2025, no breaking changes until V2 |

That last row is the point. What just broke us was a dependency changing its distribution
model with no warning. Boring and stable beats new and underrated.

## C7 — Bounded selection, bounded iteration

Added 2026-08-30 from research (`20-research-findings.md`, `21-skill-selection-limits.md`). Both
limits guard **silent** failures — the kind that produce no error, just worse output — which is why
they are constraints rather than tuning notes.

### A partition must stay under the confusability cliff

Skill selection does not degrade gracefully. Accuracy "remains stable up to a critical library size,
then drops sharply," and the driver is **semantic confusability among similar skills, rather than
library size alone** ([arXiv 2601.04748](https://arxiv.org/abs/2601.04748) — single-author technical
report, preliminary results, so treat the mechanism as likely and the numbers as unconfirmed).

- A single agent's **selectable** skill set is bounded. `find_skill` retrieval already narrows before
  anything reaches context, and the per-agent partition (`14-multi-agent.md`) is the second bound.
- Measure the cause, not the proxy: **max pairwise cosine similarity between skill descriptions
  within a partition**, using embeddings we already compute. Warn at authoring time when a new skill
  lands too close to an existing one.
- Splitting one agent into several is therefore a **correctness** action, not organisational
  preference.

**Review test:** does any feature let a single agent's selectable skill set grow without a
confusability check? If yes, it is unbounded and needs the check.

### Iteration count should be justified, not inherited

Production agents are more conservative than our default: "**68% execute at most 10 steps** before
requiring human intervention," and 70% prompt off-the-shelf models rather than tuning weights
([Measuring Agents in Production, arXiv 2512.04123](https://arxiv.org/html/2512.04123v1)).

The 70% figure validates C5 and our no-weight-updates position. The 68% figure does not validate our
`max_iterations = 20`, which sits above the norm. Twenty may still be right for a media turn that
legitimately chains generate → inspect → refine, but it should be a **measured** choice: the
distribution of actual iteration counts per turn is already in `token_ledger.py`.

**Review test:** the iteration cap is defended with the observed distribution, not with a round
number.
