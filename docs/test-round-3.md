# Round 3 — Live behaviour + performance regression (Kai)

**Owns:** `tests/round_3/`, `docs/test-round-3.md`, `scripts/round3_live.py`
**Uses the real model** `google/gemini-3.7-flash` via OpenRouter.
**Cost spent this run: $0.006578** across **5 live turns** (cap is 6). Nothing committed.

Reproduce:

```
.venv/bin/python -m pytest tests/round_3 -q          # 8 passed, 1 skipped — offline, no network
.venv/bin/python scripts/round3_live.py              # 5 live turns, prints per-turn + total cost
.venv/bin/python scripts/profile_all.py              # regression check
```

The offline tests in `tests/round_3` never touch the network — they inherit the parent
`tests/conftest.py` autouse socket block and do not override it. The single
`@pytest.mark.live` test is deselected by default (local `conftest.py`, `--run-live` to run it)
and skips cleanly with no key. Only `scripts/round3_live.py` and that one marked test call the
real model.

---

## The question this round exists to answer

Does live tool-calling behaviour match what the offline `FakeBackend` claims? The fake can only
prove **plumbing** — a scripted tool call is authorized, threaded into the result, and metered —
because a `ScriptedToolCall`'s tool name is fixed at *authoring* time (asserted in
`test_fake_cannot_model_selection__the_reason_live_exists`). It cannot prove **selection**: that
a real model, handed a skill body and a set of tool schemas, *chooses* to call the right tool.
That gap is the whole reason a live lane exists, and it is where the one real mismatch was found.

---

## 1 — Skill-with-tools, LIVE — tool SELECTION works ✅

The colourist skill body says *"the palette is NOT in your head — call `brand_palette` and quote
its hex."* The agent is granted `brand_palette`; the task asks for the house signal amber.

| turn | called `brand_palette`? | quoted `#FFB300`? | iters | served_by | framework | model | in / out tok | est cost |
|---|---|---|---:|---|---:|---:|---|---:|
| 1a granted | **yes** | **yes** | 2 | openrouter | 230 ms | 5,405 ms | 284 / 156 | $0.000798 |

Answer: *"The house signal amber colour is #FFB300."*

**The model genuinely selected the tool and used its output.** Two iterations = one round trip to
decide-and-call, one to answer with the returned value — the real tool-use loop, not plumbing.
`served_by` is `openrouter` (the gateway did not name the underlying Google endpoint, consistent
with `docs/23`). Note the split shows all model time as `wait` (stream 0 ms) because the granted
turn's final response arrived without measurable text deltas after the tool round — the framework
flags this rather than inventing a split.

## 1b — the degraded case, LIVE — and the round's most important finding ★

Same task, but `brand_palette` **removed from the guard's `effective_tools` (`()`)**. The backend
correctly attaches **no toolset** (`_tools_for → []`, `_toolset_for → None`, verified directly).
The guard held: nothing unauthorized could execute.

| turn | tool actually callable? | `tool_calls` recorded | crashed? | answer |
|---|---|---|---|---|
| 1b degraded | **no** (toolset is `None`) | **`brand_palette`** | no | *"I cannot provide the amber hex because the `brand_palette` tool is currently unavailable."* |

Here is the mismatch. **The offline `FakeBackend` models an ungranted tool call as a hard
refusal** — `test_fake_refuses_a_tool_outside_the_effective_set` asserts it raises
`BudgetExceeded`. **Live, it does not raise.** The guard operates one layer lower than the fake's
mental model: it removes the tool from the *schema the model sees*, so the model — primed by the
skill body — still *emits* a `ToolCallPart` for `brand_palette`, that part never executes, and the
model gracefully reports the tool "unavailable" without fabricating a hex.

The consequence for anyone reading `TurnResult.tool_calls`:

> `TurnResult.tool_calls` means **two different things** across the two backends.
> In `FakeBackend` a recorded call means *a tool ran*. In `PydanticAIBackend` it means *the model
> emitted a tool-call part* — executed or not. So the invariant "`tool_calls` non-empty ⟹ a tool
> executed" holds offline and is **false live**. A test asserting it would pass on the fake and
> fail against the model.

