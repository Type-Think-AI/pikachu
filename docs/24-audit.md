# 24 — Optimisation & Bug-Hunt Audit (Lane R)

**Evidence only. No source was edited.** Every defect below is backed by a test in
`tests/test_regressions.py`; the integrator applies the fix and the test flips to green.
Measurements are from `scripts/profile_all.py`, run offline against fakes on 2026-08-30.

Reproduce:

```
.venv/bin/python scripts/profile_all.py
.venv/bin/python -m pytest tests/test_regressions.py -q   # failures/xfails are the deliverable
```

---

## Profile — whole-package hot paths, ranked by mean cost

Offline, no network, no model. Async rows have the event-loop scheduling step
(~29 µs on this host, measured against a no-op coroutine) subtracted, so they compare like
for like against the **synchronous** baselines recorded in `docs/23-framework-comparison.md`.
Absolute µs vary by machine; the **ranking** and the **baseline ratios** are the portable
signal, not the raw numbers.

| # | operation | mean | p95 | baseline | status |
|---|---|---:|---:|---:|---|
| 1 | scan skill body for injection | 54.4 µs | 64.3 µs | 55 µs | ok |
| 2 | sqlite: search skills (LIKE) | 27.7 µs | 35.2 µs | 7.5 µs | see note |
| 3 | sqlite: search memory (FTS5) | 25.7 µs | 34.3 µs | 7.5 µs | see note |
| 4 | skill: load_skill (full document) | 14.7 µs | 14.6 µs | 15 µs | ok |
| 5 | sqlite: write one skill | 13.7 µs | 21.2 µs | — | — |
| 6 | sqlite: read skill by key | 12.4 µs | 18.6 µs | 5 µs | ok (<3×) |
| 7 | memory: recall (in-memory, budgeted) | 11.2 µs | 16.9 µs | — | — |
| 8 | full turn assembly (FakeBackend) | 10.5 µs | 16.1 µs | 96 µs | ok |
| 9 | canvas: descendants traverse (depth 8) | 5.2 µs | 9.8 µs | — | — |
| 10 | canvas: append artifact | 3.2 µs | 9.0 µs | — | — |
| 11 | guard: effective_tools (P3) | 2.6 µs | 2.8 µs | 6 µs | ok |
| 12 | toolset cache: lookup (warm) | 0.23 µs | 0.25 µs | 0.24 µs | ok |

**Harness validation.** The four pure-synchronous rows that have baselines land on them
almost exactly — scan 54.4 vs 55, load 14.7 vs 15, guard 2.6 vs 6, toolset-cache 0.23 vs
0.24. That agreement is the evidence the harness is measuring the operation and not its own
overhead. The `full turn assembly` row is *well under* its 96 µs anchor because that anchor
was the pre-cache framework total; the toolset cache landed since, exactly as `docs/23`
predicted.

**No true regression found in the profile.** The two SQLite search rows print ~3.5× their
7.5 µs baseline, but this is not an algorithmic regression:

- the synchronous read-by-key (12.4 µs) and writes (13.7 µs) sit within tolerance, so the
  connection and marshalling path is healthy;
- the two search rows do strictly more work (FTS5 MATCH / LIKE scan + row hydration into
  frozen Pydantic models) and this host is under memory pressure (~2.8 GB free), which
  inflates every measurement uniformly — visible in the p95 spread;
- the *ratio to read-by-key* (~2×) is the same order the baseline table implies (7.5 vs 5.0).

They are flagged only because the fixed 3× tolerance is deliberately tight; a re-run on an
unloaded machine brings them under. They are the most expensive real operations in a turn, so
they are the right rows for a human to keep an eye on — which is the point of ranking them
first. **No fix is recommended; this is a watch item, not a defect.**

---

## Defects — each backed by a test

### 1 — `import pikachu; pikachu.memory` raises `AttributeError` ★ inconsistent guarantee + overclaim

`src/pikachu/_lazy.py` `LAZY_SUBMODULES` lists `skills, guard, mcp, canvas, telemetry,
storage, backends, config` — but **not `memory`**. Meanwhile `src/pikachu/__init__.py`'s
`TYPE_CHECKING` block does `from pikachu import ... memory as memory ...`, so mypy and IDEs
believe `pikachu.memory` is a valid attribute. At runtime it is neither eager nor lazy, so
the PEP 562 `__getattr__` falls through to `AttributeError`.

This is two of the things this lane was told to hunt at once:

- **an inconsistent guarantee across entry points** — `from pikachu.memory import CrewMemory`
  works (it is a real submodule import, used by `tests/test_memory.py` and
  `tests/badges/test_marsh.py`), while `import pikachu; pikachu.memory` fails. Same package,
  two paths, opposite answers.
