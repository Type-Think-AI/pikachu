"""OpenTelemetry GenAI spans — a span per turn, a child span per tool call.

This complements ``telemetry/ledger.py``; it does **not** replace it. The ledger is our own
in-memory framework-vs-model attribution (the number this project actually tunes against). This
module speaks the *interoperable* dialect: it emits OpenTelemetry spans carrying ``gen_ai.*``
attributes so an external collector (Jaeger, Honeycomb, Grafana Tempo, an OTLP endpoint) can see
Pikachu turns alongside every other service in a trace.

Unstable attribute names — read this before trusting a name
------------------------------------------------------------
The GenAI semantic conventions **moved** to the repository
``open-telemetry/semantic-conventions-genai``, and **every** ``gen_ai.*`` attribute this module
emits is classified **Development** (i.e. *unstable*) by upstream's own status field. They can
be renamed or dropped in a future revision. This module therefore **pins the exact strings it
emits as module constants** (``GEN_AI_*`` below) rather than importing a convention package, and
does not present them as settled. When upstream stabilises them, update the constants in one
place. The names emitted here are the ones current as of 2026-08 on that repository:

    gen_ai.operation.name        gen_ai.request.model
    gen_ai.usage.input_tokens    gen_ai.usage.output_tokens

Optional dependency — a no-op when ``opentelemetry-api`` is absent
------------------------------------------------------------------
``opentelemetry-api`` lives in the ``otel`` **extra** (see ``HANDOFF-V.md``), exactly like the
``mcp`` extra. **Telemetry must never be the reason a turn fails.** So:

  * the ``opentelemetry`` import is **lazy** (inside a function, per the wave-2 rule) — importing
    ``pikachu.telemetry.otel``, and therefore ``import pikachu``, never pulls it in;
  * when it is not installed, :func:`get_tracer` returns a **null-object** tracer whose spans
    swallow every call and record nothing. There is deliberately **no** ``try/except`` scattered
    at the call sites — the no-op lives in one place (:class:`_NoopSpan` / :class:`_NoopTracer`)
    and the caller writes the same code whether or not the library is present.

So the whole module reduces, for a caller, to: build a :class:`TurnTracer`, call
:meth:`TurnTracer.turn_span` around a turn and :meth:`TurnTracer.tool_span` around each tool
call. With the library absent those are no-ops; with it present they are real OTel spans.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import TracebackType
from typing import TYPE_CHECKING, Any, Iterator, Literal, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from pikachu.core.types import ToolOutcome, TurnResult, TurnTiming

__all__ = [
    "GEN_AI_OPERATION_NAME",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "GEN_AI_USAGE_OUTPUT_TOKENS",
    "GEN_AI_TOOL_NAME",
    "GEN_AI_TOOL_CALL_OUTCOME",
    "PIKACHU_TIMING_PREFIX",
    "SpanLike",
    "TracerLike",
    "TurnTracer",
    "opentelemetry_available",
]


# --------------------------------------------------------------------------------------
# Pinned attribute names — UNSTABLE by upstream classification (see module docstring).
# Every one of these is "Development" status in open-telemetry/semantic-conventions-genai.
# Pinned here so a rename upstream is a one-line change, not a scavenger hunt.
# --------------------------------------------------------------------------------------

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# Tool-call attributes. `gen_ai.tool.name` is upstream; the call outcome is a Pikachu
# extension (our ToolOutcome models DENIED / INTERRUPTED, which the GenAI conventions do not
# express) so it is namespaced under `pikachu.` to avoid squatting an unstable gen_ai.* name.
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_OUTCOME = "pikachu.tool.call.outcome"

# TurnTiming phases annotated onto the turn span. Framework-vs-model attribution is the number
# this project uses; a blended latency on a span would waste the span. Namespaced `pikachu.`
# because these are ours, not a gen_ai.* convention.
PIKACHU_TIMING_PREFIX = "pikachu.timing."

# The instrumentation scope name and version reported on every span this module emits.
_INSTRUMENTATION_NAME = "pikachu.telemetry.otel"
_INSTRUMENTATION_VERSION = "0.0.1"

# Default operation name. The GenAI conventions enumerate operation names (chat, generate_content,
# execute_tool, ...); "chat" is the turn-level operation for an agent turn.
_DEFAULT_OPERATION = "chat"


# --------------------------------------------------------------------------------------
# Structural protocols — what a span / tracer must look like, without importing OTel.
#
# These describe the *subset* of the OpenTelemetry Span / Tracer surface this module uses.
# A real OTel span satisfies SpanLike; so does _NoopSpan. Typing against these keeps
# `mypy --strict` clean whether or not opentelemetry is installed.
# --------------------------------------------------------------------------------------


@runtime_checkable
class SpanLike(Protocol):
    """The span surface this module touches."""

    def set_attribute(self, key: str, value: Any) -> Any: ...

    def end(self) -> Any: ...


@runtime_checkable
class TracerLike(Protocol):
    """The tracer surface this module touches."""

    def start_span(self, name: str, *args: Any, **kwargs: Any) -> SpanLike: ...


# --------------------------------------------------------------------------------------
# Null objects — the no-op path. This is the entire "library absent" behaviour, in one place.
# --------------------------------------------------------------------------------------


class _NoopSpan:
    """A span that records nothing and never raises.

    Every method a caller might invoke is present and returns quietly. This is why there is no
    ``try/except`` at the call sites: with the library absent the caller still gets an object
    with ``set_attribute`` / ``end`` that does nothing.
    """

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D401 - trivial no-op
        return None

    def end(self) -> None:
        return None

    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        # Never suppress an exception raised inside the with-block: telemetry observes, it does
        # not swallow the caller's real errors. Typed Literal[False] so mypy knows it can never
        # swallow.
        return False


class _NoopTracer:
    """A tracer that only ever hands out :class:`_NoopSpan`."""

    __slots__ = ()

    def start_span(self, name: str, *args: Any, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()


# --------------------------------------------------------------------------------------
# Lazy availability check + tracer acquisition.
# --------------------------------------------------------------------------------------


def opentelemetry_available() -> bool:
    """True iff ``opentelemetry-api`` can be imported. Lazy — never imported at module scope.

    Used by tests and by callers that want to branch on availability; the normal path does not
    need it because :func:`get_tracer` already degrades to a no-op.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("opentelemetry.trace") is not None
    except (ImportError, ValueError):
        return False


