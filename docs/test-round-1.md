# Round 1 — happy path, end to end (Teo)

**Angle:** prove the things a real *user* does actually work, on `FakeBackend` (offline,
deterministic), and leave behind an example a human can learn the API from.

**Result:** `tests/round_1` — **13 passed, 0 failed**, no network.
`examples/skill_with_tools.py` runs offline and (with a key) live.
The 726-offline / 8-badge baseline still holds — round 1 adds exactly +13 offline tests and
touches nothing under `src/`.

Every claim below is backed by a named, runnable test in
`tests/round_1/test_happy_path.py`, and the three skills are real `SKILL.md` documents in
`tests/round_1/skills/`.

---

## What was exercised, and what passed

### 1. Author three skills + progressive disclosure ✅

Three `SKILL.md` documents were authored from scratch and loaded through
`pikachu.skills.loader`:

| File | Kind | Declares | Loads as |
|---|---|---|---|
| `skills/colourist-palette.md` | tool-using | `brand_palette` | `BUILTIN`, clean lineage |
| `skills/script-writer.md` | text only | *(nothing)* | `BUILTIN` |
| `skills/sticker-sheet.md` | declares a tool it won't get | `sticker_cut` | `BUILTIN` |

- `test_colourist_skill_loads_and_declares_its_tool` — the tool-using skill loads with
  `declared_tools == ("brand_palette",)`, is trusted, and its body actually names the tool.
- `test_script_writer_skill_declares_nothing` — the text-only skill loads with
  `declared_tools == ()`.
- `test_sticker_skill_declares_a_tool_it_will_not_be_granted` — the sticker skill declares
  `sticker_cut`; against an allowlist that omits it, `effective_tools` narrows it to `()`
  and records it in `removed_tools`. Declaring is not granting.
- `test_metadata_load_does_not_read_the_body` — **progressive disclosure**, proven twice:
  `SkillMeta` structurally has **no `body` attribute**, and a document with valid
  frontmatter but a deliberately corrupt (`\x00`-laden) body still yields clean metadata,
  because `load_metadata` never touches the body.

> Note on trust tier for the sticker skill: a skill that *declares* tools must be
> `BUILTIN`/`VERIFIED` — the frozen model validator (`Skill._untrusted_declares_nothing`)
> makes a tool-declaring `UNTRUSTED`/`COMMUNITY` skill structurally unconstructable. So the
> "declares a tool it will not be granted" demonstration is correctly a **trusted** skill
> narrowed at the **allowlist** boundary, not the trust boundary. The two narrowing
> mechanisms are different and this skill exercises the allowlist one; a foreign skill
> trying to declare a tool is Round 2's territory (the validator rejection).

### 2. Skill + tools through a turn — granted, then narrowed ✅

- `test_skill_tool_is_called_and_its_output_is_used` — allowlist grants `brand_palette`,
  the colourist skill is attached, the turn is scripted to call the tool then answer. The
  tool **was** invoked (`result.tool_calls == [brand_palette]`) and the answer **used its
  output** (`#FFB300` appears in the text).
- `test_same_skill_degrades_when_tool_removed_from_allowlist` — the **same skill**, but the
  agent's allowlist is now `()`. `effective_tools` narrows `brand_palette` away
  (`removed_tools`, reason `"not in fixed allowlist"`), and the turn **still completes** with
  a graceful fallback answer and `tool_calls == ()`. Degraded, not crashed — this is P3 on
  the happy path.
- `test_backend_refuses_a_tool_the_guard_did_not_grant` — belt and braces: if a script tries
  to call a tool outside `effective_tools`, `FakeBackend` refuses (raises) rather than
  silently widening. The guard is the only source of authority even at the backend seam.

### 3. Declarative function tools ✅

Three plain Python functions (`brand_palette`, `shot_count`, `storyboard`) registered as
tools and built through the real `PydanticAIBackend._toolset_for` path — **offline**,
because `FunctionToolset` schema generation touches no network. The backend is constructed
with a dummy key and no turn is run.

