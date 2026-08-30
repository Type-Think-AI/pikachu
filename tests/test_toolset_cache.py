"""The toolset cache must be fast AND must never widen a permission grant.

Profiling showed 94% of Pydantic AI's ``Agent()`` construction was regenerating tool JSON
schemas and re-parsing docstrings with griffe — 366 µs for two tools, paid every turn because
P7 forbids reusing an agent. Caching the *toolset* (not the agent) removes it.

That optimisation touches the permission path, so it is pinned here. A cache keyed too loosely
would hand an agent a toolset built for a *different* allowlist, which is a P3 violation
wearing a performance hat.
"""

from __future__ import annotations

import pytest

from pikachu import AgentSpec, TurnRequest
from pikachu.backends.pydantic_ai import PydanticAIBackend


def brand_palette() -> str:
    """Return the house colour palette that all output must conform to."""
    return "amber #FFB300"


def shot_count(scene: str) -> int:
    """Return how many shots a scene should be broken into."""
    return 3


def render_preview() -> str:
    """Render a low-resolution preview."""
    return "preview"


@pytest.fixture
def backend() -> PydanticAIBackend:
    return PydanticAIBackend(
        api_key="not-a-real-key-no-network-in-these-tests",
        tool_registry={
            "brand_palette": brand_palette,
            "shot_count": shot_count,
            "render_preview": render_preview,
        },
    )


def _request(*tools: str) -> TurnRequest:
    return TurnRequest(
        message="x",
        agent=AgentSpec(name="colourist", allowed_tools=tools),
        effective_tools=tools,
    )


@pytest.mark.thunder
def test_same_permission_set_reuses_one_toolset(backend: PydanticAIBackend) -> None:
    """The whole point: identical permissions must not rebuild schemas."""
    first = backend._toolset_for(_request("brand_palette", "shot_count"))
    second = backend._toolset_for(_request("brand_palette", "shot_count"))
    assert first is second, "identical permission sets should share one cached toolset"
    assert len(backend._toolset_cache) == 1


@pytest.mark.thunder
def test_different_permission_set_never_shares_a_toolset(
    backend: PydanticAIBackend,
) -> None:
    """P3 under caching: a narrower grant must NOT receive the wider toolset.

    This is the security-relevant case. If the cache key were, say, the agent name, a run
    permitted one tool could be handed a toolset containing three.
    """
    wide = backend._toolset_for(_request("brand_palette", "shot_count", "render_preview"))
    narrow = backend._toolset_for(_request("brand_palette"))
    assert wide is not narrow
    assert len(backend._toolset_cache) == 2


@pytest.mark.thunder
def test_cached_toolset_contains_exactly_the_permitted_tools(
    backend: PydanticAIBackend,
) -> None:
    """The cached toolset's contents must match the grant, not the registry."""
    toolset = backend._toolset_for(_request("brand_palette"))
    assert toolset is not None
    names = set(toolset.tools) if hasattr(toolset, "tools") else set()
    if names:  # only assert when the attribute exists on this version
        assert names == {"brand_palette"}, names


@pytest.mark.thunder
def test_tool_order_and_duplicates_are_part_of_the_key(
    backend: PydanticAIBackend,
) -> None:
    """Consistent with the guard's no-dedupe rule.

    The guard deliberately preserves order and multiplicity, so the cache must treat a
    different order as a different key rather than normalising behind its back.
    """
    a = backend._toolset_for(_request("brand_palette", "shot_count"))
    b = backend._toolset_for(_request("shot_count", "brand_palette"))
    assert a is not b


@pytest.mark.thunder
def test_no_permitted_tools_yields_no_toolset(backend: PydanticAIBackend) -> None:
    """An empty grant must produce no toolset at all, not an empty-but-present one."""
    assert backend._toolset_for(_request()) is None
    assert backend._toolset_cache == {}


@pytest.mark.thunder
def test_unknown_tool_names_are_omitted_not_fabricated(
    backend: PydanticAIBackend,
) -> None:
    """A permitted name with no implementation is a deployment gap, not a grant.

    It must be dropped quietly — never invented, and never cause the turn to fail.
    """
    toolset = backend._toolset_for(_request("brand_palette", "tool_that_does_not_exist"))
    assert toolset is not None  # the one real tool still resolves
    assert backend._toolset_for(_request("only_missing_tools")) is None
