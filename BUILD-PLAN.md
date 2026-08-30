# BUILD PLAN — Wave 1 & 2

Ten parallel lanes with **exclusive file ownership**. Host cap is 6 concurrent, so this drains in two
waves. Read this before touching anything.

> **The one rule that makes this work:** you own your files. You may **read** anything. You may
> **write** only the paths listed under your lane. If you need a change to a reserved file, write
> `HANDOFF-<LANE>.md` with the exact code and stop — do not edit it yourself.

---

## Why lanes and not a shared free-for-all

A previous 10-agent wave on this repo succeeded (503 tests green, 12 commits) specifically because of
exclusive ownership plus a reserved-file set integrated by one agent in a single pass. An earlier
attempt without that discipline produced merge conflicts, circular imports and broken APIs. The
contract is the reason it works, not overhead on top of it.

## Reserved files — the integrator owns these, nobody else writes them

These are frozen for the duration of both waves. They are the contract every lane codes against:

```
pyproject.toml
src/pikachu/__init__.py
src/pikachu/core/types.py
src/pikachu/core/protocols.py
src/pikachu/core/errors.py
tests/conftest.py
BUILD-PLAN.md
```

`core/` is written and committed **before** wave 1 starts. That is deliberate: it is the frozen
interface that lets ten lanes compile independently instead of queueing behind each other.

---

## The theme, and where it is allowed

Per `docs/08-naming.md`: themed naming lives in the **product and developer surface only, never the
library API.**

| Surface | Naming |
|---|---|
| Importable API — `from pikachu import Agent, Skill` | **Boring.** No theme. `Agent`, `Skill`, `Tool`, `Run`, `Artifact`. |
| Test tiers, CI output, eval report | **Themed.** Gym badges, badge case, Pokédex stats. |
| Module paths | Plain: `core/`, `guard/`, `skills/`, `canvas/`. |

So a user importing this library never sees a Pokémon reference. A developer running the tests sees
nothing but.

### The eight gym badges are the test tiers

Badges are **earned in order** and gate shipping — which is exactly the tier-1 rule in
`docs/12-evaluation.md`: hard invariants fail the build, quality signals never do. Each badge maps to
a real invariant suite, and each mapping is semantic rather than decorative.

| # | Badge | Gym / type | Proves | Owner lane |
|---|---|---|---|---|
| 1 | **Boulder** | Brock, Rock | types are solid — `mypy --strict` clean, every model validates | C |
| 2 | **Cascade** | Misty, Water | contracts flow — every Protocol round-trips through its fake | C |
| 3 | **Thunder** | Lt. Surge, **Electric** | **the guard holds** — P3: effective ⊆ allowlist ∩ declared | B |
| 4 | **Rainbow** | Erika, Grass | skills load safely — SKILL.md parses, scanner rejects injection | D |
| 5 | **Soul** | Koga, **Poison** | **taint propagates** — lineage tracking, tainted never promotes | G |
| 6 | **Marsh** | Sabrina, **Psychic** | **memory cannot lie** — memory never widens authority | G |
| 7 | **Volcano** | Blaine, Fire | it actually runs — full turn end-to-end on `FakeBackend` | F |
| 8 | **Earth** | Giovanni, Ground | grounded in measurement — cache hit > 0, token + latency budget | J |

Electric is Pikachu's own type and it guards the USP. Poison is taint. Psychic is memory. The mapping
is the mnemonic.

**Tier 2 = the Pokédex.** Trend stats that are recorded and reported but **never** gate CI: judge
scores, partition confusability (C7), skill promotion rates, cost per turn.

---

## Wave 1 — six lanes, no interdependencies

### Lane A — `skills/` loader
**Owns:** `src/pikachu/skills/__init__.py`, `loader.py`, `frontmatter.py`, `tests/test_skills_loader.py`

Parse `SKILL.md` per [agentskills.io](https://agentskills.io/home): YAML frontmatter (`name`,
`description`, `license`, `compatibility`, `metadata`, `allowed-tools`) plus markdown body.
Progressive disclosure: metadata-only load must not read the body. Malformed frontmatter is a typed
error, never a silent default. Scripts in a bundle are **stripped and recorded**, never executed.

### Lane B — `guard/` permission engine ★ the USP
**Owns:** `src/pikachu/guard/__init__.py`, `allowlist.py`, `trust.py`, `tests/test_guard_*.py`,
`tests/properties/test_p3.py`

`effective_tools(fixed_allowlist, declared) -> tuple[str, ...]` enforcing **P3: effective ⊆ fixed ∩
declared**. A skill can only ever *narrow*. Normalise identically at every entry point —
`strip().lower()` — because a guarantee that holds on one path and not another is not a guarantee.
`bash`/`terminal`/`read_file`/`browser` are stripped into a recorded `removed_tools`, never silently
dropped. Trust tiers per `TrustTier`.

**Do NOT dedupe the toolset** — order and duplicates are preserved: `['web','web'] -> ['web','web']`.
A pinned test in the parent repo depends on this.

**Fails closed:** when a tool must be denied, omit it. Never raise from a filter — `ModelRetry` does
not work inside `PrepareTools`/`prepare=`/dynamic toolsets.

Earns **Thunder**. Property tests with `hypothesis`, not examples.

### Lane C — test harness + the badge runner
**Owns:** `tests/badges/`, `tests/properties/__init__.py`, `pytest.ini` section additions via
`HANDOFF-C.md`, `scripts/badges.py`, `tests/test_badge_runner.py`

Build the badge system itself: a pytest plugin or marker scheme where each badge is a suite, plus
`scripts/badges.py` that prints a **badge case** — which badges are earned, which are not, and why.
Exit non-zero if any badge that should be earned is not. Must run with zero network access.

