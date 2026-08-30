"""Billing invariants, proven for ARBITRARY INTERLEAVINGS of reserve / capture / release /
retry (hypothesis). Tagged ``@pytest.mark.thunder`` — this is an authority/economics
invariant, and it is the reason the module exists.

The four properties, for every op sequence the machine can construct:

1. **Total charged never exceeds total reserved.** P5 has no way to charge more than was
   held, no matter how captures, releases and retries interleave.
2. **No reservation is ever captured twice.** A second capture on a different outcome, or
   after a release, raises :class:`DoubleCaptureError`; an identical re-capture is a no-op.
   Either way the credit for one reservation is committed at most once.
3. **A released reservation can never later be captured.** Once refunded, a capture of that
   id raises rather than resurrecting the charge.
4. **An INTERRUPTED reservation is never silently released.** Capturing on ``INTERRUPTED``
   leaves the reservation charged-and-flagged (``NEEDS_RECONCILIATION``), never ``RELEASED``,
   and never dropped from the charged set — because the side effect may have happened and a
   retry must not re-run it.

The test is driven the way the canvas property test is: a hypothesis-drawn op list applied
through ``asyncio.run``, with an INDEPENDENT oracle (a plain dict of expected states) that
never touches the biller's internals, so the assertions cannot be satisfied by the same bug
in both.

"retry" is modelled explicitly, because it is the whole hazard: the framework's instinct on a
failed or interrupted call is to run the tool again. A retry here reserves a *fresh* id and
captures it — which is correct (a new reservation, a new charge) — and the invariants must
still hold. What must NEVER happen is re-capturing the *same* id; the ops that attempt that
(double capture, capture-after-release) are drawn deliberately and asserted to raise.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pikachu.billing import LedgerBiller, ReservationState
from pikachu.core.errors import DoubleCaptureError
from pikachu.core.types import ToolOutcome

pytestmark = pytest.mark.thunder


_OUTCOME = st.sampled_from(list(ToolOutcome))
_AMOUNT = st.integers(min_value=0, max_value=100)
_RUN = st.sampled_from(["run-1", "run-2"])
_TOOL = st.sampled_from(["generate_image", "generate_video", "free_tool"])


@st.composite
def _op(draw: st.DrawFn) -> tuple[str, dict[str, object]]:
    """One billing operation.

    * ``reserve`` — hold a fresh amount.
    * ``capture`` — capture an existing reservation, chosen by index into what has been
      reserved so far (mod), on an arbitrary outcome. Interleaving makes this land on
      already-captured / already-released ids, which is exactly where the invariants bite.
    * ``release`` — release an existing reservation, chosen the same way.
    """
    kind = draw(st.sampled_from(["reserve", "reserve", "capture", "release"]))
    if kind == "reserve":
        return "reserve", {
            "run_id": draw(_RUN),
            "tool": draw(_TOOL),
            "amount": draw(_AMOUNT),
        }
    # capture / release target an existing reservation by index (resolved at apply time,
    # because we do not know the reservation ids until reserve has run).
    return kind, {
        "index": draw(st.integers(min_value=0, max_value=50)),
        "outcome": draw(_OUTCOME),
    }


class _Oracle:
    """Independent expected-state model. Knows nothing about LedgerBiller internals.

    For each reservation id we track: the amount, and the expected terminal disposition as
    this test understands the contract. total_charged is recomputed from the oracle, never
    read from the biller, so property 1 compares two independently-derived numbers.
    """

    def __init__(self) -> None:
        self.amount: dict[str, int] = {}
        self.state: dict[str, ReservationState] = {}
        # outcome a capture was first committed on (for the idempotent-repeat check).
        self.outcome: dict[str, ToolOutcome] = {}

    def reserved(self, rid: str, amount: int) -> None:
        self.amount[rid] = amount
        self.state[rid] = ReservationState.RESERVED

    def expected_charged(self) -> int:
        return sum(
            self.amount[rid]
            for rid, s in self.state.items()
            if s in (ReservationState.CAPTURED, ReservationState.NEEDS_RECONCILIATION)
        )


async def _run(ops: list[tuple[str, dict[str, object]]]) -> tuple[LedgerBiller, _Oracle]:
    biller = LedgerBiller()
    oracle = _Oracle()
    order: list[str] = []  # reservation ids in creation order, for index-based targeting

    for name, spec in ops:
        if name == "reserve":
            run_id = spec["run_id"]
            tool = spec["tool"]
            amount = spec["amount"]
            assert isinstance(run_id, str) and isinstance(tool, str)
            assert isinstance(amount, int)
            r = await biller.reserve(run_id=run_id, tool=tool, amount=amount)
            order.append(r.id)
            oracle.reserved(r.id, amount)
            continue

        if not order:
            continue  # nothing to target yet
        idx = spec["index"]
        assert isinstance(idx, int)
        rid = order[idx % len(order)]
        prior = oracle.state[rid]

        if name == "capture":
            outcome = spec["outcome"]
            assert isinstance(outcome, ToolOutcome)
            target = (
                ReservationState.NEEDS_RECONCILIATION
                if outcome is ToolOutcome.INTERRUPTED
                else ReservationState.CAPTURED
            )
            if prior is ReservationState.RESERVED:
                await biller.capture(rid, outcome=outcome)
                oracle.state[rid] = target
                oracle.outcome[rid] = outcome
            elif prior in (
                ReservationState.CAPTURED,
                ReservationState.NEEDS_RECONCILIATION,
            ):
                if oracle.outcome.get(rid) == outcome:
                    await biller.capture(rid, outcome=outcome)  # idempotent no-op
                else:
                    with pytest.raises(DoubleCaptureError):
                        await biller.capture(rid, outcome=outcome)
            else:  # RELEASED — capture must be refused
                with pytest.raises(DoubleCaptureError):
                    await biller.capture(rid, outcome=outcome)

        elif name == "release":
            await biller.release(rid)  # always legal; a no-op where not a refund
            if prior in (
                ReservationState.RESERVED,
                ReservationState.NEEDS_RECONCILIATION,
            ):
                oracle.state[rid] = ReservationState.RELEASED
            # CAPTURED or already RELEASED -> unchanged (release is a no-op there)

    return biller, oracle


_sequences = st.lists(_op(), min_size=0, max_size=60)


@settings(max_examples=300, deadline=None)
@given(ops=_sequences)
def test_total_charged_never_exceeds_total_reserved(
    ops: list[tuple[str, dict[str, object]]],
) -> None:
    """Property 1 — P5's ceiling holds for any interleaving."""
    biller, oracle = asyncio.run(_run(ops))
    assert biller.total_charged() <= biller.total_reserved()
    # And the biller's charged total matches an independently-derived expectation.
    assert biller.total_charged() == oracle.expected_charged()