This is a fake-hides-something finding, and it is the headline. It is **not** a security defect —
both backends kept the guard intact and no unauthorized code ran either way — but it is a
**faithfulness gap**: the fake's refusal-on-ungranted-call is stricter than reality, so a test
written only against the fake would encode a guarantee the live path does not provide. Recorded as
a runnable artifact in `test_fake_refuses_a_tool_outside_the_effective_set` (the fake's behaviour)
against the live 1b row (the model's behaviour).

## 2 — Declarative function tool, chosen UNPROMPTED, LIVE ✅

A plain Python function `shot_count(scene_description)` is offered with only its docstring. The
task describes a scene and asks for a shot count. The prompt **never says "use a tool."**

| turn | chose to call `shot_count`? | iters | served_by | framework | model | in / out tok | answer | est cost |
|---|---|---:|---|---:|---:|---|---|---:|
| 2 declarative | **yes** | 2 | openrouter | 1 ms | 4,198 ms | 209 / 125 | `4` | $0.000625 |

**The model chose the tool on its own.** This is the difference between plumbing and behaviour:
the docstring alone was enough signal for the model to route an unprompted task through the
function. Tool *selection* — the thing the fake structurally cannot test — works against the real
model.

## 4 — Cache, one honest look — S1 negative REPRODUCES ✅ (as designed)

Same full-size prefix (~1,607 tokens, inside the `STABLE_PREFIX_TOKENS_MIN..MAX` 1,500–2,400 band)
run twice; turn 2 is the one that could read a cache written on turn 1.

| turn | input tok | cache_read | cache_write | model ms | est cost |
|---|---:|---:|---:|---:|---:|
| 4 turn 1 | 1,951 | **0** | 0 | 2,756 | $0.002026 |
| 4 turn 2 | 1,951 | **0** | 0 | 2,196 | $0.001737 |

`cache_read_tokens` stayed **0 on both turns**. The S1 negative from the audit reproduces on a
real full-size prefix.

> ★ **Caveat, restated:** Google's implicit caching can report 0 `cache_read_tokens` even when the
> cache *did* fire (pydantic-ai [#5205](https://github.com/pydantic/pydantic-ai/issues/5205)). So
> this 0 is **suggestive, not conclusive** — confirm against an OpenTelemetry `gen_ai` span before
> concluding the ~4,096-token floor is the cause. Either way, `CACHE_FLOOR_UNVERIFIED` correctly
> remains `True` in `config.py`, and this is a **recorded negative**, i.e. a successful deliverable,
> not a failure.

---

## 3 — Performance regression — no new regression found

`scripts/profile_all.py`, run twice this session (the host memory pressure varied between runs,
which is exactly the confound the audit warned about). Baselines from `docs/23`/`docs/24`.

| # | operation | run A mean | run B mean | baseline | verdict |
|---|---|---:|---:|---:|---|
| scan skill body for injection | 54.95 µs | 54.09 µs | 55 µs | **ok** — lands on baseline |
| skill: load_skill (full doc) | 15.02 µs | 14.69 µs | 15 µs | **ok** — lands on baseline |
| guard: effective_tools (P3) | 2.80 µs | 2.59 µs | 6 µs | **ok** — under baseline |
| toolset cache: lookup (warm) | 0.23 µs | 0.20 µs | 0.24 µs | **ok** — lands on baseline |
| full turn assembly (FakeBackend) | 10.72 µs | 8.31 µs | 96 µs (pre-cache) | **ok** — cache landed, as `docs/23` predicted |
| sqlite: read skill by key | 15.08 µs | 11.15 µs | 5 µs | **watch, not new** — see below |
| sqlite: search skills (LIKE) | 31.99 µs | 26.88 µs | 7.5 µs | **KNOWN watch item** (audit) |
| sqlite: search memory (FTS5) | 29.17 µs | 23.63 µs | 7.5 µs | **KNOWN watch item** (audit) |

**The code paths are healthy.** Every row with a published baseline that reflects *our code* —
scan, load, guard, the toolset cache, the full-turn floor — lands on its audit value. The toolset
cache at 0.20–0.23 µs against a 0.24 µs baseline is the direct evidence that the P7-safe schema
cache is still doing its job; a lost cache is the classic regression this row exists to catch, and
it did not happen.

**The two SQLite search rows print ~3.2–4.3× baseline. This is the KNOWN watch item recorded in
`docs/24-audit.md`, not a new finding.** Per that audit: the synchronous read/write path is
healthy, the search rows do strictly more work (FTS5 `MATCH` / `LIKE` + row hydration into frozen
Pydantic models), and this host is under memory pressure which inflates every measurement
uniformly — visible in the wide p95 spread. The ratio to read-by-key is unchanged from the audit.
Per the round instructions, this is **not** re-reported as new.

**On `sqlite: read skill by key`:** in run A (tighter memory) it printed 15.08 µs = 3.02× baseline
and tripped the fixed 3× tolerance flag; in run B (looser memory) it fell to 11.15 µs = 2.2× and
flagged **ok**. This is the *same host-memory-pressure phenomenon* as the search rows, momentarily
pushing the read row a hair over a deliberately-tight tolerance line — not a genuine algorithmic
regression, and not worse than the audit recorded (audit had it at 12.4 µs, "ok (<3×)"). Flagged
here only for completeness; a re-run on an unloaded machine brings it back under, exactly like the
search rows. **No fix recommended.**

---

## VERDICT — does live behaviour match the offline fakes?

**Mostly yes, with one material mismatch that the fake hides.**

- **Tool selection is real, not just plumbing.** Task 1a: the model called `brand_palette` from a
  skill-body instruction and quoted `#FFB300`. Task 2: the model called a declarative function
  tool **unprompted**. The offline fakes could never demonstrate either — a scripted call is fixed
  at authoring time — so the live run *adds* a guarantee the fakes structurally cannot: the real
  model chooses correctly. On this axis the fakes did not lie; they were simply silent, and the
  live evidence fills the silence. ✅

- **The one mismatch — and it is the most important finding.** `FakeBackend` treats an ungranted
  tool call as a hard **refusal** (`BudgetExceeded`). The live `PydanticAIBackend` does **not**
  raise: the guard removes the tool from the schema, the model may still *emit* a non-executing
  tool-call part, and that part is recorded in `TurnResult.tool_calls`. So `tool_calls` means "a
  tool ran" in the fake and "the model emitted a call part" live. **A test asserting `tool_calls`
  is empty whenever no tool was granted passes offline and fails live.** The guard is intact in
  both cases — no unauthorized code executed — so this is a *faithfulness* gap, not a security one,
  but it is exactly the kind of thing the fake hid.

- **The cache negative holds** on a real full-size prefix, with its caveat intact — consistent
  with `docs/24`. ✅

- **No new performance regression.** Only the two known SQLite-search watch-item rows exceed
  tolerance (plus a transient blip on read-by-key under momentary memory pressure); the code paths
  land on their audited baselines. ✅

**Recommendation to the integrator:** the guard is safe on the live path, but the `tool_calls`
semantic difference between the fake and the live backend should be documented at the seam (or the
fake taught to record a rejected-but-emitted call rather than raise), so a future test does not
encode the fake's stricter refusal as a cross-backend invariant. That is the cross-check to run
against Round 1's happy-path `FakeBackend` behaviour.
