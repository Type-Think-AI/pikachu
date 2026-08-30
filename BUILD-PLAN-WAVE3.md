# BUILD PLAN — Wave 3 (final modules)

Seven lanes completing every module the roadmap still lists. Read `BUILD-PLAN.md` for the
ownership contract and `BUILD-PLAN-WAVE2.md` for the lazy-import and storage rules — both still
apply in full.

**Where we are:** 9 modules built (~5,832 lines), 410 tests, `mypy --strict` clean, **8/8 gym
badges earned**. Engine is Pydantic AI; Agno is not a dependency and must not be imported.

**What remains:** `billing/`, `curator/`, `plugins/`, `discovery/`, `durability/`, `a2a/`,
`webmcp/`, `authz/` — plus a dedicated optimisation and bug-hunt pass.

---

## Two patterns from wave 2 that every lane must reuse

Wave 2's handoffs were **better than the specs I wrote for them**, in two specific ways. Both are
now required, not optional:

**1. A handoff that ships its own verification.** Lane K marked its acceptance tests
`@pytest.mark.xfail(reason=..., strict=True)`. When the integrator applied the change, the strict
xfail turned into a reported failure *if the change did not actually work* — so the handoff proved
itself. If you need a reserved-file change, write `HANDOFF-<LANE>.md` with exact code **and** leave
strict-xfail tests that flip to required passes on application.

**2. Argue with the spec when the spec is wrong.** Lane I was told to add a dependency; it argued
for an optional **extra** instead, so an agent not using the feature never installs it. That was a
better answer than mine. If a requirement below is wrong, say so in your report with reasoning
rather than implementing it silently.

## Reserved files — integrator only

```
pyproject.toml
src/pikachu/__init__.py            (now carries the PEP 562 lazy loader — do not disturb it)
src/pikachu/core/{types,protocols,errors}.py
src/pikachu/config.py
tests/conftest.py
BUILD-PLAN.md  BUILD-PLAN-WAVE2.md  BUILD-PLAN-WAVE3.md
```

## Rules (condensed — full versions in `BUILD-PLAN.md`)

1. Own your files. Read anything. Write only yours.
2. `from __future__ import annotations` everywhere. `mypy --strict` must pass.
3. **Lazy imports.** Nothing at module scope a turn without your feature would need. `import
   pikachu` must not pull `pydantic_ai` — `tests/test_lazy_loading.py` enforces it, do not break it.
4. **No network in tests. Ever.** The autouse socket block in `tests/conftest.py` is not optional.
5. Property tests (`hypothesis`) for anything invariant-shaped.
6. **Do not invent an API.** Verify against the installed package. Several confident guesses have
   already been wrong on this project.
7. **No `git commit`.** Leave work in the tree.
8. **Report honestly.** Name what you did not finish. A partial lane described accurately is useful;
   one described as done is a trap for the integrator.
9. Badges are full at 8/8. Add tests to the **existing** badge markers — do not invent a ninth.

---

## Lane M — `billing/` ★ core IP, no framework has this
**Owns:** `src/pikachu/billing/__init__.py`, `billing/ledger.py`, `tests/test_billing.py`,
`tests/properties/test_billing.py`

Only the `Biller`/`Reservation` **Protocols** exist. Build the implementation.

- reserve → capture | release. **Capture MUST be idempotent on `reservation_id`**; a second capture
  raises `DoubleCaptureError`.
- **One charging point, refund on failure** (invariant P5). Every paid operation flows through it.
- `ToolOutcome.INTERRUPTED` means the side effect **may** have happened. Do **not** collapse it into
  failure and release — that path double-charges on the retry. Model it as a distinct state needing
  reconciliation, and document what a caller must do about it.
- A ledger of every reservation with its terminal state, so a run's spend is auditable after the
  fact. `Run.captured_reservations` already exists in the contract — use it.
- Property test: **for any interleaving of reserve/capture/release/retry, total charged never
  exceeds total reserved, and no reservation is captured twice.** That is the invariant the whole
  module exists for.
- Tag the property tests `@pytest.mark.thunder` (authority/economics invariants).

## Lane N — `curator/` + the agent-generated-skill scanner ★ security-gated
**Owns:** `src/pikachu/curator/__init__.py`, `curator/lifecycle.py`, `curator/distil.py`,
`src/pikachu/guard/authored.py`, `tests/test_curator.py`, `tests/badges/test_soul_curator.py`

