"""Resume — reconstruct a run from its last checkpoint without charging a user twice.

★ **Invariant P9: a resume must NEVER re-capture an already-captured reservation.**

Generic durable execution is *at-least-once*: on resume it will faithfully re-drive any step
whose completion it did not manage to record. That is exactly right for idempotent work and
exactly *wrong* for a paid image generation, where re-driving the capture step charges the
user a second time for one picture. ``Run.captured_reservations`` is the guard that makes the
receiver idempotent: :meth:`Resumer.safe_capture` consults it before every capture and refuses
a duplicate, raising :class:`~pikachu.core.errors.DoubleCaptureError` rather than proceeding.

The genuinely hard case, handled explicitly rather than assumed away:

    a crash AFTER the provider did the paid work, but BEFORE we recorded the capture.

The tool's recorded outcome is :attr:`ToolOutcome.INTERRUPTED`. The side effect **may** have
happened — the image may already exist and the provider may already have billed us — or it may
not. We do not know, and we must not pretend to. So a resume does **not** blindly re-capture
(that double-charges if the work landed) and does **not** blindly release (that gives away a
generation we paid for if it landed). It surfaces the unknown as a
:class:`Reconciliation` the caller must resolve against the provider's idempotency ledger
(the run's idempotency key is the join key — see ``docs/02-architecture.md``). Making the
unknown *visible* is the whole point; guessing either way is the bug.

A terminal run is terminal. Resuming a ``SUCCEEDED`` / ``FAILED`` / ``CANCELLED`` run is
refused, because "resuming" finished work is another way to re-run a paid step.

Composes the ``RunStore`` and ``Biller`` **Protocols** only. The billing lane may not exist
yet; this module never imports it. The contract is ``core.protocols.Biller``, and coding
against it is what keeps the lanes independent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pikachu.core.errors import DoubleCaptureError, PikachuError
from pikachu.core.protocols import Biller, RunStore
from pikachu.core.types import Run, RunPhase, ToolOutcome
from pikachu.durability.checkpoint import Checkpointer

__all__ = [
    "DurableRunner",
    "Reconciliation",
    "ResumeDecision",
    "Resumer",
    "TerminalRunError",
    "UnknownRunError",
]


class TerminalRunError(PikachuError):
    """Raised when a resume is attempted on an already-terminal run.

    A terminal phase (``SUCCEEDED``/``FAILED``/``CANCELLED``) is final. Re-running it would
    re-drive whatever paid steps it contained, so resume refuses instead.
    """

    def __init__(self, run_id: str, phase: RunPhase) -> None:
        super().__init__(f"run {run_id!r} is terminal ({phase.value}); it may not be resumed")
        self.run_id = run_id
        self.phase = phase


class UnknownRunError(PikachuError):
    """Raised when a resume references a run id the ``RunStore`` has never seen."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} not found; nothing to resume")
        self.run_id = run_id


class ResumeDecision(str, Enum):
    """What a resume determined should happen to a specific reservation.

    Deterministic and side-effect-free to compute — the mapping from (already-captured?,
    outcome) to a decision is plain Python, so it is trivially testable and a property test
    can assert it never lands on ``RECAPTURE``.
    """

    ALREADY_CAPTURED = "already_captured"
    """The reservation is in ``captured_reservations``. Do nothing — capturing it again is
    P9's forbidden move. This is the resume's most important branch."""

    CAPTURE = "capture"
    """The provider work completed (outcome ``SUCCESS``) and we have NOT yet recorded the
    capture. Safe to capture exactly once, then record it durably."""

    RELEASE = "release"
    """The work did not happen (``FAILED``/``DENIED``) and no capture was recorded. Return
    the reservation unspent."""

    RECONCILE = "reconcile"
    """The work's outcome is unknown (``INTERRUPTED``) and no capture was recorded. Neither
    capture nor release is safe without checking the provider. Surfaced, never guessed."""


@dataclass(frozen=True)
class Reconciliation:
    """An unknown-outcome reservation a resume could not safely resolve on its own.

    This is the ``INTERRUPTED`` case made into a value instead of a guess. A caller must
    resolve it by asking the provider — keyed on the run's **idempotency key** — whether the
    paid side effect actually happened, then calling :meth:`Resumer.settle_reconciliation`
    with the answer.
    """

    run_id: str
    reservation_id: str
    amount: int
    note: str = (
        "provider side effect may or may not have occurred; check the provider's "
        "idempotency ledger on the run's idempotency key before capturing or releasing"
    )


