"""``stream_turn`` — expose a turn as an ordered event stream.

A caller of ``backend.run_turn`` gets a completed :class:`~pikachu.core.types.TurnResult`
and can show no progress at all. This module is the streaming face of the same turn: an
``AsyncIterator`` of :mod:`pikachu.core.events` that a chat product renders as it goes.

Why it is shaped the way it is
------------------------------

The framework seam (:class:`~pikachu.backends.base.BaseBackend`,
:class:`~pikachu.core.protocols.AgentBackend`) is deliberately **one method** —
``run_turn(request) -> TurnResult``. There is no streaming method on the seam, and adding
one is out of this lane's ownership. The deltas that *do* flow live inside
``backends/pydantic_ai.py::_call`` are consumed there purely to measure time-to-first-token
and then discarded; they never cross the ``run_turn`` boundary, so an arbitrary
``AgentBackend`` cannot hand them to us.

So ``stream_turn`` does the honest thing for the seam it actually has:

* If the backend **opts in** to native streaming by implementing :class:`StreamingBackend`
  (a ``stream_turn`` coroutine returning its own event iterator), we delegate to it and
  forward its events. No backend implements this today; the hook exists so a future
  ``PydanticAIBackend.stream_turn`` can surface its already-flowing deltas without this
  module changing.
* Otherwise we **degrade visibly**: run the turn to completion via ``run_turn``, then
  reconstruct a coherent event sequence from the authoritative result — a single
  :class:`~pikachu.core.events.TextDelta` carrying the whole text, one
  started/finished pair per recorded tool call, one
  :class:`~pikachu.core.events.ArtifactProduced` per artifact. The degradation is announced
  on :attr:`~pikachu.core.events.TurnStarted.streaming` (``False``), never hidden behind a
  sequence that merely *looks* streamed.

Either way the terminal :class:`~pikachu.core.events.TurnFinished` carries **the same**
``TurnResult`` a blocking caller would have received — timing phases and framework-vs-model
attribution included. Streaming is a different view of the turn, never a lossier one.

Cancellation
------------

``stream_turn`` spawns **no background task** and holds no resource of its own on the
degraded path, so abandoning the iterator leaks nothing. When delegating to a native
streaming backend, the child iterator is closed deterministically on ``GeneratorExit`` (and
on any error) via ``aclose()`` — an unclosed async resource has already surfaced on this
project as a ``ResourceWarning`` reported against an unrelated test under
``filterwarnings=error``, so closing is not optional.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Protocol, runtime_checkable

from pikachu.core.events import (
    ArtifactProduced,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
    TurnStarted,
)
from pikachu.core.protocols import AgentBackend
from pikachu.core.types import ToolOutcome, TurnRequest, TurnResult

__all__ = ["StreamingBackend", "stream_turn"]


@runtime_checkable
class StreamingBackend(Protocol):
    """A backend that can stream a turn natively.

    Entirely optional and structural: a backend earns the native path simply by having a
    ``stream_turn`` method with this shape. It still MUST satisfy
    :class:`~pikachu.core.protocols.AgentBackend` (``run_turn``) so a non-streaming caller
    keeps working — streaming is an addition to the seam, never a replacement.

    The returned iterator MUST obey the same ordering contract this module guarantees:
    :class:`~pikachu.core.events.TurnStarted` first,
    :class:`~pikachu.core.events.TurnFinished` last and exactly once, and its
    ``result`` equal to what ``run_turn`` would return for the same request.
    """

    def stream_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]: ...


def _outcome_of(record: object) -> ToolOutcome:
    """Read a tool call's outcome from a ``TurnResult.tool_calls`` record.

    Records are plain dicts and are not uniform across backends: ``FakeBackend`` writes an
    ``"outcome"`` string, while ``PydanticAIBackend`` records only ``tool``/``args`` (the
    provider does not surface a per-call outcome). A missing or unrecognised value means the
    call was observed to complete without an error signal, which maps to ``SUCCESS`` — the
    same assumption the non-streaming result already encodes by omitting it.
    """
    if isinstance(record, dict):
        raw = record.get("outcome")
        if isinstance(raw, str):
            try:
                return ToolOutcome(raw)
            except ValueError:
                return ToolOutcome.SUCCESS
    return ToolOutcome.SUCCESS


def _tool_name_of(record: object) -> str:
    if isinstance(record, dict):
        name = record.get("tool")
        if isinstance(name, str):
            return name
    return "?"


def _tool_args_of(record: object) -> str:
    if isinstance(record, dict):
        args = record.get("args")
        if args:
            return str(args)[:500]
    return ""


async def _reconstruct(request: TurnRequest, result: TurnResult) -> AsyncIterator[TurnEvent]:
    """Rebuild a coherent event sequence from a completed, authoritative result.

    This is the degraded path: the turn already ran to completion, so nothing here streams
    in real time. It reproduces the *structure* of the turn faithfully — the tool calls in
    the order the backend recorded them, each artifact it produced, and the full text —
    then closes with the real result. It never invents ordering the result does not encode.
    """
    yield TurnStarted(agent_name=request.agent.name, streaming=False)

    for record in result.tool_calls:
        name = _tool_name_of(record)
        yield ToolCallStarted(tool=name, args=_tool_args_of(record))
        yield ToolCallFinished(tool=name, outcome=_outcome_of(record))

    for artifact in result.artifacts:
        yield ArtifactProduced(artifact=artifact)

    if result.text:
        yield TextDelta(text=result.text)

    yield TurnFinished(result=result)


async def stream_turn(
    backend: AgentBackend, request: TurnRequest
) -> AsyncGenerator[TurnEvent, None]:
    """Run one turn and yield it as an ordered :class:`~pikachu.core.events.TurnEvent` stream.

    Works with **any** :class:`~pikachu.core.protocols.AgentBackend`, including
    ``FakeBackend``, so it is fully testable offline. A backend that also implements
    :class:`StreamingBackend` is delegated to for genuine live deltas; every other backend
    is driven through ``run_turn`` and reconstructed (see module docstring).

    The final event is always :class:`~pikachu.core.events.TurnFinished` carrying the same
    ``TurnResult`` the non-streaming call returns, timing included.
    """
    if isinstance(backend, StreamingBackend):
        # Native path: forward the backend's own events, closing its iterator
        # deterministically so a cancelled consumer never leaks it.
        native = backend.stream_turn(request)
        try:
            async for event in native:
                yield event
        finally:
            aclose = getattr(native, "aclose", None)
            if aclose is not None:
                await aclose()
        return

    # Degraded path: no live deltas are reachable through the one-method seam, so run the
    # turn to completion and reconstruct. No task is spawned and no resource is held here,
    # so abandoning this iterator leaks nothing.
    result = await backend.run_turn(request)
    async for event in _reconstruct(request, result):
        yield event
