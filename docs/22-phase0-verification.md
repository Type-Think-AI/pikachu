# 22 — Phase 0 Verification

The blocking gate before any standards code. Four questions, all answered 2026-08-30 by direct
inspection of the installed package, a schema fetch, and provider docs.

**Provenance note.** Lane E gathered the Q1/Q4 evidence, then spawned a child for Q3 and ended its
turn to wait for it — but a sub-agent is never resumed by a completion event, so it never returned to
write anything. Q1/Q4 were re-derived here by running the introspection directly rather than trusting
a recovered narrative; Q2 was never attempted by the lane and was done here; Q3 is the child's work,
read in full. `scripts/verify_pydantic_ai.py` exists so none of this has to be recovered from a
transcript again.

---

## Q1 — Does Pydantic AI 2.36.0 speak MCP 2026-07-28?

**Verdict: not directly, and it does not need to. Not a blocker for `mcp/`, but the dependency story
changes.**

`pydantic_ai/mcp.py` contains exactly one protocol revision string: **`2025-11-25`**. No file
anywhere in the venv mentions `2026-07-28`.

But the module is **a wrapper over `fastmcp` + the `mcp` SDK — it does not implement the protocol
itself.** So the revision actually spoken is whichever the installed SDK speaks. Neither `fastmcp`
nor `mcp` nor `fastmcp_tasks` is currently installed:

```
MCP REVISION
  mcp: NOT INSTALLED
  fastmcp: NOT INSTALLED
  fastmcp_tasks: NOT INSTALLED
```

And it is clearly modern-era aware. Grepping `pydantic_ai/*.py` finds **SEP-2575** (statelessness),
**SEP-1686** (tasks) and **SEP-2663**. Lane E's reading also found `input_required` task parking,
modern-session detection (`modern_session = init_result is None`), and explicit refusal of
sampling/elicitation/logging on a modern session — which is the 2026-07-28 feature set, delegated to
the SDK.

### What Lane I must do

1. Add the MCP dependency explicitly — the `[mcp-tasks]` extra pulls `fastmcp-slim>=4.0.0b1`. The
   Python `mcp` SDK advertises `2025-06-18`, `2025-11-25` and `2026-07-28`; FastMCP 4 targets
   `2026-07-28`.
2. **Assert the negotiated revision at the SDK boundary, in a test.** Do not infer it from
   pydantic-ai's version. A wrapper whose docstrings lag the SDK is expected and fine; silently
   speaking an older revision is not.
3. Do not build a shim. There is nothing to shim — the protocol layer is the SDK's job.

### ✅ RESOLVED 2026-08-30 — and requirement 2 turns out to be essential

`mcp 2.1.1` was installed into the framework-comparison venv (as a transitive dependency of
`openai-agents`) and inspected directly:

```
mcp.types.LATEST_PROTOCOL_VERSION:     2026-07-28
mcp.types.DEFAULT_NEGOTIATED_VERSION:  2025-03-26
```

So **2026-07-28 is supported** — `mcp/` is not blocked, and the verdict above holds.

The second line is the important one and it was not anticipated: the **default negotiated revision
is `2025-03-26`**, three revisions behind. A client that does not explicitly ask for `2026-07-28`
silently negotiates the old revision and loses statelessness, required `server/discover`, and
`resultType`. The failure mode is a **silent downgrade, not an error**, which is exactly the kind of
thing a conformance test exists to catch. Requirement 2 above is therefore mandatory rather than
good practice.

---

## Q2 — Agent Plugins 1.0.0 schema, field by field

**Verdict: fetched and parsed. Our claims were right about the required fields and wrong about the
escape hatch.**

`https://agent-plugins.org/schemas/1.0.0/plugin.schema.json` → 200, JSON Schema draft 2020-12, title
"Agent Plugins Manifest".

| Field | Required | Type | Constraint |
|---|---|---|---|
| `$schema` | **yes** | string | **`const`** — must be *literally* `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json` |
| `name` | **yes** | string | `^(?!.*(?:--\|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$` |
| `version` | no | string | |
| `description` | no | string | |
| `author` | no | object | |
| `homepage` | no | string | |
| `repository` | no | string | |
| `license` | no | string | |
| `keywords` | no | array | |
| `extensions` | no | object | |

**`additionalProperties: false`.** The manifest is **closed**.

Three consequences:

1. **`$schema` is `const`, not merely a URL.** Any other value — including a different version path —
   fails validation. A conformance test should assert the exact string.
2. **The reverse-DNS escape hatch cannot be a top-level key.** `docs/17-standards-and-interop.md`
   describes a `com.example.client/` escape hatch; with `additionalProperties: false` a top-level
   `com.example.client` key would make the manifest **invalid**. It must go inside `extensions`.
   Correct that doc.
3. **`skills` and `mcp` are not manifest fields at all** — they are directory/file conventions
   (`skills/`, `mcp.json`) sitting beside `plugin.json`. This confirms the recorded claim that
   `plugin.json` cannot be relocated or inlined, and it reinforces the independent-component-failure
   requirement: a broken `mcp.json` must not take `skills/` down, because the manifest does not
   couple them.

The `name` pattern is worth reading closely: the negative lookahead forbids `--` and `..`, which is a
path-traversal and confusable-name defence. Our own skill names should adopt the same rule.

---

## Q3 — Default model, and the cache floor

**Verdict: recommend `anthropic/claude-sonnet-4.5`. And two numbers recorded elsewhere in these docs
were wrong.**

