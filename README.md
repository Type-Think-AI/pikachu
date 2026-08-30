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
| Python modules | **34** (6,347 lines) |
| Tests | **239 passing**, 0 failing |
| Type checking | `mypy --strict` clean, 17 files |
| Gym badges | **5 of 8 earned** |
| Design docs | 23 |
| Live model call | ❌ not yet — wave 2 |

```
  KANTO BADGE CASE
  1. Boulder  Brock, Rock          ✓ EARNED   types are solid
  2. Cascade  Misty, Water         ✓ EARNED   contracts flow
  3. Thunder  Lt. Surge, Electric  ✓ EARNED   the guard holds  ← the USP
  4. Rainbow  Erika, Grass         ✓ EARNED   skills load safely
  5. Soul     Koga, Poison         · NOT YET BUILT   taint propagates
  6. Marsh    Sabrina, Psychic     · NOT YET BUILT   memory cannot lie
  7. Volcano  Blaine, Fire         ✓ EARNED   it actually runs
  8. Earth    Giovanni, Ground     · NOT YET BUILT   grounded in measurement
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

### Blocking / next up

| # | Task | Why it matters |
|---|---|---|
| 1 | **Measure whether caching actually fires** on the default model | The minimum cacheable-prefix floor is unpublished. If Gemini's blanket 4,096 applies, caching is on and does **nothing** — no error, `cache_read_tokens` just stays 0. Our prefix is ~1,500–2,400. Run one real turn and read the number. Tracked as `CACHE_FLOOR_UNVERIFIED`. |
| 2 | **`PydanticAIBackend`** | The only thing standing between this and a live turn. The seam and its fake already exist. |
| 3 | **`memory/` + `guard/lineage.py`** (Soul + Marsh) | **Security-critical.** Closes the laundering path: without taint tracking, a single poisoned turn can be distilled into a skill, promoted on reuse, and thereafter carry our own provenance. `guard/` is a hard prerequisite for `curator/`. |
| 4 | **`canvas/`** (append-only artifact graph) | The coordination mechanism. Needs the approval-gate scaffolding wired, because more agents on an unstructured board measurably *lowers* quality. |
| 5 | **`mcp/` client** | Depends on `fastmcp`/`mcp`, **not** on pydantic-ai's own revision — pydantic-ai wraps the SDK rather than implementing the protocol. Assert the negotiated revision in a test at the SDK boundary. |
| 6 | **`telemetry/` + `scripts/report.py`** (Earth) | The tier-2 Pokédex report: cost per turn, cache-hit ratio, latency percentiles, partition confusability. |

### Known debt, recorded rather than hidden

- **Phase 0 corrections not yet applied to their target docs.** `docs/22` carries a change table;
  docs `02`, `05`, `10`, `17` and the PRD still read the old way. Two are substantive: the Agent
  Plugins manifest is **closed** (`additionalProperties: false`), so the reverse-DNS escape hatch
  `docs/17` describes as a top-level key would make a manifest *invalid* — it belongs under
  `extensions`; and **Gemini 2.5 Flash caches at a 1,024 floor** (only Pro is 2,048+), so the
  recorded "2.5 = 2048" is wrong.
- **Success criterion S1** (`cache_hit_ratio > 0`) may be unreachable on the chosen default. Since
  the model was selected on modality rather than cache fit, S1 should be re-scoped to *measured and
  reported* rather than *greater than zero* — but that is the owner's call and is not yet edited.
- **The scanner misses paraphrased injection.** Pattern matching catches literal override phrasing;
  a politely worded paragraph that redirects the agent's goal passes clean. Auto-approve on a clean
  scan is therefore unsafe and a human reviewer is required for anything published. Stated in the
  module docstring, not just here.
- **`max_iterations = 20`** sits above the production norm — 68% of production agents cap at ≤10.
  Defend it with an observed distribution or lower it.
- **No `PydanticAIBackend`, so nothing has spoken to a real model yet.** Every number in this README
  comes from tests against fakes.

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
