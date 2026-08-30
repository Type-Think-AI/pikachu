"""Lane U tests — the F3 event stream.

Driven entirely by ``FakeBackend``: no network (conftest's autouse fixture enforces it), no
model, deterministic. The suite pins the event-ordering contract, proves the streamed result
is byte-identical to the blocking result (timing included), proves the degraded path is
visibly degraded, and proves a cancelled stream leaves nothing pending — which matters
because ``filterwarnings=error`` in ``pyproject.toml`` turns a leaked async resource into a
failure attributed to some unrelated test.

The final test is a static-typing assertion: a ``match`` over the event union with **no**
default branch must type-check under ``mypy --strict``. It runs at import/call time as a
plain function too, so a broken discriminant fails the suite even before mypy is invoked.
"""

from __future__ import annotations

import asyncio
import gc
import warnings
from collections.abc import AsyncIterator

import pytest

from pikachu.backends import FakeBackend, ScriptedToolCall, ScriptedTurn
from pikachu.backends.streaming import StreamingBackend, stream_turn
from pikachu.core.events import (
    ArtifactProduced,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
    TurnStarted,
)
from pikachu.core.types import (
    AgentSpec,
    Artifact,
    ArtifactKind,
    Run,
    ToolOutcome,
    TurnRequest,
    TurnResult,
)

# --------------------------------------------------------------------------------------
# Local fixtures / helpers (mirrors test_backends.py; tests/fakes.py is a later lane)
# --------------------------------------------------------------------------------------


@pytest.fixture
def agent() -> AgentSpec:
    return AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        allowed_tools=("generate_image", "read_canvas"),
    )


@pytest.fixture
def run() -> Run:
    return Run(id="run:test", agent_name="colourist", max_iterations=10)


def make_request(
    *,
    agent: AgentSpec,
    effective_tools: tuple[str, ...],
    message: str = "grade this frame",
    run_id: str = "run:test",
) -> TurnRequest:
    return TurnRequest(
        message=message,
        agent=agent,
        effective_tools=effective_tools,
        run_id=run_id,
    )


def image_artifact(artifact_id: str) -> Artifact:
    return Artifact(
        id=artifact_id, kind=ArtifactKind.IMAGE, payload_ref=f"ref://{artifact_id}"
    )


async def collect(backend: FakeBackend, request: TurnRequest) -> list[TurnEvent]:
    return [event async for event in stream_turn(backend, request)]


# --------------------------------------------------------------------------------------
# Ordering — TurnStarted first, TurnFinished last, exactly once
# --------------------------------------------------------------------------------------


async def test_turn_started_first_turn_finished_last(agent: AgentSpec, run: Run) -> None:
    backend = FakeBackend(
        script=[
            ScriptedTurn(text="thinking"),
            ScriptedTurn(
                text="working", tool_calls=(ScriptedToolCall("read_canvas"),)
            ),
            ScriptedTurn(text="done", artifacts=(image_artifact("art-1"),)),
        ],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=("read_canvas", "generate_image"))
    events = await collect(backend, req)

    assert isinstance(events[0], TurnStarted)
    assert isinstance(events[-1], TurnFinished)
    assert sum(isinstance(e, TurnStarted) for e in events) == 1
    assert sum(isinstance(e, TurnFinished) for e in events) == 1


async def test_event_order_is_deterministic(agent: AgentSpec) -> None:
    """Same script in, same event-kind sequence out, every time."""

    def build() -> FakeBackend:
        return FakeBackend(
            script=[
                ScriptedTurn(
                    text="calling", tool_calls=(ScriptedToolCall("generate_image"),)
                ),
                ScriptedTurn(text="produced", artifacts=(image_artifact("art-2"),)),
            ],
            run=Run(id="run:det", agent_name="colourist", max_iterations=10),
        )

    req = make_request(agent=agent, effective_tools=("generate_image",), run_id="run:det")
    first = [e.kind for e in await collect(build(), req)]
    second = [e.kind for e in await collect(build(), req)]
    assert first == second


# --------------------------------------------------------------------------------------
# A tool call is started-then-finished, carrying its ToolOutcome
# --------------------------------------------------------------------------------------