| Model | Min cacheable prefix | Fires at our 1.5–2.4K prefix? |
|---|---|---|
| DeepSeek V3.2 / V4 | **~128** (64-token chunks) | ✅ lowest of any provider |
| Claude Opus 5 / Fable 5 / Mythos 5 | **512** | ✅ |
| Claude Sonnet 4.5 / 4.6 | **1,024** | ✅ |
| OpenAI GPT 4.1/4o/5-class | **1,024** | ✅ |
| **Gemini 2.5 Flash** | **1,024** | ✅ |
| Gemini 2.5 Pro | 2,048 (Vertex/OpenRouter cite up to 4,096) | ⚠️ only ≥2,048 |
| Claude Opus 4.7 | 2,048 | ⚠️ only ≥2,048 |
| Haiku 4.5, Opus 4.5/4.6 | 4,096 | ❌ |
| **Gemini 3.x Flash** | reported ~4,096, **unconfirmed for this version** | ❌ presumed |

### Two corrections to previously recorded numbers

1. **"Gemini 2.5 = 2,048 ⚠️" was wrong.** The floor splits by variant: **2.5 Flash is 1,024 and
   fires**; 2.5 Pro is the 2,048–4,096 one. So "stay in the Gemini family" is a *viable minimal-change
   fix* — `google/gemini-2.5-flash` needs no provider change and caching engages. That option was
   incorrectly ruled out.
2. **"Gemini 3.x Flash = 4,096 ❌" is not confirmed for that exact model.** It is inferred from
   OpenRouter's blanket statement that *"Gemini models typically have a 4096 token minimum for cache
   write to occur"*. No per-version published floor for `gemini-3.5-flash` could be found. The
   blocking decision "must move off Gemini or S1 is unreachable" therefore rests on a **plausible
   inference, not a measured fact** — the honest test is to run one turn and read `cache_read_tokens`.

### The arithmetic (20-iteration turn, 2,000-token prefix, Sonnet 4.5 at $3/MTok in)

```
no caching : 20 × 2,000 = 40,000 tok × $3.00/MTok            = $0.1200
caching    : 1 write  ×  2,000 × $3.75/MTok (1.25×) = $0.0075
             19 reads × 2,000 × $0.30/MTok (0.10×)  = $0.0114
                                                      total   = $0.0189
```

**≈84% off prefix input, 6.3× cheaper.** Break-even is immediate — one read at 0.10× repays the 0.25×
write surcharge. Write premium is 1.25× at 5-min TTL, 2× at 1-hour; read is 0.10×.

Cost is explicitly not the deciding factor for this project, so the recommendation rests on **cache
floor fit plus capability**: Sonnet-class is frontier for tool use and instruction following, its
automatic top-level breakpoint advances as the conversation grows, and `cache_read_input_tokens` /
`cache_creation_input_tokens` make it unambiguous whether caching fired.

**Runner-up:** `google/gemini-2.5-flash` — the minimal-change option, same family, 1,024 floor,
zero-config implicit caching, weaker on hard agent tasks.

**Not recommended but noted:** DeepSeek has the lowest floor (~128) and cheapest reads (0.10×). If
capability suffices it is the cheapest correct answer, and cost is not why we would pick it.

**Unverified:** the exact Gemini implicit-cache read multiplier (redacted in OpenRouter's docs; ~0.25×
is widely cited), and token floors for Grok / Moonshot / Groq / Z.AI, which document automatic caching
without publishing a minimum.

---

## Q4 — Does the assumed Pydantic AI surface still exist under V2?

**Verdict: yes, entirely. Zero drift. This is the best possible result and it was not guaranteed.**

The design docs argued stability on "V1 since Sept 2025, no breaking changes until V2" — and V2 has
shipped (2.36.0), which invalidated the argument, not necessarily the API. Running
`scripts/verify_pydantic_ai.py`: **all 17 symbols and all 11 fields present.**

Highlights that confirm earlier corrections were right:

- **`request_tokens_limit` is gone**, `input_tokens_limit`/`output_tokens_limit` are present — the V1
  name was correctly retired in the docs.
- **`UsageLimits().request_limit == 50`** — the recorded default is exact. This matters: a 20-iteration
  turn sits under it, but a longer one would hit a limit nobody set deliberately.
- `RunUsage` carries `cache_read_tokens`, `cache_write_tokens`, `cost` **and** `cache_hit_ratio`.
- `CachePoint`, `OpenRouterProvider`, `OpenAIChatModel`, `ToolDefinition`, `ToolRetryError` all present.
- `FunctionToolset` exposes **`.filtered()`, `.prepared()`, `.renamed()`**; there is no `filter_tools`.
  `.filtered()` remains the cleanest P3 mechanism.

Re-run the script after any dependency bump — it exits 1 if anything assumed goes missing, so it can
gate the upgrade.

---

## What this changes

| Doc | Correction |
|---|---|
| `docs/17-standards-and-interop.md` | The reverse-DNS escape hatch must live under `extensions`, **not** as a top-level key — `additionalProperties: false`. `$schema` is a `const`, not just a URL. |
| `docs/05-performance.md`, `PRD.md` | Gemini **2.5 Flash is 1,024 and does cache**; only 2.5 Pro is 2,048+. `gemini-3.5-flash`'s 4,096 floor is an **unconfirmed inference**, so S1's blocker is presumed rather than measured. |
| `docs/18-module-map-and-roadmap.md` | `mcp/` depends on `fastmcp-slim`/`mcp`, not on pydantic-ai's own revision. Assert the negotiated revision in a test at the SDK boundary. |
| `PRD.md` §9 | Open decision 2 (default model) now has a recommendation: `anthropic/claude-sonnet-4.5`. Still the owner's call. |
| — | Skill names should adopt Agent Plugins' `name` pattern, including the `--`/`..` lookahead. |
