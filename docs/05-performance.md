# 05 — Performance: measured, not benchmarked

## The trap

"Agents have heavy load" is true but the popular numbers measure the wrong thing. Agno
advertises 529× faster than LangGraph and 24× lower memory — those measure **agent
instantiation** (3.2 µs, 5.2 KiB per agent). PicX creates one agent per HTTP request
against remote Postgres with 250–600 ms round trips. Instantiation is ~0.001% of a turn.

**Do not choose a framework on instantiation benchmarks.** They are unfalsifiable
marketing for our workload.

## Where the time and money actually go

| Cost | Measurement | Source |
|------|-------------|--------|
| Blocking agent call offloaded to a worker thread | one threadpool hop per turn | `hermes_adapter.py` `HermesBackend.run_turn` |
| Skill body re-prepended to `effective_system` on **every** API call in the loop | 2,734 B measured → ~13.7 KB / ~13,700 tokens across a 20-call turn | `conversation_loop.py:959`; measured by `scripts/measure_turn_cost.py` |
| Prompt caching delivering nothing | default `google/gemini-3.5-flash` never qualifies for the cache gate | `agent_runtime_helpers.py:1796` |
| Prompt caching **broken** even when it qualifies | OpenRouter routes Claude over the OpenAI wire format; `cache_control` on a `messages[]` system entry is ignored by Anthropic, which only honours it on top-level system blocks | hermes-agent issue #20957 |
| Remote DB/Redis latency | 250–600 ms per round trip | infra: DigitalOcean Postgres + Koyeb Redis |

The prompt-caching row is the important one. **The Phase-4 cost plan was "prompt caching
first."** On our actual OpenRouter path that optimisation silently does nothing. Any cost
projection built on it is wrong.

## What the migration actually fixes

| Change | Mechanism | Expected effect |
|--------|-----------|-----------------|
| Native async backend | removes the threadpool hop | lower tail latency, one less failure mode |
| First-party OpenRouter provider | cache markers on the correct wire path | caching that actually transmits |
| Skill body sent once, not per iteration | restructure the system prompt assembly | up to ~13 KB/turn removed at 20 iterations |
| Bounded memory retrieval | caps from `04-memory.md` | memory cannot regress the above |

Note the third row is **ours to fix, not the framework's**. Re-sending the skill body
every iteration is an architectural choice in the loop, and it survives a framework swap
unless we deliberately change it. Do not assume Pydantic AI fixes it for free.

## Budget we hold ourselves to

Per turn, steady state, measured not estimated:

| Metric | Target | How measured |
|--------|--------|--------------|
| Skill/system tokens re-sent per iteration | 0 after the first | `scripts/measure_turn_cost.py` |
| Memory retrieval tokens | ≤ 3,150 | `token_ledger.py` |
| Cache hit rate on a cacheable model | > 0% (currently 0) | provider usage fields |
| Threadpool hops per turn | 0 | code inspection |
| Wall-clock overhead outside model + tool time | < 50 ms | tracing |

Instrumentation already exists: `token_ledger.py` and `scripts/measure_turn_cost.py`
(committed `d75454f6`). **Baseline both backends before claiming any win.**

## The remedy, now verified

The diagnosis above stands for hermes. The fix on Pydantic AI is configuration, not a project
— see `02-architecture.md` for detail:

- **`OpenRouterModel` / `OpenRouterProvider`** are dedicated classes, so the wire-format
  translation that silently dropped `cache_control` is the model's job, not ours.
- **`OpenRouterModelSettings`** exposes `openrouter_cache_instructions`,
  `openrouter_cache_messages`, `openrouter_cache_tool_definitions` — each `True` or a
  `'5m'`/`'1h'` TTL. Plus a provider-agnostic `CachePoint` part.
- **Static instructions are sorted before dynamic**, by design, so Anthropic/Bedrock can cache
  the stable prefix. Putting the resolved skill body in `instructions` (not `system_prompt`)
  makes it a byte-identical cacheable prefix for that turn's agent.

Correction to the framing above: the win is **not** "send the skill body once." Instructions
are re-sent every request by design. The win is that a byte-identical static prefix bills at
**cache-read** rates. Same bytes, different price.

Measurement is built in — `result.usage()` returns `RunUsage` with `cache_read_tokens`,
`cache_write_tokens`, `cost: Decimal | None`, and a ready-made `cache_hit_ratio` property,
with `input_tokens` normalised across providers so the ratio is comparable.

One scoped gap: [pydantic-ai #5205](https://github.com/pydantic/pydantic-ai/issues/5205) —
`cache_read_tokens` reads 0 for **Google implicit caching** specifically. Our default is
`google/gemini-3.5-flash`, so either read the OTel span or move the default to a provider with
explicit caching. That is now a cost decision with a measurable answer, not a preference.

## Model arithmetic — still to do

The Kimi-vs-Gemini comparison remains unrun: cheaper-per-token with no caching may still beat
pricier-with-caching. But it is now a straightforward experiment rather than a guess, because
`RunUsage.cost` and `cache_hit_ratio` give both sides of the equation per run. Do it after the
`PydanticAIBackend` lands, with `scripts/measure_turn_cost.py` as the harness.
