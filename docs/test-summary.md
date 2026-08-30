# Test Summary — three rounds, cross-checked

Integrator's consolidation of the three independent test rounds, with the lanes graded against each
other. Verdict at the bottom.

## Result

**No bugs. Three real findings, none a test failure, one now fixed.** The 726/8-badge baseline held
throughout; the rounds added ~215 tests on top of it.

| Round | Angle | New tests | Outcome |
|---|---|---|---|
| 1 | happy path (offline) | ~21 | all pass; one API-friction finding |
| 2 | adversarial (offline) | 98 | **every attack attempted and held** |
| 3 | live + performance | ~11 | live tool-calling works; one fake-vs-live mismatch |

## The three findings

### 1. ★ `tool_calls` meant two different things across backends — FIXED

Round 3's most important finding, and exactly what round 3 existed to catch. `FakeBackend` recorded
a tool call only when a tool *ran*; `PydanticAIBackend` recorded every `ToolCallPart` the model
*emitted* — and a model primed by a skill body emits a call for a tool the guard removed from the
schema, which never executes. So `TurnResult.tool_calls` non-empty meant "a tool ran" offline and
"the model said something tool-shaped" live.

The guard was never compromised — no denied tool ever executed — but a consumer reading `tool_calls`
would have drawn a wrong conclusion that **passed offline and would have been wrong in production.**
That is the fake hiding something, which is the failure the round was designed to surface.

**Fix:** every `tool_calls` record now carries `executed: bool`. Live, it is True only when a
matching `ToolReturnPart` exists; the fake sets it False for a `DENIED` outcome. Both backends now
mean the same thing, and it is verified live: a granted tool records `executed=True`, a denied one
`executed=False`. The `TurnResult.tool_calls` docstring states the invariant.

### 2. API friction: registry key vs function `__name__` (round 1) — noted, not a bug

`FunctionToolset` names a tool after the callable's `__name__`, not the registry key it was
registered under. Register `{"brand_palette": _brand_palette}` and the model sees a tool called
`_brand_palette`. Not broken — the realistic fix is to name the function to match, which is what a
user does — but it is genuine friction worth a line in the docs so the next person is not surprised.

### 3. `LedgerBiller.reserve(amount=-5)` raises `ValueError`, not `PikachuError` (round 2) — defensible

A negative reserve raises a raw `ValueError`. Round 2 flagged it against the "typed errors only"
rule, then reasoned correctly that a negative reserve is a *caller bug*, not untrusted input, so a
raw `ValueError` is defensible. Recorded as a boundary note, not a defect. `DoubleCaptureError` and
the rest of the money path are all `PikachuError` subclasses.

## Cross-check — the lanes agree

- **S2:** round 2 attacked it independently — case/whitespace/unicode variants, empty-normalising
  names, oversized declared lists, `model_copy` lineage-laundering — from the attacker's side, and
  **could not break it.** That agrees with round 1's happy-path narrowing and Lane T's property
  test. Three independent confirmations.
- **Fake vs live:** round 3 checked `FakeBackend` against the real model and found the one mismatch
  above — now closed. Everything else (guard narrowing, skill-body injection, timing phases) matched.
- **Performance:** round 3 re-ran the profiler across two passes. Only the two SQLite-search rows
  exceeded tolerance, and only under host memory pressure — the exact watch item `docs/24-audit.md`
  already recorded. A transient read-by-key spike in one pass dropped back on the second, confirming
  it as memory pressure, not code. **No new regression.**

## Live numbers (round 3, `google/gemini-3.7-flash`)

- Skill-with-tools: the model **did** call `brand_palette` and quote `#FFB300` — real tool
  *selection*, not just plumbing.
- Declarative function tool: the model chose to call it unprompted when the task needed it.
- S1 reproduced: `cache_read_tokens` 0 on a full-size prefix. Suggestive of the floor, with the
  implicit-caching caveat.
- Total live cost: fractions of a cent across the round.

## Verdict — GO for picx-studio integration

Three independent rounds found no bug, confirmed S2 from the attacker's side, and proved live
tool-calling matches the offline model — after closing the one place it did not. The two remaining
findings are a documentation note and a defensible design choice.

**No blockers.** Integrate behind the `GrootAgentBackend` seam. Carry forward, unchanged from before
this exercise: S1 is an accepted negative (model chosen on modality), the scanner misses paraphrased
injection by design, and `max_iterations = 20` is above the production norm.
