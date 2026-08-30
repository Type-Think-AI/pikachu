# Pikachu

An agent runtime that implements the open agent standards — Agent Skills, Agent Plugins, MCP,
WebMCP, A2A — and supplies the **permission, confinement and provenance layer those standards
deliberately leave to the runtime**.

`pikachu` is an internal codename. The published package name is undecided, so expect exactly one
rename of the import root before release.

> **Status: wave 1 complete.** The package installs, imports, and passes 239 tests under
> `mypy --strict`. The permission layer works. There is no live model call yet — that is wave 2.

---

## Status at a glance — 2026-08-30

| | |
|---|---|
| Features | **23 / 23** (PRD F1–F23) |
| Success criteria | **5 met**, 1 measured negative, 1 out of scope |
| Tests | **726 offline** + 5 live, 0 failing |
| Type checking | `mypy --strict` clean, 60 files |
| Gym badges | **8 of 8 earned** |
| Modules | 19 (`src/pikachu/`), 120 Python files, 26,748 lines |
| Design docs | 25 |
| Live model call | ✅ verified against `google/gemini-3.7-flash` |

```
  KANTO BADGE CASE
  1. Boulder  Brock, Rock          ✓ EARNED   types are solid
  2. Cascade  Misty, Water         ✓ EARNED   contracts flow
  3. Thunder  Lt. Surge, Electric  ✓ EARNED   the guard holds  ← the USP
  4. Rainbow  Erika, Grass         ✓ EARNED   skills load safely
  5. Soul     Koga, Poison         ✓ EARNED   taint propagates
  6. Marsh    Sabrina, Psychic     ✓ EARNED   memory cannot lie
  7. Volcano  Blaine, Fire         ✓ EARNED   it actually runs
  8. Earth    Giovanni, Ground     ✓ EARNED   grounded in measurement
```

Badges are **tier 1** — they gate shipping. Trend stats are **tier 2** (the Pokédex) and never gate.
Unbuilt is reported separately from failed, so the report stays honest mid-build.

---

## What is DONE

### The core contract — `src/pikachu/core/`
The frozen interface every module codes against, written before any parallel work started. Its
distinguishing property is that **invariants live in the type system, not at call sites**:

- an `UNTRUSTED` skill that declares tools **fails validation** — a foreign document structurally
  cannot contribute authority
- `Lineage` is frozen and monotonic with no `clear()` — laundering a taint is not expressible
- `MemoryRecord.may_justify_authority` is typed `Literal[False]`, so branching on it is a *type
  error* rather than a runtime decision
- `normalize_tool_name()` is the single normalisation point, closing a historical bug where one path
  matched a literal string while another normalised
- `declared_tools` normalises but deliberately does **not** dedupe — `('web','web')` stays
  `('web','web')`

Storage and billing are **Protocols, not implementations**, so the package stays embeddable: the
product plugs in Postgres and credits; an open-source user plugs in SQLite and a no-op biller.
`SignalLedger` has no `score()` method by design — scores drive retrieval rank and must never be
readable from anywhere that could place them in an agent's context.

### The permission guard — `src/pikachu/guard/` ★
`effective_tools()` enforces **P3: effective ⊆ fixed allowlist ∩ declared**. A skill can only ever
narrow. Dangerous tools (`bash`, `terminal`, `read_file`, `browser`) are stripped into a recorded
`removed_tools`, never silently dropped. `declared=None` (inherit the allowlist) is distinguished
from `declared=()` (declare nothing). Fails **closed** — a denied tool is omitted, never raised from
a filter hook. Proven by hypothesis property tests over arbitrary inputs, not examples.

### Skills — `src/pikachu/skills/`
`SKILL.md` frontmatter parsing with **progressive disclosure** (metadata loads without reading the
body), bundle loading that **strips and records** executable scripts, an injection scanner, and
partition-scoped confusability detection that warns at authoring time.

### Backends — `src/pikachu/backends/`
The one-method framework seam, plus a complete deterministic `FakeBackend` that drives multi-turn
runs, the reserve→capture credit path, `ToolOutcome.INTERRUPTED`, and cache-ratio reporting — with
no network and no model.

### Test harness — `scripts/badges.py`, `tests/`
The gym-badge runner, in-memory fakes for every Protocol, and an autouse fixture that **hard-fails
any test opening a socket**.

### Phase 0 verification — `docs/22-phase0-verification.md`
All four blocking questions answered by inspection, with `scripts/verify_pydantic_ai.py` making the
API surface re-checkable on any upgrade (exits 1 if an assumed symbol disappears).

### Default model — decided
`google/gemini-3.7-flash`, recorded in `src/pikachu/config.py`. Chosen for **native video, audio,
image and file input** — the deciding factor for a media product, which no text-and-image model
matches at any price. 1M context, cache read at 0.10×, and cache *write* at ~0.056× (Google bills
it as storage, not a premium, so there is no break-even to reach).

---

## What REMAINS

### Success criteria — where each one stands

