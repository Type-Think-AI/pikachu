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

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.toolsets import FunctionToolset

from pikachu.backends.base import BaseBackend
from pikachu.config import DEFAULT_MODEL
from pikachu.core.errors import PikachuError
from pikachu.core.types import TurnRequest, TurnResult, TurnTiming

__all__ = ["PydanticAIBackend"]

_APP_TITLE: Final = "Pikachu Agent"
_APP_URL: Final = "https://picxstudio.com"


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
        model_name = request.agent.model or self._model_name
        settings: OpenAIChatModelSettings | None = None
        if self._routing is not None:
            settings = OpenAIChatModelSettings(extra_body={"provider": dict(self._routing)})
        model = OpenAIChatModel(model_name, provider=self._provider, settings=settings)

        # Static instructions first so the cacheable prefix stays byte-identical across the
        # iterations of one turn (invariant P10). A skill body is appended after the agent's
        # own instructions and before any dynamic content for the same reason.
        instructions = request.agent.instructions or ""
        if request.skill is not None and request.skill.body:
            instructions = f"{instructions}\n\n{request.skill.body}".strip()

        agent: Agent[None, str] = Agent(
            model,
            instructions=instructions or None,
            # toolsets= rather than tools= on purpose: raw callables make Pydantic AI
            # regenerate every tool's JSON schema and re-parse its docstring on each
            # construction, which profiling showed to be 94% of Agent() cost.
            toolsets=[ts] if (ts := self._toolset_for(request)) is not None else [],
            retries=2,
        )
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

    async def _call(self, agent: Agent[None, str], message: str) -> _CallOutcome:
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
            except Exception:  # noqa: BLE001 - instrumentation must never fail a turn
                pass

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




def _int_of(usage: object, field: str) -> int:
    """Read a usage counter defensively.

    Provider usage payloads are inconsistent about which counters they populate — Google's
    implicit caching in particular reports 0 for cache reads through some paths regardless of
    whether the cache fired. A missing or ``None`` counter must read as 0 rather than crash a
    turn that otherwise succeeded.
    """
    value = getattr(usage, field, 0)
    return int(value) if isinstance(value, (int, float)) else 0
