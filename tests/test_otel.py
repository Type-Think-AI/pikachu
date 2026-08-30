"""Tests for ``pikachu.telemetry.otel`` — GenAI spans that must never break a turn.

Two halves, matching the lane spec:

* **Library ABSENT** (the deliverable that proves telemetry cannot break a turn): the
  ``opentelemetry`` import is patched to raise ``ImportError`` and we assert the module still
  imports, the no-op tracer swallows everything, and a full turn completes normally. This half
  needs no third-party package and always runs.

* **Library PRESENT**: an in-memory span exporter captures the emitted spans and we assert the
  exact ``gen_ai.*`` attribute names and values, the child tool spans, and that the
  ``TurnTiming`` phases reached the span. This half needs ``opentelemetry-sdk`` (the
  ``InMemorySpanExporter`` lives there); it ``importorskip``s when the SDK is not installed, so
  it goes green the moment the integrator applies ``HANDOFF-V.md``.

No network anywhere (the autouse ``_no_network`` fixture in ``conftest.py`` enforces it).
"""

from __future__ import annotations

import builtins
import importlib
from typing import TYPE_CHECKING, Any

import pytest

from pikachu.core.types import ToolOutcome, TurnResult, TurnTiming
from pikachu.config import DEFAULT_MODEL
from pikachu.telemetry import otel

if TYPE_CHECKING:
    from collections.abc import Iterator


# --------------------------------------------------------------------------------------
# Fixtures — a representative TurnResult with a real phase split.
# --------------------------------------------------------------------------------------


@pytest.fixture
def timing() -> TurnTiming:
    """A turn with a real framework-vs-model split so annotations are non-trivial."""
    return TurnTiming(
        setup_ms=2,
        wait_ms=2800,
        stream_ms=90,
        finalize_ms=1,
        total_ms=2893,
        streaming_measured=True,
    )


@pytest.fixture
def result(timing: TurnTiming) -> TurnResult:
    return TurnResult(
        text="a graded still",
        input_tokens=1800,
        output_tokens=120,
        cache_read_tokens=0,
        iterations=1,
        latency_ms=2893,
        timing=timing,
    )


# --------------------------------------------------------------------------------------
# LIBRARY ABSENT — the deliverable. Telemetry cannot break a turn.
# --------------------------------------------------------------------------------------


