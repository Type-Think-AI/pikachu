# 23 — Framework Speed Comparison, and Where the Time Actually Is

Measured 2026-08-30 on this machine, all three frameworks in **one venv** on one interpreter, so
the numbers are comparable. Reproduce with `scripts/compare_frameworks.py`.

---

## The comparison table

| Framework | Cold import | Model object | Agent instantiation | Share of a median turn |
|---|---:|---:|---:|---:|
| **Agno** 2.5.17 | **248.3 ms** | **1.37 µs** | **4.15 µs** (p95 4.25) | 0.00014% |
| **Pydantic AI** 2.36.0 | 298.0 ms | 5.06 µs | 237.25 µs (p95 363.92) | 0.00816% |
| **OpenAI Agents SDK** 0.22.0 | 842.5 ms | n/a (model is a string) | 64.50 µs (p95 77.46) | 0.00222% |
| **Pikachu** (own layer) | — | — | 96.10 µs (parse+scan+guard+build) | 0.00331% |

## Agno is genuinely faster, and the margin is large

**Agno instantiates an agent ~57× faster than Pydantic AI** — 4.15 µs against 237.25 µs — and
imports 50 ms quicker. That is not marketing; it reproduces here on identical hardware in the same
interpreter. The OpenAI Agents SDK sits in between on instantiation but is **2.8× slower to import**
than Agno, which matters for cold starts.

So the claim is true. The question is whether it is *relevant*, and the honest answer is no:

```
Agno agent instantiation          4 µs
Pydantic AI agent instantiation 237 µs
difference                      233 µs

measured provider wait, same prompt, same model:
  min 2,345 ms · median 2,907 ms · max 3,336 ms
run-to-run spread                991 ms  =  991,000 µs
```

**The provider's own variance on identical requests is ~4,250× the entire difference between the
two frameworks.** Choosing Agno to save 233 µs, on a workload where the network alone varies by a
second, is optimising the wrong term by four orders of magnitude.

This is exactly what constraint C2 in `docs/09-design-constraints.md` predicted, and it is now
measured rather than asserted: *"Explicitly not a goal: winning instantiation benchmarks."*

### Where cold import DOES matter

One row is genuinely actionable: **cold import**. Our measured 236 ms first-call setup is mostly
`import pydantic_ai` at 298 ms (lazy, so it lands on the first turn). In a long-lived server this
is paid once and is irrelevant. In a **serverless or per-request process** it is paid on every cold
start, and there Agno's 50 ms advantage — and the OpenAI SDK's 544 ms penalty — is real.

Since Pikachu targets a long-running service, this does not change the framework choice. If it ever
runs per-request, revisit.

---

## The finding worth acting on: our own overhead is not our code

Our per-turn framework cost decomposes as:

| Component | Cost | Share of framework |
|---|---:|---:|
| **Pydantic AI `Agent()` construction** | **237 µs** | **69%** |
| Pikachu: injection scan | 55 µs | 16% |
| Pikachu: skill load + parse | 15 µs | 4% |
| Pydantic AI model object | 5 µs | 1% |
| Pikachu: guard, AgentSpec, TurnRequest | 6 µs | 2% |
| — total | ~338 µs | |

**The single largest framework cost is Pydantic AI constructing an `Agent`, not anything we
wrote.** And we pay it every turn because **P7 forbids sharing an agent instance across turns**.

Could we cache it? Technically yes — Pydantic AI agents hold no per-run state, history is passed
per call. But P7 is a *safety* invariant carried over from the parent system, and trading a
recorded safety invariant for 237 µs — 0.008% of a turn — is a bad trade at any exchange rate.
**Recommendation: do not touch it.** Revisit only if a measurement ever shows framework share above
a few percent.

---

## How Agno is actually fast — mechanism, measured

Profiled 2026-08-30 by reading the installed source and timing construction against tool count.
This is the part worth learning from, and it is a single design decision.

### Pydantic AI does the work eagerly; Agno defers it

| Tools passed at construction | Agno | Pydantic AI |
|---|---:|---:|
| 0 | 4.04 µs | ~25 µs |
| 2 | 4.23 µs | 390.9 µs |
| 10 | 4.42 µs | ~1,860 µs (extrapolated) |
| **marginal cost per tool** | **0.04 µs** | **~183 µs** |

Agno's per-tool construction cost is essentially zero, and the profile of Pydantic AI's
`Agent.__init__` says exactly where its 183 µs goes:

```
add_function                    85% of construction
  function_schema
    _griffe.doc_descriptions    28%   ← re-parses the tool's DOCSTRING
    _infer_docstring_style      23%   ← 17,100 genexpr calls, for ONE tool
  pydantic json_schema build    19%
```

