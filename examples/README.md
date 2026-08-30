# Examples — success criteria, proven end to end

Each example here *exercises* a PRD success criterion so you can run it and watch the property
hold, rather than reading an assertion that it does. Every example runs on `FakeBackend` with
**no network and no API key**, and takes a `--live` flag for an optional real run against the
default model through OpenRouter.

The offline path is the source of truth: `tests/test_examples.py` drives the same code in CI,
so an example cannot silently rot. `--live` confirms the offline behaviour against a real
model; it never changes what the example proves.

Run all offline (this is what CI does):

```bash
.venv/bin/python -m pytest tests/test_examples.py -q
```

| File | Proves | Success criterion |
| --- | --- | --- |
| `canvas_handoff.py` | Two agents coordinate by reading/writing the shared canvas; the storyboard agent produces frames from a script it was **never passed** | **S5** |
| `create_agent_at_runtime.py` | An end user creates an `AgentSpec` at runtime, registers it, invokes it **by name** — no code change, no deploy | **S4** |
| `../scripts/measure_cache.py` | Whether prompt caching **fires** on the default model with a full-size prefix — a recorded negative is a valid result | **S1** |

---

## S5 — `canvas_handoff.py`

**The claim.** *A storyboard agent produces frames from a script artifact it was never passed
as an argument.* This is the blackboard pattern from the PRD's film-production reference use
case: roles coordinate by reading and writing artifacts on the canvas, never by messaging each
other, and never by an orchestrator wiring an edge between them.

**What the example does.**
1. A `script-writer` agent runs a turn and appends **one** script artifact to a `CanvasGraph`.
2. A `storyboard` agent runs a turn whose `TurnRequest` carries its role instruction and skill
   — and **nothing about the script**: not the id, not the payload ref, not the text. That
   absence is checked by `storyboard_request_is_blind()`.
3. The storyboard agent **discovers** the script by reading the board (`CanvasGraph.read`,
   which also propagates canvas-read taint), then appends frames whose `parent` points at the
   script and whose provenance records `storyboard` as producer.

**Why it is unmistakable.** The dependency edge lives in the *output* (each frame's `parent`),
created by the agent from what it read — not in the *input*, where an argument-passing design
would put it. The test asserts both halves: the input is blind, and the output derives anyway.
A control test (`test_s5_removing_the_script_breaks_the_handoff`) shows that with an empty
board the storyboard turn fails for lack of anything to read — proving the read *is* the
dependency.

```bash
.venv/bin/python examples/canvas_handoff.py            # offline, FakeBackend
.venv/bin/python examples/canvas_handoff.py --live     # real model (needs OPENROUTER_API_KEY)
```

---

## S4 — `create_agent_at_runtime.py`

**The claim.** *An end user creates an agent in the UI and invokes it, with no code change and
no deploy.* The person creating the agent is P2 — a user of a product built on this SDK — not
a developer editing a repo.

**What the example does.**
1. Builds an `AgentSpec` from the six declarative fields (name, role, instructions,
   skill_tags, allowed_tools, triggers) — plain data, no subclass, no decorator.
2. `AgentRegistry.create()`s it. That call *is* the entire "deploy".
3. `AgentRegistry.get(name)`s it back **by name** and runs a turn through the generic backend
   seam.
4. Creates a **second** agent with a disjoint `skill_tags` partition, so the partition
   boundary is visible: the two selectable sets do not overlap.

**Why it is unmistakable.** The test asserts that no agent-specific module
(`examples.agents.*`, `pikachu.agents.*`) is ever imported — there is nothing to import,
because the agent is data. The round trip (created → listed → fetched by name → invoked →
produced output) runs entirely on generic SDK surface.

```bash
.venv/bin/python examples/create_agent_at_runtime.py            # offline, FakeBackend
.venv/bin/python examples/create_agent_at_runtime.py --live     # real model
```

---

## S1 — `../scripts/measure_cache.py`

**The claim.** *`RunUsage.cache_hit_ratio` > 0 on the default model.* Today it is believed to
be 0 because the stable prefix (~1,500–2,400 tokens) may sit below the model's minimum
cacheable-prefix floor. This is a **measurement**, and *a recorded negative is the deliverable*
— not a failure to be retried until green.

**What the script does.** Builds a full-size prefix — a real skill body plus tool schemas,
sized into `STABLE_PREFIX_TOKENS_MIN..MAX` — and runs the **same** turn **three** times against
the real model, reporting `cache_read_tokens` / `cache_write_tokens` per turn and the timing
phases, plus the estimated dollar cost. (The existing live tests use tiny prompts, which is why
they proved nothing about the floor.)

- If `cache_read_tokens > 0`: caching fired, **S1 is met**, and `CACHE_FLOOR_UNVERIFIED` in
  `config.py` can be cleared by the integrator (that file is reserved; the script only says so).
- If it stays `0`: that is the recorded negative, with a model recommendation (a model whose
  published floor — e.g. Anthropic/OpenAI at ~1,024 tokens — our prefix clears).

**⚠ Caveat printed next to the number.** Google's implicit caching can report `0`
`cache_read_tokens` even when the cache *did* fire (pydantic-ai #5205), so a `0` is
**suggestive, not conclusive** — confirm against an OpenTelemetry `gen_ai` span before
concluding the prefix floor is the cause.

This script **costs real money**, is capped at 3 turns, and is **never** run in CI (it needs a
key and a network, both forbidden in the test suite).

```bash
.venv/bin/python scripts/measure_cache.py            # 3 real turns (needs OPENROUTER_API_KEY)
```