@pytest.fixture
def otel_absent(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make ``import opentelemetry...`` raise ImportError, simulating the extra not installed.

    Patches ``builtins.__import__`` so any attempt to import an ``opentelemetry`` module fails,
    and evicts already-imported ``opentelemetry`` modules from ``sys.modules`` so the lazy import
    inside ``otel`` actually re-runs and hits the patched importer.
    """
    import sys

    for name in list(sys.modules):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(f"simulated: {name} not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    yield


def test_module_imports_without_opentelemetry(otel_absent: None) -> None:
    """Re-importing the module with opentelemetry absent must still succeed."""
    reimported = importlib.reload(otel)
    assert reimported is not None
    # Clean up: reload again once the block is lifted so later tests see a normal module.


def test_availability_is_false_when_absent(otel_absent: None) -> None:
    assert otel.opentelemetry_available() is False


def test_tracer_is_noop_when_absent(otel_absent: None) -> None:
    tracer = otel.TurnTracer(model=DEFAULT_MODEL)
    assert tracer.is_noop is True


def test_turn_completes_normally_when_absent(
    otel_absent: None, result: TurnResult
) -> None:
    """The whole point: a turn wrapped in telemetry runs to completion with the library gone."""
    tracer = otel.TurnTracer(model=DEFAULT_MODEL)
    turn_ran = False
    tool_ran = False

    with tracer.turn_span() as turn:
        with tracer.tool_span("web_search", parent=turn, outcome=ToolOutcome.SUCCESS):
            tool_ran = True
        tracer.finish_turn(turn, result)
        turn_ran = True

    assert turn_ran is True
    assert tool_ran is True


def test_noop_span_does_not_suppress_exceptions(otel_absent: None) -> None:
    """A no-op span must not swallow the caller's real error — telemetry observes, never eats."""
    tracer = otel.TurnTracer()
    with pytest.raises(ValueError, match="real turn error"):
        with tracer.turn_span():
            raise ValueError("real turn error")


def test_noop_set_attribute_and_end_are_silent(otel_absent: None) -> None:
    span = otel._NoopSpan()
    assert span.set_attribute("gen_ai.request.model", "x") is None
    assert span.end() is None


# --------------------------------------------------------------------------------------
# LIBRARY PRESENT — exact gen_ai.* attributes, child tool spans, timing phases.
# Needs opentelemetry-sdk (InMemorySpanExporter). Skips until HANDOFF-V is applied.
# --------------------------------------------------------------------------------------


def _make_recording_provider() -> tuple[Any, Any]:
    """Build a real TracerProvider with an in-memory exporter, and set it as the global.

    Returns ``(provider, exporter)``. Skips the test if ``opentelemetry-sdk`` is absent.
    """
    pytest.importorskip("opentelemetry.sdk.trace", reason="opentelemetry-sdk not installed")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # ONE provider for the whole process, cached in module globals.
    #
    # OpenTelemetry honours `set_tracer_provider()` exactly ONCE per process and silently
    # ignores every later call — so a per-test provider *looks* installed and is not, and every
    # test after the first records into the first test's exporter. Shutting a provider down in
    # teardown made it worse: later tests then held a dead provider and saw no spans at all.
    #
    # This only surfaced once opentelemetry-sdk was actually installed; while it was absent all
    # of these tests were skipped, so they had never really run. Install once, and clear the
    # exporter between tests instead of reinstalling.
    global _PROVIDER, _EXPORTER
    if _PROVIDER is None:
        _EXPORTER = InMemorySpanExporter()
        _PROVIDER = TracerProvider()
        _PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(_PROVIDER)
    assert _EXPORTER is not None
    return _PROVIDER, _EXPORTER


_PROVIDER: Any = None
_EXPORTER: Any = None


@pytest.fixture
def recording_exporter() -> Iterator[Any]:
    _, exporter = _make_recording_provider()
    exporter.clear()  # per-test isolation without touching the global provider
    yield exporter


def test_present_turn_span_has_required_gen_ai_attributes(
    recording_exporter: Any, result: TurnResult
) -> None:
    tracer = otel.TurnTracer(model=DEFAULT_MODEL)
    assert tracer.is_noop is False

    with tracer.turn_span() as turn:
        tracer.finish_turn(turn, result)

    spans = recording_exporter.get_finished_spans()
    turn_spans = [s for s in spans if s.attributes.get(otel.GEN_AI_OPERATION_NAME) == "chat"]
    assert len(turn_spans) == 1
    attrs = turn_spans[0].attributes

    assert attrs[otel.GEN_AI_OPERATION_NAME] == "chat"
    assert attrs[otel.GEN_AI_REQUEST_MODEL] == DEFAULT_MODEL
    assert attrs[otel.GEN_AI_USAGE_INPUT_TOKENS] == 1800
    assert attrs[otel.GEN_AI_USAGE_OUTPUT_TOKENS] == 120


def test_present_span_name_follows_operation_model_convention(
    recording_exporter: Any, result: TurnResult
) -> None:
    tracer = otel.TurnTracer(model=DEFAULT_MODEL)
    with tracer.turn_span() as turn:
        tracer.finish_turn(turn, result)
    spans = recording_exporter.get_finished_spans()
    assert any(s.name == f"chat {DEFAULT_MODEL}" for s in spans)


def test_present_child_tool_span_carries_name_and_outcome(
    recording_exporter: Any,
) -> None:
    tracer = otel.TurnTracer(model="m")
    with tracer.turn_span() as turn:
        with tracer.tool_span("web_search", parent=turn, outcome=ToolOutcome.DENIED):
            pass

    spans = recording_exporter.get_finished_spans()
    tool_spans = [
        s for s in spans if s.attributes.get(otel.GEN_AI_OPERATION_NAME) == "execute_tool"
    ]
    assert len(tool_spans) == 1
    attrs = tool_spans[0].attributes
    assert attrs[otel.GEN_AI_TOOL_NAME] == "web_search"
    assert attrs[otel.GEN_AI_TOOL_CALL_OUTCOME] == "denied"
    assert tool_spans[0].name == "execute_tool web_search"


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (ToolOutcome.SUCCESS, "success"),
        (ToolOutcome.FAILED, "failed"),
        (ToolOutcome.DENIED, "denied"),
        (ToolOutcome.INTERRUPTED, "interrupted"),
    ],
)
def test_present_every_tool_outcome_serialises_to_its_value(
    recording_exporter: Any, outcome: ToolOutcome, expected: str
) -> None:
    tracer = otel.TurnTracer(model="m")
    with tracer.turn_span() as turn:
        with tracer.tool_span("t", parent=turn, outcome=outcome):
            pass
    spans = recording_exporter.get_finished_spans()
    tool = next(
        s for s in spans if s.attributes.get(otel.GEN_AI_OPERATION_NAME) == "execute_tool"
    )
    assert tool.attributes[otel.GEN_AI_TOOL_CALL_OUTCOME] == expected


def test_present_timing_phases_reach_the_span(
    recording_exporter: Any, result: TurnResult
) -> None:
    """The phase split must be on the span — a blended latency alone would waste it."""
    tracer = otel.TurnTracer(model="m")
    with tracer.turn_span() as turn:
        tracer.finish_turn(turn, result)

    spans = recording_exporter.get_finished_spans()
    turn_span = next(
        s for s in spans if s.attributes.get(otel.GEN_AI_OPERATION_NAME) == "chat"
    )
    attrs = turn_span.attributes
    p = otel.PIKACHU_TIMING_PREFIX

    # Raw phases straight off TurnTiming.
    assert attrs[p + "setup_ms"] == 2
    assert attrs[p + "wait_ms"] == 2800
    assert attrs[p + "stream_ms"] == 90
    assert attrs[p + "finalize_ms"] == 1
    assert attrs[p + "total_ms"] == 2893
    # Derived attribution — the numbers the project actually tunes against.
    assert attrs[p + "framework_ms"] == 3  # setup + finalize
    assert attrs[p + "model_ms"] == 2890  # wait + stream
    assert attrs[p + "streaming_measured"] is True


def test_present_multiple_tool_calls_each_get_their_own_span(
    recording_exporter: Any,
) -> None:
    tracer = otel.TurnTracer(model="m")
    with tracer.turn_span() as turn:
        for name in ("web_search", "generate_image", "read_canvas"):
            with tracer.tool_span(name, parent=turn, outcome=ToolOutcome.SUCCESS):
                pass

    spans = recording_exporter.get_finished_spans()
    tool_names = sorted(
        s.attributes[otel.GEN_AI_TOOL_NAME]
        for s in spans
        if s.attributes.get(otel.GEN_AI_OPERATION_NAME) == "execute_tool"
    )
    assert tool_names == ["generate_image", "read_canvas", "web_search"]
