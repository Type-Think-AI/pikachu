"""Canvas — the append-only artifact graph. The core of the product.

No agent framework ships this. Agno's ``Artifact`` is a media wrapper with no ``parent``,
no provenance and no immutability; ours is an append-only node in a graph that records who
produced it and what it was derived from. That difference is the reason this project exists.

Why append-only rather than the classical mutable blackboard: the blackboard architecture —
agents coordinating by reading and writing a shared board instead of messaging each other —
measures 13-57% better end-to-end success than message-passing baselines, with fewer tokens
(arXiv 2510.01285). The classical board is *mutable*, which hands an attacker an overwrite
vector: poison the shared state and every reader downstream is compromised. Making the board
**append-only** removes overwrite entirely — a revision is a new node, the old one still
stands — so the only remaining adversarial move is *injection*, which taint-on-read handles.

A shared artifact space alone is not sufficient: quality measurably *drops* as agents are
added to a bare shared space (arXiv 2606.18413). The remedy is shared memory **plus approval
gates**, so the gate hook here is in scope, not optional.

Lazy import rule (BUILD-PLAN-WAVE2): a turn that never touches the canvas must not pay for
it. Nothing heavy is imported at module scope; the graph itself only depends on the
already-loaded core types, so ``from pikachu.canvas import CanvasGraph`` is cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "CanvasGraph",
    "DuplicateArtifactError",
    "taint_for_reader",
]

if TYPE_CHECKING:
    from pikachu.canvas.graph import (
        CanvasGraph,
        DuplicateArtifactError,
        taint_for_reader,
    )


def __getattr__(name: str) -> object:
    """PEP 562 lazy re-export. Keeps ``import pikachu.canvas`` from eagerly pulling graph.py
    (and, transitively, anything it might grow to import) into a turn that never reads the
    board."""
    if name in __all__:
        from pikachu.canvas import graph

        return getattr(graph, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
