"""Example tests for the billing ledger.

These are the concrete, named scenarios — refund on failure, idempotent capture under
repetition, ledger auditability, the ``INTERRUPTED`` reconciliation path, and the
``isinstance`` round-trip against the ``runtime_checkable`` ``Biller`` Protocol. The
*arbitrary-interleaving* invariants live in ``tests/properties/test_billing.py``; this file
pins the behaviours a reader needs to see spelled out.
"""

from __future__ import annotations

import pytest

from pikachu.billing import LedgerBiller, LedgerEntry, Reservation, ReservationState
from pikachu.core.errors import DoubleCaptureError
from pikachu.core.protocols import Biller
from pikachu.core.protocols import Reservation as ReservationProto
from pikachu.core.types import ToolOutcome


# --------------------------------------------------------------------------------------
# Protocol conformance — earns its place in the Cascade round-trip
# --------------------------------------------------------------------------------------


def test_ledger_biller_satisfies_biller_protocol() -> None:
    """The real biller is an instance of the runtime_checkable Biller Protocol."""
    biller = LedgerBiller()
    assert isinstance(biller, Biller)


@pytest.mark.asyncio
async def test_reservation_satisfies_reservation_protocol() -> None:
    """A reservation exposes the ``id`` and ``amount`` the Reservation Protocol requires.

    The ``Reservation`` Protocol is intentionally NOT ``runtime_checkable`` in
    ``core.protocols`` (only ``Biller`` is), so this asserts the structural surface directly
    rather than via ``isinstance`` — a static ``Reservation`` annotation below is what proves
    conformance to mypy.
    """
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)
    assert isinstance(r, Reservation)
    proto: ReservationProto = r  # structural conformance, checked by mypy
    assert proto.id
    assert proto.amount == 35


# --------------------------------------------------------------------------------------
# reserve -> capture (the happy path) and P5's one charging point
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_success_charges_the_amount() -> None:
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)
    assert biller.total_charged() == 0  # reserved, not yet charged

    await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)
    assert biller.total_charged() == 35
    assert biller.total_reserved() == 35
    assert biller.is_captured(r.id)
    assert r.id in biller.captured_reservations()


@pytest.mark.asyncio
async def test_total_charged_never_exceeds_total_reserved() -> None:
    biller = LedgerBiller()
    a = await biller.reserve(run_id="run-1", tool="t", amount=10)
    b = await biller.reserve(run_id="run-1", tool="t", amount=20)
    await biller.capture(a.id, outcome=ToolOutcome.SUCCESS)
    await biller.capture(b.id, outcome=ToolOutcome.SUCCESS)
    assert biller.total_charged() == 30
    assert biller.total_charged() <= biller.total_reserved()


# --------------------------------------------------------------------------------------
# refund on failure — release returns the reservation unspent
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_refunds_and_charges_nothing() -> None:
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)
    await biller.release(r.id)
    assert biller.total_charged() == 0
    assert not biller.is_captured(r.id)
    assert r.id not in biller.captured_reservations()
    (entry,) = biller.ledger()
    assert entry.state is ReservationState.RELEASED
    assert entry.outcome is None


@pytest.mark.asyncio
async def test_release_is_idempotent() -> None:
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=5)
    await biller.release(r.id)
    await biller.release(r.id)  # no raise, no change
    await biller.release("res-does-not-exist")  # unknown -> no-op
    assert biller.total_charged() == 0


@pytest.mark.asyncio
async def test_released_reservation_cannot_later_be_captured() -> None:
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=5)
    await biller.release(r.id)
    with pytest.raises(DoubleCaptureError):
        await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)
    assert biller.total_charged() == 0


@pytest.mark.asyncio
async def test_release_after_capture_is_a_noop_not_a_refund() -> None:
    """A settled charge is not refundable through release — release of a CAPTURED id is a
    no-op, so a cleanup pass cannot silently reverse a committed charge."""
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=5)
    await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)
    await biller.release(r.id)  # no-op
    assert biller.total_charged() == 5
    assert biller.is_captured(r.id)


# --------------------------------------------------------------------------------------
# idempotent capture under repetition
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_is_idempotent_for_same_outcome() -> None:
    """Re-issuing the identical capture (the resume case) is a tolerated no-op."""
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=15)
    await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)
    await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)  # repeat -> no-op
    await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)
    assert biller.total_charged() == 15  # charged exactly once


@pytest.mark.asyncio
async def test_second_capture_different_outcome_raises() -> None:
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=15)
    await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)
    with pytest.raises(DoubleCaptureError):
        await biller.capture(r.id, outcome=ToolOutcome.FAILED)
    assert biller.total_charged() == 15  # still charged once, on the first outcome


@pytest.mark.asyncio
async def test_capture_unknown_reservation_raises_keyerror() -> None:
    biller = LedgerBiller()
    with pytest.raises(KeyError):
        await biller.capture("res-nope", outcome=ToolOutcome.SUCCESS)


@pytest.mark.asyncio
async def test_negative_reserve_amount_rejected() -> None:
    biller = LedgerBiller()
    with pytest.raises(ValueError):
        await biller.reserve(run_id="run-1", tool="t", amount=-1)