async def test_tool_call_appears_as_started_then_finished(agent: AgentSpec, run: Run) -> None:
    backend = FakeBackend(
        script=[
            ScriptedTurn(
                tool_calls=(
                    ScriptedToolCall("read_canvas", outcome=ToolOutcome.SUCCESS),
                )
            )
        ],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=("read_canvas",))
    events = await collect(backend, req)

    started = [e for e in events if isinstance(e, ToolCallStarted)]
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert len(started) == 1 and len(finished) == 1
    assert started[0].tool == "read_canvas"
    assert finished[0].tool == "read_canvas"
    assert finished[0].outcome == ToolOutcome.SUCCESS

    # started strictly precedes its finished
    assert events.index(started[0]) < events.index(finished[0])


async def test_tool_outcome_is_carried_not_collapsed(agent: AgentSpec, run: Run) -> None:
    """INTERRUPTED must survive to the finished event — it is not FAILED."""
    backend = FakeBackend(
        script=[
            ScriptedTurn(
                tool_calls=(
                    ScriptedToolCall("read_canvas", outcome=ToolOutcome.INTERRUPTED),
                )
            )
        ],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=("read_canvas",))
    events = await collect(backend, req)
    (finished,) = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished.outcome == ToolOutcome.INTERRUPTED


async def test_artifact_produced_carries_the_artifact(agent: AgentSpec, run: Run) -> None:
    backend = FakeBackend(
        script=[ScriptedTurn(text="made it", artifacts=(image_artifact("art-7"),))],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=())
    events = await collect(backend, req)
    produced = [e for e in events if isinstance(e, ArtifactProduced)]
    assert len(produced) == 1
    assert produced[0].artifact.id == "art-7"
    assert produced[0].artifact.kind == ArtifactKind.IMAGE


# --------------------------------------------------------------------------------------
# ★ TurnFinished.result equals the non-streaming call, timing included
# --------------------------------------------------------------------------------------


async def test_finished_result_equals_non_streaming_call(agent: AgentSpec) -> None:
    """The streamed terminal result is byte-identical to the blocking result.

    Two independent backends with the same script are built so neither mutates state the
    other reads; the comparison is over ``model_dump_json`` so it is byte-level, and it
    includes ``timing`` — dropping the framework-vs-model split would show up here.
    """

    def build() -> FakeBackend:
        return FakeBackend(
            script=[
                ScriptedTurn(text="a", input_tokens=100, cache_read_tokens=20),
                ScriptedTurn(
                    text="b",
                    tool_calls=(ScriptedToolCall("read_canvas"),),
                    output_tokens=4,
                ),
            ],
            run=Run(id="run:parity", agent_name="colourist", max_iterations=10),
        )

    req = make_request(agent=agent, effective_tools=("read_canvas",), run_id="run:parity")

    blocking: TurnResult = await build().run_turn(req)

    events = await collect(build(), req)
    (finished,) = [e for e in events if isinstance(e, TurnFinished)]

    assert finished.result.model_dump_json() == blocking.model_dump_json()
    # Explicit on the load-bearing part: the timing object survives verbatim.
    assert finished.result.timing == blocking.timing


async def test_finished_text_matches_result_text(agent: AgentSpec, run: Run) -> None:
    backend = FakeBackend(
        script=[ScriptedTurn(text="hello"), ScriptedTurn(text="world")],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=())
    events = await collect(backend, req)
    (finished,) = [e for e in events if isinstance(e, TurnFinished)]
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert finished.result.text == "hello\nworld"
    assert "".join(d.text for d in deltas) == finished.result.text


# --------------------------------------------------------------------------------------
# The degraded path is identifiable as degraded
# --------------------------------------------------------------------------------------


async def test_degraded_path_is_flagged_not_faked(agent: AgentSpec, run: Run) -> None:
    """FakeBackend does not implement StreamingBackend, so the stream is reconstructed.

    The whole text arrives as ONE delta and TurnStarted.streaming is False — the sequence is
    coherent and honestly labelled, not dressed up as live streaming.
    """
    assert not isinstance(FakeBackend(), StreamingBackend)

    backend = FakeBackend(
        script=[ScriptedTurn(text="one"), ScriptedTurn(text="two")],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=())
    events = await collect(backend, req)

    started = events[0]
    assert isinstance(started, TurnStarted)
    assert started.streaming is False
    # Degraded => text is not chunked: exactly one TextDelta carrying the whole text.
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert len(deltas) == 1
    assert deltas[0].text == "one\ntwo"