@dataclass
class ResumePlan:
    """The full result of resolving one interrupted reservation on resume.

    Bundles the decision, the (possibly unchanged) run, and — when the decision is
    ``RECONCILE`` — the :class:`Reconciliation` the caller must act on. Returned by
    :meth:`Resumer.resume_reservation` so a caller gets both the effect and an explicit,
    inspectable account of what was decided and why.
    """

    decision: ResumeDecision
    run: Run
    reconciliation: Reconciliation | None = None


class Resumer:
    """Resume a checkpointed run, upholding P9 at every capture.

    Wraps a ``RunStore`` (to read the last checkpoint and persist forward) and a ``Biller``
    (to actually capture/release). All capture paths funnel through :meth:`safe_capture`, so
    there is exactly one place the ``captured_reservations`` guard is consulted and exactly
    one place ``DoubleCaptureError`` can originate — a single chokepoint is easier to prove
    correct than a guard sprinkled across call sites.
    """

    def __init__(self, store: RunStore, biller: Biller) -> None:
        self._store = store
        self._biller = biller
        self._checkpointer = Checkpointer(store)

    async def load(self, run_id: str) -> Run:
        """Fetch the last durable checkpoint, or raise if the run is unknown/terminal.

        This is the gate every resume passes through: an unknown id is
        :class:`UnknownRunError`, a terminal run is :class:`TerminalRunError`. A run that
        clears both is safe to continue.
        """
        run = await self._store.get(run_id)
        if run is None:
            raise UnknownRunError(run_id)
        if run.phase.is_terminal:
            raise TerminalRunError(run_id, run.phase)
        return run

    @staticmethod
    def decide(run: Run, reservation_id: str, outcome: ToolOutcome) -> ResumeDecision:
        """Pure decision function: what should happen to ``reservation_id`` on resume.

        No I/O, no mutation — just the P9 truth table, so it can be exhaustively property
        tested. The first check is the one that matters: a reservation already in
        ``captured_reservations`` is ``ALREADY_CAPTURED`` regardless of the reported outcome,
        which is precisely why a resume never re-captures.
        """
        if reservation_id in run.captured_reservations:
            return ResumeDecision.ALREADY_CAPTURED
        if outcome is ToolOutcome.SUCCESS:
            return ResumeDecision.CAPTURE
        if outcome is ToolOutcome.INTERRUPTED:
            return ResumeDecision.RECONCILE
        # FAILED or DENIED: the paid work did not land and nothing was captured.
        return ResumeDecision.RELEASE

    async def safe_capture(
        self,
        run: Run,
        reservation_id: str,
        *,
        amount: int,
        outcome: ToolOutcome = ToolOutcome.SUCCESS,
    ) -> Run:
        """Capture a reservation exactly once, or refuse if it was already captured.

        The single enforcement point for P9. If ``reservation_id`` is already in
        ``run.captured_reservations`` this raises :class:`DoubleCaptureError` **before**
        touching the ``Biller`` — no second charge is even attempted. Otherwise it captures
        through the ``Biller`` and then records the capture durably (adding ``amount`` to
        ``charged_credits`` and the id to ``captured_reservations``), returning the updated
        run.

        Ordering note: the ``Biller`` capture must itself be idempotent on ``reservation_id``
        (its Protocol requires it), so even if a crash lands between the biller call and the
        durable record, a subsequent resume sees the id absent from the run record, calls the
        biller again, and the biller's own idempotency makes that a no-op — the money is
        charged once. The run-record guard here is the fast, in-process line of defence; the
        biller's idempotency is the backstop.
        """
        if reservation_id in run.captured_reservations:
            raise DoubleCaptureError(reservation_id)
        await self._biller.capture(reservation_id, outcome=outcome)
        return await self._checkpointer.record_capture(run, reservation_id, amount=amount)

    async def resume_reservation(
        self,
        run: Run,
        reservation_id: str,
        *,
        amount: int,
        outcome: ToolOutcome,
    ) -> ResumePlan:
        """Resolve one reservation on resume, applying the safe action for its state.

        Runs :meth:`decide`, then executes it:

        * ``ALREADY_CAPTURED`` → no-op; the run is returned unchanged. This is the branch
          that makes replaying a completed capture a no-op instead of a double charge.
        * ``CAPTURE`` → :meth:`safe_capture` (charges once, records durably).
        * ``RELEASE`` → release the reservation through the ``Biller`` (unspent, idempotent).
        * ``RECONCILE`` → do **not** touch billing; return a :class:`Reconciliation` for the
          caller to settle against the provider. The run is unchanged so the interrupted
          reservation stays visibly unresolved until reconciled.
        """
        decision = self.decide(run, reservation_id, outcome)
        if decision is ResumeDecision.ALREADY_CAPTURED:
            return ResumePlan(decision, run)
        if decision is ResumeDecision.CAPTURE:
            updated = await self.safe_capture(
                run, reservation_id, amount=amount, outcome=outcome
            )
            return ResumePlan(decision, updated)
        if decision is ResumeDecision.RELEASE:
            await self._biller.release(reservation_id)
            return ResumePlan(decision, run)
        # RECONCILE — the INTERRUPTED case. Surface it; do not guess.
        recon = Reconciliation(
            run_id=run.id, reservation_id=reservation_id, amount=amount
        )
        return ResumePlan(decision, run, reconciliation=recon)

    async def settle_reconciliation(
        self,
        run: Run,
        recon: Reconciliation,
        *,
        side_effect_occurred: bool,
    ) -> Run:
        """Resolve a previously-surfaced :class:`Reconciliation` once the caller knows the truth.

        The caller has checked the provider's idempotency ledger and learned whether the paid
        side effect actually happened:

        * ``side_effect_occurred=True`` → the image exists and we were billed, so
          :meth:`safe_capture` charges the user for it exactly once (still guarded by P9, so
          a reconciliation settled twice cannot double-charge).
        * ``side_effect_occurred=False`` → nothing happened; release the reservation unspent.

        This is the *only* path by which an ``INTERRUPTED`` reservation becomes a charge, and
        it requires the caller to assert the fact — the module never decides it alone.
        """
        if side_effect_occurred:
            return await self.safe_capture(
                run, recon.reservation_id, amount=recon.amount, outcome=ToolOutcome.SUCCESS
            )
        await self._biller.release(recon.reservation_id)
        return run

    async def resume(
        self,
        run_id: str,
        *,
        pending: tuple[tuple[str, int, ToolOutcome], ...] = (),
    ) -> tuple[Run, tuple[Reconciliation, ...]]:
        """Load a run and resolve any reservations that were in flight at the crash.

        ``pending`` is the set of reservations whose fate the checkpoint could not settle —
        each a ``(reservation_id, amount, outcome)`` triple, typically read from a tool-call
        journal the caller kept alongside the run. For each, the safe action is applied via
        :meth:`resume_reservation`. Reservations already in ``captured_reservations`` are
        no-ops (P9), so it is safe to pass the same ``pending`` set on repeated resumes.

        Returns the run advanced past all resolvable reservations, plus the list of
        :class:`Reconciliation`s the caller must still settle. An empty second element means
        the run is fully reconciled and safe to continue iterating.
        """
        run = await self.load(run_id)
        reconciliations: list[Reconciliation] = []
        for reservation_id, amount, outcome in pending:
            plan = await self.resume_reservation(
                run, reservation_id, amount=amount, outcome=outcome
            )
            run = plan.run
            if plan.reconciliation is not None:
                reconciliations.append(plan.reconciliation)
        return run, tuple(reconciliations)


@dataclass
class DurableRunner:
    """The seam a heavyweight durable-execution engine plugs into — an integration, not a requirement.

    Temporal, DBOS, Prefect and Restate all solve *orchestration* durability (deterministic
    replay, never re-running a completed step) and all leave the *paid side effect* as
    at-least-once homework — the exact homework :class:`Resumer` does with
    ``captured_reservations``. If a host wants one of those engines, it wraps this runner: the
    engine drives the retry/replay loop, and every capture the engine's steps make routes
    through :meth:`Resumer.safe_capture` so the P9 guard holds regardless of how many times
    the engine replays.

    Adding Temporal/DBOS/Prefect/Restate as a *dependency* is deliberately refused — it would
    put a heavyweight framework into a package whose dependency list is intentionally one
    framework (Pydantic AI). This class is the plug; the engines are optional integrations a
    host supplies, documented in ``docs/02-architecture.md``.
    """

    resumer: Resumer
    checkpointer: Checkpointer

    @classmethod
    def build(cls, store: RunStore, biller: Biller) -> DurableRunner:
        """Assemble a runner from the two Protocols, sharing one ``Checkpointer``."""
        return cls(resumer=Resumer(store, biller), checkpointer=Checkpointer(store))
