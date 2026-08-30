# 10 — What the Module Is

Pikachu is an **importable Python library**, not a service and not an app you run. You
`pip install` it, construct an agent, and call it from your own code — a FastAPI handler, a
worker, a script.

This is the distinction that made `hermes-agent` the wrong dependency: it is *"an agent you
run, not a library you import into a custom multi-step pipeline"*
([review](https://markaicode.com/best/best-hermes-agent-production-practices/)). Pikachu is
deliberately the second thing.

---

## The five concepts

Per `09-design-constraints.md` C1, the whole mental model is five nouns. If you understand
these you understand the library.

| Concept | What it is |
|---|---|
| **Agent** | A configured turn-runner. Model + tools + skill. Cheap, built per turn, never shared. |
| **Skill** | Instructions + optional resources that specialise the agent for a task. A `SKILL.md`. Loaded on demand. |
| **Tool** | A function the model can call. May be **metered** — costing real money. |
| **Run** | One durable execution of a turn. Checkpointed, cancellable, resumable. |
| **Memory** | What persists between runs: episodic, semantic, and the skill library itself. |

## Quickstart target

The bar from `09-design-constraints.md`: a working agent in under 20 lines. This is the
shape to design toward — **not yet implemented**, and the exact signatures will move.

```python
from pikachu import Agent, tool

@tool
async def generate_image(prompt: str) -> str:
    """Generate an image and return its URL."""
    return await my_provider.generate(prompt)

agent = Agent(
    model="google/gemini-3.5-flash",   # any Pydantic AI model string
    tools=[generate_image],
)

result = await agent.run("a watercolour fox on a bicycle")
print(result.text, result.artifacts)
```

No billing, no durability, no Postgres. Those are opt-in protocols — an adopter who wants a
plain agent should never meet them.

## Opting into the parts that make it different

The three primitives from `00-problem-statement.md` are each one protocol implementation.
Nothing is mandatory.

```python
from pikachu import Agent
from pikachu.billing import MeteredTool
from pikachu.durability import RunStore
from pikachu.memory import MemoryStore

agent = Agent(
    model="google/gemini-3.5-flash",
    tools=[generate_image],
    biller=MyCreditLedger(),    # MeteredTool: quote -> reserve -> capture -> refund
    runs=MyPostgresRunStore(),  # RunStore: checkpoint after each tool result
    memory=MyPgVectorMemory(),  # MemoryStore: bounded retrieval per turn
)
```

Omit `biller` and tools are free. Omit `runs` and the turn is not resumable. Omit `memory`
and the agent is stateless. Every one degrades to something simpler rather than failing.

## The protocols

Structural typing (`typing.Protocol`), not inheritance. Implement the methods; you do not
import a base class.

```python
class MeteredTool(Protocol):
    def quote(self, call: ToolCall) -> Cost: ...
    async def reserve(self, cost: Cost, ctx: RunContext) -> Reservation: ...
    async def capture(self, r: Reservation) -> None: ...
    async def refund(self, r: Reservation, reason: str) -> None: ...

class RunStore(Protocol):
    async def start(self, run: RunSpec) -> RunId: ...
    async def checkpoint(self, run_id: RunId, step: Step) -> None: ...
    async def resume(self, run_id: RunId) -> ResumeState | None: ...
    async def cancel(self, run_id: RunId) -> None: ...

class MemoryStore(Protocol):
    async def recall(self, query: str, budget: TokenBudget) -> list[Memory]: ...
    def remember(self, event: Event) -> None: ...   # enqueued, never in the request path
```

Two deliberate choices worth noting. `resume` returns `None` for an unknown run rather than
raising, so a cold start is not an error path. `remember` is sync-returning because writes
are queued — a dropped memory write degrades quality but must never fail a paid generation
(`04-memory.md`).

## What is NOT in the public API

Saying no here is what keeps C1 true:

- No multi-agent orchestration, graphs, or handoffs.
- No prompt-template engine. Skills are Markdown; f-strings exist.
- No RAG pipeline. Memory retrieval is bounded and specific, not a document QA system.
- No model router. We pass a model string to Pydantic AI.
- No CLI. Pikachu is a library; ship a CLI as a separate package if wanted.
- No web server, no HTTP routes. PicX's FastAPI layer stays in PicX.

## Definition of done for "independent module"

Testable, so the extraction cannot be declared finished on vibes:

1. `pip install <name>` works in a clean venv with no PicX code present.
2. No import of anything under `app.` outside `tools/`.
3. Test suite passes with a SQLite run store and a no-op biller — no Postgres, no credits.
4. Quickstart above runs as written, under 20 lines.
5. `grep -r "picx" pikachu/ --include=*.py` returns hits only under `tools/picx_media/`.

Item 5 is the honest one. Everything else can pass while PicX assumptions are still smeared
through the core.
