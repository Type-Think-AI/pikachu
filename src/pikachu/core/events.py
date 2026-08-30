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
* A tool call is always a :class:`ToolCallStarted` followed by its
  :class:`ToolCallFinished`, in that order, carrying the tool's
  :class:`~pikachu.core.types.ToolOutcome`.

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
    """A tool call has begun. Always paired with a later :class:`ToolCallFinished`."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["tool_call_started"] = "tool_call_started"
    tool: str
    args: str = ""
    """A rendered, size-bounded view of the arguments — a string, not the live object, so an
    event is cheap to log and carries no reference a consumer could mutate."""


class ToolCallFinished(BaseModel):
    """A tool call has completed, carrying its outcome.

    The :class:`~pikachu.core.types.ToolOutcome` is load-bearing: ``INTERRUPTED`` is not
    ``FAILED``, and a consumer showing progress must be able to tell a denied call from a
    failed one from a paid one that may have fired.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["tool_call_finished"] = "tool_call_finished"
    tool: str
    outcome: ToolOutcome


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
