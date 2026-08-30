"""The metered-tool ledger — the primitive no agent framework has, because their tools
are free and ours are not.

A generic agent framework can afford to model a tool call as *retryable*: "the call failed,
run it again." When the tool costs real credits per invocation, "retry the failed tool"
silently means "charge the user twice" unless the accounting underneath is exactly right.
This module is that accounting.

The shape (matches the ``Biller`` / ``Reservation`` Protocols in ``core.protocols``)::

    reservation = await biller.reserve(run_id=..., tool=..., amount=...)
    # ... perform the paid side effect ...
    await biller.capture(reservation.id, outcome=ToolOutcome.SUCCESS)   # commit the charge
    # or, if the side effect never happened:
    await biller.release(reservation.id)                                # refund, unspent

Invariants this module owes, each proven by ``tests/properties/test_billing.py``:

* **P5 — one charging point, refund on failure.** ``capture`` is the *only* method that
  moves credit from reserved to charged, and ``release`` is the *only* refund path. There is
  no second way to charge. Total charged can never exceed total reserved.
* **Capture is idempotent on ``reservation_id``.** A second capture of the same id raises
  :class:`DoubleCaptureError` — never a silent double-charge. Re-issuing the *identical*
  capture (same id, same outcome) is the resume case and is a tolerated no-op.
* **``release`` is idempotent.** Releasing an unknown, already-released, or already-captured
  reservation does not raise and does not change credit.
* **A released reservation can never later be captured**, and a captured one can never be
  released. The state machine is terminal.

The subtle state: :class:`ToolOutcome.INTERRUPTED`
--------------------------------------------------
``INTERRUPTED`` means we lost visibility *while the paid side effect may already have
happened* — the process died mid-call, the connection dropped after the request was sent but
before the response arrived. There are two wrong things to do with it and one right thing:

* **Wrong: treat it as failure and release.** If the generation *did* happen we have now
  given away a paid result for free, and — worse — the natural next step is a retry, which
  reserves and captures a *second* time. That is the double-charge this module exists to
  prevent, arrived at from the opposite direction.
* **Wrong: treat it as plain success and forget it.** We would charge for a result we cannot
  prove was produced, with no record that the charge is disputable.
* **Right: capture it, but into a distinct terminal state that is flagged for
  reconciliation.** The credit is held as charged (so no retry re-runs a possibly-completed
  paid call), and the reservation is marked ``needs_reconciliation`` so a human or a
  provider-side check can later confirm the side effect and either keep the charge or issue a
  refund through the one refund path.

So ``INTERRUPTED`` is captured, not released, and its ledger entry carries
``needs_reconciliation=True``. See :meth:`LedgerBiller.capture` and
:meth:`LedgerBiller.unreconciled` — and the callers' contract is stated there in full.

Pure logic + the Protocol
-------------------------
This module imports only ``pikachu.core`` (Pydantic + stdlib). Persistence is
``storage/``'s job: a ``RunStore`` with a durable ``capture`` (see ``storage.sqlite``) is
where a real deployment records captures across a restart. :class:`LedgerBiller` works fully
in memory and takes no store; the durable seam is the ``Biller`` Protocol itself, which
``storage.sqlite.SqliteRunStore`` already satisfies. Lane Q's resume path consults
``Run.captured_reservations`` — the same set this ledger exposes — and refuses to re-capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pikachu.core.errors import DoubleCaptureError
from pikachu.core.types import ToolOutcome, utcnow

__all__ = [
    "LedgerBiller",
    "LedgerEntry",
    "Reservation",
    "ReservationState",
]


class ReservationState(str, Enum):
    """Terminal disposition of a reservation.

    The machine is ``RESERVED -> {CAPTURED, RELEASED}``, with ``NEEDS_RECONCILIATION`` a
    *sub-kind of captured* rather than a fourth state: credit is held either way, but an
    interrupted capture is flagged so it can be revisited. Modelling it as its own state
    keeps ``INTERRUPTED`` from ever being confused with a clean success or with a release.
    """

    RESERVED = "reserved"
    """Held, not yet committed. The only non-terminal state."""

    CAPTURED = "captured"
    """Charge committed on a known outcome (SUCCESS / FAILED / DENIED). Terminal."""

    NEEDS_RECONCILIATION = "needs_reconciliation"
    """Charge committed on an INTERRUPTED outcome — the side effect MAY have happened, so
    the credit is held rather than refunded, but the entry is flagged for a later check.
    Terminal until a caller reconciles it (confirm-and-keep, or refund via ``release`` is
    NOT how this is undone — reconciliation issues an explicit refund; see ``unreconciled``).
    """

    RELEASED = "released"
    """Returned unspent. No charge. Terminal."""

    @property
    def is_terminal(self) -> bool:
        return self is not ReservationState.RESERVED

    @property
    def is_charged(self) -> bool:
        """Whether this state holds credit as charged (captured, including interrupted)."""
        return self in (
            ReservationState.CAPTURED,
            ReservationState.NEEDS_RECONCILIATION,
        )


@dataclass(frozen=True)
class Reservation:
    """A held-but-not-yet-charged amount of credit.

    Frozen: a reservation's ``id`` and ``amount`` never change after issue. Satisfies the
    structural ``Reservation`` Protocol in ``core.protocols`` (``id`` and ``amount``
    properties). The remaining fields are ledger bookkeeping, not part of the Protocol.
    """

    _id: str
    _amount: int
    run_id: str
    tool: str

    @property
    def id(self) -> str:
        return self._id

    @property
    def amount(self) -> int:
        return self._amount


@dataclass(frozen=True)
class LedgerEntry:
    """One immutable audit row: a reservation and its terminal disposition.

    The ledger is append-and-transition-once: an entry starts ``RESERVED`` and is replaced
    by exactly one terminal entry. Frozen so a recorded charge cannot be edited after the
    fact — an audit that can be rewritten is not an audit. Both the run's spend and the
    reason for it are recoverable from the entries alone.
    """

    reservation_id: str
    run_id: str
    tool: str
    amount: int
    state: ReservationState
    outcome: ToolOutcome | None = None
    """The outcome the capture was committed on. ``None`` while still ``RESERVED`` or once
    ``RELEASED`` (a release has no tool outcome)."""

    at: str = field(default_factory=lambda: utcnow().isoformat())

    @property
    def needs_reconciliation(self) -> bool:
        return self.state is ReservationState.NEEDS_RECONCILIATION

    @property
    def charged_amount(self) -> int:
        """The credit this entry holds as charged — its ``amount`` if charged, else 0."""
        return self.amount if self.state.is_charged else 0


class LedgerBiller:
    """In-memory :class:`Biller` with an auditable ledger.

    Satisfies the ``Biller`` Protocol structurally (``isinstance(biller, Biller)`` is True
    against the ``runtime_checkable`` Protocol). Every reservation is recorded, and every
    reservation's terminal state is recorded, so a run's spend is auditable after the fact
    via :meth:`ledger` / :meth:`captured_reservations` / :meth:`unreconciled`.

    **One charging point.** ``capture`` is the sole method that marks credit charged;
    ``release`` is the sole refund. There is deliberately no ``charge()``, no ``debit()``,
    no direct ledger mutation — a second charging path is exactly how P5 gets violated, so
    it does not exist.

    Thread-note: this is single-connection / single-task logic like the rest of the pure
    layer; concurrency control belongs to the durable store (SQLite's ``captures`` PRIMARY
    KEY), not here. The property tests exercise arbitrary *interleavings* of the operations,
    which is the ordering hazard this layer must survive; true parallel writers are the
    store's problem.
    """

    def __init__(self) -> None:
        self._counter = 0
        # reservation_id -> current entry (RESERVED, then replaced by a terminal entry).
        self._entries: dict[str, LedgerEntry] = {}
        # Full append-only history for audit: every state an entry ever held, in order.
        self._history: list[LedgerEntry] = []

    # -- Biller Protocol ---------------------------------------------------------------

    async def reserve(self, *, run_id: str, tool: str, amount: int) -> Reservation:
        """Hold ``amount`` credits for ``tool`` on ``run_id``. Records a RESERVED entry.

        ``amount`` must be non-negative; a zero-cost (free) tool may still be reserved, in
        which case capture and release both move zero credit but the audit row still exists.
        """
        if amount < 0:
            raise ValueError(f"reservation amount must be >= 0, got {amount}")
        self._counter += 1
        rid = f"res-{run_id}-{tool}-{self._counter}"
        reservation = Reservation(_id=rid, _amount=amount, run_id=run_id, tool=tool)
        entry = LedgerEntry(
            reservation_id=rid,
            run_id=run_id,
            tool=tool,
            amount=amount,
            state=ReservationState.RESERVED,
        )
        self._entries[rid] = entry
        self._history.append(entry)
        return reservation

    async def capture(self, reservation_id: str, *, outcome: ToolOutcome) -> None:
        """Commit the charge for ``reservation_id``. The ONLY charging point (P5).

        Idempotent on ``reservation_id`` in the exact sense the resume path needs: a repeat
        capture with the **same outcome** is a tolerated no-op (a resumed run re-issuing the
        capture it already made). A repeat capture with a **different outcome**, or any
        capture of a reservation that was already released, is a genuine double-charge or an
        illegal transition and raises :class:`DoubleCaptureError`.

        ``outcome`` decides the terminal state:

        * ``SUCCESS`` / ``FAILED`` / ``DENIED`` -> ``CAPTURED``. The charge stands. (The
          charge is for *invoking* the metered tool; a provider that ran and returned FAILED
          still consumed the paid call. If your provider does not bill failed calls, release
          instead of capturing on FAILED — that is a policy choice the caller makes by
          choosing capture vs release, not something this method second-guesses.)
        * ``INTERRUPTED`` -> ``NEEDS_RECONCILIATION``. The side effect MAY have happened, so
          the credit is HELD (not refunded) to stop a retry re-running a possibly-completed
          paid call, and the entry is flagged. **Caller contract on INTERRUPTED:** do NOT
          retry the tool against a fresh reservation on the assumption it failed. Surface the
          reservation via :meth:`unreconciled`; a later provider-side check confirms whether
          the side effect occurred and either keeps the charge (reconcile) or refunds it
          (``release`` of the *same* reservation is permitted only out of
          ``NEEDS_RECONCILIATION`` — see :meth:`release`).
        """
        entry = self._entries.get(reservation_id)
        if entry is None:
            raise KeyError(f"unknown reservation {reservation_id!r}")

        target = (
            ReservationState.NEEDS_RECONCILIATION
            if outcome is ToolOutcome.INTERRUPTED
            else ReservationState.CAPTURED
        )

        if entry.state.is_charged:
            # Already captured. Idempotent iff the same outcome is being re-committed;
            # otherwise it is a real double-capture and must be refused.
            if entry.outcome == outcome:
                return
            raise DoubleCaptureError(reservation_id)

        if entry.state is ReservationState.RELEASED:
            # A released reservation can never be captured — that would charge for credit
            # already refunded. Refuse loudly.
            raise DoubleCaptureError(reservation_id)

        self._transition(entry, target, outcome=outcome)

    async def release(self, reservation_id: str) -> None:
        """Return a reservation unspent. Idempotent. The ONLY refund path.

        Releasing an unknown reservation, an already-released one, or an already-``CAPTURED``
        one is a no-op — never an error, so a cleanup pass that releases everything it is
        unsure about cannot double-refund or crash.

        The one substantive release is out of ``RESERVED`` (the ordinary refund-on-failure
        case) and out of ``NEEDS_RECONCILIATION`` (an interrupted charge that a later check
        confirmed did NOT happen — this is the sanctioned reconciliation refund). A release
        out of ``NEEDS_RECONCILIATION`` clears the reconciliation flag by moving the entry to
        ``RELEASED``.
        """
        entry = self._entries.get(reservation_id)
        if entry is None:
            return  # unknown -> no-op (idempotent)
        if entry.state is ReservationState.RELEASED:
            return  # already released -> no-op
        if entry.state is ReservationState.CAPTURED:
            return  # a settled charge is not refundable through release -> no-op
        # RESERVED or NEEDS_RECONCILIATION -> RELEASED (refund).
        self._transition(entry, ReservationState.RELEASED, outcome=None)

    # -- Audit surface (not part of the Biller Protocol) -------------------------------

    def ledger(self, *, run_id: str | None = None) -> tuple[LedgerEntry, ...]:
        """Current entries, one per reservation, optionally scoped to a run.

        Deterministic order: by reservation id. This is the after-the-fact audit of what a
        run reserved and how each reservation settled.
        """
        entries = [
            e
            for e in self._entries.values()
            if run_id is None or e.run_id == run_id
        ]
        entries.sort(key=lambda e: e.reservation_id)
        return tuple(entries)

    def history(self) -> tuple[LedgerEntry, ...]:
        """Full append-only transition history, in the order changes happened.

        Every state every reservation passed through, so the audit shows not just where a
        reservation ended but the path it took there."""
        return tuple(self._history)

    def captured_reservations(self, *, run_id: str | None = None) -> frozenset[str]:
        """Ids currently holding a charge (CAPTURED or NEEDS_RECONCILIATION).

        This is the set ``Run.captured_reservations`` mirrors and the set a resume consults
        to refuse a re-capture (invariant P9, Lane Q). Interrupted-but-held reservations are
        included, because a resume must not re-run them either — that they still need
        reconciliation does not make them safe to charge again."""
        return frozenset(
            rid
            for rid, e in self._entries.items()
            if e.state.is_charged and (run_id is None or e.run_id == run_id)
        )

    def unreconciled(self, *, run_id: str | None = None) -> tuple[LedgerEntry, ...]:
        """Entries captured on an INTERRUPTED outcome and still awaiting reconciliation.

        The work queue for the reconciliation step: each of these is a charge held against a
        side effect we could not confirm. A caller resolves each one by either leaving it
        (confirmed to have happened) or releasing it (confirmed not to have)."""
        entries = [
            e
            for e in self._entries.values()
            if e.needs_reconciliation and (run_id is None or e.run_id == run_id)
        ]
        entries.sort(key=lambda e: e.reservation_id)
        return tuple(entries)

    def total_reserved(self, *, run_id: str | None = None) -> int:
        """Sum of every reservation ever made (the ceiling total_charged must not exceed)."""
        return sum(
            e.amount
            for e in self._entries.values()
            if run_id is None or e.run_id == run_id
        )

    def total_charged(self, *, run_id: str | None = None) -> int:
        """Sum of credit currently held as charged. P5: this never exceeds total_reserved."""
        return sum(
            e.charged_amount
            for e in self._entries.values()
            if run_id is None or e.run_id == run_id
        )

    def is_captured(self, reservation_id: str) -> bool:
        entry = self._entries.get(reservation_id)
        return entry is not None and entry.state.is_charged

    # -- internal ----------------------------------------------------------------------

    def _transition(
        self,
        entry: LedgerEntry,
        state: ReservationState,
        *,
        outcome: ToolOutcome | None,
    ) -> None:
        """Replace ``entry`` with a new terminal entry and append it to history.

        The current entry is immutable, so a transition creates a fresh ``LedgerEntry`` — the
        old one stays in ``_history`` as the record that the reservation was once RESERVED.
        """
        new_entry = LedgerEntry(
            reservation_id=entry.reservation_id,
            run_id=entry.run_id,
            tool=entry.tool,
            amount=entry.amount,
            state=state,
            outcome=outcome,
        )
        self._entries[entry.reservation_id] = new_entry
        self._history.append(new_entry)