Also: `mypy --strict` config, and the Boulder + Cascade badges.

Output format matters — this is the report the owner reads. Make it legible in a terminal.

### Lane D — `skills/` scanner + confusability
**Owns:** `src/pikachu/skills/scanner.py`, `confusability.py`, `tests/test_scanner.py`,
`tests/test_confusability.py`

Injection scanner over skill bodies. Detected payloads are **rejected as a typed error, not
sanitised-and-accepted**. Be honest in docstrings: pattern matching catches "ignore previous
instructions" and **misses paraphrased injection** — that limitation is recorded in
`docs/06-security.md` and must not be overclaimed in code comments.

Confusability: cosine distance between skill description embeddings, scoped to a partition, warning
at authoring time (C7 in `docs/09-design-constraints.md`). Take an embedding function as a
**parameter** — do not hardcode a provider, and do not make a network call in a test.

Earns **Rainbow** jointly with Lane A.

### Lane E — Phase 0 verification spike ★ unblocks Lane I
**Owns:** `docs/22-phase0-verification.md`, `scripts/verify_pydantic_ai.py`

Answer four questions with **evidence from primary sources or a run**, not from memory:

1. Does Pydantic AI's `MCPToolset` speak **MCP 2026-07-28**? (stateless, `server/discover` required,
   `resultType`, MRTR via `input_required`). If not, what revision — and is a shim viable?
2. Read `agent-plugins.org/schemas/1.0.0/plugin.schema.json` field by field. Only the announcement
   has been read so far; field-level conformance claims are currently unverified.
3. Which OpenRouter-reachable models have a **1,024-token cache floor**? Our measured stable prefix
   is ~1,500–2,400 tokens, and the current default `google/gemini-3.5-flash` has a **4,096** floor,
   so caching is silently a no-op. Recommend a default and show the arithmetic.
4. Confirm the API surface actually exists in the installed version: `ProcessHistory`,
   `.filtered()`, `RunUsage.cache_hit_ratio`, `ToolReturnPart.outcome`, `UsageLimits.cost_limit`.
   Several of these were corrected from wrong guesses earlier — verify, do not recall.

Write findings with a verdict per question. **No source code changes.**

### Lane F — `backends/`
**Owns:** `src/pikachu/backends/__init__.py`, `base.py`, `fake.py`, `tests/test_backends.py`

`AgentBackend` Protocol with one method, mirroring the seam that already works in the parent repo:

```python
async def run_turn(self, request: TurnRequest) -> TurnResult: ...
```

Plus a complete `FakeBackend` — scripted responses, deterministic, no network, no model. `FakeBackend`
is the most important file in wave 1: every other lane tests against it, and it is what earns
**Volcano**. `PydanticAIBackend` is wave 2 (Lane K) and depends on Lane E's verdict.

---

## Wave 2 — four lanes, start after wave 1 integrates

### Lane G — `memory/` + lineage ★ security-critical
**Owns:** `src/pikachu/memory/`, `src/pikachu/guard/lineage.py`, `tests/test_memory.py`,
`tests/properties/test_lineage.py`

Taint propagation: a value derived from untrusted input inherits its taint, and **tainted content can
never widen authority or reach promotion**. This closes the laundering path documented in
`docs/06-security.md` — memory evolution can turn a one-time injection into permanent compromise, so
this is not a nice-to-have. Earns **Soul** and **Marsh**.

### Lane H — `canvas/`
**Owns:** `src/pikachu/canvas/`, `tests/test_canvas.py`, `tests/properties/test_canvas.py`

Append-only artifact graph. Immutable artifacts; a revision is a **new** artifact with `parent` set.
Provenance records the **producing agent**. Property test: no operation mutates an existing artifact.

### Lane I — `mcp/` client
**Owns:** `src/pikachu/mcp/`, `tests/test_mcp.py`
**Blocked on Lane E.** Build to whatever revision Lane E establishes, not to an assumption.

### Lane J — eval harness + metrics report
**Owns:** `src/pikachu/telemetry/`, `scripts/report.py`, `tests/test_telemetry.py`

Token ledger, cost per turn, cache-hit ratio, latency percentiles. `scripts/report.py` prints the
**Pokédex** — tier-2 trend stats, clearly separated from badges, explicitly non-gating. Earns
**Earth**.

---

## Rules for every lane

1. **Own your files. Read anything. Write only yours.** Reserved files need `HANDOFF-<LANE>.md`.
2. **`from __future__ import annotations`** at the top of every module.
3. **Type everything.** `mypy --strict` must pass. No bare `Any` without a comment saying why.
4. **No network in tests.** Ever. Not even "it's just an embedding call."
5. **Property tests over examples** for anything invariant-shaped. `hypothesis` is installed.
6. **Do not invent an API you have not verified.** If unsure whether a Pydantic AI symbol exists,
   check the installed package — several confident guesses were already wrong this project.
7. **Report honestly.** If you could not finish, say which files are incomplete. A partial lane that
   is described accurately is useful; one described as done is a trap for the integrator.
8. **No `git commit`.** The integrator commits. Leave your work in the tree.
9. Run `.venv/bin/python -m pytest tests/ -x -q` on your own files before reporting.

## Definition of done for wave 1

- `.venv/bin/python -c "import pikachu"` succeeds
- `mypy --strict src/pikachu` clean
- Badges 1–4 and 7 earned (Boulder, Cascade, Thunder, Rainbow, Volcano)
- `scripts/badges.py` prints a legible badge case
- Lane E's verdict written, with a recommended default model
