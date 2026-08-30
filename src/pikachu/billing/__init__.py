"""Metered-tool billing — the core-IP module.

No agent framework ships this, because their tools are free. Ours cost real credits per
call, so "retry the failed tool" means "charge the user twice" unless the ledger underneath
is exactly right: one charging point, refund on failure, idempotent capture, and an
``INTERRUPTED`` state that is held-for-reconciliation rather than silently released.

Public surface — a :class:`~pikachu.billing.ledger.LedgerBiller` implementing the ``Biller``
Protocol from ``pikachu.core.protocols``, plus the audit types it records::

    from pikachu.billing import LedgerBiller

    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)
    await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)   # or: await biller.release(r.id)

This subpackage imports only ``pikachu.core`` (Pydantic + stdlib) — nothing heavy at module
scope — so it honours the wave-3 lazy-import rule without needing a ``TYPE_CHECKING`` dance.
"""

from __future__ import annotations

from pikachu.billing.ledger import (
    LedgerBiller,
    LedgerEntry,
    Reservation,
    ReservationState,
)

__all__ = [
    "LedgerBiller",
    "LedgerEntry",
    "Reservation",
    "ReservationState",
]