**Read `docs/06-security.md` and `docs/13-self-improvement.md` first.** A recorded hard
prerequisite applies: `guard/` must cover **agent-generated** skills before `curator/` may ship.

- `guard/authored.py`: run the **same scanner** over agent-generated skills as over imported ones.
  Provenance `agent_created` confers **no** trust. A skill distilled from a turn that consumed
  untrusted tool output or a foreign skill body **inherits that taint**, and **taint blocks
  promotion** (`TaintedPromotion`). Without this the distil step is a laundering path: poison one
  turn, it becomes a draft, reuse promotes it, and the injection is durable *and* carries our own
  provenance.
- `curator/distil.py`: the four-check creation gate from `docs/03-skill-lifecycle.md` — succeeded,
  non-trivial, **not a near-duplicate** (reuse `skills/confusability.py`, do not reimplement), and
  parameterisable. **Most turns must produce no skill.** Record every rejection with its reason;
  the rejection log is how the gate gets tuned.
- `curator/lifecycle.py`: `draft → candidate` on first reuse, `candidate → active` at ≥3 successful
  uses. **Only candidate and active are retrievable.** Improvement writes a **new version**, never
  mutates. **Archive, never delete.** Pinned skills bypass every automatic transition — a user
  override the machine may not argue with.
- Adopt the published split: **an LLM may diagnose and propose; only deterministic code measures and
  credits.** No model call decides a promotion. Promotion thresholds are plain Python.
- Evidence to respect rather than re-argue: two independent sources find self-generated skills give
  no benefit or actively degrade performance. The gate is the feature.

## Lane O — `plugins/` Agent Plugins 1.0.0
**Owns:** `src/pikachu/plugins/__init__.py`, `plugins/manifest.py`, `plugins/loader.py`,
`tests/test_plugins.py`

Schema facts **verified** in `docs/22-phase0-verification.md` Q2 — build to these, do not re-fetch:

- Required: **`$schema` and `name` only**. `$schema` is a **`const`** — it must equal
  `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json` exactly. Assert the literal.
- `name` pattern: `^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$` — note the negative
  lookahead forbidding `--` and `..`, a path-traversal and confusable-name defence. **Adopt the same
  rule for our own skill names.**
- Optional: `version, description, author, homepage, repository, license, keywords, extensions`.
- **`additionalProperties: false` — the manifest is CLOSED.** A top-level reverse-DNS key makes a
  manifest *invalid*; vendor extensions belong under `extensions`.
- `skills/` and `mcp.json` are **directory conventions, not manifest fields**. `plugin.json` cannot
  be relocated or inlined.
- ★ **Independent component failure:** a broken `mcp.json` must **not** take `skills/` down. Load
  each component separately, collect per-component errors, and return a partial result with the
  failures attached. Test it with a fixture whose `mcp.json` is malformed.
- Everything loaded from a plugin is **untrusted**: `TrustTier.UNTRUSTED`, tainted, and it
  contributes no toolsets. Route through `guard/`.

## Lane P — `discovery/` + AgentSpec registry + routing
**Owns:** `src/pikachu/discovery/__init__.py`, `discovery/registry.py`, `discovery/routing.py`,
`tests/test_discovery.py`

- A registry of user-created `AgentSpec`s: create, list, get, retire. Six declarative fields, made
  at runtime, no code. The end user of a product built on this SDK is the underserved persona.
- **Conservative routing:** trigger-match only. An agent with no triggers is **by-name invocation
  only** and is never auto-selected. Ambiguous match → do not guess; return the candidates.
  Default is a single agent; multi-agent is opt-in.
- ★ **Partition management is a correctness feature, not cosmetics.** Each agent's `skill_tags`
  define its partition, and the partition keeps its selectable set below the confusability cliff
  where selection accuracy drops sharply. Wire `skills/confusability.py` in: warn when a new skill
  is too close to an existing one **in the same partition**, and expose a "this agent should be
  split" signal based on max pairwise description similarity (constraint C7).
- Expose max-pairwise-similarity per partition as a metric for the Pokédex (`telemetry/`).

## Lane Q — `durability/` ★ where resume meets money
**Owns:** `src/pikachu/durability/__init__.py`, `durability/checkpoint.py`, `durability/resume.py`,
`tests/test_durability.py`, `tests/properties/test_resume.py`

