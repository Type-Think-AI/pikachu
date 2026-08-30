# BUILD PLAN — Wave 4 (completion)

Six lanes closing every remaining feature and every reachable success criterion. Read
`BUILD-PLAN.md` for the ownership contract; `BUILD-PLAN-WAVE2.md` for the lazy-import and storage
rules; `BUILD-PLAN-WAVE3.md` for the two handoff patterns. All still apply.

**Where we are:** 18 modules, 635 tests, `mypy --strict` clean on 52 files, 8/8 badges, **19 of 23
features**, 3 of 7 success criteria verified. Engine is Pydantic AI; Agno is not a dependency.

**What this wave closes:** F3, F12, F20, F21 — and S2, S4, S5, and S1 (settled either way).

---

## The one that matters most: S2 does not currently hold

S2 requires that a hostile skill, a hostile plugin **and** a hostile MCP server are refused by the
*same* code path, proven by property test. Audited reality:

```
skills     NO guard reference
plugins    NO guard reference      ← loads third-party code
mcp        routes through guard
a2a        routes through guard
webmcp     NO guard reference
```

`plugins/` relies on the type contract instead — an `UNTRUSTED` skill cannot declare tools, so
validation rejects it. That is real defence, but it is **not the same path**, and S2 is the claim
the entire positioning rests on: *we supply the permission layer the standards leave out*. A
one-path guarantee that is actually three different mechanisms is not the thing we are claiming.

Lane T closes it. It is the highest-value work in this wave.

## Honest scope note

**S6 is out of scope and is not a build task.** It requires running SkillsBench (86 tasks, 11
domains, thousands of trajectories) against a curated-auto arm. That is a research exercise costing
real money and hours. Recording that it has not been run is the honest state; claiming it as done
would be false. Do not fake it.

**S1 may be unreachable rather than unbuilt.** The default model's minimum cacheable-prefix size is
unpublished. Lane Y measures it with a full-size prefix; if caching does not fire, the deliverable is
a recorded measurement plus a recommendation, not a green tick.

## Reserved files — integrator only

```
pyproject.toml            src/pikachu/__init__.py    src/pikachu/_lazy.py
src/pikachu/core/{types,protocols,errors}.py         src/pikachu/config.py
tests/conftest.py         BUILD-PLAN*.md
```

Two lanes will need dependencies added. Write `HANDOFF-<LANE>.md` with the exact change **and**
`xfail(strict=True)` acceptance tests that flip to required passes when applied — that pattern has
caught real mistakes twice now.

---

## Lane T — close S2 ★ highest value in this wave
**Owns:** `src/pikachu/guard/untrusted.py`, `src/pikachu/plugins/loader.py`,
`src/pikachu/webmcp/tools.py`, `src/pikachu/skills/loader.py`,
`tests/properties/test_s2_single_path.py`

Build **one** function every untrusted-input boundary calls, then make all of them call it.

- `guard/untrusted.py::admit(source, *, declared_tools, fixed_allowlist, trust, lineage) ->
  Admission` — the single admission point. It returns the narrowed toolset (via the existing
  `effective_tools`), the inherited taint, and the reasons for anything removed. It **never** raises
  for a denied tool; it omits, per the existing fail-closed rule.
- Route **`plugins/loader.py`, `webmcp/tools.py` and `skills/loader.py`** through it. `mcp/client.py`
  and `a2a/cards.py` already route through `effective_tools` — leave them alone, but make sure
  `admit` is a compatible wrapper so a later pass can converge them too.
- Do **not** change `guard/allowlist.py`. `admit` composes it; it does not replace it.
- ★ `tests/properties/test_s2_single_path.py` (`@pytest.mark.thunder`): hypothesis over an arbitrary
  hostile input **for each of the three source kinds** — skill, plugin, MCP server — asserting all
  three are narrowed identically by the same call, and that no source kind can produce a tool outside
  `allowlist ∩ declared`. Also assert by introspection that each boundary module actually calls
  `admit`, so a future module cannot quietly bypass it.

## Lane U — F3 event stream ★ the only tier-A gap
**Owns:** `src/pikachu/core/events.py`, `src/pikachu/backends/streaming.py`,
`tests/test_streaming.py`

There is currently **no `AsyncIterator` anywhere** — a caller gets a completed `TurnResult` and
cannot show progress. For a chat product this is the most user-visible missing piece.

- `core/events.py`: a frozen event union — `TurnStarted`, `TextDelta`, `ToolCallStarted`,
  `ToolCallFinished` (carrying `ToolOutcome`), `ArtifactProduced`, `TurnFinished` (carrying the final
  `TurnResult`). Discriminated on a `kind` literal so a consumer can match exhaustively.
- `backends/streaming.py`: `stream_turn(backend, request) -> AsyncIterator[TurnEvent]`. Reuse the
  streaming path that already exists in `backends/pydantic_ai.py::_call` — it streams today purely to
  measure time-to-first-token, so the deltas are already flowing and are being discarded. Do not
  duplicate that logic; expose it.
