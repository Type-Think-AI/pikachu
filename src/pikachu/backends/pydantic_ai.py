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
from typing import Any, Final

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from pikachu.backends.base import BaseBackend
from pikachu.config import DEFAULT_MODEL
from pikachu.core.errors import PikachuError
from pikachu.core.types import TurnRequest, TurnResult

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
    ) -> None:
        if not api_key:
            raise PikachuError("PydanticAIBackend requires an OpenRouter API key")
        self._model_name = model
        self._registry: dict[str, Callable[..., Any]] = dict(tool_registry or {})
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

    async def run_turn(self, request: TurnRequest) -> TurnResult:
        model_name = request.agent.model or self._model_name
        model = OpenAIChatModel(model_name, provider=self._provider)

        # Static instructions first so the cacheable prefix stays byte-identical across the
        # iterations of one turn (invariant P10). A skill body is appended after the agent's
        # own instructions and before any dynamic content for the same reason.
        instructions = request.agent.instructions or ""
        if request.skill is not None and request.skill.body:
            instructions = f"{instructions}\n\n{request.skill.body}".strip()

        agent: Agent[None, str] = Agent(
            model,
            instructions=instructions or None,
            tools=self._tools_for(request),
            retries=2,
        )

        started = time.perf_counter()
        result = await agent.run(request.message)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # V2 CHANGE: `usage` is a PROPERTY on AgentRunResult, not a method. The V1 form
        # `result.usage()` raises "RunUsage not callable" - it is the one place V2 actually
        # moved the surface our design docs recorded.
        usage = result.usage
        messages = result.all_messages()

        # Count only model requests as iterations, so a single-shot turn reports 1 rather than
        # counting the user prompt or tool-return parts.
        iterations = sum(1 for m in messages if type(m).__name__ == "ModelResponse")

        tool_calls: list[dict[str, Any]] = []
        for message in messages:
            for part in getattr(message, "parts", ()):
                if type(part).__name__ == "ToolCallPart":
                    tool_calls.append(
                        {
                            "tool": getattr(part, "tool_name", "?"),
                            "args": str(getattr(part, "args", ""))[:500],
                        }
                    )

        return TurnResult(
            text=str(result.output),
            tool_calls=tuple(tool_calls),
            input_tokens=_int_of(usage, "input_tokens"),
            output_tokens=_int_of(usage, "output_tokens"),
            cache_read_tokens=_int_of(usage, "cache_read_tokens"),
            cache_write_tokens=_int_of(usage, "cache_write_tokens"),
            iterations=max(iterations, 1),
            latency_ms=elapsed_ms,
        )


def _int_of(usage: object, field: str) -> int:
    """Read a usage counter defensively.

    Provider usage payloads are inconsistent about which counters they populate — Google's
    implicit caching in particular reports 0 for cache reads through some paths regardless of
    whether the cache fired. A missing or ``None`` counter must read as 0 rather than crash a
    turn that otherwise succeeded.
    """
    value = getattr(usage, field, 0)
    return int(value) if isinstance(value, (int, float)) else 0
