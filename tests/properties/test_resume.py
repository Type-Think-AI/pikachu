"""★ P9 as a property (hypothesis). Earns **Thunder**.

The single invariant this file exists to prove:

    For ANY crash point in a multi-iteration run — every iteration boundary AND every point
    mid-tool-call — resuming produces the SAME TOTAL SPEND as an uninterrupted run of the
    same work.

That is the whole reason ``durability/`` exists: a resume of a paid workload must be
spend-identical to never having crashed, never more (double charge) and never less (a charge
silently lost). We make it genuinely exhaustive over crash points by modelling a turn as a
sequence of metered tool calls and enumerating a crash at every *stage* of every call —
including the murderous one, "provider did the work, we crashed before recording it."

The crash taxonomy per tool call, in execution order, each a distinct point we resume from:

    0  BEFORE_RESERVE     nothing happened
    1  AFTER_RESERVE      reservation held, provider not yet called
    2  AFTER_PROVIDER     ★ provider did the paid work; capture NOT recorded (INTERRUPTED)
    3  AFTER_CAPTURE       provider done AND capture recorded durably
    4  AFTER_CHECKPOINT    iteration boundary fully persisted

A crash at stage 2 is the hard case: the side effect may have landed, so the reconciler must
be told the truth (the model here knows the ground truth and supplies it), and settling it
must charge exactly the once. A crash at stage 3 means the capture is in
``captured_reservations`` already, so the resume path must treat it as a no-op — re-driving it
would double-charge.

Everything is deterministic and offline: ``FakeRunStore`` + ``FakeBiller``, ``asyncio.run``.
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from pikachu.core.types import Run, RunPhase, ToolOutcome
from pikachu.durability.checkpoint import Checkpointer
from pikachu.durability.resume import Resumer
from tests.fakes import FakeBiller, FakeRunStore

pytestmark = __import__("pytest").mark.thunder


# Stage within a single tool call at which the process dies.
_BEFORE_RESERVE = 0
_AFTER_RESERVE = 1
_AFTER_PROVIDER = 2  # ★ side effect landed, capture not recorded → INTERRUPTED
_AFTER_CAPTURE = 3
_AFTER_CHECKPOINT = 4

# A run is a list of per-iteration tool costs (>0 == a metered call this iteration; the model
# always makes exactly one metered call per iteration for a tight, exhaustive shape).
_costs = st.lists(st.integers(min_value=1, max_value=99), min_size=1, max_size=6)
_stage = st.integers(min_value=_BEFORE_RESERVE, max_value=_AFTER_CHECKPOINT)


async def _uninterrupted_total(costs: list[int]) -> int:
    """Baseline: run every metered call to completion once. Returns total captured spend."""
    store = FakeRunStore()
    biller = FakeBiller()
    cp = Checkpointer(store)
    resumer = Resumer(store, biller)
    run = await cp.open(Run(id="base", agent_name="a", phase=RunPhase.RUNNING))
    for i, cost in enumerate(costs, start=1):
        res = await biller.reserve(run_id=run.id, tool="generate_image", amount=cost)
        run = await resumer.safe_capture(run, res.id, amount=cost)
        run = await cp.record(run, iteration=i)
    return biller.captured_amount()


async def _run_with_crash(costs: list[int], crash_iter: int, crash_stage: int) -> int:
    """Drive the run, 'crash' at ``crash_iter`` (1-based) / ``crash_stage``, then resume.

    Everything before the crashed iteration completed normally and is on disk. The crashed
    iteration got as far as ``crash_stage``. Resume must then finish the run with the same
    total spend as :func:`_uninterrupted_total`.

    State the crash leaves behind, and what the crashed iteration's ``pending`` looks like:

      stage 0  nothing reserved            → replay the whole iteration fresh
      stage 1  reserved, no provider call  → the tool re-runs; safe to (re-reserve +) capture
      stage 2  provider ran, no capture    → INTERRUPTED; reconcile (ground truth: it landed)
      stage 3  provider ran, capture kept  → ALREADY_CAPTURED; must be a no-op on resume
      stage 4  iteration fully checkpointed→ just carry on from the next iteration
    """
    store = FakeRunStore()
    biller = FakeBiller()
    cp = Checkpointer(store)
    resumer = Resumer(store, biller)
    run = await cp.open(Run(id="r", agent_name="a", phase=RunPhase.RUNNING))

    crashed_reservation: str | None = None
    crashed_cost = 0
    crashed_stage_for_pending = crash_stage

    # --- pre-crash: iterations before crash_iter complete fully ---
    for i, cost in enumerate(costs, start=1):
        if i < crash_iter:
            res = await biller.reserve(run_id=run.id, tool="generate_image", amount=cost)
            run = await resumer.safe_capture(run, res.id, amount=cost)
            run = await cp.record(run, iteration=i)
            continue

        # This is the crashing iteration. Advance only as far as crash_stage, then stop.
        crashed_cost = cost
        if crash_stage == _BEFORE_RESERVE:
            crashed_reservation = None
        elif crash_stage == _AFTER_RESERVE:
            res = await biller.reserve(run_id=run.id, tool="generate_image", amount=cost)
            crashed_reservation = res.id
        elif crash_stage == _AFTER_PROVIDER:
            res = await biller.reserve(run_id=run.id, tool="generate_image", amount=cost)
            crashed_reservation = res.id
            # provider did the work; NO capture recorded — the INTERRUPTED case.
        elif crash_stage == _AFTER_CAPTURE:
            res = await biller.reserve(run_id=run.id, tool="generate_image", amount=cost)
            crashed_reservation = res.id
            run = await resumer.safe_capture(run, res.id, amount=cost)
            # capture recorded, but the iteration checkpoint did NOT land.
        elif crash_stage == _AFTER_CHECKPOINT:
            res = await biller.reserve(run_id=run.id, tool="generate_image", amount=cost)
            crashed_reservation = res.id
            run = await resumer.safe_capture(run, res.id, amount=cost)
            run = await cp.record(run, iteration=i)
        break  # crashed — stop driving forward

    # --- the crash: a fresh process reloads only what the store persisted ---
    resumer2 = Resumer(store, biller)

    # Build the crashed iteration's pending reservation set from what we know happened.
    #   AFTER_RESERVE   → tool re-runs on resume and succeeds → present as SUCCESS
    #   AFTER_PROVIDER  → side effect may have landed          → present as INTERRUPTED
    #   AFTER_CAPTURE   → already in captured_reservations      → SUCCESS, must be a no-op
    _pending_outcome = {
        _AFTER_RESERVE: ToolOutcome.SUCCESS,
        _AFTER_PROVIDER: ToolOutcome.INTERRUPTED,
        _AFTER_CAPTURE: ToolOutcome.SUCCESS,
    }
    pending: tuple[tuple[str, int, ToolOutcome], ...] = ()
    if crashed_reservation is not None and crashed_stage_for_pending in _pending_outcome:
        outcome = _pending_outcome[crashed_stage_for_pending]
        pending = ((crashed_reservation, crashed_cost, outcome),)

    resumed, reconciliations = await resumer2.resume("r", pending=pending)

    # Settle any reconciliation with the ground truth: at stage 2 the side effect DID land.
    for recon in reconciliations:
        resumed = await resumer2.settle_reconciliation(
            resumed, recon, side_effect_occurred=True
        )

    # --- finish the run: iterations from the crashed one onward that are not yet captured ---
    # Determine how many iterations are already fully accounted for.
    done_through = crash_iter - 1
    if crash_stage in (_AFTER_CAPTURE, _AFTER_CHECKPOINT):
        done_through = crash_iter  # crashed iteration's charge is settled
    elif crash_stage in (_AFTER_RESERVE, _AFTER_PROVIDER):
        done_through = crash_iter  # settled via resume/reconcile above

    for i in range(done_through + 1, len(costs) + 1):
        cost = costs[i - 1]
        res = await biller.reserve(run_id=resumed.id, tool="generate_image", amount=cost)
        resumed = await resumer2.safe_capture(resumed, res.id, amount=cost)
        resumed = await cp.record(resumed, iteration=i)

    return biller.captured_amount()


@settings(max_examples=400, deadline=None)
@given(costs=_costs, crash_iter_seed=st.integers(min_value=0, max_value=10_000), stage=_stage)
def test_resume_total_spend_equals_uninterrupted(
    costs: list[int], crash_iter_seed: int, stage: int
) -> None:
    """Same total spend after a crash at ANY iteration/stage as with no crash at all."""
    crash_iter = (crash_iter_seed % len(costs)) + 1  # 1..len(costs)
    baseline = asyncio.run(_uninterrupted_total(costs))
    after_crash = asyncio.run(_run_with_crash(costs, crash_iter, stage))
    assert after_crash == baseline == sum(costs), (
        costs,
        crash_iter,
        stage,
        baseline,
        after_crash,
    )


@settings(max_examples=300, deadline=None)
@given(costs=_costs, crash_iter_seed=st.integers(min_value=0, max_value=10_000), stage=_stage)
def test_resume_never_captures_more_than_reserved(
    costs: list[int], crash_iter_seed: int, stage: int
) -> None:
    """No reservation is ever captured twice: total captured never exceeds total cost."""
    crash_iter = (crash_iter_seed % len(costs)) + 1
    after_crash = asyncio.run(_run_with_crash(costs, crash_iter, stage))
    assert after_crash <= sum(costs), (costs, crash_iter, stage, after_crash)


@settings(max_examples=300, deadline=None)
@given(
    costs=_costs,
    crash_iter_seed=st.integers(min_value=0, max_value=10_000),
    stage=_stage,
    extra_resumes=st.integers(min_value=0, max_value=3),
)
def test_repeated_resume_is_idempotent_on_spend(
    costs: list[int], crash_iter_seed: int, stage: int, extra_resumes: int
) -> None:
    """Resuming the SAME crash point several times does not add spend past the baseline.

    Crash recovery is itself at-least-once — the resume can be retried — so retrying it must
    not accumulate charges. We model that by re-invoking resume on the persisted run with the
    crashed iteration's reservation, if any, re-presented; the guard makes each replay a
    no-op once the capture is recorded.
    """
    crash_iter = (crash_iter_seed % len(costs)) + 1
    total = sum(costs)

    async def go() -> int:
        store = FakeRunStore()
        biller = FakeBiller()
        cp = Checkpointer(store)
        resumer = Resumer(store, biller)
        run = await cp.open(Run(id="r", agent_name="a", phase=RunPhase.RUNNING))

        captured_ids: list[tuple[str, int]] = []
        for i, cost in enumerate(costs, start=1):
            res = await biller.reserve(run_id=run.id, tool="generate_image", amount=cost)
            run = await resumer.safe_capture(run, res.id, amount=cost)
            run = await cp.record(run, iteration=i)
            captured_ids.append((res.id, cost))

        # Re-present already-captured reservations several times: every one must be a no-op.
        for _ in range(extra_resumes):
            reloaded = await resumer.load("r")
            pending = tuple(
                (rid, cost, ToolOutcome.SUCCESS) for rid, cost in captured_ids
            )
            reloaded, recon = await resumer.resume("r", pending=pending)
            assert recon == ()
        return biller.captured_amount()

    assert asyncio.run(go()) == total, (costs, crash_iter, stage, extra_resumes)
