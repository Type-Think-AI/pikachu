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
    # Explicit `as name` re-export form, which is what mypy --strict requires under
    # no-implicit-reexport. Walking this branch keeps `pikachu.skills.load_skill` fully
    # typed with working autocomplete, while at runtime the branch never executes and the
    # cold-start win is untouched.
    #
    # HANDOFF-K.md omitted canvas and telemetry because those lanes had not landed when it
    # was written; both exist now, so they are included.
    from pikachu import (
        backends as backends,
        canvas as canvas,
        config as config,
        guard as guard,
        mcp as mcp,
        memory as memory,
        skills as skills,
        storage as storage,
        telemetry as telemetry,
        tools as tools,
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
# canvas/, memory/, telemetry/, storage/ or backends/ — and therefore from importing
# pydantic_ai at all. They resolve on first attribute access instead.
#
# Measured: `import pikachu` 55.0 ms vs `import pydantic_ai` 217.2 ms, so a caller who only
# wants the types never pays 162 ms for a model framework. tests/test_lazy_loading.py locks
# the invariant in a subprocess, because a lazy-loading claim without that assertion rots the
# first time someone adds a convenience import.
#
# Installed last, after __all__ exists — the loader reads it for __dir__.
from pikachu import _lazy

_lazy.install_lazy_submodules(globals())
