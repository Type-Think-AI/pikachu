"""Lane 2 — tool-progress streaming events.

Why this file exists
--------------------
A media turn (image, and especially video) takes minutes. PicX's Hermes handler emits a
``tool_started`` event immediately — carrying a STABLE ``tool_call_id`` — so the UI renders a
"generating" skeleton, then reconciles it with the final result so start and finish render as
ONE tool card (see ``api/app/groot/agent_tools.py::_emit`` and its ``tool_started`` payload).
Pikachu streaming emitted only ``TurnStarted`` / ``TextDelta`` / ``TurnFinished``, and its
``ToolCallStarted`` / ``ToolCallFinished`` carried no correlation id — so a consumer could not
tie a start to its finish, and a media turn looked frozen for minutes.

This lane adds:

* a STABLE ``call_id`` on :class:`ToolCallStarted` and :class:`ToolCallFinished` so a consumer
  correlates the two halves of one call (the Pikachu analogue of Hermes' ``tool_call_id``);
* a new :class:`ToolCallProgress` event (started / progress / finished) for the long minutes
  in between, carrying the same ``call_id`` and the tool name;
* the failure signal: a crashed tool emits :class:`ToolCallFinished` with
  ``outcome == ToolOutcome.FAILED`` **and** the stream still terminates with
  :class:`TurnFinished` — a crashed tool must never strand the stream.

What is asserted
----------------
Everything here is driven by deterministic stubs — no model, no network (conftest's autouse
fixture hard-fails a socket). Two stub shapes are used:

* ``ToolLifecycleStreamer`` — a :class:`StreamingBackend` that emits the full lifecycle with
  stable ids, so the ORDER contract, id correlation, progress, and the crashed-tool-still-
  finishes property are pinned without any provider.
* ``FakeChunkStream`` — a stand-in for the pydantic-ai ``run_stream_events`` flat event
  stream, so the real ``PydanticAIBackend.stream_turn`` mapping is exercised end to end
  (text incrementality preserved: >1 ``TextDelta`` when the stub sends >1 chunk; a
  ``FunctionToolResultEvent`` carrying a ``RetryPromptPart`` maps to ``FAILED``) with the
  provider fully stubbed — never a live call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from pikachu.backends.streaming import StreamingBackend, stream_turn
from pikachu.core.events import (
    TextDelta,
    ToolCallFinished,
    ToolCallProgress,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
    TurnStarted,
)
from pikachu.core.types import (
    AgentSpec,
    Run,
    ToolOutcome,
    TurnRequest,
    TurnResult,
)

# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------


@pytest.fixture
def agent() -> AgentSpec:
    return AgentSpec(
        name="animator",
        role="Generate media on request.",
        allowed_tools=("generate_video", "generate_image"),
    )


@pytest.fixture
def run() -> Run:
    return Run(id="run:tools", agent_name="animator", max_iterations=10)


def make_request(
    *,
    agent: AgentSpec,
    effective_tools: tuple[str, ...],
    message: str = "make me a video",
    run_id: str = "run:tools",
) -> TurnRequest:
    return TurnRequest(
        message=message,
        agent=agent,
        effective_tools=effective_tools,
        run_id=run_id,
    )


async def collect(backend: Any, request: TurnRequest) -> list[TurnEvent]:
    return [event async for event in stream_turn(backend, request)]


# --------------------------------------------------------------------------------------
# A StreamingBackend that emits the full tool lifecycle with STABLE ids.
#
# This is the shape a real media backend produces: started -> progress* -> finished, each
# carrying the same call_id so a consumer reconciles them into one card. It also lets us
# script a CRASHING tool (finished with FAILED) and prove the stream still terminates.
# --------------------------------------------------------------------------------------


@dataclass
class _Call:
    call_id: str
    tool: str
    outcome: ToolOutcome
    progress: tuple[str, ...] = ()
    args: str = ""


class ToolLifecycleStreamer:
    """A native ``StreamingBackend`` that scripts tool-lifecycle events deterministically.

    Emits, in order: ``TurnStarted`` -> for each scripted call a ``ToolCallStarted`` then its
    ``ToolCallProgress`` notes then a ``ToolCallFinished`` carrying the scripted outcome ->
    the closing text as one ``TextDelta`` -> ``TurnFinished``. No model, no network.
    """

    def __init__(self, *, calls: tuple[_Call, ...], text: str = "done", run_id: str = "run:tools") -> None:
        self._calls = calls
        self._text = text
        self._run_id = run_id

    async def run_turn(self, request: TurnRequest) -> TurnResult:
        # Present only so the type is a full AgentBackend; the streaming path below never
        # calls it, and no test drives it.
        return TurnResult(text=self._text)

    async def stream_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        yield TurnStarted(agent_name=request.agent.name, streaming=True)
        tool_calls: list[dict[str, Any]] = []
        for call in self._calls:
            yield ToolCallStarted(call_id=call.call_id, tool=call.tool, args=call.args)
            for note in call.progress:
                yield ToolCallProgress(call_id=call.call_id, tool=call.tool, message=note)
            yield ToolCallFinished(call_id=call.call_id, tool=call.tool, outcome=call.outcome)
            tool_calls.append(
                {"tool": call.tool, "outcome": call.outcome.value, "executed": True}
            )
        if self._text:
            yield TextDelta(text=self._text)
        yield TurnFinished(
            result=TurnResult(text=self._text, tool_calls=tuple(tool_calls))
        )


# --------------------------------------------------------------------------------------
# Ordering — TurnStarted first, TurnFinished last exactly once,
# every ToolCallStarted precedes its ToolCallFinished
# --------------------------------------------------------------------------------------


async def test_turn_started_first_turn_finished_last(agent: AgentSpec) -> None:
    backend = ToolLifecycleStreamer(
        calls=(
            _Call("media-aaa", "generate_video", ToolOutcome.SUCCESS, progress=("queued", "50%")),
        )
    )
    req = make_request(agent=agent, effective_tools=("generate_video",))
    events = await collect(backend, req)

    assert isinstance(events[0], TurnStarted)
    assert isinstance(events[-1], TurnFinished)
    assert sum(isinstance(e, TurnStarted) for e in events) == 1
    assert sum(isinstance(e, TurnFinished) for e in events) == 1


async def test_tool_started_precedes_tool_finished(agent: AgentSpec) -> None:
    backend = ToolLifecycleStreamer(
        calls=(_Call("media-bbb", "generate_video", ToolOutcome.SUCCESS),)
    )
    req = make_request(agent=agent, effective_tools=("generate_video",))
    events = await collect(backend, req)

    (started,) = [e for e in events if isinstance(e, ToolCallStarted)]
    (finished,) = [e for e in events if isinstance(e, ToolCallFinished)]
    assert events.index(started) < events.index(finished)
    # And both sit strictly inside the TurnStarted..TurnFinished envelope.
    assert 0 < events.index(started) < len(events) - 1
    assert 0 < events.index(finished) < len(events) - 1


async def test_full_ordering_started_before_finished_before_turn_finished(agent: AgentSpec) -> None:
    """Two calls: each start precedes its own finish, and everything precedes TurnFinished."""
    backend = ToolLifecycleStreamer(
        calls=(
            _Call("media-c1", "generate_image", ToolOutcome.SUCCESS),
            _Call("media-c2", "generate_video", ToolOutcome.SUCCESS),
        )
    )
    req = make_request(agent=agent, effective_tools=("generate_image", "generate_video"))
    events = await collect(backend, req)

    kinds = [e.kind for e in events]
    assert kinds[0] == "turn_started"
    assert kinds[-1] == "turn_finished"
    for cid in ("media-c1", "media-c2"):
        starts = [i for i, e in enumerate(events)
                  if isinstance(e, ToolCallStarted) and e.call_id == cid]
        finishes = [i for i, e in enumerate(events)
                    if isinstance(e, ToolCallFinished) and e.call_id == cid]
        assert len(starts) == 1 and len(finishes) == 1
        assert starts[0] < finishes[0] < len(events) - 1


# --------------------------------------------------------------------------------------
# A stable id ties start to finish (and to progress)
# --------------------------------------------------------------------------------------


async def test_start_and_finish_share_a_stable_call_id(agent: AgentSpec) -> None:
    backend = ToolLifecycleStreamer(
        calls=(_Call("media-stable", "generate_video", ToolOutcome.SUCCESS),)
    )
    req = make_request(agent=agent, effective_tools=("generate_video",))
    events = await collect(backend, req)

    (started,) = [e for e in events if isinstance(e, ToolCallStarted)]
    (finished,) = [e for e in events if isinstance(e, ToolCallFinished)]
    assert started.call_id == finished.call_id == "media-stable"
    assert started.tool == finished.tool == "generate_video"


async def test_progress_events_carry_the_same_call_id(agent: AgentSpec) -> None:
    backend = ToolLifecycleStreamer(
        calls=(
            _Call(
                "media-prog",
                "generate_video",
                ToolOutcome.SUCCESS,
                progress=("queued", "rendering 50%", "encoding"),
            ),
        )
    )
    req = make_request(agent=agent, effective_tools=("generate_video",))
    events = await collect(backend, req)

    progress = [e for e in events if isinstance(e, ToolCallProgress)]
    assert len(progress) == 3
    assert all(p.call_id == "media-prog" and p.tool == "generate_video" for p in progress)
    # Progress sits between the start and the finish of its call.
    (started,) = [e for e in events if isinstance(e, ToolCallStarted)]
    (finished,) = [e for e in events if isinstance(e, ToolCallFinished)]
    for p in progress:
        assert events.index(started) < events.index(p) < events.index(finished)


async def test_two_concurrent_calls_do_not_share_an_id(agent: AgentSpec) -> None:
    backend = ToolLifecycleStreamer(
        calls=(
            _Call("media-x", "generate_image", ToolOutcome.SUCCESS),
            _Call("media-y", "generate_video", ToolOutcome.SUCCESS),
        )
    )
    req = make_request(agent=agent, effective_tools=("generate_image", "generate_video"))
    events = await collect(backend, req)
    ids = [e.call_id for e in events if isinstance(e, (ToolCallStarted, ToolCallFinished))]
    # Each id appears exactly twice (one start, one finish) and the two calls differ.
    assert sorted(ids) == ["media-x", "media-x", "media-y", "media-y"]


# --------------------------------------------------------------------------------------
# A crashed tool emits a failure event AND still emits TurnFinished
# --------------------------------------------------------------------------------------


async def test_failing_tool_emits_failure_and_turn_still_finishes(agent: AgentSpec) -> None:
    backend = ToolLifecycleStreamer(
        calls=(_Call("media-boom", "generate_video", ToolOutcome.FAILED),)
    )
    req = make_request(agent=agent, effective_tools=("generate_video",))
    events = await collect(backend, req)

    (finished,) = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished.outcome == ToolOutcome.FAILED
    # A crashed tool must NOT strand the stream.
    assert isinstance(events[-1], TurnFinished)
    assert sum(isinstance(e, TurnFinished) for e in events) == 1


async def test_failure_is_distinct_from_denied_and_interrupted(agent: AgentSpec) -> None:
    """The outcome is carried, not collapsed: FAILED, DENIED, INTERRUPTED stay themselves."""
    for outcome in (ToolOutcome.FAILED, ToolOutcome.DENIED, ToolOutcome.INTERRUPTED):
        backend = ToolLifecycleStreamer(
            calls=(_Call("media-o", "generate_video", outcome),)
        )
        req = make_request(agent=agent, effective_tools=("generate_video",))
        events = await collect(backend, req)
        (finished,) = [e for e in events if isinstance(e, ToolCallFinished)]
        assert finished.outcome == outcome
        assert isinstance(events[-1], TurnFinished)


# --------------------------------------------------------------------------------------
# The PydanticAIBackend.stream_turn mapping — driven by a stub event stream,
# never a live call. Preserves TextDelta incrementality.
# --------------------------------------------------------------------------------------


# Minimal stand-ins for the pydantic-ai flat event stream parts. Only the attributes
# stream_turn reads are present; anything else the mapping must tolerate as absent.


@dataclass
class _TextPart:
    content: str
    part_kind: str = "text"


@dataclass
class _TextDeltaPart:
    content_delta: str


@dataclass
class _ToolCallPart:
    tool_name: str
    tool_call_id: str
    args: Any = ""
    part_kind: str = "tool-call"


@dataclass
class _ToolReturnPart:
    tool_call_id: str
    tool_name: str = ""


# Named to match the real pydantic-ai class: _outcome_of_result_part keys on the class
# NAME ("RetryPromptPart"), which is how the live stream signals a failed function tool.
@dataclass
class RetryPromptPart:  # noqa: N801 - deliberately mirrors the pydantic-ai class name
    tool_call_id: str
    tool_name: str = ""


@dataclass
class _PartStartEvent:
    part: Any
    index: int = 0
    event_kind: str = "part_start"


@dataclass
class _PartDeltaEvent:
    delta: Any
    index: int = 0
    event_kind: str = "part_delta"


@dataclass
class _FunctionToolCallEvent:
    part: Any
    event_kind: str = "function_tool_call"

    @property
    def tool_call_id(self) -> str:
        return self.part.tool_call_id


@dataclass
class _FunctionToolResultEvent:
    part: Any
    event_kind: str = "function_tool_result"

    @property
    def tool_call_id(self) -> str:
        return self.part.tool_call_id


@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class _FakeAgentRunResult:
    def __init__(self, output: str, messages: list[Any]) -> None:
        self.output = output
        self._messages = messages

    def usage(self) -> _FakeUsage:
        return _FakeUsage(input_tokens=7, output_tokens=3)

    def all_messages(self) -> list[Any]:
        return self._messages


@dataclass
class _AgentRunResultEvent:
    result: _FakeAgentRunResult
    event_kind: str = "agent_run_result"


class _FakeEventsHandle:
    """Async context manager mimicking ``agent.run_stream_events(...)``.

    Yields a scripted flat event stream (text deltas + tool events + the terminal
    ``AgentRunResultEvent``). Entering/exiting without iterating never "calls a model" —
    there is no model.
    """

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aenter__(self) -> "_FakeEventsHandle":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def __aiter__(self) -> AsyncIterator[Any]:
        for ev in self._events:
            yield ev


def _make_backend_with_scripted_stream(events: list[Any]) -> Any:
    """A PydanticAIBackend whose agent's run_stream_events yields the scripted events.

    The provider/model are never constructed for real work: we build the backend with a
    dummy key (constructor requires a non-empty key) and monkeypatch ``_build_agent`` to
    return a stub agent. No network, no model — conftest would hard-fail a socket anyway.
    """
    from pikachu.backends.pydantic_ai import PydanticAIBackend

    class _StubAgent:
        def run_stream_events(
            self, message: str, *, message_history: object = None
        ) -> _FakeEventsHandle:
            return _FakeEventsHandle(events)

    backend = PydanticAIBackend(api_key="dummy-not-used")
    backend._build_agent = lambda request: _StubAgent()  # type: ignore[assignment,method-assign]
    return backend


async def test_pydantic_ai_stream_preserves_text_incrementality(agent: AgentSpec) -> None:
    """More than one chunk in => more than one TextDelta out (real incrementality)."""
    events = [
        _PartStartEvent(part=_TextPart(content="Here ")),
        _PartDeltaEvent(delta=_TextDeltaPart(content_delta="is ")),
        _PartDeltaEvent(delta=_TextDeltaPart(content_delta="your video.")),
        _AgentRunResultEvent(result=_FakeAgentRunResult("Here is your video.", [])),
    ]
    backend = _make_backend_with_scripted_stream(events)
    req = make_request(agent=agent, effective_tools=())
    out = await collect(backend, req)

    deltas = [e for e in out if isinstance(e, TextDelta)]
    assert len(deltas) > 1, "collapsed to a single blob — incrementality lost"
    assert "".join(d.text for d in deltas) == "Here is your video."
    assert isinstance(out[0], TurnStarted) and out[0].streaming is True
    assert isinstance(out[-1], TurnFinished)


async def test_pydantic_ai_stream_emits_tool_lifecycle_with_stable_id(agent: AgentSpec) -> None:
    """A tool call in the flat stream becomes started/finished sharing the provider id."""
    events = [
        _FunctionToolCallEvent(
            part=_ToolCallPart(tool_name="generate_video", tool_call_id="call-42", args={"prompt": "a cat"})
        ),
        _FunctionToolResultEvent(
            part=_ToolReturnPart(tool_call_id="call-42", tool_name="generate_video")
        ),
        _PartStartEvent(part=_TextPart(content="Done.")),
        _AgentRunResultEvent(result=_FakeAgentRunResult("Done.", [])),
    ]
    backend = _make_backend_with_scripted_stream(events)
    req = make_request(agent=agent, effective_tools=("generate_video",))
    out = await collect(backend, req)

    (started,) = [e for e in out if isinstance(e, ToolCallStarted)]
    (finished,) = [e for e in out if isinstance(e, ToolCallFinished)]
    assert started.call_id == finished.call_id == "call-42"
    assert started.tool == finished.tool == "generate_video"
    assert finished.outcome == ToolOutcome.SUCCESS
    assert out.index(started) < out.index(finished)
    assert isinstance(out[0], TurnStarted)
    assert isinstance(out[-1], TurnFinished)


async def test_pydantic_ai_stream_maps_retry_prompt_to_failed(agent: AgentSpec) -> None:
    """A tool result delivered as a RetryPromptPart is a FAILED call, and the turn finishes."""
    events = [
        _FunctionToolCallEvent(
            part=_ToolCallPart(tool_name="generate_video", tool_call_id="call-err")
        ),
        _FunctionToolResultEvent(
            part=RetryPromptPart(tool_call_id="call-err", tool_name="generate_video")
        ),
        _AgentRunResultEvent(result=_FakeAgentRunResult("Sorry, that failed.", [])),
    ]
    backend = _make_backend_with_scripted_stream(events)
    req = make_request(agent=agent, effective_tools=("generate_video",))
    out = await collect(backend, req)

    (finished,) = [e for e in out if isinstance(e, ToolCallFinished)]
    assert finished.call_id == "call-err"
    assert finished.outcome == ToolOutcome.FAILED
    assert isinstance(out[-1], TurnFinished)


async def test_pydantic_ai_stream_falls_back_and_still_finishes_on_stream_error(agent: AgentSpec) -> None:
    """If the event stream raises mid-flight, stream_turn falls back and still emits TurnFinished."""
    from pikachu.backends.pydantic_ai import PydanticAIBackend

    class _BoomHandle:
        async def __aenter__(self) -> "_BoomHandle":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def __aiter__(self) -> AsyncIterator[Any]:
            yield _PartStartEvent(part=_TextPart(content="partial"))
            raise RuntimeError("provider dropped the connection")

    class _StubAgent:
        def run_stream_events(
            self, message: str, *, message_history: object = None
        ) -> _BoomHandle:
            return _BoomHandle()

    async def _fallback_run_turn(request: TurnRequest) -> TurnResult:
        return TurnResult(text="recovered via run_turn")

    backend = PydanticAIBackend(api_key="dummy-not-used")
    backend._build_agent = lambda request: _StubAgent()  # type: ignore[assignment,method-assign]
    backend.run_turn = _fallback_run_turn  # type: ignore[assignment,method-assign]

    req = make_request(agent=agent, effective_tools=())
    out = await collect(backend, req)

    assert isinstance(out[0], TurnStarted)
    assert isinstance(out[-1], TurnFinished)
    assert out[-1].result.text == "recovered via run_turn"


# --------------------------------------------------------------------------------------
# The new events are structurally sound: frozen, carry their fields, discriminated by kind
# --------------------------------------------------------------------------------------


def test_new_events_are_frozen_and_carry_fields() -> None:
    started = ToolCallStarted(call_id="c1", tool="generate_video", args="{...}")
    progress = ToolCallProgress(call_id="c1", tool="generate_video", message="50%")
    finished = ToolCallFinished(call_id="c1", tool="generate_video", outcome=ToolOutcome.SUCCESS)

    assert started.kind == "tool_call_started"
    assert progress.kind == "tool_call_progress"
    assert finished.kind == "tool_call_finished"
    assert started.call_id == progress.call_id == finished.call_id == "c1"

    # Frozen: a field cannot be reassigned.
    with pytest.raises((TypeError, ValueError)):
        started.call_id = "mutated"  # type: ignore[misc]


def test_call_id_defaults_empty_for_reconstructed_streams() -> None:
    """The degraded/reconstructed path has no provider id, so call_id defaults to empty —
    that is what keeps the existing streaming reconstruction valid without a correlation id."""
    started = ToolCallStarted(tool="generate_video")
    finished = ToolCallFinished(tool="generate_video", outcome=ToolOutcome.SUCCESS)
    assert started.call_id == ""
    assert finished.call_id == ""


def test_stream_turn_returns_streaming_backend_for_lifecycle_streamer(agent: AgentSpec) -> None:
    """The lifecycle streamer is recognised as a native StreamingBackend (delegated to)."""
    backend = ToolLifecycleStreamer(calls=())
    assert isinstance(backend, StreamingBackend)
