"""Telemetry — the token/cost/latency ledger and the two numbers that separate blame.

This package answers one question: **did we regress, independent of which model is in use?**
It does so by never reporting a blended latency without splitting it into ``framework_ms``
(ours) and ``model_ms`` (the provider's), using the attribution already in
``core.types.TurnTiming``.

Nothing here does I/O or network — persistence is Lane L's ``storage/``. The Pokédex report
that renders these numbers lives in ``scripts/report.py`` and is TIER 2: it is a trend line and
**never** gates a build. The gate is the Earth badge (``tests/badges/test_earth.py``).

Imports are lazy per PEP 562 so ``import pikachu.telemetry`` costs nothing until a name is
actually touched — the wave rule is that a turn without telemetry pays nothing for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "AgentTotals",
    "LedgerTotals",
    "Percentiles",
    "Prices",
    "TokenLedger",
]

if TYPE_CHECKING:
    from pikachu.telemetry.ledger import (
        AgentTotals,
        LedgerTotals,
        Percentiles,
        Prices,
        TokenLedger,
    )


def __getattr__(name: str) -> Any:
    """Lazily resolve the public names from ``ledger`` on first access (PEP 562)."""
    if name in __all__:
        from pikachu.telemetry import ledger

        return getattr(ledger, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
