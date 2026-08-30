# BUILD PLAN — Wave 2

Six lanes, **exclusive file ownership**, same contract that made wave 1 work (zero reserved-file
writes, zero handoffs). Read `BUILD-PLAN.md` first — the rules there still apply in full.

**Decision context, settled 2026-08-30:** Pikachu is built solo on Pydantic AI. Agno is **not** a
dependency and must not be imported. The engine choice is closed; what we own is the layer no
framework provides — canvas, permission guard, metered tools, taint/lineage.

---

## Two decisions already measured — build to these, do not re-litigate

### Storage: SQLite is the engine, markdown is an export format

Measured on 2,000 memory records, local disk, warm cache:

| Operation | SQLite | md (1 file/record) | md (single file) | JSON |
|---|---:|---:|---:|---:|
| read by key | **5.0 µs** | 30.6 µs | 205.5 µs | 829 µs |
| **search** | **7.5 µs** | **38,883 µs** | 238 µs | 867 µs |
| write one record | 349 µs | **73.6 µs** | — | 1,104 µs |

Search is what retrieval actually does, and SQLite FTS5 wins it by **32× to 5,184×**. Markdown per
file is catastrophic there — 38.9 ms per search, so ten recalls in a turn would cost 389 ms.

For scale: a remote Postgres round trip measured **250,000–600,000 µs** in this project, and a model
turn waits ~2,900,000 µs. So DB latency is **materially** part of a turn (10–20%), unlike framework
overhead which is 0.01%. Getting this right matters; getting instantiation right did not.

SQLite's only loss is single-record write (349 µs vs 73 µs) and that is fsync — batch writes in one
transaction and it disappears.

### Lazy loading: pay for nothing you do not use

Measured startup is 236 ms on the first call and ~0 after, almost all of it `import pydantic_ai`
(298 ms cold). The rule for every module in this wave:

> **Import nothing at module scope that a turn without that feature would not need.**

If an agent has no skills, the skill loader, scanner, embedder and confusability code must never be
imported. If it has no MCP servers, the MCP client must not be imported. Put heavy imports inside the
function that needs them, and make the absence of a feature the cheap path.

---

## Reserved files — integrator only, no lane writes these

```
pyproject.toml
src/pikachu/__init__.py
src/pikachu/core/types.py
src/pikachu/core/protocols.py
src/pikachu/core/errors.py
src/pikachu/config.py
tests/conftest.py
BUILD-PLAN.md   BUILD-PLAN-WAVE2.md
```

Need a change there? Write `HANDOFF-<LANE>.md` with the exact code and stop.

---

## Lane G — `memory/` + taint propagation ★ security-critical
**Owns:** `src/pikachu/memory/__init__.py`, `memory/store.py`, `src/pikachu/guard/lineage.py`,
`tests/test_memory.py`, `tests/badges/test_soul.py`, `tests/badges/test_marsh.py`

Implements the `MemoryStore` Protocol logic (the SQLite *backend* is Lane L — you build the
in-memory reference implementation and the taint rules).

- Three scopes per `MemoryScope`: SHORT (this turn), MID (this conversation), LONG (**shared across
  the crew** — this is the answer to day-one emptiness: a new agent joins a house that already knows
  the brand).
- `confidence` decays without reinforcement; `evidence_count` rises with it. **Decay lowers rank and
  never deletes.**
- **Retrieval is budgeted.** Unbounded recall destroys the stable prompt prefix, which breaks
  caching. Enforce a hard cap.
- **`guard/lineage.py`:** taint propagation. A value derived from tainted input inherits the taint,
  monotonically (`Lineage` already has no `clear()` — keep it that way). **Tainted content can never
  be promoted and can never widen authority.** Raise `TaintedPromotion`.
- **Marsh badge:** prove `MemoryRecord.may_justify_authority` is structurally False and that no code
  path can derive a tool grant from memory.
- **Soul badge:** hypothesis property test — for any chain of derivations, taint never disappears.

Earns **Soul** and **Marsh**.

## Lane H — `canvas/` ★ the core of the product
**Owns:** `src/pikachu/canvas/__init__.py`, `canvas/graph.py`, `tests/test_canvas.py`,
`tests/properties/test_canvas.py`

The append-only artifact graph. **No framework has this** — Agno's `Artifact` is a media wrapper with
no `parent`, no provenance, no immutability — so this is genuinely ours and the reason the project
exists.

- Append-only. `append()` **rejects** an existing id rather than overwriting.
- A revision is a **new** artifact with `parent` set. Nothing mutates.
- Provenance records the **producing agent** — on a shared canvas "who made this frame" is a real
  question and it is how output quality is judged later.