- `test_function_docstring_becomes_the_tool_description` — each function's **docstring is
  verbatim the tool description** the model would see (`Tool.description`).
- `test_toolset_is_built_once_and_reused_for_the_same_permission_set` — a second
  `_toolset_for` call with the identical permitted-name tuple returns the **same object**
  (`first is second`) — the cache fires.
- `test_a_different_permission_set_is_a_different_cache_entry` — a **narrower** permission
  set produces a **different** toolset (`wide is not narrow`), so the cache key preserves P3:
  a cached toolset can never widen a grant.

### 4. Streaming ✅

- `test_stream_event_order_and_tool_pairing` — `TurnStarted` first, `TurnFinished` last and
  exactly once; the tool call is `ToolCallStarted` → `ToolCallFinished` in that order,
  carrying `ToolOutcome.SUCCESS`; the reconstructed (degraded) path is announced on the wire
  (`TurnStarted.streaming is False`), not hidden.
- `test_turnfinished_result_equals_the_non_streaming_result_with_timing` — `TurnFinished.result`
  is **structurally equal** to the `run_turn` result for the same request, **including the
  `timing` model**. Streaming is the same turn observed live, never a lossier view.

### Bonus — the credit path ✅

- `test_metered_tool_reserves_and_captures_offline` — a metered tool (`cost_credits=35`)
  drives reserve→capture through `FakeBiller`; the charge lands **once** (`charged == 35`,
  `refunded == 0`, `result.cost_credits == 35`). This is the money path a user hits without
  spending real money.

---

## Findings — API friction (nothing broken, but not free either)

Per the rule, friction is a finding even when the tests pass. Each item below is real
enough that it changed how the test or the example had to be written.

### F1 — A declarative tool's name is the function's `__name__`, not the registry key. *(medium)*

`PydanticAIBackend(tool_registry={...})` takes a `name -> callable` map, so it *looks* like
the dict key is the tool name. It is not. `FunctionToolset` names each tool after the
callable's `__name__`. My first draft used `def _brand_palette` under the key
`"brand_palette"`, and the toolset registered the tool as `_brand_palette` — which would not
match `effective_tools`, and the model would be offered a tool under a name the guard never
authorised.

- **Evidence:** the two failing assertions in the first `pytest tests/round_1` run
  (`{'_brand_palette'} == {'brand_palette'}`), fixed by renaming the functions to match
  their registry keys.
- **Why it bites:** the guard normalises and matches on the registry/allowlist name, but the
  toolset registers on `__name__`. A silent divergence here means a *permitted* tool is
  offered to the model under an *unrecognised* name — a benign-looking bug that defeats the
  name-based authority check. The registry currently trusts the caller to keep key and
  `__name__` in lockstep; nothing enforces it.
