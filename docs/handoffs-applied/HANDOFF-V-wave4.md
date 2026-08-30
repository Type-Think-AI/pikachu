# HANDOFF-V — OTel GenAI spans dependency (`otel` extra)

`src/pikachu/telemetry/otel.py` emits OpenTelemetry `gen_ai.*` spans, but `pyproject.toml` is
reserved. Integrator: add the extra below, then the strict-xfail acceptance tests at the bottom
of this file flip from "xpassed-would-fail-strict" to required passes.

## Exact change

Add to `[project.optional-dependencies]` in `pyproject.toml` (an **extra**, so an agent that
does not export telemetry never installs it — matching the `mcp` extra and the wave-2
lazy-import rule; `otel.py` imports `opentelemetry` only *inside* functions, so `import pikachu`
never pulls it in):

```toml
[project.optional-dependencies]
otel = [
    "opentelemetry-api==1.44.0",
    "opentelemetry-sdk==1.44.0",
]
```

If an `otel` extra already exists, ensure it pins these two.

## Why these exact packages and versions

Verified by direct inspection of this environment (`.venv/bin/python`, 2026-08-30):

```
opentelemetry-api    1.44.0   INSTALLED (transitive; provides trace.get_tracer, SpanKind,
                              Status/StatusCode, set_tracer_provider)
opentelemetry-sdk    NOT INSTALLED
opentelemetry-semantic-conventions   NOT INSTALLED
```

Two deliberate choices:

1. **The module only needs `opentelemetry-api`.** Emitting spans depends on `opentelemetry.trace`
   (the API), never on the SDK. The API is already present here as a transitive dependency, so
   the *module* and the *no-op fallback* both work today with nothing added.

2. **The extra also pins `opentelemetry-sdk`** because the **present-library tests** in
   `tests/test_otel.py` need a real span pipeline to capture what was emitted — the
   `InMemorySpanExporter` + `TracerProvider` live in the SDK. Without the SDK those tests
   `pytest.importorskip("opentelemetry.sdk.trace")` and **skip**; with the pin above they run and
   assert the exact `gen_ai.*` attribute names, the child tool spans, and the timing phases.
   Pin the SDK to the **same 1.44.0** as the API — the two are released in lockstep and a
   mismatched pair is a known source of `TracerProvider` incompatibility.

`opentelemetry-semantic-conventions` is deliberately **not** required: the `gen_ai.*` attributes
are still **Development**-status upstream, so `otel.py` pins the exact strings it emits as module
constants rather than importing a convention package that could rename them under us. Add that
package only if a later lane wants generated convention constants, and expect the names to move.

## Attribute names are UNSTABLE (upstream's own classification)

The GenAI semantic conventions moved to `open-telemetry/semantic-conventions-genai`, and every
`gen_ai.*` attribute `otel.py` emits is classified **Development** (unstable). The four required
ones are pinned as `GEN_AI_OPERATION_NAME`, `GEN_AI_REQUEST_MODEL`, `GEN_AI_USAGE_INPUT_TOKENS`,
`GEN_AI_USAGE_OUTPUT_TOKENS` in `otel.py`. When upstream stabilises or renames them, update those
constants in one place. This is documented in the module docstring; do not present the names as
settled.

## What works WITHOUT this dependency (the no-op guarantee)

`tests/test_otel.py` runs **green today** with the extra unapplied:

* the **library-absent** half (the deliverable that proves telemetry cannot break a turn) patches
  the `opentelemetry` import to raise `ImportError` and asserts the module still imports, the
  no-op tracer swallows everything, and a full turn completes normally — no package needed;
* the **library-present** half skips (SDK absent) rather than failing.

`opentelemetry` is imported **lazily** inside `get_tracer()` / `opentelemetry_available()`, so
`import pikachu.telemetry.otel` — and `import pikachu` — do not pull it in. With the extra
uninstalled, `TurnTracer` degrades to a null-object tracer; telemetry is never the reason a turn
fails.

## Acceptance tests — strict xfail, flip to required passes when applied