# --------------------------------------------------------------------------------------
# INTERRUPTED — held for reconciliation, never silently released
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupted_capture_holds_the_charge() -> None:
    """INTERRUPTED means the side effect MAY have happened: the charge is HELD, not
    refunded, so a retry cannot re-run a possibly-completed paid call."""
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)
    await biller.capture(r.id, outcome=ToolOutcome.INTERRUPTED)

    assert biller.total_charged() == 35  # held as charged
    assert biller.is_captured(r.id)
    assert r.id in biller.captured_reservations()  # a resume must NOT re-capture it


@pytest.mark.asyncio
async def test_interrupted_is_flagged_for_reconciliation() -> None:
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)
    await biller.capture(r.id, outcome=ToolOutcome.INTERRUPTED)

    (entry,) = biller.ledger()
    assert entry.state is ReservationState.NEEDS_RECONCILIATION
    assert entry.needs_reconciliation
    assert entry.outcome is ToolOutcome.INTERRUPTED

    unreconciled = biller.unreconciled()
    assert len(unreconciled) == 1
    assert unreconciled[0].reservation_id == r.id


@pytest.mark.asyncio
async def test_interrupted_is_never_silently_released() -> None:
    """The wrong behaviour would be to treat INTERRUPTED as failure and release. It must
    not appear as a RELEASED entry and must not drop out of the charged set."""
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=35)
    await biller.capture(r.id, outcome=ToolOutcome.INTERRUPTED)
    (entry,) = biller.ledger()
    assert entry.state is not ReservationState.RELEASED
    assert r.id in biller.captured_reservations()


@pytest.mark.asyncio
async def test_interrupted_capture_is_idempotent() -> None:
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=35)
    await biller.capture(r.id, outcome=ToolOutcome.INTERRUPTED)
    await biller.capture(r.id, outcome=ToolOutcome.INTERRUPTED)  # resume -> no-op
    assert biller.total_charged() == 35
    assert len(biller.unreconciled()) == 1


@pytest.mark.asyncio
async def test_reconciliation_refund_via_release_out_of_needs_reconciliation() -> None:
    """The sanctioned reconciliation path: a later check confirmed the side effect did NOT
    happen, so the held charge is refunded by releasing the same reservation."""
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=35)
    await biller.capture(r.id, outcome=ToolOutcome.INTERRUPTED)
    assert biller.total_charged() == 35

    await biller.release(r.id)  # reconcile-as-did-not-happen
    assert biller.total_charged() == 0
    assert r.id not in biller.captured_reservations()
    assert biller.unreconciled() == ()
    (entry,) = biller.ledger()
    assert entry.state is ReservationState.RELEASED


@pytest.mark.asyncio
async def test_interrupted_then_conflicting_capture_raises() -> None:
    """Once held as needs-reconciliation, a capture with a different outcome is a double
    capture — you cannot quietly rewrite an interrupted charge into a clean success."""
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=35)
    await biller.capture(r.id, outcome=ToolOutcome.INTERRUPTED)
    with pytest.raises(DoubleCaptureError):
        await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)


# --------------------------------------------------------------------------------------
# ledger auditability
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_records_every_reservation_and_terminal_state() -> None:
    biller = LedgerBiller()
    win = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)
    lose = await biller.reserve(run_id="run-1", tool="generate_video", amount=50)
    lost = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)

    await biller.capture(win.id, outcome=ToolOutcome.SUCCESS)
    await biller.release(lose.id)
    await biller.capture(lost.id, outcome=ToolOutcome.INTERRUPTED)

    entries = {e.reservation_id: e for e in biller.ledger()}
    assert entries[win.id].state is ReservationState.CAPTURED
    assert entries[lose.id].state is ReservationState.RELEASED
    assert entries[lost.id].state is ReservationState.NEEDS_RECONCILIATION

    # Spend is auditable after the fact: charged = success + interrupted-held, minus refund.
    assert biller.total_reserved() == 120
    assert biller.total_charged() == 70  # 35 + 35 held; 50 refunded


@pytest.mark.asyncio
async def test_ledger_scopes_by_run() -> None:
    biller = LedgerBiller()
    a = await biller.reserve(run_id="run-A", tool="t", amount=10)
    b = await biller.reserve(run_id="run-B", tool="t", amount=20)
    await biller.capture(a.id, outcome=ToolOutcome.SUCCESS)
    await biller.capture(b.id, outcome=ToolOutcome.SUCCESS)

    assert biller.total_charged(run_id="run-A") == 10
    assert biller.total_charged(run_id="run-B") == 20
    assert {e.reservation_id for e in biller.ledger(run_id="run-A")} == {a.id}
    assert biller.captured_reservations(run_id="run-B") == frozenset({b.id})


@pytest.mark.asyncio
async def test_history_preserves_the_transition_path() -> None:
    """The append-only history shows RESERVED -> terminal, not just the final state."""
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=5)
    await biller.capture(r.id, outcome=ToolOutcome.SUCCESS)
    states = [e.state for e in biller.history() if e.reservation_id == r.id]
    assert states == [ReservationState.RESERVED, ReservationState.CAPTURED]


@pytest.mark.asyncio
async def test_ledger_entries_are_frozen() -> None:
    """An audit row cannot be edited after the fact."""
    biller = LedgerBiller()
    r = await biller.reserve(run_id="run-1", tool="t", amount=5)
    (entry,) = biller.ledger()
    assert isinstance(entry, LedgerEntry)
    with pytest.raises((AttributeError, TypeError)):
        entry.amount = 999  # type: ignore[misc]
