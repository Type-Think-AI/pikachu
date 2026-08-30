"""Durability unit tests — checkpoint, resume, and the P9 no-double-charge guard.

Covers the behaviours the lane spec enumerates:

* a checkpoint is written after each iteration and continues from the right one;
* a captured reservation is never captured again (``DoubleCaptureError``);
* ``INTERRUPTED`` is surfaced as a reconciliation, not guessed either way;
* resuming a terminal run is refused;
* checkpoint/resume round-trips every ``Run`` field.

Everything runs against the in-memory ``FakeRunStore`` / ``FakeBiller`` from
``tests/fakes.py`` — no network, no clock-dependent assertion.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from pikachu.core.errors import DoubleCaptureError
from pikachu.core.types import Run, RunPhase, ToolOutcome
from pikachu.durability.checkpoint import Checkpointer
from pikachu.durability.resume import (
    Reconciliation,
    ResumeDecision,
    Resumer,
    TerminalRunError,
    UnknownRunError,
)
from tests.fakes import FakeBiller, FakeRunStore


def _run(run_id: str = "run-1", **kw: object) -> Run:
    base: dict[str, object] = {"id": run_id, "agent_name": "colourist"}
    base.update(kw)
    return Run(**base)


# --------------------------------------------------------------------------------------
# Checkpoint after each iteration
# --------------------------------------------------------------------------------------


def test_checkpoint_after_each_iteration_advances_stored_state() -> None:
    async def go() -> None:
        store = FakeRunStore()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING))

        for i in range(1, 6):
            run = await cp.record(run, iteration=i)
            stored = await store.get("run-1")
            assert stored is not None
            assert stored.iteration == i, f"checkpoint {i} did not persist the iteration"

        # The store holds the last checkpoint's iteration, not the base 0.
        final = await store.get("run-1")
        assert final is not None and final.iteration == 5

    asyncio.run(go())


def test_open_refuses_duplicate_run_id() -> None:
    async def go() -> None:
        store = FakeRunStore()
        cp = Checkpointer(store)
        await cp.open(_run())
        with pytest.raises(KeyError):
            await cp.open(_run())  # a second open must not silently reset the run

    asyncio.run(go())


def test_record_carries_unset_fields_forward() -> None:
    async def go() -> None:
        store = FakeRunStore()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING, iteration=3, charged_credits=35))
        # Advance only the iteration; everything else must be preserved.
        run = await cp.record(run, iteration=4)
        assert run.phase is RunPhase.RUNNING
        assert run.charged_credits == 35
        assert run.iteration == 4

    asyncio.run(go())


def test_record_never_shrinks_captured_reservations() -> None:
    async def go() -> None:
        store = FakeRunStore()
        cp = Checkpointer(store)
        run = await cp.open(_run(captured_reservations=frozenset({"res-a"})))
        # A bare record must not drop the existing reservation.
        run = await cp.record(run, iteration=1)
        assert run.captured_reservations == frozenset({"res-a"})
        # Passing a new set unions, it does not replace.
        run = await cp.record(run, captured_reservations=frozenset({"res-b"}))
        assert run.captured_reservations == frozenset({"res-a", "res-b"})

    asyncio.run(go())


def test_close_requires_terminal_phase() -> None:
    async def go() -> None:
        store = FakeRunStore()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING))
        with pytest.raises(ValueError):
            await cp.close(run, phase=RunPhase.RUNNING)
        closed = await cp.close(run, phase=RunPhase.SUCCEEDED)
        assert closed.phase is RunPhase.SUCCEEDED
        assert closed.ended_at is not None

    asyncio.run(go())


# --------------------------------------------------------------------------------------
# Resume continues from the right iteration
# --------------------------------------------------------------------------------------


def test_resume_loads_last_checkpoint() -> None:
    async def go() -> None:
        store = FakeRunStore()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING))
        run = await cp.record(run, iteration=7)

        resumer = Resumer(store, FakeBiller())
        loaded = await resumer.load("run-1")
        assert loaded.iteration == 7, "resume must continue from the last checkpoint"
        assert loaded.phase is RunPhase.RUNNING

    asyncio.run(go())


def test_resume_unknown_run_raises() -> None:
    async def go() -> None:
        resumer = Resumer(FakeRunStore(), FakeBiller())
        with pytest.raises(UnknownRunError):
            await resumer.load("nope")

    asyncio.run(go())


# --------------------------------------------------------------------------------------
# ★ P9: a captured reservation is never captured again
# --------------------------------------------------------------------------------------


def test_safe_capture_refuses_already_captured_reservation() -> None:
    async def go() -> None:
        store = FakeRunStore()
        biller = FakeBiller()
        cp = Checkpointer(store)

        run = await cp.open(_run(phase=RunPhase.RUNNING))
        reservation = await biller.reserve(run_id=run.id, tool="generate_image", amount=35)

        # First capture: succeeds, records durably.
        run = await Resumer(store, biller).safe_capture(
            run, reservation.id, amount=35
        )
        assert reservation.id in run.captured_reservations
        assert run.charged_credits == 35
        assert biller.is_captured(reservation.id)

        # Second capture of the SAME reservation on the same run: refused before the biller
        # is even touched.
        with pytest.raises(DoubleCaptureError):
            await Resumer(store, biller).safe_capture(run, reservation.id, amount=35)
        # Not double-charged.
        assert biller.captured_amount() == 35

    asyncio.run(go())


def test_resume_of_recorded_capture_is_a_noop() -> None:
    """Replaying a capture that a prior run already recorded charges nothing more."""

    async def go() -> None:
        store = FakeRunStore()
        biller = FakeBiller()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING))
        reservation = await biller.reserve(run_id=run.id, tool="generate_image", amount=35)
        run = await Resumer(store, biller).safe_capture(run, reservation.id, amount=35)

        # Now "crash" and resume: the same reservation is presented again as SUCCESS.
        resumer = Resumer(store, biller)
        resumed = await resumer.load("run-1")
        plan = await resumer.resume_reservation(
            resumed, reservation.id, amount=35, outcome=ToolOutcome.SUCCESS
        )
        assert plan.decision is ResumeDecision.ALREADY_CAPTURED
        assert plan.run.charged_credits == 35  # unchanged
        assert biller.captured_amount() == 35  # charged exactly once

    asyncio.run(go())


def test_release_returns_reservation_unspent() -> None:
    async def go() -> None:
        store = FakeRunStore()
        biller = FakeBiller()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING))
        reservation = await biller.reserve(run_id=run.id, tool="generate_image", amount=35)

        resumer = Resumer(store, biller)
        plan = await resumer.resume_reservation(
            run, reservation.id, amount=35, outcome=ToolOutcome.FAILED
        )
        assert plan.decision is ResumeDecision.RELEASE
        assert reservation.id not in plan.run.captured_reservations
        assert biller.captured_amount() == 0

    asyncio.run(go())


# --------------------------------------------------------------------------------------
# INTERRUPTED is surfaced, not guessed
# --------------------------------------------------------------------------------------


def test_interrupted_surfaces_reconciliation_and_touches_no_billing() -> None:
    async def go() -> None:
        store = FakeRunStore()
        biller = FakeBiller()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING))
        reservation = await biller.reserve(run_id=run.id, tool="generate_image", amount=35)

        resumer = Resumer(store, biller)
        plan = await resumer.resume_reservation(
            run, reservation.id, amount=35, outcome=ToolOutcome.INTERRUPTED
        )
        assert plan.decision is ResumeDecision.RECONCILE
        assert isinstance(plan.reconciliation, Reconciliation)
        assert plan.reconciliation.reservation_id == reservation.id
        assert plan.reconciliation.amount == 35
        # Neither captured nor released — the unknown stays unresolved and visible.
        assert not biller.is_captured(reservation.id)
        assert plan.run.charged_credits == 0

    asyncio.run(go())


def test_settle_reconciliation_captures_when_side_effect_occurred() -> None:
    async def go() -> None:
        store = FakeRunStore()
        biller = FakeBiller()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING))
        reservation = await biller.reserve(run_id=run.id, tool="generate_image", amount=35)
        resumer = Resumer(store, biller)
        plan = await resumer.resume_reservation(
            run, reservation.id, amount=35, outcome=ToolOutcome.INTERRUPTED
        )
        assert plan.reconciliation is not None

        # Caller checked the provider: the image DID land. Charge once.
        settled = await resumer.settle_reconciliation(
            plan.run, plan.reconciliation, side_effect_occurred=True
        )
        assert settled.charged_credits == 35
        assert biller.captured_amount() == 35

        # Settling the same reconciliation again cannot double-charge (P9 still holds).
        with pytest.raises(DoubleCaptureError):
            await resumer.settle_reconciliation(
                settled, plan.reconciliation, side_effect_occurred=True
            )
        assert biller.captured_amount() == 35

    asyncio.run(go())


def test_settle_reconciliation_releases_when_side_effect_did_not_occur() -> None:
    async def go() -> None:
        store = FakeRunStore()
        biller = FakeBiller()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING))
        reservation = await biller.reserve(run_id=run.id, tool="generate_image", amount=35)
        resumer = Resumer(store, biller)
        plan = await resumer.resume_reservation(
            run, reservation.id, amount=35, outcome=ToolOutcome.INTERRUPTED
        )
        assert plan.reconciliation is not None

        settled = await resumer.settle_reconciliation(
            plan.run, plan.reconciliation, side_effect_occurred=False
        )
        assert settled.charged_credits == 0
        assert biller.captured_amount() == 0

    asyncio.run(go())


# --------------------------------------------------------------------------------------
# Terminal runs are terminal
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase", [RunPhase.SUCCEEDED, RunPhase.FAILED, RunPhase.CANCELLED]
)
def test_resuming_a_terminal_run_is_refused(phase: RunPhase) -> None:
    async def go() -> None:
        store = FakeRunStore()
        await store.create(_run(phase=phase))
        resumer = Resumer(store, FakeBiller())
        with pytest.raises(TerminalRunError):
            await resumer.load("run-1")

    asyncio.run(go())


# --------------------------------------------------------------------------------------
# decide() truth table — pure, no I/O
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ToolOutcome.SUCCESS, ResumeDecision.CAPTURE),
        (ToolOutcome.FAILED, ResumeDecision.RELEASE),
        (ToolOutcome.DENIED, ResumeDecision.RELEASE),
        (ToolOutcome.INTERRUPTED, ResumeDecision.RECONCILE),
    ],
)
def test_decide_for_uncaptured_reservation(
    outcome: ToolOutcome, expected: ResumeDecision
) -> None:
    run = _run()
    assert Resumer.decide(run, "res-x", outcome) is expected


@pytest.mark.parametrize("outcome", list(ToolOutcome))
def test_decide_always_already_captured_when_in_the_set(outcome: ToolOutcome) -> None:
    """No matter the reported outcome, a reservation already in the set is never re-captured."""
    run = _run(captured_reservations=frozenset({"res-x"}))
    assert Resumer.decide(run, "res-x", outcome) is ResumeDecision.ALREADY_CAPTURED


# --------------------------------------------------------------------------------------
# Round-trip: checkpoint/resume preserves every Run field
# --------------------------------------------------------------------------------------


def test_checkpoint_resume_round_trips_all_run_fields() -> None:
    async def go() -> None:
        store = FakeRunStore()
        started = datetime(2026, 8, 30, 7, 44, tzinfo=timezone.utc)
        original = Run(
            id="run-42",
            agent_name="colourist",
            phase=RunPhase.AWAITING_APPROVAL,
            iteration=9,
            max_iterations=30,
            charged_credits=70,
            refunded_credits=35,
            captured_reservations=frozenset({"res-a", "res-b"}),
            started_at=started,
        )
        cp = Checkpointer(store)
        await cp.open(original)
        loaded = await Resumer(store, FakeBiller()).load("run-42")

        # Frozen model equality is structural: every field survived the round-trip.
        assert loaded == original
        assert loaded.net_credits == 35

    asyncio.run(go())


def test_full_resume_drains_pending_and_reports_reconciliations() -> None:
    """resume() applies safe actions and returns exactly the unresolved reconciliations."""

    async def go() -> None:
        store = FakeRunStore()
        biller = FakeBiller()
        cp = Checkpointer(store)
        run = await cp.open(_run(phase=RunPhase.RUNNING))

        r_ok = await biller.reserve(run_id=run.id, tool="generate_image", amount=35)
        r_fail = await biller.reserve(run_id=run.id, tool="generate_image", amount=35)
        r_int = await biller.reserve(run_id=run.id, tool="generate_image", amount=53)

        resumer = Resumer(store, biller)
        resumed, reconciliations = await resumer.resume(
            "run-1",
            pending=(
                (r_ok.id, 35, ToolOutcome.SUCCESS),
                (r_fail.id, 35, ToolOutcome.FAILED),
                (r_int.id, 53, ToolOutcome.INTERRUPTED),
            ),
        )
        # SUCCESS captured, FAILED released, INTERRUPTED surfaced.
        assert resumed.charged_credits == 35
        assert biller.captured_amount() == 35
        assert len(reconciliations) == 1
        assert reconciliations[0].reservation_id == r_int.id
        assert reconciliations[0].amount == 53

        # Re-running the same resume is safe: the captured one is now a no-op, still 35.
        resumed2, recon2 = await resumer.resume(
            "run-1",
            pending=(
                (r_ok.id, 35, ToolOutcome.SUCCESS),
                (r_int.id, 53, ToolOutcome.INTERRUPTED),
            ),
        )
        assert biller.captured_amount() == 35
        assert len(recon2) == 1

    asyncio.run(go())
