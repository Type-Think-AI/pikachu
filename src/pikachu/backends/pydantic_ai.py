"""The live Pydantic AI backend.

The only module in this package that talks to a model. Verified against **pydantic-ai 2.36.0**
(V2) on 2026-08-30 using the real signatures, not from memory:

    OpenRouterProvider(*, api_key=None, app_url=None, app_title=None, ...)
    OpenAIChatModel(model_name, *, provider=..., profile=None, settings=None)
    Agent(model, *, instructions=..., tools=..., toolsets=..., model_settings=..., retries=...)
    await agent.run(prompt, message_history=...) -> AgentRunResult
    result.usage() -> RunUsage

Re-run ``scripts/verify_pydantic_ai.py`` after any dependency bump; it exits non-zero if any
symbol used here disappears.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.toolsets import FunctionToolset

from pikachu.backends.base import BaseBackend
from pikachu.config import DEFAULT_MODEL
from pikachu.core.errors import PikachuError
from pikachu.core.events import (
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
    TurnStarted,
)
from pikachu.core.types import ToolOutcome, TurnRequest, TurnResult, TurnTiming

__all__ = ["PydanticAIBackend"]

logger = logging.getLogger(__name__)

_APP_TITLE: Final = "Pikachu Agent"
_APP_URL: Final = "https://picxstudio.com"

# Bounded retry for a provider that answered but said nothing.
#
# OpenRouter intermittently returns HTTP 200 with EVERY ChatCompletion field null
# except ``created``:
#
#     ChatCompletion(id=None, choices=None, created=1788197893, model=None, object=None)
#
# pydantic-ai validates that into ``UnexpectedModelBehavior`` ("Invalid response from
# openrouter chat completions endpoint: 4 validation errors for ChatCompletion"), which
# reaches the user as a hard failure on an otherwise healthy turn. It is the reason a
# plain "hi" succeeds on one attempt and fails on the next.
#
# ``Agent(retries=...)`` does NOT cover this: that budget is for tool/output validation
# (``ModelRetry``), and a malformed envelope never reaches it.
#
# Retrying is safe here specifically because the response is EMPTY — no choices means no
# completion tokens were produced, so there is nothing billed to repeat and no partial
# output to duplicate. That is why the predicate below is narrow: a content-policy
# refusal, an auth error, a rate limit, or a tool-validation failure are all real answers
# and must surface on the first attempt rather than being hammered.
_MAX_UPSTREAM_ATTEMPTS: Final = 3
_UPSTREAM_RETRY_BACKOFF_S: Final = (0.4, 1.2)
_EMPTY_COMPLETION_MARKERS: Final = (
    "invalid response from",
    "validation errors for chatcompletion",
)


def _is_empty_completion(exc: BaseException) -> bool:
    """True only for a structurally empty/malformed completion envelope.

    Deliberately matches on the validation-failure shape rather than on any
    ``UnexpectedModelBehavior``: that exception also covers genuine model misbehaviour
    (e.g. an unparseable tool call) where a retry is not obviously correct.
    """
    if not isinstance(exc, UnexpectedModelBehavior):
        return False
    text = str(exc).lower()
    return all(marker in text for marker in _EMPTY_COMPLETION_MARKERS)


class PydanticAIBackend(BaseBackend):
    """Runs a turn against a real model through OpenRouter.

    Two properties matter more than anything else in here:

    1. **It never computes its own toolset.** ``TurnRequest.effective_tools`` arrives already
       narrowed by ``pikachu.guard``, and this class can only ever *select from* the registry
       it was handed using those names. A backend that re-derives tools defeats the entire
       permission layer, so the narrowing is applied as a dict lookup against the request and
       an unknown name is simply absent rather than fetched from anywhere.
    2. **The model is pinned.** ``DEFAULT_MODEL`` is the project default and a per-agent
       override is honoured only if explicitly present on the spec. There is no silent
       fallback to another model family.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        tool_registry: Mapping[str, Callable[..., Any]] | None = None,
        app_title: str = _APP_TITLE,
        app_url: str = _APP_URL,
        measure_streaming: bool = True,
        provider_routing: Mapping[str, Any] | None = None,
    ) -> None:
        if not api_key:
            raise PikachuError("PydanticAIBackend requires an OpenRouter API key")
        self._model_name = model
        self._registry: dict[str, Callable[..., Any]] = dict(tool_registry or {})
        self._toolset_cache: dict[tuple[str, ...], FunctionToolset[None]] = {}
        """Tool schemas keyed by the exact permitted-name tuple. See _toolset_for."""
        self._measure_streaming = measure_streaming
        """Stream the response purely to measure time-to-first-token, which is what separates
        waiting from decoding. Set False to use the simpler non-streaming call, at the cost of
        losing that split."""
        self._routing: dict[str, Any] | None = dict(provider_routing) if provider_routing else None
        """OpenRouter provider-routing block, sent as ``extra_body['provider']``.

        This is the lever on **queue time**, which measurement showed to be the largest and most
        variable term in a turn — far larger than anything in our own code. Shapes OpenRouter
        accepts include ``{"sort": "latency"}``, ``{"order": ["<tag>"], "allow_fallbacks": False}``
        and ``{"only": [...]}``.

        ``allow_fallbacks: False`` pins hard: the request fails rather than silently landing on a
        different endpoint. That is the right default for a *measurement*, and a risky one for
        production, where a fallback is usually better than an error.
        """
        self._provider = OpenRouterProvider(
            api_key=api_key, app_title=app_title, app_url=app_url
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    async def aclose(self) -> None:
        """Close the underlying HTTP client.

        Without this the async transport is left for the garbage collector, which raises
        ``ResourceWarning: unclosed transport`` at an arbitrary later moment. Under a strict
        ``filterwarnings = ["error"]`` policy that surfaces as a *teardown failure attributed to
        whichever test happened to be running when GC fired* — so an API call that succeeded
        gets reported as a failure somewhere else entirely. Closing deterministically is the
        fix; muting the warning only hides where the leak lands.

        ``AsyncOpenAI`` exposes ``close()`` (a coroutine, despite the name) and wraps an httpx
        ``AsyncClient`` that has ``aclose()``. Both are attempted, most-specific first, and any
        failure is ignored because a close error must never mask the real result of a turn.
        """
        client = getattr(self._provider, "client", None)
        for closer in ("close", "aclose"):
            fn = getattr(client, closer, None)
            if fn is None:
                continue
            try:
                result = fn()
                if hasattr(result, "__await__"):
                    await result
                return
            except Exception:  # noqa: BLE001 - a close failure must not mask the turn result
                continue
        inner = getattr(client, "_client", None)
        inner_close = getattr(inner, "aclose", None)
        if inner_close is not None:
            try:
                await inner_close()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> PydanticAIBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _tools_for(self, request: TurnRequest) -> list[Callable[..., Any]]:
        """Select implementations for exactly the tools the guard permitted.

        Names absent from the registry are silently omitted rather than raising: the guard has
        already decided what is allowed, and a missing *implementation* is a deployment gap,
        not a permission decision. Raising here would turn a benign gap into a failed turn.
        """
        return [
            self._registry[name] for name in request.effective_tools if name in self._registry
        ]

    def _toolset_for(self, request: TurnRequest) -> FunctionToolset[None] | None:
        """Return a cached toolset for this exact permission set, building it once.

        **Why this exists.** Profiling showed 94% of ``Agent()`` construction was Pydantic AI
        regenerating each tool's JSON schema and re-parsing its docstring with griffe — 366 µs
        for two tools, and it scales with tool count. P7 forbids sharing an *agent* across
        turns, so we build a fresh agent every turn and were paying that schema cost every
        time, for schemas that never change.

        A toolset is not an agent. Reusing it keeps P7 intact (the agent is still new each
        turn) while turning per-turn schema generation into one-time work: measured 390.9 µs ->
        24.5 µs, which is the no-tools floor.

        **P3 is preserved** because the cache key is the exact tuple of permitted tool names.
        A different permission set is a different key and therefore a different toolset — the
        cache can never widen a grant, and two agents with different allowlists cannot collide.
        Order and duplicates are part of the key, consistent with the guard's no-dedupe rule.
        """
        tools = self._tools_for(request)
        if not tools:
            return None
        key = tuple(request.effective_tools)
        cached = self._toolset_cache.get(key)
        if cached is None:
            cached = FunctionToolset(tools)
            self._toolset_cache[key] = cached
        return cached

    async def run_turn(self, request: TurnRequest) -> TurnResult:
        """Run one turn, attributing wall-clock time to whoever spent it.

        Four phases are timed independently so a latency change can be blamed correctly. See
        ``TurnTiming``: ``setup`` and ``finalize`` are ours, ``wait`` and ``stream`` are the
        provider's. Measuring them as one number is what makes a provider swap look like a
        framework regression.
        """
        t_start = time.perf_counter()

        # ---- phase 1: setup (OURS) -----------------------------------------------------
        agent = self._build_agent(request)
        t_setup_done = time.perf_counter()

        # ---- phase 2+3: the model call (NOT OURS) ---------------------------------------
        call = await self._call(agent, request.message)

        # ---- phase 4: finalize (OURS) ---------------------------------------------------
        iterations = sum(1 for m in call.messages if type(m).__name__ == "ModelResponse")

        # Record every tool-call part the model emitted, and whether it actually executed.
        # A ToolCallPart with a matching ToolReturnPart ran; one the guard removed from the
        # schema can still be emitted by a primed model and never execute (round-3 finding).
        # `executed` is what makes tool_calls mean the same thing here as in FakeBackend.
        returned_ids: set[str] = set()
        for message in call.messages:
            for part in getattr(message, "parts", ()):
                if type(part).__name__ == "ToolReturnPart":
                    tcid = getattr(part, "tool_call_id", None)
                    if tcid:
                        returned_ids.add(str(tcid))

        tool_calls: list[dict[str, Any]] = []
        for message in call.messages:
            for part in getattr(message, "parts", ()):
                if type(part).__name__ == "ToolCallPart":
                    tcid = getattr(part, "tool_call_id", None)
                    tool_calls.append(
                        {
                            "tool": getattr(part, "tool_name", "?"),
                            "args": str(getattr(part, "args", ""))[:500],
                            "executed": tcid is not None and str(tcid) in returned_ids,
                        }
                    )
        t_end = time.perf_counter()

        def ms(a: float, b: float) -> int:
            return max(0, int((b - a) * 1000))

        timing = TurnTiming(
            setup_ms=ms(t_start, t_setup_done),
            # With a first-token moment, wait and decode separate cleanly. Without one, the
            # whole call is reported as wait and the split is flagged unavailable rather than
            # invented.
            wait_ms=ms(t_setup_done, call.first_token_at if call.streamed else call.done_at),
            stream_ms=ms(call.first_token_at, call.done_at) if call.streamed else 0,
            finalize_ms=ms(call.done_at, t_end),
            total_ms=ms(t_start, t_end),
            streaming_measured=call.streamed,
        )

        # Which endpoint ACTUALLY served this. Essential when testing routing: without it a
        # "priority is faster" conclusion cannot be distinguished from "the routing block was
        # ignored and we measured the default twice".
        served_by = ""
        for message in call.messages:
            name = getattr(message, "provider_name", None)
            if name:
                served_by = str(name)
                details = getattr(message, "provider_details", None)
                if isinstance(details, dict):
                    tag = details.get("provider_name") or details.get("provider")
                    if tag:
                        served_by = f"{name} ({tag})"
                break

        return TurnResult(
            text=call.text,
            tool_calls=tuple(tool_calls),
            input_tokens=_int_of(call.usage, "input_tokens"),
            output_tokens=_int_of(call.usage, "output_tokens"),
            cache_read_tokens=_int_of(call.usage, "cache_read_tokens"),
            cache_write_tokens=_int_of(call.usage, "cache_write_tokens"),
            iterations=max(iterations, 1),
            latency_ms=timing.total_ms,
            timing=timing,
            served_by=served_by,
        )

    def _build_agent(self, request: TurnRequest) -> Agent[None, str]:
        """Construct the per-turn agent. Shared by run_turn and stream_turn so the two can
        never diverge on model, instructions, toolset or routing.

        Instructions are static-first so the cacheable prefix stays byte-identical across the
        iterations of one turn (invariant P10). Tools go in via a cached toolset, not raw
        callables, because raw callables make Pydantic AI regenerate every tool's JSON schema
        and re-parse its docstring on each construction — 94% of Agent() cost.
        """
        model_name = request.agent.model or self._model_name
        settings: OpenAIChatModelSettings | None = None
        if self._routing is not None:
            settings = OpenAIChatModelSettings(extra_body={"provider": dict(self._routing)})
        model = OpenAIChatModel(model_name, provider=self._provider, settings=settings)

        instructions = request.agent.instructions or ""
        if request.skill is not None and request.skill.body:
            instructions = f"{instructions}\n\n{request.skill.body}".strip()

        return Agent(
            model,
            instructions=instructions or None,
            toolsets=[ts] if (ts := self._toolset_for(request)) is not None else [],
            retries=2,
        )

    async def stream_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Native live event stream — the path ``backends/streaming.py`` delegates to.

        Driven by pydantic-ai's flat ``run_stream_events`` stream, which interleaves, in the
        order the run produces them: text-part deltas, ``FunctionToolCallEvent`` /
        ``FunctionToolResultEvent`` for each tool call, and a terminal ``AgentRunResultEvent``.
        We map that stream onto :mod:`pikachu.core.events`:

        * each text delta -> one :class:`TextDelta` (so a chat UI renders the answer as it
          arrives rather than in one blob — the incrementality this backend was recently fixed
          to preserve stays preserved: >1 provider chunk -> >1 ``TextDelta``);
        * ``FunctionToolCallEvent`` -> :class:`ToolCallStarted`, carrying the provider's
          ``tool_call_id`` as the stable ``call_id`` and the tool name, emitted the instant the
          call begins so the UI shows a "generating" skeleton for a minutes-long media job;
        * ``FunctionToolResultEvent`` -> :class:`ToolCallFinished` with the **same** ``call_id``
          (so start and finish reconcile into one card) and an outcome read from the result
          part: a ``RetryPromptPart`` is a ``FAILED`` call, anything else is ``SUCCESS``.

        Events are recognised structurally by ``event_kind`` and duck-typed attributes rather
        than by importing a wall of pydantic-ai symbols — the mapping tolerates an unknown
        event kind by ignoring it, and only the handful of fields it reads need exist.

        The terminal :class:`TurnFinished` carries a full :class:`TurnResult` equal in shape to
        what ``run_turn`` returns. A live-stream failure (including a mid-stream provider drop)
        falls back to ``run_turn`` and STILL emits :class:`TurnFinished`, so a crashed stream
        never strands the consumer.

        NOTE (integrator): progress *between* start and finish is modelled by
        :class:`~pikachu.core.events.ToolCallProgress`, but pydantic-ai's flat stream surfaces
        no per-call progress ticks for an opaque function tool — the provider reports the call
        as started then returned, nothing in between. So no ``ToolCallProgress`` is emitted from
        THIS path today; the Lane-3 bridge (which owns the media wrappers and sees generation
        state) is where progress ticks originate, and the event exists so it can. See HANDOFF.
        """
        yield TurnStarted(agent_name=request.agent.name, streaming=True)

        t_start = time.perf_counter()
        agent = self._build_agent(request)
        t_setup = time.perf_counter()

        try:
            first: float | None = None
            pieces: list[str] = []
            output = ""
            usage: object = None
            messages: list[Any] = []
            async with agent.run_stream_events(request.message) as events:
                async for event in events:
                    kind = getattr(event, "event_kind", None)
                    # ---- text deltas: preserve one-delta-per-chunk incrementality -------
                    if kind == "part_start":
                        part = getattr(event, "part", None)
                        text = _text_of_part(part)
                        if text:
                            if first is None:
                                first = time.perf_counter()
                            pieces.append(text)
                            yield TextDelta(text=text)
                    elif kind == "part_delta":
                        delta = getattr(event, "delta", None)
                        raw_delta = getattr(delta, "content_delta", None)
                        if isinstance(raw_delta, str) and raw_delta:
                            if first is None:
                                first = time.perf_counter()
                            pieces.append(raw_delta)
                            yield TextDelta(text=raw_delta)
                    # ---- tool lifecycle: started / finished sharing the provider id -----
                    elif kind == "function_tool_call":
                        part = getattr(event, "part", None)
                        yield ToolCallStarted(
                            call_id=_tool_call_id_of_event(event, part),
                            tool=str(getattr(part, "tool_name", "?")),
                            args=str(getattr(part, "args", ""))[:500],
                        )
                    elif kind == "function_tool_result":
                        part = getattr(event, "part", None)
                        yield ToolCallFinished(
                            call_id=_tool_call_id_of_event(event, part),
                            tool=str(getattr(part, "tool_name", "") or "?"),
                            outcome=_outcome_of_result_part(part),
                        )
                    # ---- terminal result event -----------------------------------------
                    elif kind == "agent_run_result":
                        run_result = getattr(event, "result", None)
                        output = str(getattr(run_result, "output", "") or "")
                        usage = _maybe_call(getattr(run_result, "usage", None))
                        all_messages = getattr(run_result, "all_messages", None)
                        if callable(all_messages):
                            messages = list(all_messages())
                    # any other event kind is ignored by design
            done = time.perf_counter()
        except Exception:  # noqa: BLE001 - a live-stream failure must not kill the turn
            result = await self.run_turn(request)
            if result.text:
                yield TextDelta(text=result.text)
            yield TurnFinished(result=result)
            return

        def ms(a: float, b: float) -> int:
            return max(0, int((b - a) * 1000))

        timing = TurnTiming(
            setup_ms=ms(t_start, t_setup),
            wait_ms=ms(t_setup, first) if first else ms(t_setup, done),
            stream_ms=ms(first, done) if first else 0,
            finalize_ms=0,
            total_ms=ms(t_start, done),
            streaming_measured=first is not None,
        )
        iterations = sum(1 for m in messages if type(m).__name__ == "ModelResponse")
        result = TurnResult(
            text=output or "".join(pieces),
            input_tokens=_int_of(usage, "input_tokens"),
            output_tokens=_int_of(usage, "output_tokens"),
            cache_read_tokens=_int_of(usage, "cache_read_tokens"),
            cache_write_tokens=_int_of(usage, "cache_write_tokens"),
            iterations=max(iterations, 1),
            latency_ms=timing.total_ms,
            timing=timing,
        )
        yield TurnFinished(result=result)

    async def _call(self, agent: Agent[None, str], message: str) -> _CallOutcome:
        """Execute the run, retrying only a structurally empty provider response.

        See ``_is_empty_completion``. The retry lives here because this is the single
        point every turn's model invocation passes through, so both the streamed and the
        plain path are covered by one budget rather than two.

        The final attempt re-raises, so a provider that is genuinely down still surfaces
        as an error instead of being retried into a timeout.
        """
        last: BaseException | None = None
        for attempt in range(1, _MAX_UPSTREAM_ATTEMPTS + 1):
            try:
                return await self._call_once(agent, message)
            except Exception as exc:
                if not _is_empty_completion(exc):
                    raise
                last = exc
                if attempt == _MAX_UPSTREAM_ATTEMPTS:
                    break
                delay = _UPSTREAM_RETRY_BACKOFF_S[
                    min(attempt - 1, len(_UPSTREAM_RETRY_BACKOFF_S) - 1)
                ]
                logger.warning(
                    "pikachu: provider returned an empty completion "
                    "(attempt %d/%d), retrying in %.1fs: %s",
                    attempt,
                    _MAX_UPSTREAM_ATTEMPTS,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        logger.error(
            "pikachu: provider returned an empty completion on all %d attempts; "
            "failing the turn: %s",
            _MAX_UPSTREAM_ATTEMPTS,
            last,
        )
        assert last is not None  # noqa: S101 - only reachable via the retry break
        raise last

    async def _call_once(self, agent: Agent[None, str], message: str) -> _CallOutcome:
        """Execute the run, capturing the first-token moment when possible.

        Streaming is used purely as an **instrument**: it is the only way to separate
        **waiting** (network round trip + provider queue + prefill) from **decoding** (which
        scales with output tokens). Those have different remedies — waiting is addressed by
        provider or region choice, decoding by asking for less output — so collapsing them
        discards the actionable part.

        The two code paths return different objects (``StreamedRunResult`` vs
        ``AgentRunResult``), so both are normalised here rather than leaking the difference
        into ``run_turn``. Any streaming failure falls back to a plain run with
        ``streamed=False``, so a split is never reported that was not measured.
        """
        if self._measure_streaming:
            try:
                first: float | None = None
                async with agent.run_stream(message) as stream:
                    async for _delta in stream.stream_text(delta=True):
                        if first is None:
                            first = time.perf_counter()
                    text = str(await stream.get_output())
                    usage = _maybe_call(getattr(stream, "usage", None))
                    messages = list(stream.all_messages())
                done = time.perf_counter()
                if first is not None:
                    return _CallOutcome(text, usage, messages, first, done, True)
                # No text deltas (e.g. a tool-only response): nothing to split on.
                return _CallOutcome(text, usage, messages, done, done, False)
            except Exception as exc:
                # An empty provider envelope is NOT an instrumentation problem, so it
                # must not be absorbed here: falling through would re-run the same
                # failing request on the plain path, pay the latency twice, and surface
                # the second failure with the first one's cause erased. Let the retry
                # wrapper in `_call` see it.
                if _is_empty_completion(exc):
                    raise
                # Anything else really is instrumentation-only — the plain path below
                # can still answer — but it is logged rather than swallowed. This block
                # previously did a bare `pass`, which is why a failing stream left no
                # trace at all and the fallback looked like the original error.
                logger.warning(
                    "pikachu: streamed measurement path failed (%s: %s); "
                    "falling back to a non-streamed run",
                    type(exc).__name__,
                    exc,
                )

        t0 = time.perf_counter()
        result = await agent.run(message)
        return _CallOutcome(
            str(result.output),
            _maybe_call(getattr(result, "usage", None)),
            list(result.all_messages()),
            t0,
            time.perf_counter(),
            False,
        )


@dataclass(frozen=True, slots=True)
class _CallOutcome:
    """Normalised result of a model call, whichever path produced it."""

    text: str
    usage: object
    messages: list[Any]
    first_token_at: float
    done_at: float
    streamed: bool


def _maybe_call(value: object) -> object:
    """Return ``value``, calling it first if it is callable.

    ``usage`` is a property on ``AgentRunResult`` in V2 but was a method in V1, and the
    streaming result exposes it differently again. Tolerating both costs one line and removes
    a whole class of version-drift breakage.
    """
    if callable(value):
        try:
            return value()
        except Exception:  # noqa: BLE001
            return None
    return value


def _text_of_part(part: object) -> str:
    """Text carried by a ``part_start`` event's part, if it is a text part.

    Read structurally: a ``TextPart`` exposes ``content`` and ``part_kind == "text"``. A
    tool-call part started here (``part_kind == "tool-call"``) carries no assistant text and
    must not be mistaken for one, so only a text-kinded part with string ``content`` yields
    text; everything else yields ``""`` and is ignored by the caller.
    """
    if getattr(part, "part_kind", None) == "text":
        content = getattr(part, "content", None)
        if isinstance(content, str):
            return content
    return ""


def _tool_call_id_of_event(event: object, part: object) -> str:
    """The provider ``tool_call_id`` for a tool call/result event.

    pydantic-ai exposes it as a property on the event itself and also on the underlying part;
    the event property is preferred, the part is the fallback, and an empty string is the last
    resort so a missing id never raises inside a stream.
    """
    for source in (event, part):
        tcid = getattr(source, "tool_call_id", None)
        if isinstance(tcid, str) and tcid:
            return tcid
    return ""


def _outcome_of_result_part(part: object) -> ToolOutcome:
    """Map a tool-result part to a :class:`ToolOutcome`.

    pydantic-ai delivers a successful function-tool return as a ``ToolReturnPart`` and a
    failed one as a ``RetryPromptPart`` (the model is asked to retry). So a ``RetryPromptPart``
    is the FAILED signal for a crashed/erroring tool; anything else is SUCCESS. The provider
    does not surface DENIED or INTERRUPTED on this path — those are decided upstream (the guard
    denies before the call is ever emitted) — so they are deliberately not inferred here.
    """
    if type(part).__name__ == "RetryPromptPart":
        return ToolOutcome.FAILED
    return ToolOutcome.SUCCESS


def _int_of(usage: object, field: str) -> int:
    """Read a usage counter defensively.

    Provider usage payloads are inconsistent about which counters they populate — Google's
    implicit caching in particular reports 0 for cache reads through some paths regardless of
    whether the cache fired. A missing or ``None`` counter must read as 0 rather than crash a
    turn that otherwise succeeded.
    """
    value = getattr(usage, field, 0)
    return int(value) if isinstance(value, (int, float)) else 0