**Pydantic AI generates each tool's JSON schema and re-parses its docstring with `griffe` every
time an `Agent` is constructed.** Agno stores the raw callables and builds nothing — inspecting a
freshly constructed Agno agent shows `tools` populated but no computed schema — so the work happens
later, at the model call.

Both are plain dataclasses. Pydantic AI even has `__slots__` and Agno does not. **The 57× gap is
not about object layout, validation, or Pydantic — it is entirely eager-vs-deferred schema
generation.**

### The honest caveat: Agno moves the cost, it does not remove it

The schema *must* exist before the first request. Agno pays it on the run instead of the
constructor, where it disappears into network latency. So a large part of the instantiation gap is
an **accounting difference**, not work avoided — which is another reason the benchmark cannot decide
a framework choice.

---

## What we changed as a result — 16× on our own overhead

The lesson transfers, and it gives something better than either default: **do the eager work once
and keep it.**

P7 forbids sharing an *agent* across turns, so we build a fresh one every turn — and were therefore
regenerating identical tool schemas on every single turn. A toolset is not an agent, so caching the
**toolset** satisfies P7 while making schema generation one-time.

| Variant | Mean | Median | p95 |
|---|---:|---:|---:|
| `tools=[callables]` — before | 390.9 µs | 366.2 µs | 515.9 µs |
| `toolsets=[prebuilt]` — after | **24.5 µs** | 24.2 µs | 25.1 µs |
| no tools at all (floor) | 25.1 µs | 24.7 µs | 26.6 µs |

**16× faster, and it lands exactly on the no-tools floor** — schema reuse removes 100% of the tool
cost, which was 94% of construction. Cached lookup through our backend measures **0.24 µs**.

Implemented in `backends/pydantic_ai.py::_toolset_for`, keyed by the **exact tuple of permitted
tool names**. That key choice is the security-relevant part: a looser key (agent name, say) could
hand a narrowly-permitted run a toolset built for a wider allowlist, which would be a P3 violation
wearing a performance costume. `tests/test_toolset_cache.py` pins it, including that a different
permission set never shares a toolset and that order/duplicates are part of the key.

### Updated table

| | Agno | Pydantic AI raw | Pikachu before | **Pikachu after** |
|---|---:|---:|---:|---:|
| construction, 2 tools | 4.2 µs | 390.9 µs | 390.9 µs | **24.5 µs** |
| per-tool marginal | 0.04 µs | 183 µs | 183 µs | **~0** |
| when schemas are built | per run | per construction | per turn | **once, cached** |

We remain ~6× slower than Agno at instantiation and are now within the same order of magnitude. On
the axis that actually matters — schema work per turn — the cache is better than Agno's deferral,
because deferral still repeats the work on every run.

---

## Provider routing: tested, and it does NOT pay ✗

Recorded here as a **negative result**, because an earlier version of this document called it "the
highest-value untested experiment available". It has now been tested and that was wrong.

`google/gemini-3.7-flash` has six endpoints — `google-ai-studio` and `google-vertex/global`, each in
standard, `/flex` (half price) and `/priority` (1.8×) tiers.

**First run, 4 samples per config, round-robin:** `sort=latency` looked like **−14.4%**.

**Second run, 8 attempts per config on the one promising config:**

| Config | n | min | median | mean | max | stdev |
|---|---:|---:|---:|---:|---:|---:|
| default | 6 | 2167 | 2584 | 2814 | 4057 | **716** |
| `sort=latency` | 5 | 1964 | 2439 | 2695 | 3761 | **699** |

Median delta **−5.6% (145 ms)** against a within-group spread of **1,890 ms**. The difference is
**well inside the noise**, and the first run's −14.4% was a small-sample artifact.

**Conclusion: provider routing gave no measurable improvement.** The ~700 ms standard deviation is
present *identically* on both configs, so the variance is inherent to the provider path rather than
a choice between fast and slow endpoints. There was no queue to route around.

### Why `/flex` and `/priority` could not be tested

Both failed all attempts with:

```
403 PERMISSION_DENIED — "Your project has been denied access."
provider_name: Google AI Studio,  is_byok: True
```

**`is_byok: True`** — the account routes Gemini through a *bring-your-own-key* Google credential, and
that project lacks entitlement to the flex and priority tiers. So those tiers are unavailable to
this account, not misconfigured routing. If lower latency is wanted, obtaining priority-tier access
on the Google project is the prerequisite, and only then is it worth re-measuring.

### Methodology notes, so this is re-runnable honestly

- **Round-robin, never blocked per config.** Running all of config A then all of config B lets a
  transient network slowdown bias one group entirely.
- **`served_by` reported only `openrouter`**, so the *actual* serving endpoint could not be
  confirmed. The default-vs-`sort=latency` A/B is still valid (same mechanism, same account), but no
  per-endpoint claim can be made from this data.