Create `tests/test_handoff_v_acceptance.py` **exactly as below**. Each test is
`@pytest.mark.xfail(strict=True)` **while the extra is unapplied**: with `opentelemetry-sdk`
absent the test's body cannot succeed, so xfail is satisfied; with `strict=True`, if the SDK is
ever present but the behaviour is wrong, the suite **fails loudly** rather than silently xpassing.

After you apply the pyproject change and install (`pip install -e '.[otel,dev]'`), **remove the
`xfail` markers** (or the whole file) so these become ordinary required passes — the strict xfail
is the tripwire that proves the dependency is actually wired, exactly as it did for HANDOFF-I and
the `mcp` extra.

```python
"""HANDOFF-V acceptance — strict xfail until the `otel` extra is applied.

While `opentelemetry-sdk` is absent these xfail (their bodies cannot capture a span). Once the
integrator applies the pyproject `otel` extra and installs it, DELETE the xfail markers: they
become required passes. strict=True means a wrong-but-present implementation fails the suite
instead of silently xpassing.
"""

from __future__ import annotations

import importlib.util

import pytest

from pikachu.core.types import ToolOutcome, TurnResult, TurnTiming
from pikachu.telemetry import otel

_SDK_PRESENT = importlib.util.find_spec("opentelemetry.sdk.trace") is not None


def _exporter():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider, exporter


@pytest.mark.xfail(not _SDK_PRESENT, reason="otel extra not applied", strict=True)
def test_acceptance_required_gen_ai_attributes_present() -> None:
    provider, exporter = _exporter()
    try:
        result = TurnResult(
            text="ok", input_tokens=1800, output_tokens=120,
            timing=TurnTiming(setup_ms=2, wait_ms=2800, stream_ms=90, finalize_ms=1, total_ms=2893),
        )
        tracer = otel.TurnTracer(model="google/gemini-3.5-flash")
        assert tracer.is_noop is False
        with tracer.turn_span() as turn:
            with tracer.tool_span("web_search", parent=turn, outcome=ToolOutcome.SUCCESS):
                pass
            tracer.finish_turn(turn, result)

        spans = exporter.get_finished_spans()
        turn_span = next(s for s in spans if s.attributes.get(otel.GEN_AI_OPERATION_NAME) == "chat")
        assert turn_span.attributes[otel.GEN_AI_REQUEST_MODEL] == "google/gemini-3.5-flash"
        assert turn_span.attributes[otel.GEN_AI_USAGE_INPUT_TOKENS] == 1800
        assert turn_span.attributes[otel.GEN_AI_USAGE_OUTPUT_TOKENS] == 120

        tool_span = next(
            s for s in spans if s.attributes.get(otel.GEN_AI_OPERATION_NAME) == "execute_tool"
        )
        assert tool_span.attributes[otel.GEN_AI_TOOL_NAME] == "web_search"
        assert tool_span.attributes[otel.GEN_AI_TOOL_CALL_OUTCOME] == "success"
    finally:
        provider.shutdown()


@pytest.mark.xfail(not _SDK_PRESENT, reason="otel extra not applied", strict=True)
def test_acceptance_timing_phases_reach_the_span() -> None:
    provider, exporter = _exporter()
    try:
        result = TurnResult(
            text="ok", input_tokens=10, output_tokens=5,
            timing=TurnTiming(setup_ms=2, wait_ms=2800, stream_ms=90, finalize_ms=1, total_ms=2893),
        )
        tracer = otel.TurnTracer(model="m")
        with tracer.turn_span() as turn:
            tracer.finish_turn(turn, result)
        spans = exporter.get_finished_spans()
        turn_span = next(s for s in spans if s.attributes.get(otel.GEN_AI_OPERATION_NAME) == "chat")
        p = otel.PIKACHU_TIMING_PREFIX
        assert turn_span.attributes[p + "framework_ms"] == 3
        assert turn_span.attributes[p + "model_ms"] == 2890
    finally:
        provider.shutdown()
```

## Optional: expose the extra name to users

Nothing else in the tree references `otel`; a host enables telemetry export with
`pip install 'pikachu[otel]'`. No code change beyond the pyproject extra is required — the
module already degrades to a no-op without it.