@settings(max_examples=300, deadline=None)
@given(ops=_sequences)
def test_no_reservation_is_captured_twice(
    ops: list[tuple[str, dict[str, object]]],
) -> None:
    """Property 2 — every reservation contributes its amount to the charged total at most
    once. A double capture cannot inflate the total, because a captured amount is counted
    from the terminal entry, which holds a single amount."""
    biller, oracle = asyncio.run(_run(ops))
    for entry in biller.ledger():
        # A charged entry contributes exactly its own amount, never a multiple.
        assert entry.charged_amount in (0, entry.amount)
    # captured set never exceeds reserved set, and every captured id was reserved.
    reserved_ids = {e.reservation_id for e in biller.ledger()}
    assert biller.captured_reservations() <= reserved_ids


@settings(max_examples=300, deadline=None)
@given(ops=_sequences)
def test_released_reservation_never_captured(
    ops: list[tuple[str, dict[str, object]]],
) -> None:
    """Property 3 — no id the oracle believes is RELEASED is in the biller's charged set."""
    biller, oracle = asyncio.run(_run(ops))
    released = {
        rid for rid, s in oracle.state.items() if s is ReservationState.RELEASED
    }
    assert not (released & biller.captured_reservations())
    for rid in released:
        assert not biller.is_captured(rid)


@settings(max_examples=300, deadline=None)
@given(ops=_sequences)
def test_interrupted_never_silently_released(
    ops: list[tuple[str, dict[str, object]]],
) -> None:
    """Property 4 — every reservation the oracle captured on INTERRUPTED is held as
    NEEDS_RECONCILIATION in the biller (never RELEASED, never dropped from charged), UNLESS
    the sequence explicitly released it as the sanctioned reconciliation refund."""
    biller, oracle = asyncio.run(_run(ops))
    by_id = {e.reservation_id: e for e in biller.ledger()}
    for rid, s in oracle.state.items():
        if s is ReservationState.NEEDS_RECONCILIATION:
            entry = by_id[rid]
            assert entry.state is ReservationState.NEEDS_RECONCILIATION
            assert entry.needs_reconciliation
            assert entry.state is not ReservationState.RELEASED
            assert rid in biller.captured_reservations()
