# HANDOFF-K — wire lazy submodule access into `src/pikachu/__init__.py`

**For:** the integrator (owner of the reserved `src/pikachu/__init__.py`).
**From:** Lane K (startup + lazy loading).
**Why a handoff:** the mechanism lives in `src/pikachu/_lazy.py` (mine), but *activating* it on
the top-level namespace means adding two lines to `__init__.py`, which is RESERVED. This file
contains the exact replacement and the reasoning.

---

## What already works without this handoff — do not undersell it

`import pikachu` **already does not pull `pydantic_ai`.** Measured, best-of-5 cold, fresh
subprocess each time:

```
import pikachu       55.0 ms
import pydantic_ai  217.2 ms      →  import pikachu is 3.9× cheaper, 162 ms not paid
pydantic_ai after `import pikachu`:  ABSENT ✓
```

The bare import pulls only `pikachu.core.{errors,protocols,types}` — the 40 public types — and
nothing heavier. That invariant is now **locked by tests** (`tests/test_lazy_loading.py`,
13 passing) so it cannot silently regress when a future edit adds a convenience import.

**So this handoff is not required to hit the headline win.** It is the *second* half: letting a
caller reach a feature submodule as an **attribute** — `pikachu.skills.load_skill` — without that
submodule being imported at `import pikachu` time. Today `pikachu.skills` raises
`AttributeError` unless the caller first did `import pikachu.skills` (or `from pikachu.skills
import …`). PEP 562 fixes that while keeping the submodule off the cold-start path.

Three tests are `xfail(strict=True)` pending this change and will flip to **required passes** the
moment you apply it:

- `test_lazy_attribute_access_resolves_submodule`
- `test_lazy_attribute_access_still_defers_framework`

(`strict` = if the change is applied and they *don't* pass, CI goes red — they are the
acceptance test for this handoff.)

---

## The change — add exactly two things to `src/pikachu/__init__.py`

### 1. A `TYPE_CHECKING` block so mypy/IDEs still see real submodule types

Add this near the top, after `from __future__ import annotations`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import for the type checker / IDE only — never at runtime
    from pikachu import (
        backends as backends,
        canvas as canvas,
        config as config,
        guard as guard,
        mcp as mcp,
        skills as skills,
        storage as storage,
        telemetry as telemetry,
    )
```

`mypy --strict` and Pyright/Pylance walk the `TYPE_CHECKING` branch, so `pikachu.skills.load_skill`
keeps its true signature and autocomplete. At runtime the branch is dead, so nothing is imported —
the cold-start win is untouched. The `as name` redundancy is the explicit-re-export form mypy wants
under `--strict` (`no-implicit-reexport`); without it mypy reports the names as unused.

> `canvas` and `telemetry` do not exist yet (other wave-2 lanes). Under `TYPE_CHECKING` a missing
> module makes mypy emit `import` errors, so **until those two lanes land, omit `canvas` and
> `telemetry` from the block above** and add them when they exist. `skills`, `guard`, `mcp`,
> `storage`, `backends`, `config` all exist today and are safe to include now.

### 2. Install the runtime PEP 562 loader — at the very END of the file

After `__all__` is defined (the loader reads it for `__dir__`):

```python
# Lazy submodule access (PEP 562). Keeps `import pikachu` from importing skills/, mcp/,
# canvas/, telemetry/, storage/, backends/ (and therefore pydantic_ai) — they load on first
# attribute access instead. See src/pikachu/_lazy.py and docs/23-framework-comparison.md.
from pikachu import _lazy