| | Criterion | Status |
|---|---|---|
| S1 | `cache_hit_ratio > 0` on the default model | ❌ **measured negative** — see below |
| S2 | Hostile skill, plugin **and** MCP server refused by the *same* code path | ✅ property test, `tests/properties/test_s2_single_path.py` |
| S3 | Plugin loads with a broken `mcp.json` skipped and reported | ✅ `test_malformed_mcp_json_still_loads_skills` |
| S4 | An agent created at runtime and invoked, no code change, no deploy | ✅ `examples/create_agent_at_runtime.py` |
| S5 | A storyboard agent works from a script artifact it was never passed | ✅ `examples/canvas_handoff.py` |
| S6 | Our curated-auto arm beats SkillsBench's self-generated arm | ⏸ **not run** — external benchmark, see below |
| S7 | `pip install` in a clean venv; no host-app name leakage | ✅ verified in a throwaway venv |

### The two that are not green, and why

**S1 — prompt caching does not fire, and this is a recorded measurement rather than an open
question.** `scripts/measure_cache.py` ran three real turns carrying a full-size prefix:

```
prefix on the wire   1,964 input tokens   (inside the measured 1,500–2,400 band)
cache_read_tokens    0    0    0
cache_write_tokens   0    0    0          (turns 2 and 3 reused an identical prefix)
```

Consistent with Gemini's blanket documented 4,096-token minimum. **Stated with its caveat:** Google's
implicit caching can report `0` even when the cache *did* fire
([pydantic-ai #5205](https://github.com/pydantic/pydantic-ai/issues/5205)), so this is suggestive,
not conclusive — confirming would need an OTel `gen_ai` span reading.

The model was chosen on **modality** (native video, audio, image and file input), not cache fit, so
this is a known, accepted trade. `CACHE_FLOOR_UNVERIFIED` stays `True` in `config.py` until someone
measures a model whose floor our prefix clears.

**S6 — not run, and not a build task.** SkillsBench is 86 tasks across 11 domains and thousands of
trajectories. It is a research exercise costing real money and hours. Recording that it has not run
is the honest state; claiming it would be false.

### Known debt, recorded rather than hidden

- **The scanner misses paraphrased injection.** Pattern matching catches literal override phrasing; a
  politely worded paragraph that redirects the agent's goal passes clean. Auto-approve on a clean scan
  is therefore **unsafe** and anything published needs a human reviewer. Stated in the module
  docstring, not only here.
- **`max_iterations = 20`** sits above the production norm — 68% of production agents cap at ≤10
  ([arXiv 2512.04123](https://arxiv.org/html/2512.04123v1)). Defend it with an observed distribution
  or lower it.
- **Phase 0 corrections are recorded in `docs/22` but not yet applied to docs `02`, `05`, `10`, `17`.**
  Two are substantive: the Agent Plugins manifest is **closed** (`additionalProperties: false`), so
  the reverse-DNS escape hatch `docs/17` describes as a top-level key would make a manifest
  *invalid* — it belongs under `extensions`; and Gemini **2.5 Flash** caches at a 1,024 floor (only
  2.5 Pro is 2,048+), so the recorded "2.5 = 2048" is wrong.
- **Two SQLite search paths run ~3.5× their microbenchmark baseline.** Audited and deliberately
  **not** fixed: the harness was validated against four known anchors, the gap is more work plus host
  memory pressure, and the ratio to read-by-key is unchanged. A watch item, not a defect
  (`docs/24-audit.md`).
- **`pikachu` is an internal codename** and the published distribution name is undecided, so expect
  exactly one rename of the import root before any public release.

---

## Layout

```
pikachu/
├── BUILD-PLAN.md              lane contract + the badge scheme
├── PRD.md                     F1-F23, success criteria, open decisions
├── pyproject.toml             exact pins, and the 8 badge markers
├── docs/                      00-22 design docs
├── scripts/
│   ├── badges.py              the badge case runner
│   └── verify_pydantic_ai.py  API-surface check, exits 1 on drift
├── src/pikachu/
│   ├── config.py              DEFAULT_MODEL + the cache caveat
│   ├── core/                  types, protocols, errors  ← frozen contract
│   ├── guard/                 P3 enforcement, trust tiers
│   ├── skills/                loader, frontmatter, scanner, confusability
│   └── backends/              the seam + FakeBackend
└── tests/
    ├── badges/                one suite per gym badge
    ├── properties/            hypothesis invariant tests
    ├── fakes.py               in-memory Protocol implementations
    └── conftest.py            fixtures; blocks all network
```

## Running it

```bash
cd pikachu
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

.venv/bin/python -m pytest tests/ -q          # 239 tests
.venv/bin/python -m mypy --strict src/pikachu  # clean
.venv/bin/python scripts/badges.py             # the badge case
.venv/bin/python scripts/verify_pydantic_ai.py # API drift check
```

## Design docs

Start with `PRD.md`. Then `docs/22-phase0-verification.md` for what was actually verified versus
assumed, `docs/17-standards-and-interop.md` for the positioning, and `BUILD-PLAN.md` for how the
parallel build is organised.
