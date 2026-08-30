"""Durability — checkpoint a run after every iteration, resume it without charging twice.

This is the seam where resume meets money. Generic durable-execution engines (Temporal,
DBOS, Prefect, Restate) give **exactly-once orchestration state** — the workflow replays
deterministically and never re-runs a *completed* step — but they explicitly do **not** give
exactly-once *side effects*: a worker can succeed at a paid external call and then crash
before recording that success, so the step is retried. Every engine documents this and every
engine's canonical example is payment processing. See ``docs/02-architecture.md`` §"Durable
execution, and the guarantee nobody can give you".

So the guarantee this package ships is the honest one:

    at-least-once delivery  +  idempotent receiver  =  effectively-once

``Run.captured_reservations`` is the idempotent-receiver ledger. :mod:`resume` consults it on
every capture and refuses a duplicate (:class:`~pikachu.core.errors.DoubleCaptureError`),
which is invariant **P9**: *a resume must never re-capture an already-captured reservation.*

Two objects, both composing Protocols from :mod:`pikachu.core.protocols` (a ``RunStore`` and a
``Biller``) so this module has no hard dependency on any storage or billing implementation —
that Protocol contract is what keeps this lane independent of the SQLite lane and the billing
lane:

* :class:`~pikachu.durability.checkpoint.Checkpointer` — persist a ``Run`` after every
  iteration, cheaply and losslessly.
* :class:`~pikachu.durability.resume.Resumer` — reconstruct from the last checkpoint, replay
  only what is safe, and never re-capture a captured reservation.

Kept dependency-light on purpose: importing this module pulls only the core types and
stdlib, never ``pydantic_ai`` — consistent with the package's lazy-import rule.
"""

from __future__ import annotations

from pikachu.durability.checkpoint import Checkpointer
from pikachu.durability.resume import (
    DurableRunner,
    Reconciliation,
    ResumeDecision,
    Resumer,
)

__all__ = [
    "Checkpointer",
    "DurableRunner",
    "Reconciliation",
    "ResumeDecision",
    "Resumer",
]