- **Suggested fix (for `src`, not applied here):** have `_tools_for` / `FunctionToolset`
  construction rename each callable to its registry key (pydantic-ai's `.renamed(...)`), or
  assert `fn.__name__ == key` at registration and fail loudly. Either closes the gap; the
  assert is the cheaper honest one.

### F2 — A freshly loaded skill is `DRAFT`, so a user must `model_copy` it to `ACTIVE`. *(low)*

`load_skill(...)` returns a skill at `SkillStatus.DRAFT`. Only `CANDIDATE`/`ACTIVE` are
retrievable, so to actually *use* a just-authored skill you must
`skill.model_copy(update={"status": SkillStatus.ACTIVE})` — because `Skill` is frozen. That
is correct and safe, but it is an unobvious extra step with no `load_skill(..., status=...)`
convenience, and the frozen-model `model_copy` idiom is not something a first-time user
reaches for. Both the tests and the example carry this dance.

### F3 — Running one turn takes ~6 imports and a hand-built `Run` + `TurnRequest`. *(low–medium)*

The minimal offline turn needs, in the example: `AgentSpec`, `Skill`/`load_skill`,
`ToolSpec`, `Run`, `TurnRequest`, `effective_tools`, `FakeBackend`, `ScriptedTurn`,
`ScriptedToolCall`. The user is also responsible for calling `effective_tools` themselves
and threading its `.tools` into `TurnRequest.effective_tools`, and for minting a `run_id`
that matches between `Run` and `TurnRequest`. There is no single "run a turn with this skill
and allowlist" front door — the guard step is manual and easy to skip, which is precisely
the step you least want optional. A thin `Session`/`run_turn(agent, skill, message)` helper
that calls the guard for you would remove five imports and make the safe path the default
path. (This is a shape observation, not a defect — the seam is deliberately one method.)

### F4 — `effective_tools(fixed_allowlist, declared)` argument order is easy to invert. *(low)*

The signature is `effective_tools(fixed_allowlist, declared)` — allowlist first. It reads
naturally as "narrow the declared set against the allowlist", which tempts you to pass
`declared` first. Inverting them still returns a plausible-looking `EffectiveToolset` (it is
symmetric-looking), so a mistake would not raise — it would silently compute the wrong
intersection. Keyword-only parameters, or names like `allow=`/`declared=`, would make the
inversion impossible.

### F5 — `--live` is gated by a `--live` flag, not by the network being reachable. *(informational, by design)*

`get_api_key()` falls back to `pikachu/.env` and `api/.env`, so `env -u OPENROUTER_API_KEY`
does **not** force the offline path — the example found a key on the search path and made a
real call. This is correct behaviour for a `--live` demo (the whole point is to hit the model
when a key exists), and it is *safe for CI* because **no test imports or invokes the live
path** — the socket block in `tests/conftest.py` is never reached. Recording it so the next
person does not mistake "I unset the env var" for "this cannot touch the network": the guard
is the flag, not the environment.

---

## Cross-check hooks for the integrator

- **S2 / guard narrowing (vs Round 2):** Round 1 asserts the *allowlist* narrows a trusted
  skill's declared tool (`test_same_skill_degrades_when_tool_removed_from_allowlist`) and
  that the backend refuses an un-granted tool. Round 2 attacks the same guard from the
  hostile side across skill/plugin/MCP. They should agree that `effective_tools` narrows and
  never widens.
- **FakeBackend fidelity (vs Round 3):** Round 1's fake models "tool called → output quoted"
  and "tool removed → graceful degrade". Round 3's live run should show the real model
  actually calling `brand_palette` and quoting `#FFB300`, confirming the fake did not hide a
  behaviour. (An unforced live run during development did exactly that:
  `answer: "…the hex code #FFB300"`, framework 254 ms · model 7970 ms — the fake is faithful.)

---

## Baselines (verification commands)

```
.venv/bin/python -m pytest tests/round_1 -q
    → 13 passed

.venv/bin/python examples/skill_with_tools.py
    → OFFLINE run, tool called ['brand_palette'], answer quotes #FFB300, iterations 2

# 726 baseline (offline suite excluding all round lanes and live) — unchanged:
.venv/bin/python -m pytest tests/ -q --ignore=tests/live \
    --ignore=tests/round_1 --ignore=tests/round_2 --ignore=tests/round_3
    → 726 passed, 1 skipped

.venv/bin/python -m mypy --strict src/pikachu
    → Success: no issues found in 60 source files   (boulder badge intact; src untouched)
```

### One out-of-lane observation (not Round 1's to fix)

`tests/ -q --ignore=tests/live` currently reports **1 failed** —
`tests/round_3/test_live_behaviour.py::test_cost_estimate_matches_published_pricing`, which
fails with `ModuleNotFoundError: No module named 'scripts.round3_live'`. That is Kai's Round
3 deliverable (`scripts/round3_live.py`) not yet written, and it is independent of Round 1
(round_1 passes 13/13 in isolation, and the pre-round baseline is a clean 726). Flagged for
the integrator; not touched here, per the ownership rule.
