"""Pikachu — an agent runtime for standards-based, permission-confined agents.

The public API is deliberately boring. Themed naming lives in the product and developer
surface (test tiers, CLI output, reports) and never in the importable API: a user of this
library should never read a Pokémon reference.

    from pikachu import AgentSpec, Skill, TrustTier

``pikachu`` is an internal codename. The published distribution name is undecided, so
expect exactly one rename of this import root before any release.
"""

from __future__ import annotations

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
