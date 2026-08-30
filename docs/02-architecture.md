# 02 — Architecture

Agent-level design. PicX is one consumer, not the subject.

All Pydantic AI names below were verified against the live docs on 2026-08-29. Where a name
is unconfirmed it is marked **UNVERIFIED**.

---

## The organizing principle: the capability boundary

Pydantic AI is far larger than "a typed agent loop." It ships first-class **capabilities**
for history editing, compaction, per-step tool filtering, usage/cost limits, step
persistence, durable execution and prompt caching. Most of what I would otherwise have built
as Pikachu layers already exists there, tested, by the team that owns the loop.

Their own spend doc draws our line:

> "…caps tokens and requests… for the duration of a single `run()`. **What it does not cover
> is money, a period longer than one run, a per-tenant share of a shared allowance, or a
> counter that several worker processes agree on.**"
> — [Pydantic AI, Spend](https://pydantic.dev/docs/ai/harness/spend/)

That sentence is the architecture. Everything it names is what a metered, multi-tenant,
durable agent runtime must own. Everything else we configure.

**Rule: if Pydantic AI has a capability for it, we configure it. We write code only where
Pydantic AI explicitly stops.**

---

## Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│  CONSUMER            FastAPI handler · worker · script · notebook     │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  Agent.run() → AsyncIterator[Event]
┌───────────────────────────────▼──────────────────────────────────────┐
│  PIKACHU — only what Pydantic AI does not do                          │
│                                                                       │
│   ┌─────────────────────────────────────────────────────────────┐    │
│   │ TURN RUNTIME    phases · termination · cancellation · events  │    │
│   └─────────────────────────────────────────────────────────────┘    │
│   ┌───────────────┬───────────────────┬───────────────────────────┐  │
│   │ SKILLS        │ CONTEXT PLAN      │ GUARD                     │  │
│   │ resolve       │ what is static vs │ allowlist ∩ declared (P3) │  │
│   │ find/load     │ dynamic, + budget │ injection scan · strip    │  │
│   │ lifecycle     │ per tier          │ terminal/bash             │  │
│   └───────────────┴───────────────────┴───────────────────────────┘  │
│   ┌───────────────┬───────────────────┬───────────────────────────┐  │
│   │ METER         │ LEDGER            │ ARTIFACTS                 │  │
│   │ quote·reserve │ run store         │ media as typed output     │  │
│   │ capture·refund│ checkpoints keyed │ provenance: prompt,       │  │
│   │ reconcile (P5)│ to PAID effects   │ model, cost, parent, seed │  │
│   └───────────────┴───────────────────┴───────────────────────────┘  │
│   ┌─────────────────────────────────────────────────────────────┐    │
│   │ TELEMETRY   token ledger · cache_hit_ratio · OTel spans        │    │
│   └─────────────────────────────────────────────────────────────┘    │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  composes, does not wrap
┌───────────────────────────────▼──────────────────────────────────────┐
│  PYDANTIC AI capabilities — configure, never reimplement               │
│                                                                       │
│   ProcessHistory · Compaction · PrepareTools · .filtered()             │
│   UsageLimits · StepPersistence · ConversationSearch                   │
│   CachePoint + OpenRouterModelSettings cache flags                     │
│   TemporalDurability · DBOSDurability · Prefect · Restate              │
└──────────────────────────────────────────────────────────────────────┘
```

Three layers I had planned to build — history compaction, per-step tool filtering, and
prompt-cache plumbing — collapse into configuration. That is the improvement: less of our
code, and the code we keep is the code nobody else has.

---

## The headline correction: caching is a settings flag, not a project

The measured waste — skill body re-prepended every model call, 2,734 B/iteration, ~13.7 KB
per 20-call turn — and the broken caching
([hermes #20957](https://github.com/NousResearch/hermes-agent/issues/20957): OpenRouter
routes Claude over the OpenAI wire format, so `cache_control` on a `messages[]` system entry
is ignored) are both **solved by Pydantic AI directly**.

Three verified facts:

**1. There is a dedicated `OpenRouterModel` and `OpenRouterProvider`**
([docs](https://pydantic.dev/docs/ai/models/openrouter/)) — not `OpenAIChatModel` plus a
generic provider. The wire-format translation that broke hermes is the model's job here.

**2. `instructions` are a cacheable static prefix, by design.** Static instructions (literal
strings) are **always sorted before dynamic** ones specifically so Anthropic/Bedrock can
prompt-cache the stable prefix ([Agents](https://ai.pydantic.dev/agents/#instructions)). And
`instructions` are *not* persisted in message history the way `system_prompt` is — only the
current agent's are sent. The docs recommend `instructions` over `system_prompt` by default.

**3. Cache control is explicit.** `OpenRouterModelSettings` exposes
`openrouter_cache_instructions`, `openrouter_cache_messages`, and
`openrouter_cache_tool_definitions`, each accepting `True` or a `'5m'` / `'1h'` TTL. There is
also a provider-agnostic `CachePoint` part (`from pydantic_ai import CachePoint`) — insert it
in a prompt list and everything before it is cached.

So the fix is roughly:

```python
from pydantic_ai import Agent
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings

agent = Agent(
    OpenRouterModel('anthropic/claude-sonnet-4.6'),
    instructions=[AGENT_INSTRUCTIONS, resolved_skill.body],   # static → stable prefix
    model_settings=OpenRouterModelSettings(
        openrouter_cache_instructions='1h',
        openrouter_cache_tool_definitions=True,
    ),
)
```

We build a fresh agent per turn anyway (P7), so the resolved skill body is a *static*
instruction for that turn's agent — byte-identical across all its iterations, sorted first,
cached.

**Correction to my earlier framing:** the win is not "send the skill body once." Instructions
are re-sent on every request by design. The win is that a byte-identical static prefix is
billed at **cache-read** rates instead of full input rates. That reframes the whole cost
plan — and it means the migration is not just a threadpool-hop deletion, it repairs the cost
lever hermes broke.

Verification is built in: `result.usage()` returns a **`RunUsage`** with `cache_read_tokens`,
`cache_write_tokens`, `cost: Decimal | None`, and a ready-made **`cache_hit_ratio`**
property. `input_tokens` is normalised to *include* cache reads across providers, so the
ratio is provider-comparable.

### The gate: a prefix below the provider minimum caches nothing

Verified minimum cacheable prefix sizes (primary docs, 2026-08-29):

| Provider / model | Minimum prefix |
|---|---|
| Claude Opus 5, Fable 5, Mythos 5 | 512 tok |
| Claude Sonnet 5 / 4.6 / 4.5, Opus 4.8 / 4.1 / 4 | **1,024 tok** |
| Claude Haiku 3.5, Opus 4.7 | 2,048 tok |
| Claude Haiku 4.5, Opus 4.6 / 4.5 | 4,096 tok |
| OpenAI GPT-5.6+ | 1,024 tok |
| OpenAI pre-5.6 | 2,048 tok |
| Gemini 2.5 Flash / Pro (implicit) | 2,048 tok |
| **Gemini 3.x Flash / 3.1 Pro (implicit)** | **4,096 tok** |

**Our measured skill body is 2,734 B ≈ ~700 tokens — below every threshold on its own.** The
prefix only clears the bar once base instructions + skill body + tool schemas are summed, and
our current default `google/gemini-3.5-flash` sits in the **highest** tier at 4,096.

### Measured, 2026-08-30 — and the answer is no on our current default

Actual byte sizes from `api/app/groot/`:

| Component | Bytes |
|---|---|
| Largest builtin skill body (`skills/plan/SKILL.md`) | 2,734 |
| Tool descriptions (`agent_tools.py` 2,596 + `picx_tools.py` 204 + `sandbox_tools.py` 432) | 3,232 |
| **Conservative floor (descriptions only, no JSON scaffolding)** | **5,966** |
| With JSON schema scaffolding for ~37 tool definitions | ~9,000 |

At 3.5–4 chars/token that is roughly **1,500–2,400 tokens**.

| Provider / model | Threshold | Verdict |
|---|---|---|
| Claude Sonnet 5 / 4.6, Opus 4.8 | 1,024 | ✅ clears comfortably |
| OpenAI GPT-5.6+ | 1,024 | ✅ clears |
| Gemini 2.5 Flash / Pro | 2,048 | ⚠️ marginal, right at the line |
| **Gemini 3.x Flash — our default** | **4,096** | ❌ **does not clear** |

**Conclusion: on `google/gemini-3.5-flash`, the caching flag would be on and do nothing.**
Reaching 4,096 tokens needs ~16 KB of stable prefix; we have 6–9 KB. The gap is far larger
than the estimate's error bars, so this holds even though it is a byte-based estimate rather
than a tokenizer count (no `tiktoken` in the venv — worth installing to confirm exactly).

So **the caching win requires moving off Gemini 3.x** to a 1,024-tier model — Claude Sonnet
being the obvious candidate. That converts "which default model" from a preference into a cost
decision with a measured answer, which is what `05-performance.md` said it should become.

Second finding, more useful than expected: **the tool descriptions (3,232 B) are larger than
the skill body (2,734 B).** The "skill body re-sent every iteration" framing understated the
waste — the tool schemas are re-sent too, and they are bigger. They are also naturally stable,
so `openrouter_cache_tool_definitions=True` is not a minor flag; it covers the single largest
component of the prefix.

So the caching fix is conditional, not automatic. Below the minimum, providers silently do
not cache — which is fail-safe (no write premium, no benefit) but means **the flag can be on
and do nothing**. Measure the actual prefix token count before claiming the win.

### The economics: caching pays off *within* a turn

Writes are not free. Anthropic: 5-min write = **1.25×** base input, 1-hour write = **2×**,
read = **0.1×** (90% off). OpenAI GPT-5.6+ now matches that structure (read 0.1×, write
1.25×) — the older "no write charge" behaviour applies only pre-5.6. Gemini's cached input is
~0.1× but **explicit** caching adds a per-hour storage charge ($1.00 / 1M tok / hr on 3.x
Flash, $4.50 on Pro).

For a 20-iteration turn with prefix `P`, holding the prefix byte-identical:

```
uncached :  20 × P × 1.00                      = 20.00 P
cached   :   1 × P × 1.25  +  19 × P × 0.10    =  3.15 P
                                                 ────────
                                                 ~84% saved on the prefix
```

The write premium amortises over the 19 reads. This is why P10 (identical static prefix
across iterations) is the load-bearing invariant: break it and you pay 1.25× twenty times
instead of once.

Caveat on TTL: Anthropic's clock starts at the **beginning of the request that writes the
entry**, and response generation counts against it. Default is 5 minutes. A 20-iteration turn
at 250–600 ms round trips plus generation time should fit, but a long turn that crosses the
TTL re-pays the write. Worth measuring; `'1h'` is available at 2× write if needed.

### Per-tier retrieval budget

Still ours, so memory cannot reintroduce the waste it exists to reduce:

| Tier | Cap |
|---|---|
| Semantic / style memory | 8 items / 400 tok |
| Episodic memory | 3 items / 600 tok |
| Skill find | 5 results / 150 tok |
| Skill body | 1 / 2,000 tok |

Ceiling ~3,150 tokens regardless of catalogue size.

### Two compaction semantics we must respect

`ProcessHistory` (a capability, `capabilities=[ProcessHistory(fn)]`; the legacy
`history_processors=` kwarg still auto-wraps) takes
`(messages: list[ModelMessage]) -> list[ModelMessage]`, optionally with `RunContext` first.
Two documented hazards:

- **Tool-call/tool-return pairs must stay intact.** Slicing history can orphan a
  `ToolCallPart` from its `ToolReturnPart`, which makes providers error
  ([#2050](https://github.com/pydantic/pydantic-ai/issues/2050)).
- **A compaction part is a visibility boundary.** "Tool discoveries and on-demand capability
  loads before the boundary reset, so later requests advertise them again"
  ([docs](https://pydantic.dev/docs/ai/capabilities/compaction/)). So a skill loaded before a
  boundary is re-advertised after it — `MAX_LOAD_SKILL_CALLS=3` must count *effective* loads
  across boundaries or a long run silently re-pays for the same skill.

### Tool-result bloat, and the restorability rule

"Tool returns persist in history as `ToolReturnParts`, so an oversized one is re-sent on
every later model request, paying its token cost for the rest of the run"
([Tool Output Limits](https://pydantic.dev/docs/ai/harness/tool-output-limits/)).

The convergent primary-source pattern is **reference + re-fetch**: never route a large blob
through the model. Return an opaque handle, and make every truncation *restorable* by
preserving the reference. Manus states the rule most sharply:

> "Our compression strategies are always designed to be **restorable**. For instance, the
> content of a web page can be dropped from the context **as long as the URL is preserved**…
> This allows Manus to shrink context length without permanently losing information."
> — [Context Engineering for AI Agents](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)

Anthropic's framing agrees, and names the safer of the two compaction forms: the risk is "the
loss of subtle but critical context whose importance only becomes apparent later," so
**tool-result clearing** (a result you can re-fetch) beats lossy summarisation
([Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)).

For a media agent this is unusually clean: a generation tool returns an **artifact id plus
metadata** (dimensions, model, cost, seed) — never image bytes, never a long URL list. The
artifact store holds the payload; context holds the id; the agent re-reads on demand. Because
the artifact is immutable and addressable, dropping it from context is lossless by
construction.

---

## The turn loop

```
  user message
      │
      ▼
 ①  RESOLVE ────── skill (id, or find_skill mid-run)
      │             Guard: strip terminal/bash, intersect allowlist (P3)
      │             via .filtered(lambda ctx, td: td.name in allowed)
      ▼
 ②  BUILD AGENT ── static instructions = [base, skill.body]  → cacheable prefix
      │             UsageLimits(cost_limit, request_limit, tool_calls_limit)
      ▼
 ③  OPEN RUN ────── Ledger.start() → run_id, base checkpoint
      │
      ├─────────────────── iteration ◄──────────────────────┐
      ▼                                                      │
 ④  ModelRequestNode      ProcessHistory + Compaction run    │
      │                   before the request (theirs)        │
      ▼                                                      │
 ⑤  CallToolsNode ── no tool calls ──► End                   │
      │                                                      │
      ▼                                                      │
 ⑥  METER: quote → reserve ── insufficient ──► HALT, no spend │
      │                                                      │
      ▼                                                      │
 ⑦  EXECUTE TOOL (idempotency key REQUIRED)                  │
      │                                                      │
      ├─ ok ─────► capture → Artifact → checkpoint ───────────┤
      ├─ retry ──► release reservation, backoff ──────────────┤
      └─ fatal ──► refund → checkpoint → HALT                 │
      │                                                      │
 ⑧  CANCEL / BUDGET CHECK ──────────────────────────────────┘
      │
      ▼
 ⑨  CLOSE RUN ──── final checkpoint, usage roll-up, events flushed
```

`agent.iter()` yields the graph nodes named above — `UserPromptNode`, `ModelRequestNode`,
`CallToolsNode`, `End` — with type guards (`Agent.is_model_request_node(node)`) and
`node.stream(run.ctx)` per node. That is our interception point.

**Termination**, in precedence order — flat `max_iterations=20` was only ever a proxy:

| # | Condition | Source |
|---|---|---|
| 1 | Cancelled | `agent.run(cancellation_token=…)` + our Ledger |
| 2 | Credit budget exhausted (cross-run, cross-worker) | **ours** — the named gap |
| 3 | `cost_limit` / `total_tokens_limit` / `tool_calls_limit` / `request_limit` | `UsageLimits` |
| 4 | No tool calls returned | Pydantic AI |
| 5 | Fatal tool failure | taxonomy below |
| 6 | Iteration cap | backstop only |

Note `request_limit` **defaults to 50**, not unlimited. And the field names are
`input_tokens_limit` / `output_tokens_limit` — `request_tokens_limit` /
`response_tokens_limit` are legacy V1 spellings.

---

## Failure → billing taxonomy

The crux, and absent from every generic durable-execution engine: a retry that re-runs a paid
step charges the user twice. Now mapped onto real exception types.

| Signal | Retry? | Reservation | Why |
|---|---|---|---|
| `ModelHTTPError` 429/5xx **before** the tool ran | yes — honour `.retry_after` | release | nothing spent |
| `ToolReturnPart.outcome == 'interrupted'` | **no** | **hold, reconcile** | the unsafe case — the generation may have succeeded |
| `ToolFailed` | no | refund | terminal, and deliberately does *not* consume retry budget |
| `ModelRetry` from a tool | yes | release | model-side correction, cheap |
| `ContentFilterError` | no | refund | deterministic; retrying burns money |
| `IncompleteToolCall` | yes | release | model hit token limit mid tool-call |
| `UsageLimitExceeded` | no | refund uncaptured | budget stop, not a failure |
| `RunCancelled` | no | refund uncaptured only | captured work was delivered |
| Process died mid-tool | no — resume | reconcile from checkpoint | the double-charge scenario |

Three things this table gains from the real API:

- **`ToolReturnPart.outcome` is a first-class enum**: `'success' | 'failed' | 'denied' |
  'interrupted'`. `'interrupted'` *is* the unknown-outcome case I flagged as the crux — it
  does not have to be invented.
- **`ToolFailed` vs `ModelRetry` is exactly our fatal/retryable split.** `ToolFailed` surfaces
  to the model as `outcome='failed'` without consuming retry budget; `ModelRetry` inserts a
  `RetryPromptPart` and does consume it.
- **`ModelHTTPError.retry_after`** parses the `Retry-After` header, so backoff is not guesswork.

### Not every 429 is retryable — the distinction that matters

Treating `ModelHTTPError.status_code == 429` as uniformly retryable is wrong and will hammer a
dead endpoint. Verified from provider docs:

| Signal | Retryable? | Source wording |
|---|---|---|
| OpenAI 429 rate limit | **yes** — honour `Retry-After` | "Pace your requests and follow the `Retry-After` header when it's present" |
| OpenAI 429 quota/spend (`insufficient_quota`, `credit_balance_exhausted`, spend-limit codes) | **no** | "**Retrying billing, spend, or quota errors won't restore API access.** Update the relevant credits or limits before sending another request." |
| Anthropic 429 **with** `retry-after` | **yes** | transient `rate_limit_error` |
| Anthropic 429 **without** `retry-after`, keeps failing | **no** | "A tier spend-cap 429 has no `retry-after` header and keeps failing until access resumes" |
| Anthropic 402 `billing_error`, 400, 401, 403, 404, 413 | **no** | fix the request or the billing |
| OpenAI 500 / 503, Anthropic 500 `api_error` / 529 `overloaded_error` | **yes**, exponential backoff | "Retry the request with exponential backoff" |

**Rule: the presence of `retry-after` on a 429 is the discriminator.** Absent it, treat a
repeating 429 as a spend cap — non-retryable until billing is resolved.

This bisects cleanly onto our own concern. A **provider** billing failure and a **user** credit
failure are both non-retryable, but they need opposite handling: the user's reservation must be
refunded and the user told; the provider's is an ops alert, and the user must still be refunded
because their work cannot proceed. Never retry either.

Two gotchas worth recording now:

- **Azure uses `retry-after-ms` (milliseconds), not the standard `Retry-After`.** If we ever
  route through Azure, a header reader that only knows `Retry-After` silently falls back to
  blind backoff.
- **Mid-stream SSE errors return HTTP 200 first, then an error event.** Standard retry
  machinery does not see them. Our streaming path must detect in-band stream errors explicitly,
  or a failed generation looks like a successful one — and a successful-looking failure is the
  worst possible case for a metered tool.


**Every metered call needs an idempotency key** so the reconciler can ask "did this specific
call already happen?" PicX already sends one at the API level; the architecture makes it
mandatory rather than incidental.

**Gotcha that constrains the Guard:** `ModelRetry` does **not** work inside `prepare=`
callbacks, `PrepareTools`, dynamic toolsets, or `before_model_request` — it propagates out of
the run unchanged. The Guard must fail closed by *omitting* a tool, never by raising
`ModelRetry`. There is a dedicated `on_tool_execute_error` hook for substituting a result or
raising `ModelRetry` at execution time.

---

## Durable execution, and the guarantee nobody can give you

Four officially supported — **Temporal, DBOS, Prefect, Restate** — plus Kitaru and Apache
Airflow via external SDKs
([overview](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)). The
current idiom is a **capability** (`TemporalDurability`, `DBOSDurability`); the older
`TemporalAgent` / `DBOSAgent` wrappers are deprecated and slated for removal in v3.

### The universal caveat, in their own words

Durable execution gives **exactly-once orchestration state** — the workflow replays
deterministically and never re-runs a completed step. It does **not** give exactly-once
*side effects*, because a worker can succeed at the external call and then crash before
recording that success. Every engine documents this, and Temporal's example is literally ours:

> "Activities follow an **at-least-once** execution model. If a Worker executes an Activity
> successfully but crashes before notifying the Temporal Service, the Activity will be
> retried. **Without idempotence, this could cause duplicate charges in payment processing**."
> — [Temporal, error handling](https://docs.temporal.io/develop/python/best-practices/error-handling)

DBOS states its tiers precisely:

> "Steps are tried **at least once** but are never re-executed after they complete… Transactions
> commit **exactly once**."
> — [DBOS, workflow tutorial](https://docs.dbos.dev/python/tutorials/workflow-tutorial)

A paid HTTP call lives in a *step*, not a transaction. So it is at-least-once on every engine.

**The honest guarantee is: at-least-once delivery + idempotent receiver = effectively-once.**
Never claim exactly-once for a paid call.

### The prescribed remedy — mint a stable key, push the guarantee downstream

All four converge on the same recipe, and it is the recipe Pikachu should implement once so no
consumer has to:

| Engine | Stable key source |
|---|---|
| Temporal | `f"{info.workflow_run_id}-{info.activity_id}"` — "remains constant across Activity retries but is unique among all Workflow Executions" |
| DBOS | the workflow ID — "if a workflow is called multiple times with the same ID, it executes only once" |
| Restate | `ctx.uuid()` — "seeded by the invocation ID, so they return the same result on retries"; plus a request-level `idempotency-key` header for engine-side dedup |

Downstream, Stripe defines the contract the provider side must honour
([docs](https://docs.stripe.com/api/idempotent_requests)): it stores "the resulting status code
and body of the first request… regardless of whether it succeeds or fails," returns the same
result on replay "**including 500 errors**," errors if the replayed parameters differ, and
prunes keys after **24 hours** — which sets the dedup window.

Fallback when a provider has no key support, in preference order:
1. **check-then-act** — an idempotent read to see whether it already happened. Temporal
   explicitly warns this has a **race window** if the first attempt outlives the timeout, so
   keys are strictly better.
2. **`operations` table** with `idempotency_key VARCHAR UNIQUE` and insert-first
   `ON CONFLICT DO NOTHING`; a constraint violation is treated as success, not a re-charge.
   Prune on a timestamp index.
3. **at-most-once** (`maximumAttempts=1`) plus a compensating saga — trades duplicates for
   possible zero executions.

### What this does to our thesis — a sharpening, not a retraction

I previously framed no-double-charge resume as unclaimed territory. That was too strong. The
*problem* is well documented by every engine, and they all prescribe the same fix.

What none of them provide is the **accounting**: a credit ledger, reserve/capture/refund
states, a cross-run and cross-worker budget, and reconciliation of the unknown-outcome case.
They hand you the pattern and leave it as homework — which is exactly what Pydantic AI's spend
doc says about money too. **Pikachu's claim is that it ships the prescribed pattern as a
runtime primitive with the ledger attached.** That is a weaker novelty claim and a much more
defensible product claim.

Reinforcing this: attaching `DBOSDurability` auto-wraps model requests and MCP comms, but
**your own tool functions are not wrapped** — you decorate them with `@DBOS.step` yourself.
Our paid media tools are exactly those functions. The engine will happily replay a step it does
not know cost money.

---

## Telemetry

**Finding that invalidates stale guidance: the GenAI semantic conventions moved repositories.**
They are no longer in `open-telemetry/semantic-conventions`; the old
`opentelemetry.io/docs/specs/semconv/gen-ai/*` pages now render a "Moved" stub. The
authoritative source is
[open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai)
(no tagged release yet; Schema URL still TODO).

**Everything GenAI-specific is `Development` stability.** There is no stable `gen_ai.*`
attribute. The only Stable attributes on a GenAI span are borrowed from core semconv:
`server.address`, `server.port`, `error.type`. So emit them, but do not build a contract on
them.

Span names we should emit, mapped to our layers:

| Our layer | Span name | Kind |
|---|---|---|
| Turn runtime | `invoke_agent {gen_ai.agent.name}` | INTERNAL (in-process) |
| Model call | `chat {gen_ai.request.model}` | CLIENT |
| Tool execution | `execute_tool {gen_ai.tool.name}` | INTERNAL |
| Memory tier | `search_memory` / `upsert_memory` / `update_memory` | INTERNAL |
| Skill find | `retrieval {gen_ai.data_source.id}` | CLIENT |

There is a whole **memory span family** (`create_memory_store`, `create_memory`,
`update_memory`, `upsert_memory`, `search_memory`, `delete_memory`, `delete_memory_store`) and
a `plan` operation — both map onto `04-memory.md` and the loop's resolve phase without
inventing anything. One rule to respect: `fetch_response` **SHOULD NOT** report token usage.

Why this matters beyond tidiness: `pydantic-evals` ships **span-based evaluators that inspect
tool calls and execution flow via OpenTelemetry traces**. Conventional spans are therefore the
*substrate for evaluation*, not just dashboards — and they are how we read cache-hit rate on
Gemini, where `RunUsage.cache_read_tokens` reads 0.

---

## Events

Pydantic AI already emits a rich stream — `PartStartEvent`, `PartDeltaEvent`, `PartEndEvent`,
`FinalResultEvent`, `FunctionToolCallEvent`, `FunctionToolResultEvent`,
`ToolAvailabilityDeltaEvent`, `DeferredToolRequestsEvent`, `AgentRunResultEvent` — with
`agent.run_stream_events()` as the entry point.

So we **extend**, not replace. Only two events are genuinely ours:

| Event | Why nobody else has it |
|---|---|
| `CostReserved` | shows spend before it lands — a paid-media UI needs this |
| `ArtifactProduced` | renders the artifact the moment it exists, not at end of turn |

`run_turn() -> tuple[str, list[dict]]` stays available by draining the stream, which keeps the
migration additive.

---

## Property invariants

Framework-blind, already written (43 tests), and they become the public contract:

| ID | Invariant |
|---|---|
| P3 | Effective toolset ⊆ fixed allowlist ∩ declared. A skill can only narrow its authority. |
| P5 | Every paid operation passes exactly one charging point, with refund on failure. |
| P7 | An agent instance is never shared across turns. |
| P9 | *(new)* Resume never re-captures an already-captured reservation. |
| P10 | *(new)* Static instruction bytes are identical across every iteration of one turn. |

P10 is now not just checkable but *measurable in production* — a turn whose
`RunUsage.cache_hit_ratio` is ~0 after the first iteration has a broken prefix.

Note: the adapter deliberately does **not** dedupe toolsets —
`test_p3_effective_toolsets_examples` pins `['web','web'] -> ['web','web']`. Do not add dedup
during the port; it breaks a documented property.

---

## Migration sequence

Unchanged and reversible. The seam is one abstract method (`GrootAgentBackend.run_turn`, 379
of 8,180 lines in `api/app/groot/`, lazily imported, `FakeBackend` already proving a
non-Hermes implementation works).

1. `PydanticAIBackend(GrootAgentBackend)` alongside `HermesBackend`, env-var selected.
2. A/B the 503 tests + P3/P5 property tests against both.
3. Baseline per-turn cost on both (`scripts/measure_turn_cost.py`), and record
   `cache_hit_ratio` — expect hermes ≈ 0.
4. Lift `pikachu/` out to its own repo, replacing PicX imports with protocol implementations.
5. Delete `HermesBackend`; drop `hermes-agent` and `agno` (both currently declared and
   installed).

---

## Verified vs not

**Verified** against live docs 2026-08-29: `Agent` kwargs (`deps_type`, `output_type`,
`system_prompt`, `instructions`, `toolsets`, `capabilities`, `retries`, `tool_timeout`);
static-before-dynamic instruction ordering for cache; `ProcessHistory` signature and
`before_model_request` relationship; compaction classes and visibility-boundary semantics;
`.filtered()` / `.prepared()` / `PrepareTools` / `ToolsPrepareFunc` and their ordering;
per-tool `prepare`; the full `UsageLimits` field list incl. `cost_limit` and
`request_limit=50` default; `RunUsage` fields incl. `cache_read_tokens`, `cache_write_tokens`,
`cache_hit_ratio`, `cost`; the exception hierarchy (`ModelRetry`, `ToolFailed`,
`ModelHTTPError.retry_after`, `UnexpectedModelBehavior`, `ContentFilterError`,
`IncompleteToolCall`, `UsageLimitExceeded`, `RunCancelled`); `ToolReturnPart.outcome` enum;
`ModelMessage` / part types; `agent.iter()` graph nodes and type guards; the streaming event
types; `OpenRouterModel` / `OpenRouterProvider` / `OpenRouterModelSettings` cache flags;
`CachePoint`; the four durable-execution integrations and the DBOS per-tool decoration
requirement.

**Scoped caveat:** [pydantic-ai #5205](https://github.com/pydantic/pydantic-ai/issues/5205) —
`cache_read_tokens` reads 0 for **Google implicit caching** even when the OTel span shows
cached tokens. The fields and `cache_hit_ratio` work generally; the gap is Gemini's implicit
path. Since the current default is `google/gemini-3.5-flash`, either read the OTel span or
move the default to a provider with explicit caching. That decision is now a cost lever, not
a preference.

**UNVERIFIED:** exact `Agent.__init__` type annotations; the Prefect capability class name;
whether the V1 wheel still accepts the legacy `request_tokens_limit` spelling.

**Research status: complete for this pass.** Context layering, prompt-caching mechanics and
thresholds, tool-output patterns, durable-execution idempotency, OTel GenAI conventions and the
eval harnesses have all been checked against primary sources. Fourteen research agents
reported; **one (`804660c5`) returned success but wrote a zero-byte result** — it was the
failure-taxonomy brief, which is already covered from Pydantic AI's own exception hierarchy, so
no gap remains.

**Three results remain unread on disk** — context-engineering layering
(`/Users/yash/.kiro/crew/subagents/84f8f278/result.txt`), compaction strategies
(`/Users/yash/.kiro/crew/subagents/433d7402/result.txt`), and context-management failure modes
(`/Users/yash/.kiro/crew/subagents/f7523178/result.txt`). They may refine compaction trigger
thresholds and the context-rot list; they will not change any number above.

**Still to verify before code:** exact `Agent.__init__` type annotations; the Prefect
capability class name; the `SkillLearnBench` licence and run command; and our own stable-prefix
token count, which decides whether caching fires at all on Gemini.