_lazy.install_lazy_submodules(globals())
```

That is the whole change: one `TYPE_CHECKING` block, one two-line install call. **No existing
import, symbol, or `__all__` entry is removed or reordered** — the public API is byte-for-byte
unchanged; you are only *adding* attribute resolution that used to fail.

---

## Full resulting `__init__.py` (drop-in, `canvas`/`telemetry` omitted until they exist)

```python
"""Pikachu — an agent runtime for standards-based, permission-confined agents.

The public API is deliberately boring. Themed naming lives in the product and developer
surface (test tiers, CLI output, reports) and never in the importable API: a user of this
library should never read a Pokémon reference.

    from pikachu import AgentSpec, Skill, TrustTier

``pikachu`` is an internal codename. The published distribution name is undecided, so
expect exactly one rename of this import root before any release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # type-checker / IDE only — dead at runtime, imports nothing
    from pikachu import (
        backends as backends,
        config as config,
        guard as guard,
        mcp as mcp,
        skills as skills,
        storage as storage,
    )

from pikachu.core.errors import (
    ApprovalRequired,
    BudgetExceeded,
    DoubleCaptureError,
    InjectionDetected,
    PikachuError,
    SkillParseError,
    TaintedPromotion,
    ToolDenied,
)
from pikachu.core.protocols import (
    AgentBackend,
    Biller,
    CanvasStore,
    Embedder,
    MemoryStore,
    Reservation,
    RunStore,
    SignalLedger,
    SkillStore,
)
from pikachu.core.types import (
    AgentSpec,
    Artifact,
    ArtifactKind,
    Lineage,
    MemoryRecord,
    MemoryScope,
    Provenance,
    Run,
    RunPhase,
    Signal,
    SignalKind,
    SignalSubject,
    Skill,
    SkillStatus,
    Taint,
    ToolOutcome,
    ToolSpec,
    TrustTier,
    TurnRequest,
    TurnResult,
    normalize_tool_name,
    utcnow,
)

__version__ = "0.0.1"

__all__ = [
    # types
    "AgentSpec",
    "Artifact",
    "ArtifactKind",
    "Lineage",
    "MemoryRecord",
    "MemoryScope",
    "Provenance",
    "Run",
    "RunPhase",
    "Signal",
    "SignalKind",
    "SignalSubject",
    "Skill",
    "SkillStatus",
    "Taint",
    "ToolOutcome",
    "ToolSpec",
    "TrustTier",
    "TurnRequest",
    "TurnResult",
    "normalize_tool_name",
    "utcnow",
    # protocols
    "AgentBackend",
    "Biller",
    "CanvasStore",
    "Embedder",
    "MemoryStore",
    "Reservation",
    "RunStore",
    "SignalLedger",
    "SkillStore",
    # errors
    "ApprovalRequired",
    "BudgetExceeded",
    "DoubleCaptureError",
    "InjectionDetected",
    "PikachuError",
    "SkillParseError",
    "TaintedPromotion",
    "ToolDenied",
    "__version__",
]

# Lazy submodule access (PEP 562). Keeps `import pikachu` from importing skills/, mcp/,
# storage/, backends/ (and therefore pydantic_ai) — they load on first attribute access
# instead. See src/pikachu/_lazy.py and docs/23-framework-comparison.md.
from pikachu import _lazy

_lazy.install_lazy_submodules(globals())
```

---

## After you apply it — verify

```bash
.venv/bin/python -m pytest tests/test_lazy_loading.py -q      # the 2 xfails must now PASS
.venv/bin/python -m mypy --strict src/pikachu                 # must stay clean (my files are)
.venv/bin/python scripts/startup_profile.py                   # ABSENT ✓ must still hold
```

If the two previously-xfail tests pass and `import pikachu` still shows `pydantic_ai ABSENT`, the
handoff is correct. If `startup_profile.py` ever prints `PRESENT ✗`, the `TYPE_CHECKING` guard was
dropped or a real import replaced it — that is the regression the tests exist to catch.

## One caveat to keep in mind

`install_lazy_submodules` reads `globals()["__all__"]`, so it must run **after** `__all__` is
assigned — hence "at the very end of the file". Placing the install call before `__all__` would
make `__dir__` omit the eager names. The runtime import of `_lazy` at the bottom is itself cheap
(`_lazy` imports only `importlib` + stdlib typing; measured negligible and framework-free).
