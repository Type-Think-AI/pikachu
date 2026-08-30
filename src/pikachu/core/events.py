"""Turn events — the streaming contract.

A non-streaming caller gets a completed :class:`~pikachu.core.types.TurnResult` and cannot
show progress at all. This module is the other shape of the same turn: an ordered sequence
of frozen events a consumer can render as it goes, terminated by a :class:`TurnFinished`
that carries **the very same** ``TurnResult`` the non-streaming path returns — timing and
all. Streaming must never be a lossy view of a turn; it is the same turn, observed live.

The union is **discriminated on a ``kind`` literal**. Every event carries a distinct
``kind`` string, so a consumer can ``match`` over ``TurnEvent`` exhaustively and
``mypy --strict`` verifies the match is total — a new event kind added here without a
matching branch downstream becomes a type error rather than a silent fall-through. That is
the whole reason the discriminant is a ``Literal`` and not a plain ``str``.

Ordering guarantee, asserted by the streaming tests:

* :class:`TurnStarted` is always first.
* :class:`TurnFinished` is always last, and appears exactly once.
* A tool call is always a :class:`ToolCallStarted` followed by zero or more
  :class:`ToolCallProgress` events and then its :class:`ToolCallFinished`, in that order,
  the finished event carrying the tool's :class:`~pikachu.core.types.ToolOutcome`. All three
  share a stable ``call_id`` so a consumer correlates start, progress and finish into one
  rendered tool card. A tool that crashes still emits its :class:`ToolCallFinished` (with
  ``outcome == ToolOutcome.FAILED``), so a failure never strands the stream before
  :class:`TurnFinished`.

Consistent with the rest of ``core/``: frozen Pydantic models, dependency-light, no
knowledge of Pydantic AI, HTTP, or a backend implementation.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from pikachu.core.types import Artifact, ToolOutcome, TurnResult

__all__ = [
    "ArtifactProduced",
    "TextDelta",
    "ToolCallFinished",
    "ToolCallProgress",
    "ToolCallStarted",
    "TurnEvent",
    "TurnFinished",
    "TurnStarted",
]


class TurnStarted(BaseModel):
    """The turn has begun. Always the first event in a stream.

    ``streaming`` records whether the deltas that follow were produced by a backend that
    can genuinely stream, or reconstructed from a completed result on the degraded path.
    A consumer that cares about time-to-first-token must not mistake one for the other, so
    the distinction is on the wire rather than inferred — see
    :func:`pikachu.backends.streaming.stream_turn`.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["turn_started"] = "turn_started"
    agent_name: str
    streaming: bool = False
    """True only when the underlying backend streamed natively. False means the sequence was
    reconstructed after the turn completed (still coherent, just not live)."""


class TextDelta(BaseModel):
    """A chunk of assistant text.

    On a natively streaming backend these arrive incrementally as the model decodes. On the
    degraded path the whole text arrives as a single delta — the sequence stays valid, it
    simply does not stream, and :attr:`TurnStarted.streaming` says so.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallStarted(BaseModel):
    """A tool call has begun. Always paired with a later :class:`ToolCallFinished`.

    ``call_id`` is a STABLE per-call correlation id: it is emitted here and repeated on every
    :class:`ToolCallProgress` for this call and on its terminating :class:`ToolCallFinished`,
    so a consumer ties the three together and renders one tool card that transitions from a
    "generating" skeleton to the finished asset. This is the Pikachu analogue of the
    ``tool_call_id`` PicX's Hermes handler emits on its ``tool_started`` payload
    (``api/app/groot/agent_tools.py::_emit``) — the property that lets a minutes-long media
    generation not look frozen.

    It defaults to ``""`` because the degraded/reconstructed streaming path
    (:func:`pikachu.backends.streaming.stream_turn` when a backend does not stream natively)
    has no provider-issued id to carry: the turn already completed, so there is nothing live
    to correlate. On the native path (``PydanticAIBackend.stream_turn``) it carries the
    provider's own ``tool_call_id``, which is the same id the matching result part carries.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["tool_call_started"] = "tool_call_started"
    tool: str
    call_id: str = ""
    """Stable per-call id shared with this call's progress and finished events. Empty on the
    reconstructed path (no live id to correlate); the provider's ``tool_call_id`` on the
    native path."""
    args: str = ""
    """A rendered, size-bounded view of the arguments — a string, not the live object, so an
    event is cheap to log and carries no reference a consumer could mutate."""


