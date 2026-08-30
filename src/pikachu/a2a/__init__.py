"""A2A — signed Agent Cards for cross-boundary agent discovery.

★ **Cross-boundary ONLY.** A2A is the door to agents run by a *different vendor or
organisation*. It is **never** how this runtime's own in-process crew coordinates — that is
the canvas (``docs/15-extensibility.md``). Using A2A internally reintroduces the exact
message-passing topology this design rejected. See :mod:`pikachu.a2a.cards` for the full
argument.

Emit a signed card for one of our agents, consume a peer's card (verifying its signature and
tainting everything derived from it), and narrow the peer's advertised tools against our fixed
allowlist. A peer's card is untrusted; an unverifiable signature is rejected, never tolerated.

Lazy import (:pep:`562`): ``import pikachu.a2a`` does not import :mod:`cards` — and therefore
imports no crypto/serialisation machinery — until a symbol is referenced. An agent that never
speaks A2A pays nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "A2A_CARD_VERSION",
    "WELL_KNOWN_CARD_PATH",
    "AgentCard",
    "CardSigner",
    "CardVerifier",
    "CardVerificationError",
    "PeerAgent",
    "consume_card",
    "emit_card",
    "peer_effective_tools",
]

if TYPE_CHECKING:
    from pikachu.a2a.cards import (
        A2A_CARD_VERSION,
        WELL_KNOWN_CARD_PATH,
        AgentCard,
        CardSigner,
        CardVerificationError,
        CardVerifier,
        PeerAgent,
        consume_card,
        emit_card,
        peer_effective_tools,
    )


def __getattr__(name: str) -> Any:
    if name in __all__:
        from pikachu.a2a import cards

        return getattr(cards, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