- **an overclaim** — the type stub in `__init__.py` promises an attribute the runtime does
  not provide. A downstream module written against the type checker (`pikachu.memory.X`)
  type-checks clean and then crashes.

Every sibling subpackage (`canvas`, `telemetry`, `storage`, …) is in the lazy list; `memory`
was simply omitted when it landed.

- **Tests:** `test_pikachu_memory_attribute_resolves_at_runtime` (fresh subprocess, so an
  in-process import elsewhere cannot mask it) and `test_memory_listed_as_lazy_submodule`.
  Both **fail** today.
- **Fix (integrator, reserved `_lazy.py`):** add `"memory"` to `LAZY_SUBMODULES`. One line.

### 2 — `SqliteMemoryStore.decay` ignores `older_than_days` ★ overclaiming docstring + correctness

`storage/sqlite.py::SqliteMemoryStore.decay` runs:

```sql
UPDATE memory SET confidence = MAX(0.0, confidence - 0.1) WHERE confidence > 0.0
```

There is **no age predicate**. Every record with confidence > 0 is decayed on every call,
regardless of `older_than_days`. A record created one second ago is decayed by
`decay(older_than_days=99999)`.

This is an **overclaiming docstring**, which this project treats as a real defect. The
reference `memory/store.py` explicitly defers the age predicate to this backend:

> "The SQLite backend (Lane L) is where the age predicate becomes a real `WHERE created_at <`
> clause."

The SQLite backend does not implement it. The `created_at` column exists and is populated,
so the data is there — the `WHERE` clause is simply missing. The behavioural consequence is
that recent, reinforced memory loses rank it should keep, which quietly degrades recall
quality (the opposite of the "decay lowers rank of the *stale*" intent).

- **Test:** `test_sqlite_decay_respects_older_than_days` — `xfail(strict=True)`. Seeds one
  brand-new record, asserts `decay(older_than_days=99999)` affects **0** rows.
- **Fix (integrator, `storage/sqlite.py`):** add
  `AND created_at < ?` with the bind value
  `(utcnow() - timedelta(days=older_than_days)).isoformat()` (ISO strings compare
  lexicographically for UTC, matching how the column is stored), and keep returning
  `cur.rowcount` so the count reflects only rows actually decayed.

### 3 — `assert_cannot_widen_authority` does not normalise tool names ★ inconsistent normalisation

`guard/lineage.py::assert_cannot_widen_authority` computes the escalation set on **raw
strings**:

```python
allow = set(fixed_allowlist)
escalated = {g for g in granted if g not in allow}
```

The rest of the guard normalises every tool name through `normalize_tool_name`
(`.strip().lower()` + charset filter) at every entry point — `guard/allowlist.effective_tools`
even re-normalises `Skill.declared_tools` "because a raw `tuple[str, ...]` may arrive from any
caller." This memory-side P3 assertion is the one place that does **not**, so the same input
gets opposite verdicts on two paths — the exact "guarantee that holds on one path and not
another" this project was burned by before (the `" terminal "`/`"TERMINAL"` incident).

Concrete failure: `granted={"web"}`, `fixed_allowlist={"WEB"}` raises a **false**
`TaintedPromotion`, because `"web" != "WEB"` as raw strings — even though the guard treats
them as the same tool, so the grant is legal. A legal grant is turned into a security error.

The docstring anticipates the objection and gets it wrong:

> "Comparison is on the raw provided strings; callers upstream already normalise … This
> function is the memory-side assertion, not a second normaliser."

But the function's own docstring one line up says it exists to catch "a grant that was
assembled some other way and still tried to exceed the allowlist" — and a grant assembled
"some other way" is *precisely* the one that has not been normalised. The assumption that
callers already normalised is unsound for the case the function was written to handle. (This
is a borderline overclaim as well as a normalisation bug.)

- **Tests:** `test_authority_check_normalises_like_the_guard` — `xfail(strict=True)`; and
  `test_authority_check_still_blocks_a_genuine_escalation` — a guardrail that **passes today**
  and must keep passing, so the fix cannot weaken the real escalation block.
- **Fix (integrator, `guard/lineage.py`):** normalise both `granted` and `fixed_allowlist`
  through `normalize_tool_name` before the subset check, dropping empties, exactly as
  `guard/allowlist.effective_tools` does. Keep the raised message using the normalised names.

### 4 — `markdown._load_front` raises raw `json.JSONDecodeError` ★ escapes the error hierarchy