async def test_native_streaming_backend_is_delegated_to(agent: AgentSpec, run: Run) -> None:
    """A backend that implements StreamingBackend gets to stream its own events.

    Proves the opt-in hook works and that its ordering contract is honoured — the same
    guarantees, produced live rather than reconstructed.
    """

    class NativeStreamer(FakeBackend):
        async def stream_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
            result = await self.run_turn(request)
            yield TurnStarted(agent_name=request.agent.name, streaming=True)
            for chunk in result.text.split():
                yield TextDelta(text=chunk)
            yield TurnFinished(result=result)

    backend = NativeStreamer(script=[ScriptedTurn(text="live here")], run=run)
    assert isinstance(backend, StreamingBackend)

    req = make_request(agent=agent, effective_tools=())
    events = await collect(backend, req)

    started = events[0]
    assert isinstance(started, TurnStarted) and started.streaming is True
    assert isinstance(events[-1], TurnFinished)
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert [d.text for d in deltas] == ["live", "here"]  # streamed, chunked


# --------------------------------------------------------------------------------------
# Cancellation leaves nothing pending — no warnings, no leaked tasks
# --------------------------------------------------------------------------------------


async def test_cancelling_mid_stream_leaves_nothing_pending(
    agent: AgentSpec, run: Run
) -> None:
    """Abandon the iterator after the first event and prove no resource leaks.

    Under filterwarnings=error a leaked async generator surfaces as a ResourceWarning at GC,
    attributed to an unrelated test. We assert zero warnings AND no extra pending tasks.
    """
    before = {t for t in asyncio.all_tasks()}

    backend = FakeBackend(
        script=[
            ScriptedTurn(text="1", tool_calls=(ScriptedToolCall("read_canvas"),)),
            ScriptedTurn(text="2"),
        ],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=("read_canvas",))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        gen = stream_turn(backend, req)
        first = await gen.__anext__()
        assert isinstance(first, TurnStarted)
        # Close the generator early — this is what a cancelled consumer does.
        await gen.aclose()
        # Force GC so any unclosed resource would raise here, inside the error filter.
        gc.collect()

    after = {t for t in asyncio.all_tasks()}
    assert after <= before, f"leaked tasks: {after - before}"


async def test_native_child_iterator_is_closed_on_cancel(agent: AgentSpec, run: Run) -> None:
    """Cancelling a delegated native stream closes the backend's child iterator.

    A native backend that leaves an async resource open on early exit is the exact
    teardown-failure trap; stream_turn must aclose the child on GeneratorExit.
    """
    closed = {"value": False}

    class NativeStreamer(FakeBackend):
        async def stream_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
            result = await self.run_turn(request)
            try:
                yield TurnStarted(agent_name=request.agent.name, streaming=True)
                yield TextDelta(text="a")
                yield TextDelta(text="b")
                yield TurnFinished(result=result)
            finally:
                closed["value"] = True

    backend = NativeStreamer(script=[ScriptedTurn(text="a b")], run=run)
    req = make_request(agent=agent, effective_tools=())

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        gen = stream_turn(backend, req)
        await gen.__anext__()  # TurnStarted
        await gen.aclose()  # cancel before the stream is drained
        gc.collect()

    assert closed["value"] is True


# --------------------------------------------------------------------------------------
# mypy exhaustiveness — a match with no default branch type-checks
# --------------------------------------------------------------------------------------


def _render(event: TurnEvent) -> str:
    """Exhaustive match over the event union with NO default branch.

    Under ``mypy --strict`` the ``assert_never`` in an unreachable ``case _`` is what proves
    the match is total: add an event kind to the union without a branch here and mypy fails.
    At runtime this doubles as a smoke test that every ``kind`` discriminant is distinct and
    reachable.
    """
    from typing import assert_never

    match event:
        case TurnStarted():
            return "started"
        case TextDelta():
            return "delta"
        case ToolCallStarted():
            return "tool_started"
        case ToolCallFinished():
            return "tool_finished"
        case ArtifactProduced():
            return "artifact"
        case TurnFinished():
            return "finished"
        case _:
            assert_never(event)


def test_match_over_event_union_is_exhaustive() -> None:
    result = TurnResult(text="x")
    samples: list[TurnEvent] = [
        TurnStarted(agent_name="a"),
        TextDelta(text="x"),
        ToolCallStarted(tool="read_canvas"),
        ToolCallFinished(tool="read_canvas", outcome=ToolOutcome.SUCCESS),
        ArtifactProduced(artifact=Artifact(id="i", kind=ArtifactKind.TEXT, payload_ref="r")),
        TurnFinished(result=result),
    ]
    assert [_render(e) for e in samples] == [
        "started",
        "delta",
        "tool_started",
        "tool_finished",
        "artifact",
        "finished",
    ]