- Graph reads: `children()`, `ancestors()`, `descendants()`, and a `lineage_of()` that walks parents.
- **Coordination scaffolding is in scope, not optional:** a shared artifact space alone measurably
  *lowers* quality as agents are added ([arXiv 2606.18413](https://arxiv.org/html/2606.18413)); the
  remedy is shared memory **plus approval gates**. Provide the gate hook — an artifact kind that
  requires approval before downstream agents may read it.
- `guard/` must cover canvas **reads**: append-only stops overwrites, not injection. A poisoned
  artifact another agent reads is still poison, so reading a tainted artifact must taint the reader.
- Property test: **no operation mutates an existing artifact**, over arbitrary op sequences.

## Lane I — `mcp/` client
**Owns:** `src/pikachu/mcp/__init__.py`, `mcp/client.py`, `tests/test_mcp.py`

- Add the dependency via `HANDOFF-I.md` (pyproject is reserved). `mcp==2.1.1` is verified to expose
  `LATEST_PROTOCOL_VERSION = "2026-07-28"`.
- **The critical requirement:** `DEFAULT_NEGOTIATED_VERSION` in that SDK is **`2025-03-26`**, three
  revisions behind. A client that does not *explicitly* request `2026-07-28` silently negotiates the
  old revision and loses statelessness, required `server/discover`, and `resultType`. **Assert the
  negotiated revision in a test.** The failure mode is a silent downgrade, not an error.
- Import `mcp` **lazily** — an agent with no MCP servers must not pay for it.
- No network in tests. Use a fake transport.

## Lane J — `telemetry/` + the Pokédex report
**Owns:** `src/pikachu/telemetry/__init__.py`, `telemetry/ledger.py`, `scripts/report.py`,
`tests/test_telemetry.py`, `tests/badges/test_earth.py`

- Token/cost ledger per run. Use the existing `TurnTiming` phases — **framework vs model attribution
  is the whole point**, so never report a single blended latency.
- `scripts/report.py` prints the **Pokédex**: tier-2 trend stats, explicitly non-gating, visually
  distinct from the badge case. Cost per turn, cache-hit ratio, latency percentiles, partition
  confusability, tokens/sec on decode only.
- **Earth badge:** a latency *budget* test. Assert `timing.framework_ms` stays under a threshold
  (currently ~0.3 ms; set the gate at 5 ms so it catches a real regression without being flaky).
  This is the test that catches us regressing our own code, independent of which model is used.

Earns **Earth**.

## Lane K — startup + lazy loading ★ the owner's explicit ask
**Owns:** `src/pikachu/_lazy.py`, `scripts/startup_profile.py`, `tests/test_lazy_loading.py`

- `scripts/startup_profile.py`: measure cold `import pikachu` and attribute it per submodule, using
  a fresh subprocess per measurement (in-process re-import is free and would report ~0).
- Make `import pikachu` cheap. It currently pulls `core.*` only — **verify that it does not
  transitively import `pydantic_ai`**, and if it does, fix it. Importing our types should not cost
  298 ms.
- Provide the lazy mechanism (module `__getattr__` per PEP 562, or a small lazy-proxy helper) and
  apply it so `skills/`, `mcp/`, `canvas/`, `telemetry/` and `backends/pydantic_ai` load **on first
  use**.
- **Test it properly:** assert via a subprocess that `import pikachu` leaves `pydantic_ai` absent
  from `sys.modules`, and that touching a feature pulls in exactly what it needs and nothing more.
  That assertion is the whole deliverable — a lazy-loading claim without it rots immediately.
- Do NOT break `from pikachu import Agent, Skill` — the public API must keep working unchanged.

## Lane L — `storage/` SQLite backends + markdown export
**Owns:** `src/pikachu/storage/__init__.py`, `storage/sqlite.py`, `storage/markdown.py`,
`tests/test_storage.py`

- SQLite implementations of the Protocols in `core/protocols.py`: `SkillStore`, `MemoryStore`,
  `RunStore`, `CanvasStore`. One file, WAL mode, `FTS5` for search.
- **`SkillStore.find` must structurally return only retrievable skills** — build the status filter
  into the SQL, not into a caller-supplied argument. The safest access control is the kind that
  cannot be called wrongly.
- **`RunStore` capture must be idempotent** and a second capture of one reservation id must raise
  `DoubleCaptureError`. This is the no-double-charge invariant and it is the reason this lane is not
  cosmetic.
- **`CanvasStore.append` must reject a duplicate id**, enforced by a PRIMARY KEY rather than a check.
- `storage/markdown.py`: **export and import only.** Human-readable, git-diffable, one file per
  record with YAML frontmatter. Document in the module docstring that it is deliberately **not** a
  retrieval path, with the measured reason: search over md-per-file is 38,883 µs against SQLite's
  7.5 µs, a 5,184× difference.
- Import `sqlite3` lazily. Batch writes in one transaction — SQLite's only measured loss is per-write
  fsync.
- No network. Use `:memory:` or a tmp_path fixture in tests.

---

## Definition of done for wave 2

- All **8 badges earned** — Soul, Marsh and Earth are the remaining three
- `mypy --strict` clean, offline suite green, no network in the offline suite
- `import pikachu` does **not** pull `pydantic_ai`, proven by a subprocess test
- A negotiated MCP revision of `2026-07-28` asserted in a test
- `scripts/report.py` prints a Pokédex distinct from the badge case
- Nothing imports `agno`