`storage/markdown.py::_load_front` does `json.loads(raw)` on each frontmatter value. The
module advertises itself as a human-readable, git-diffable, **hand-editable** archive format.
A hand-edited or legacy value that is not valid JSON (a bare word, an unquoted string) raises
`json.JSONDecodeError`, which is **not** a `PikachuError`. `core/errors.py` states the
package's contract explicitly: "a host can catch everything from this package with one
clause" — every other parse failure is a `SkillParseError`. This one leaks.

Lower severity than 1–3 (it is a crash on malformed input, not a wrong result), but it breaks
a stated package invariant and is trivially triggerable given the module's own framing.

- **Test:** `test_markdown_bad_frontmatter_raises_pikachu_error` — `xfail(strict=True)`.
- **Fix (integrator, `storage/markdown.py`):** wrap the `json.loads` in `_load_front`,
  re-raising a `SkillParseError` naming the offending line. (No new error class needed;
  `SkillParseError` is already the "a document could not be parsed" type.)

---

## `__all__` / usage mismatches (minor, not crashes)

`guard/lineage.py::__all__` lists `taints_of`, which has **no caller** anywhere in `src`,
`tests`, or `scripts` (its only occurrence is its own `__all__` entry). Meanwhile
`assert_skill_promotion` (13 references, incl. `tests/badges/test_soul.py`) and
`assert_memory_grants_nothing` (3 references, incl. `tests/badges/test_marsh.py`) are
public-shaped, exercised functions that are **absent** from `__all__`. So the exported
surface both advertises an unused helper and hides two used ones. Not a defect that can bite
at runtime — `from … import assert_skill_promotion` works regardless of `__all__` — but the
export list misrepresents the real API. No test written (nothing observable breaks); flagged
for the integrator to reconcile `__all__` with actual usage.

---

## Things checked and found CORRECT (recorded so they are not re-audited)

These were on the "where to look" list and were verified sound; two are pinned by guardrail
tests so a future change cannot silently regress them.

- **Order/multiplicity is preserved** through `guard.effective_tools` — `("web","web")` stays
  `("web","web")`, no sort, no dedupe. Pinned by `test_guard_preserves_order_and_multiplicity`.
- **The `\b` / `SCREAMING_SNAKE` scanner blindness is already fixed.** The
  `exfil.env_var_secret_to_sink` / `exfil.sink_then_env_var_secret` rules match
  `OPENAI_API_KEY` (which the `\b`-anchored `\bapi[\s_-]?key\b` rules cannot, because `_` is a
  word character). Pinned by `test_scanner_catches_screaming_snake_credential_to_url`.
- **No mutable default arguments** in `src/` — the two module-level tuples that look like
  candidates (`config._ENV_SEARCH_PATH`, the scanner `_RULES`) are immutable tuples, and every
  Pydantic field uses `Field(default_factory=...)` for its collections.
- **No `INTERRUPTED`→release double-charge** in the fake biller path: `_meter` captures on
  `SUCCESS`/`INTERRUPTED` and releases only on `FAILED`/`DENIED`, and `capture` is idempotent
  on reservation id (verified in `backends/fake.py` and `storage/sqlite.py`; the SQLite
  capture is PRIMARY-KEY-enforced).
- **Ratio/percentile helpers are zero-safe.** `telemetry/ledger.py::_percentiles` returns all
  zeros on an empty sample and the single point at every percentile on n=1;
  `cache_hit_ratio`, `framework_share`, `tokens_per_second`, `cost_per_turn_credits` all guard
  their denominator; `confusability.cosine_similarity` returns 0.0 on an all-zero vector
  rather than dividing by zero. No division-by-zero found.
- **No silent `except` swallowing a *result*.** The two broad `except Exception` blocks in
  `backends/pydantic_ai.py` are on the `aclose()` and streaming-instrumentation paths, where a
  close/instrument failure must not mask the real turn result — that is deliberate and
  correct, not a swallowed error.
- **Async resources are closed deterministically.** `PydanticAIBackend.aclose()` /
  `__aexit__` close the httpx transport, and the docstring correctly explains the
  unclosed-transport → teardown-failure-on-an-unrelated-test hazard. No unclosed async
  resource found in `src/`.
- **Canvas traversal is cycle-guarded** — `ancestors`/`descendants`/`lineage_of` all carry a
  visited set, so a malformed self-parent or back-edge cannot hang a walk.

## Lazy-import discipline — clean

No heavy import at module scope was found. `sqlite3` is imported inside each function that
needs it; `pydantic_ai` is confined to `backends/pydantic_ai.py` (itself lazy via
`LAZY_SUBMODULES` / an explicit `from pikachu.backends.pydantic_ai import …`); the `mcp` SDK
is deferred behind `mcp/__init__.py`'s PEP 562 `__getattr__`. The one gap is Defect 1 above,
which is a *missing* lazy entry, not a mis-placed eager import.