- **n must be ≫10** to detect anything under ~700 ms on this path. Four samples cannot.

### Revised latency ranking

| Lever | Worth | Status |
|---|---|---|
| Avoid a round trip | ~2,900 ms each | **the only large win**; lower `max_iterations`, batch tool calls |
| Prompt caching | part of prefill | unmeasured, `CACHE_FLOOR_UNVERIFIED` still `True` |
| Provider routing | ~0 | **tested, no effect** |
| Priority tier | unknown | **blocked** — BYOK project lacks entitlement |
| Output size | ~1% | decode is ~4,900 tok/s |
| Framework code | 366 µs, now saved | done, via the toolset cache |


Ranked by how much wall clock each can remove from a real turn.

### 1. Round trips — worth ~2,900 ms each ★★★

Every extra iteration is a **complete provider wait**. From the live report:

| Task | Iterations | Total |
|---|---:|---:|
| 01 basic turn | 1 | 2,524 ms |
| 04 tool call | **2** | **3,931 ms** |

One extra iteration cost ~1,400 ms in that sample, and the median wait is 2,907 ms. So a single
avoided round trip is worth roughly **8,600× the entire framework overhead**.

Concrete actions:
- **Lower `max_iterations` from 20.** It is already flagged as above the production norm (68% of
  production agents cap at ≤10). Twenty iterations is a worst case of ~60 s.
- **Batch tool calls.** If a turn needs three independent tool results, request them in one
  response rather than three sequential rounds.
- **Give tools better descriptions** so the model picks correctly first time. A wrong tool choice
  costs a full round trip — which is why the confusability work is a *latency* feature as well as
  an accuracy one.

### 2. Provider routing — worth up to ~1,000 ms ★★★

The 991 ms spread between the fastest and slowest identical request is **queueing, not model
capability**. OpenRouter exposes provider routing preferences; pinning to a less contended provider
or a nearer region attacks the single largest variable term. Untested here and the highest-value
experiment available.

### 3. Prompt caching — worth part of prefill ★★

`wait` includes input prefill, and caching removes most of it on repeat turns. Currently
`cache_read_tokens` is 0 and `CACHE_FLOOR_UNVERIFIED` is still `True`, so the size of this win is
unknown. Note it only shrinks *prefill*, not queue time, so it is smaller than it looks when queue
dominates.

### 4. Output size — worth ~0.97% ✗

`stream` totalled 204 ms against 11,525 ms of `wait` — decode ran at ~4,900 tok/s. Asking for
shorter answers is nearly free of benefit here. **Streaming to the user also barely helps perceived
latency**, because the wait is *before* the first token, not during delivery.

### 5. Framework code — worth ~338 µs ✗

Optimising anything in this repo. Included to be explicit that it is the last thing to touch.

---

## What to choose a framework on instead

Since speed cannot separate them at this scale, the axes that actually differ:

| | Agno | Pydantic AI | OpenAI Agents SDK |
|---|---|---|---|
| Instantiation speed | ★★★ | ★ | ★★ |
| Cold import | ★★★ | ★★ | ★ |
| Type safety / validation | ★★ | ★★★ (Pydantic-native) | ★★ |
| Agent Skills support | — | ★★★ first-party | — |
| MCP client | ★★ | ★★★ (wraps `mcp`/`fastmcp`) | ★★★ (depends on `mcp`) |
| Durable execution | ★ | ★★★ (Temporal/DBOS/Prefect/Restate) | ★ |
| Stated API stability | ★★ | ★★★ | ★★ |
| Multi-agent orchestration | ★★★ (4 team modes) | ★ (by design) | ★★ |

Pydantic AI remains the right choice here on **Agent Skills, durable execution and type safety** —
all of which are load-bearing for this product — and it loses only on a term that measurement shows
to be irrelevant. Agno is the better pick for a cold-start-sensitive or orchestration-heavy
workload.

Constraint C5 stands: exactly one agent framework in our dependencies.

---

## Bonus finding: the MCP revision question is settled

The comparison venv pulled `mcp 2.1.1` transitively via `openai-agents`, which let us check
directly:

```
LATEST_PROTOCOL_VERSION:     2026-07-28
DEFAULT_NEGOTIATED_VERSION:  2025-03-26
```

This closes Q1 in `docs/22-phase0-verification.md`. Pydantic AI wraps the `mcp` SDK rather than
implementing the protocol, and **that SDK does speak 2026-07-28** — so the `mcp/` module is not
blocked.

**But note the second line.** The *default negotiated* revision is `2025-03-26`, three revisions
behind. A client that does not explicitly request `2026-07-28` will silently negotiate the old one
and lose statelessness, `server/discover` and `resultType`. So Lane I must **assert the negotiated
revision in a test**, not assume it — the failure mode is silent downgrade, not an error.