- **The final event must carry the same `TurnResult` a non-streaming call returns**, including
  `timing`. A streaming consumer must not lose the phase breakdown.
- Test with `FakeBackend`, no network: event order is deterministic; a tool call appears as
  started-then-finished; `TurnFinished` carries a result equal to the non-streaming path; cancelling
  mid-stream does not leak a task.

## Lane V — F20 OTel GenAI spans
**Owns:** `src/pikachu/telemetry/otel.py`, `tests/test_otel.py`, `HANDOFF-V.md`

- `telemetry/` currently has our own ledger and **zero** `gen_ai.*` spans. Add real ones.
- The conventions **moved** to `open-telemetry/semantic-conventions-genai`; every `gen_ai.*`
  attribute is still **Development** status, so pin what you emit and say in the docstring that it is
  unstable by upstream's own classification.
- Emit a span per turn with `gen_ai.operation.name`, `gen_ai.request.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and a child span per tool call.
- ★ **Optional dependency, and a no-op when absent.** `opentelemetry-api` goes in an **extra**, and
  with it uninstalled the module must still import and silently do nothing — same reasoning as the
  `mcp` extra. Telemetry must never be the reason a turn fails.
- `HANDOFF-V.md` for the pyproject extra, with strict-xfail acceptance tests.
- Test with an in-memory span exporter, no network, and a test proving the no-op path works when the
  library is absent (simulate by patching the import).

## Lane W — F21 pydantic-evals
**Owns:** `src/pikachu/evals/__init__.py`, `evals/cases.py`, `evals/runner.py`,
`tests/test_evals.py`, `HANDOFF-W.md`

- The declared eval library was never wired in. The badge harness is ours and stays — **badges are
  tier 1 and gate; evals are tier 2 and never gate.** Do not let an eval failure fail a build.
- Build a small case set over things we can judge deterministically: does a skill body reach the
  model, is a denied tool absent, does the guard narrow correctly, does a tainted skill stay
  unpromoted. Deterministic verifiers first; LLM-as-judge only where nothing else works, and clearly
  labelled as noisy.
- `evals/runner.py` writes results into the **Pokédex** (`scripts/report.py`), not the badge case.
- Optional dependency in an extra; absent means skip, not fail. `HANDOFF-W.md` with strict-xfail
  acceptance tests.

## Lane X — F12 MCP server mode
**Owns:** `src/pikachu/mcp/server.py`, `tests/test_mcp_server.py`

We built a **client**. This exposes a Pikachu agent **as** an MCP server so other agents can call it.

- Do **not** touch `mcp/client.py` — read it, reuse its constants, own only `server.py`.
- Serve MCP **2026-07-28**: stateless (no `initialize`), `server/discover` **required**, every result
  carries `resultType: 'complete' | 'input_required'`, tasks are an extension. Roots/Sampling/Logging
  are deprecated — do not implement them.
- ★ **Only expose tools the agent's allowlist permits.** Serving is not a licence to widen: run the
  exposed set through the guard, and never advertise a tool the agent could not itself call.
- An inbound request is **untrusted input**: tainted, validated, and a malformed request is rejected
  with a typed error rather than partially applied.
- Test against an in-memory transport, no sockets. Assert the advertised revision is `2026-07-28`,
  that `server/discover` works without any handshake, and that a tool outside the allowlist is never
  advertised.

## Lane Y — S1, S4, S5: prove it end to end
**Owns:** `examples/` (all files), `scripts/measure_cache.py`, `tests/test_examples.py`

Three success criteria are unproven not because code is missing but because nothing exercises it.

- ★ **S5** — `examples/canvas_handoff.py`: a script-writer agent writes a script artifact; a
  storyboard agent produces frames **from that artifact without being passed it as an argument**,
  finding it on the canvas. This is the blackboard claim demonstrated rather than asserted. Runs on
  `FakeBackend` so it is testable offline, with a flag for a live run.
- ★ **S4** — `examples/create_agent_at_runtime.py`: create an `AgentSpec` at runtime, register it,
  invoke it by name. **No code change, no deploy.** Prove the end-user persona claim.
- ★ **S1** — `scripts/measure_cache.py`: run a turn carrying a **full-size prefix** (a real skill
  body plus tool schemas, ~1,500–2,400 tokens as measured) three times, and report
  `cache_read_tokens` per turn. If it fires, S1 is met and `CACHE_FLOOR_UNVERIFIED` can be cleared.
  If it does not, report the measurement and recommend a model that would — **a recorded negative is
  the deliverable, not a failure.** Note Google's implicit caching can report 0 even when it fired,
  so state that caveat next to the number.
- `tests/test_examples.py`: every example runs on `FakeBackend` in CI, offline. An example that only
  works live is an example that rots.

---

## Definition of done for wave 4

- **23/23 features** exist
- **S2 proven by property test**, with all five untrusted boundaries on one admission path
- S3, S4, S5, S7 verified; **S1 settled with a measurement either way**
- S6 recorded as not run, with the reason
- 8/8 badges still earned; `import pikachu` still does not pull `pydantic_ai`
- New dependencies are **extras** that no-op when absent; nothing imports `agno`