def get_tracer() -> TracerLike:
    """Return the process tracer, or a no-op tracer when ``opentelemetry-api`` is absent.

    The import is done **here**, lazily, so ``import pikachu.telemetry.otel`` costs nothing and
    an environment without the extra installed simply gets the null object. Any import failure
    is treated as "not installed" and degrades to the no-op — telemetry never propagates its
    own ImportError to a turn.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return _NoopTracer()
    tracer = trace.get_tracer(_INSTRUMENTATION_NAME, _INSTRUMENTATION_VERSION)
    # A real Tracer satisfies TracerLike structurally. Cast (rather than a type: ignore) so this
    # stays clean whether opentelemetry-api is installed (typed Tracer) or absent (untyped), and
    # is never flagged as an unused ignore in either environment.
    return cast("TracerLike", tracer)


# --------------------------------------------------------------------------------------
# The public entry point: TurnTracer.
# --------------------------------------------------------------------------------------


class TurnTracer:
    """Emit GenAI spans for a turn and its tool calls.

    Construct once (cheaply — it grabs a tracer, real or no-op) and use as a context manager
    factory:

        from pikachu.config import DEFAULT_MODEL

        tracer = TurnTracer(model=DEFAULT_MODEL)
        with tracer.turn_span() as turn:
            ...
            with tracer.tool_span("web_search", parent=turn):
                ...
            tracer.finish_turn(turn, result)

    With ``opentelemetry-api`` absent, every ``with`` block is a no-op that still runs the body
    normally. The caller writes identical code either way.
    """

    def __init__(self, *, model: str | None = None, operation: str = _DEFAULT_OPERATION) -> None:
        self._model = model
        self._operation = operation
        self._tracer: TracerLike = get_tracer()

    @property
    def is_noop(self) -> bool:
        """True when running without the OTel library (spans record nothing)."""
        return isinstance(self._tracer, _NoopTracer)

    # -- turn span ---------------------------------------------------------------------

    @contextmanager
    def turn_span(
        self,
        *,
        name: str | None = None,
        model: str | None = None,
        operation: str | None = None,
    ) -> Iterator[SpanLike]:
        """Open a span for one turn, carrying the required ``gen_ai.*`` attributes.

        ``gen_ai.operation.name`` and ``gen_ai.request.model`` are set on open (model may also be
        supplied per-call, overriding the tracer default). Token usage is filled in by
        :meth:`finish_turn` once the ``TurnResult`` exists. The span name follows the GenAI
        convention ``{operation} {model}`` when a model is known, else just the operation.
        """
        op = operation or self._operation
        mdl = model if model is not None else self._model
        span_name = name or (f"{op} {mdl}" if mdl else op)
        span = self._tracer.start_span(span_name)
        try:
            span.set_attribute(GEN_AI_OPERATION_NAME, op)
            if mdl is not None:
                span.set_attribute(GEN_AI_REQUEST_MODEL, mdl)
            yield span
        finally:
            span.end()

    def finish_turn(self, span: SpanLike, result: TurnResult) -> None:
        """Annotate a turn span with usage and the ``TurnTiming`` phase split.

        Token usage uses the pinned ``gen_ai.usage.*`` names. The timing phases are annotated
        under the ``pikachu.timing.`` prefix — framework-vs-model attribution is the number this
        project uses, so a blended latency alone would waste the span. Safe to call on a no-op
        span (it swallows everything).
        """
        span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, result.input_tokens)
        span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, result.output_tokens)
        self.annotate_timing(span, result.timing)

    def annotate_timing(self, span: SpanLike, timing: TurnTiming) -> None:
        """Write every ``TurnTiming`` phase onto the span under ``pikachu.timing.*``.

        Both the raw phases (setup/wait/stream/finalize) and the two derived numbers that decide
        anything (``framework_ms``, ``model_ms``) are recorded, plus whether the wait/stream
        split was actually measured — so a reader never mistakes an unmeasured split for a real
        one.
        """
        span.set_attribute(PIKACHU_TIMING_PREFIX + "setup_ms", timing.setup_ms)
        span.set_attribute(PIKACHU_TIMING_PREFIX + "wait_ms", timing.wait_ms)
        span.set_attribute(PIKACHU_TIMING_PREFIX + "stream_ms", timing.stream_ms)
        span.set_attribute(PIKACHU_TIMING_PREFIX + "finalize_ms", timing.finalize_ms)
        span.set_attribute(PIKACHU_TIMING_PREFIX + "total_ms", timing.total_ms)
        span.set_attribute(PIKACHU_TIMING_PREFIX + "framework_ms", timing.framework_ms)
        span.set_attribute(PIKACHU_TIMING_PREFIX + "model_ms", timing.model_ms)
        span.set_attribute(
            PIKACHU_TIMING_PREFIX + "streaming_measured", timing.streaming_measured
        )

    # -- tool child span ---------------------------------------------------------------

    @contextmanager
    def tool_span(
        self,
        tool_name: str,
        *,
        parent: SpanLike | None = None,
        outcome: ToolOutcome | None = None,
    ) -> Iterator[SpanLike]:
        """Open a child span for one tool call.

        Carries ``gen_ai.tool.name`` and the ``execute_tool`` operation name. The outcome (our
        ``ToolOutcome``: success / failed / denied / interrupted) is recorded via
        :meth:`set_tool_outcome` — pass it here to set it on open, or call that method when the
        outcome is known. ``parent`` is accepted for API symmetry; with the real library, span
        parenting follows OTel's active-context model, so passing the parent span is advisory.
        """
        span = self._tracer.start_span(f"execute_tool {tool_name}")
        try:
            span.set_attribute(GEN_AI_OPERATION_NAME, "execute_tool")
            span.set_attribute(GEN_AI_TOOL_NAME, tool_name)
            if outcome is not None:
                self.set_tool_outcome(span, outcome)
            yield span
        finally:
            span.end()

    def set_tool_outcome(self, span: SpanLike, outcome: ToolOutcome) -> None:
        """Record a tool call's ``ToolOutcome`` on its span as a stable string value.

        ``outcome.value`` (``success`` / ``failed`` / ``denied`` / ``interrupted``) is written
        rather than the enum object, because a span attribute must be a primitive and the string
        form is what a collector can filter on.
        """
        span.set_attribute(GEN_AI_TOOL_CALL_OUTCOME, outcome.value)