class ToolCallProgress(BaseModel):
    """Progress within a tool call that is still running.

    A video generation takes minutes; ``started`` then a long silence then ``finished`` is
    indistinguishable from a hung turn. This event fills that gap — emitted zero or more times
    between a call's :class:`ToolCallStarted` and its :class:`ToolCallFinished`, carrying the
    **same** :attr:`call_id` so a consumer updates the one skeleton it already rendered rather
    than drawing a new card.

    It is advisory: a consumer that ignores it loses nothing but the live progress text. It is
    never load-bearing for correctness — ordering and the final outcome live on the
    started/finished pair — so a backend that cannot report progress simply emits none, and
    the lifecycle is still complete.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["tool_call_progress"] = "tool_call_progress"
    tool: str
    call_id: str = ""
    """The id of the in-flight call this progress belongs to. Same value as the call's
    :class:`ToolCallStarted`."""
    message: str = ""
    """Human-readable progress note, e.g. ``"queued"`` / ``"rendering 50%"``. Size-bounded and
    display-only; never parsed for control flow."""
    percent: float | None = None
    """Optional 0.0–1.0 completion fraction when the backend can estimate one; ``None`` when it
    cannot. A consumer must tolerate ``None`` rather than assume a number is always present."""


class ToolCallFinished(BaseModel):
    """A tool call has completed, carrying its outcome and correlation id.

    The :class:`~pikachu.core.types.ToolOutcome` is load-bearing: ``INTERRUPTED`` is not
    ``FAILED``, and a consumer showing progress must be able to tell a denied call from a
    failed one from a paid one that may have fired. ``outcome == ToolOutcome.FAILED`` is the
    **failure signal** for a crashed tool: a tool that raised still emits this event (so the
    skeleton resolves to an error state) and the stream still terminates with
    :class:`TurnFinished` — a crashed tool must never strand the stream.

    ``call_id`` matches this call's :class:`ToolCallStarted` (and any
    :class:`ToolCallProgress`), so a consumer reconciles start and finish into one card. Same
    defaulting rule as :class:`ToolCallStarted`: empty on the reconstructed path, the
    provider's ``tool_call_id`` on the native path.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["tool_call_finished"] = "tool_call_finished"
    tool: str
    outcome: ToolOutcome
    call_id: str = ""
    """Stable per-call id, equal to the matching :class:`ToolCallStarted`'s ``call_id``."""


class ArtifactProduced(BaseModel):
    """An artifact was appended to the canvas during the turn.

    Carries the immutable :class:`~pikachu.core.types.Artifact` node itself, so a consumer
    can render a produced image or document the moment it exists rather than waiting for the
    final result.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["artifact_produced"] = "artifact_produced"
    artifact: Artifact


class TurnFinished(BaseModel):
    """The turn is complete. Always the last event, exactly once.

    ★ ``result`` is **the same** :class:`~pikachu.core.types.TurnResult` a non-streaming
    ``run_turn`` returns for this request — including ``timing`` with all four phases and
    the framework-vs-model attribution. A streaming consumer that reads ``result`` loses
    nothing a blocking caller would have had. Dropping the timing split here would be a real
    regression, not a cosmetic one, so the streaming path is required to forward the result
    verbatim.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["turn_finished"] = "turn_finished"
    result: TurnResult


TurnEvent = Annotated[
    Union[
        TurnStarted,
        TextDelta,
        ToolCallStarted,
        ToolCallProgress,
        ToolCallFinished,
        ArtifactProduced,
        TurnFinished,
    ],
    Field(discriminator="kind"),
]
"""The discriminated union of everything a turn can emit.

Discriminated on ``kind`` so a ``match`` over it type-checks exhaustively under
``mypy --strict``. Use it as the element type of the ``AsyncIterator`` a streaming turn
yields."""