- Checkpoint a `Run` after every iteration through `RunStore`; resume from the last checkpoint.
- ★ **Invariant P9: a resume must NEVER re-capture a captured reservation.** Generic durable
  execution is at-least-once, which is *unsafe to repeat* for a paid image generation.
  `Run.captured_reservations` is the guard — consult it on resume and refuse a duplicate capture.
- **Property test:** for any crash point in a multi-iteration run, resuming produces the same total
  spend as an uninterrupted run. Simulate crashes at every iteration boundary and mid-tool.
- Cooperate with Lane M's ledger via the `Biller` Protocol; do not import their module directly if
  it does not exist yet — code against the Protocol, which is the contract that makes lanes
  independent.
- Do **not** add Temporal/DBOS/Prefect. Provide the checkpoint/resume seam those would plug into,
  and note in the docstring that they are integrations, not requirements.

## Lane R — optimisation + bug hunt ★ EVIDENCE ONLY, no edits to others' files
**Owns:** `docs/24-audit.md`, `tests/test_regressions.py`, `scripts/profile_all.py`

You are the only lane that reads everything, so you must not *write* everything. **Do not edit any
module another lane owns.** Produce evidence; the integrator applies fixes.

- Profile the whole package: `scripts/profile_all.py` covering skill load, scan, guard, canvas
  append/traverse, memory recall, SQLite read/search/write, and full turn assembly against
  `FakeBackend`. Report mean/p95 in µs and rank by cost.
- **Hunt real bugs.** For each one, write a **failing or `xfail(strict=True)`** test in
  `tests/test_regressions.py` that demonstrates it, and describe the fix in `docs/24-audit.md`.
  A failing test is worth ten paragraphs of prose.
- Where to look, based on what has already bitten this project: inconsistent normalisation across
  entry points; anything that dedupes or sorts where order is load-bearing; `\b` word-boundary
  regexes against `snake_case` identifiers; silent `except` swallowing; unclosed async resources;
  mutable default arguments; anything that can widen a permission grant; error paths that lose a
  reservation.
- Also report: dead code, modules imported but unused, duplicated logic across lanes, and any
  docstring that **overclaims** what the code does. Overclaiming docs are a real defect here.
- Do **not** report style opinions or suggest reformatting. Only defects, measurements, and
  overclaims.

## Lane S — `a2a/` + `webmcp/` + `authz/` (external boundaries)
**Owns:** `src/pikachu/a2a/__init__.py`, `a2a/cards.py`, `src/pikachu/webmcp/__init__.py`,
`webmcp/tools.py`, `src/pikachu/authz/__init__.py`, `authz/oauth.py`, `tests/test_a2a.py`,
`tests/test_webmcp.py`, `tests/test_authz.py`

Three small surfaces, deliberately last because they are the least load-bearing.

- **`a2a/`**: signed Agent Cards at a well-known URI; emit ours, consume a peer's, verify the
  signature. ★ **Cross-boundary ONLY** — different vendors or organisations. It must **never** be
  used for internal crew coordination; that is the canvas. Say so in the module docstring, because
  the temptation to use it internally is exactly how the message-passing topology we rejected comes
  back.
- **`webmcp/`**: expose an agent's tools to a browser page. Verified constraints — a tool's
  `execute` must return the **content envelope** `{content:[{type:'text',text:...}]}` and never a
  bare string; declarative form attributes are **bare** (`toolname`, `toolparamdescription`), never
  `data-` prefixed; `Origin-Agent-Cluster: ?1` is a hard requirement and `?0` disables WebMCP
  outright. A declarative form without `toolautosubmit` needs a real `<button type="submit">`.
- **`authz/`**: OAuth 2.1 for MCP. Server is the resource server; `401` → RFC 9728 metadata
  discovery; PKCE required; RFC 8707 resource indicator. **DCR is deprecated** in favour of Client
  ID Metadata Documents (back-compat ≥12 months) — do not build DCR as the primary path.
- No network in tests. Fake the HTTP layer.

---

## Definition of done for wave 3

- Every module in the roadmap exists, `mypy --strict` clean, offline suite green
- **8/8 badges still earned** — no regression
- `import pikachu` still does not pull `pydantic_ai` (subprocess-asserted)
- P5 (one charging point, refund on failure) and **P9 (resume never re-captures)** both proven by
  property tests
- A broken `mcp.json` does not prevent a plugin's skills from loading
- `docs/24-audit.md` exists with measurements and a defect list backed by tests
- Nothing imports `agno`
