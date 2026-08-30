# 01 — Positioning: a vertical agent, not a general one

## The decision

Pikachu is **not** a general-purpose agent framework. It is an agent runtime for
**content creation and visual/graphic design**.

This is a deliberate narrowing, and it is the correct one, because the general slot is
gone: LangGraph, CrewAI, Pydantic AI, Microsoft Agent Framework, Google ADK and the
OpenAI/Anthropic SDKs all compete there with far more engineering behind them. A ninth
general framework has no reason to exist.

The vertical slot is open, and — critically — the vertical is what *creates* the three
technical problems in `00-problem-statement.md`. Content/visual work means image and
video generation. Image and video generation costs money per call. That is why metered
tools, no-double-charge resume, and untrusted-skill confinement are not bolt-ons here;
they are the domain.

**General frameworks did not build these because their tools are free. Ours are not.**

## What "vertical" buys us

| General framework | Pikachu |
|---|---|
| Tool = free function call | Tool = priced operation with reserve/capture/refund |
| Output = text | Output = media artifact with provenance, cost and a place to land |
| Skill = prompt template | Skill = a reproducible visual recipe, with example output as its cover |
| Success = task completed | Success = artifact the user keeps, at a cost they accepted |
| Memory = conversation history | Memory = style preferences, brand constraints, prior artifacts |

That last row is the most under-served. A design agent's most valuable memory is not
"what did the user say three turns ago" — it is *this user's aesthetic*: palettes they
reject, aspect ratios they use, brands they work for, models that gave them good results.
None of the general frameworks model that. See `04-memory.md`.

## What must stay domain-free

The vertical is the *product*. The runtime core must stay domain-agnostic or it cannot
be open-sourced and cannot be reused:

- `billing/`, `durability/`, `skills/`, `security/`, `telemetry/` — **no PicX types**
- `tools/picx_media/` — an optional extra, one tool pack among many

An adopter doing paid legal research, paid data enrichment, or paid inference should be
able to `pip install <name>`, implement two protocols, and get metered + durable +
untrusted-skill-safe behaviour. PicX media is the reference tool pack, not the core.

This is the same shape as `00-problem-statement.md` P1–P3: the primitives are general,
our instantiation of them is visual.

## Non-goals

- Not a multi-agent orchestration graph. One agent, one turn, done well.
- Not a general LLM app framework. If you want RAG over PDFs, use something else.
- Not a hosted platform. Pikachu is a library; PicX is one deployment of it.
- Not a model router. We call OpenRouter; we do not reimplement it.

## The naming risk

"Pikachu" is a Marvel/Disney character. Fine as an internal codename, a genuine trademark
problem for a public PyPI package and GitHub org. Decide before first publish — see
`07-open-questions.md`.
