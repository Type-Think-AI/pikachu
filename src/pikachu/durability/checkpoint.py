"""Checkpointing — persist a ``Run`` after every iteration through the ``RunStore`` seam.

A checkpoint is deliberately dumb: it writes the **whole** ``Run`` (phase, iteration,
charged/refunded credits, and — the load-bearing field — ``captured_reservations``). It does
not compute a diff, does not summarise, and never drops a field a resume would need, because
the thing a resume needs most is the exact set of reservations already captured, and a lossy
checkpoint is how that set silently shrinks and a user gets charged twice.

Cheapness is a real requirement (the checkpoint runs after *every* iteration of a turn), and
it is met by construction: a ``Run`` is a frozen Pydantic model, so recording state is one
``RunStore.checkpoint`` call with no serialisation logic living here — the store owns
persistence. The only work this class does is compose the immutable updates onto the frozen
model before handing it over.

Composes the ``RunStore`` **Protocol** from :mod:`pikachu.core.protocols`; it never imports a
concrete store. The SQLite store, an in-memory fake, or a Postgres-backed host store all
satisfy it identically — that is what lets this lane be built and tested with no dependency on
the storage lane.
"""

from __future__ import annotations

from pikachu.core.protocols import RunStore
from pikachu.core.types import Run, RunPhase, utcnow

__all__ = ["Checkpointer"]


class Checkpointer:
    """Persist run state after each iteration, losslessly.

    Wraps a ``RunStore``. Every method returns the ``Run`` that was persisted, so a caller
    can keep an in-memory copy in lockstep with the durable one without a re-read.
    """

    def __init__(self, store: RunStore) -> None:
        self._store = store

    async def open(self, run: Run) -> Run:
        """Create the durable base checkpoint for a new run.

        A fresh ``Run`` starts ``PENDING`` at iteration 0. This is the one write that MUST
        NOT overwrite: ``RunStore.create`` refuses a duplicate id, so an accidental second
        ``open`` of the same run raises rather than silently resetting a run that may already
        have captured reservations. Use :meth:`record` (checkpoint, which overwrites) for
        every write after this one.
        """
        return await self._store.create(run)

    async def record(
        self,
        run: Run,
        *,
        iteration: int | None = None,
        phase: RunPhase | None = None,
        charged_credits: int | None = None,
        refunded_credits: int | None = None,
        captured_reservations: frozenset[str] | None = None,
    ) -> Run:
        """Checkpoint the run's current state, optionally advancing fields in one write.

        Every argument left ``None`` is carried forward from ``run`` unchanged, so a bare
        ``record(run)`` persists exactly what it was handed and a partial update touches only
        what it names. The updated (still frozen) ``Run`` is written through
        ``RunStore.checkpoint`` and returned.

        ``captured_reservations`` is *merged*, never replaced: capture is monotonic, and a
        checkpoint must be unable to *forget* a reservation was captured. Passing a set here
        unions it with what the run already records; there is intentionally no way to shrink
        it, because shrinking it is the double-charge bug.
        """
        merged_reservations = run.captured_reservations
        if captured_reservations is not None:
            merged_reservations = merged_reservations | captured_reservations

        updated = run.model_copy(
            update={
                "iteration": run.iteration if iteration is None else iteration,
                "phase": run.phase if phase is None else phase,
                "charged_credits": (
                    run.charged_credits if charged_credits is None else charged_credits
                ),
                "refunded_credits": (
                    run.refunded_credits if refunded_credits is None else refunded_credits
                ),
                "captured_reservations": merged_reservations,
            }
        )
        return await self._store.checkpoint(updated)

    async def record_capture(
        self,
        run: Run,
        reservation_id: str,
        *,
        amount: int,
    ) -> Run:
        """Record that a reservation was captured, adding its cost to ``charged_credits``.

        This is the *checkpoint* half of a capture — it makes the capture durable in the run
        record so a later resume sees the reservation in ``captured_reservations`` and does
        not repeat it. The *billing* half (the actual charge) belongs to the ``Biller`` and
        is driven by :class:`~pikachu.durability.resume.Resumer`; keeping the two in one
        transaction is the host's job, documented on the resumer.

        Idempotent by set semantics: recording the same reservation id twice adds it to the
        frozenset once and (because the amount was already counted the first time) must not
        be double-added. If the id is already present, ``charged_credits`` is left untouched.
        """
        if reservation_id in run.captured_reservations:
            # Already recorded — do not add its cost again. This is the resume path.
            return await self._store.checkpoint(run)
        updated = run.model_copy(
            update={
                "captured_reservations": run.captured_reservations | {reservation_id},
                "charged_credits": run.charged_credits + amount,
            }
        )
        return await self._store.checkpoint(updated)

    async def close(self, run: Run, *, phase: RunPhase) -> Run:
        """Write the final checkpoint, stamping a terminal phase and ``ended_at``.

        ``phase`` must be terminal (``SUCCEEDED`` / ``FAILED`` / ``CANCELLED``); a
        non-terminal phase here is a caller bug and is rejected, because "closing" a run into
        a running state is meaningless and would let a resume re-run it.
        """
        if not phase.is_terminal:
            raise ValueError(
                f"close requires a terminal phase, got {phase.value!r}; "
                "use record() for non-terminal checkpoints"
            )
        updated = run.model_copy(update={"phase": phase, "ended_at": utcnow()})
        return await self._store.checkpoint(updated)
