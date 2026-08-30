"""Round 1 — the happy path, proven end to end on FakeBackend.

Every test here asserts a claim a *user* would make about the library:

* SKILLS — three authored SKILL.md documents load, and metadata-only load never
  reads the body (progressive disclosure).
* SKILL + TOOLS THROUGH A TURN — a skill's declared tool reaches a turn, is called,
  its output is used; the same skill with the tool removed from the allowlist still
  completes (guard narrowed it, turn degraded not crashed).
* DECLARATIVE FUNCTION TOOLS — a plain function's docstring becomes the tool
  description, and the toolset is built once and reused for the same permission set.
* STREAMING — event order holds and TurnFinished carries the same result the
  non-streaming call returns, timing phases included.

All offline. The autouse socket block in tests/conftest.py hard-fails any network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pikachu import (
    AgentSpec,
    Run,
    Skill,
    SkillStatus,
    ToolSpec,
    TrustTier,
    TurnRequest,
)
from pikachu.backends.fake import FakeBackend, FakeBiller, ScriptedTurn, ScriptedToolCall
from pikachu.backends.streaming import stream_turn
from pikachu.core.events import (
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from pikachu.core.types import ToolOutcome
from pikachu.guard import effective_tools
from pikachu.skills.loader import load_metadata, load_skill

SKILLS_DIR = Path(__file__).parent / "skills"


# ======================================================================================
# Shared helpers — the small amount of glue a user assembles to run a turn.
# ======================================================================================


def _read(name: str) -> str:
    return (SKILLS_DIR / name).read_text(encoding="utf-8")


def _load(name: str, *, trust: TrustTier = TrustTier.BUILTIN) -> Skill:
    """Load an authored SKILL.md from this round's fixtures, as ACTIVE.

    load_skill leaves status at DRAFT; a user running the skill sets it ACTIVE, so we
    do the same via model_copy (Skill is frozen).
    """
    skill = load_skill(_read(name), trust=trust, source=f"round_1:{name}")
    return skill.model_copy(update={"status": SkillStatus.ACTIVE})


# These are declarative function tools. The function's OWN NAME (__name__) becomes the
# tool name in the toolset — see the friction note in docs/test-round-1.md — so a tool
# named "brand_palette" must be defined as `def brand_palette`, not `def _brand_palette`
# under a registry alias, or the toolset registers it under the wrong name.
def brand_palette() -> str:
    """Return the house colour palette that all output must conform to."""
    return "Brand palette: ink #101014, bone #F4F1EA, signal amber #FFB300. Never pure black."


def shot_count(scene_description: str) -> int:
    """Return how many shots a scene should be broken into."""
    return max(1, min(6, len(scene_description.split()) // 4))


def storyboard(premise: str) -> str:
    """Draft a three-beat storyboard from a one-line premise."""
    return f"Beat 1/2/3 for: {premise}"


# ======================================================================================
# 1. AUTHOR THREE SKILLS + progressive disclosure
# ======================================================================================


def test_colourist_skill_loads_and_declares_its_tool() -> None:
    """The tool-using skill loads, is trusted, and carries brand_palette as declared."""
    skill = _load("colourist-palette.md")
    assert skill.name == "colourist-palette"
    assert skill.declared_tools == ("brand_palette",)
    assert skill.trust is TrustTier.BUILTIN
    assert skill.trust.may_contribute_tools is True
    assert "brand_palette" in skill.body  # the body tells the model to use it
    assert skill.lineage.is_clean  # a trusted skill is not tainted


def test_script_writer_skill_declares_nothing() -> None:
    """The text-only skill loads with an empty declared_tools tuple — declared nothing."""
    skill = _load("script-writer.md")
    assert skill.name == "script-writer"
    assert skill.declared_tools == ()
    assert skill.body.strip() != ""


def test_sticker_skill_declares_a_tool_it_will_not_be_granted() -> None:
    """The sticker skill declares sticker_cut; grant is the allowlist's call, not the skill's."""
    skill = _load("sticker-sheet.md")
    assert skill.declared_tools == ("sticker_cut",)
    # Declaring is not granting. With an allowlist that omits sticker_cut, the guard
    # narrows it to nothing — proven fully in test 2's degraded run below.
    narrowed = effective_tools(("brand_palette",), skill.declared_tools)
    assert narrowed.tools == ()
    assert "sticker_cut" in narrowed.removed_tools


def test_metadata_load_does_not_read_the_body() -> None:
    """load_metadata parses ONLY the frontmatter — progressive disclosure.

    Proven two ways: SkillMeta structurally has no body field, and a document whose
    body is deliberately corrupt still yields clean metadata because the body is never
    parsed.
    """
    meta = load_metadata(_read("colourist-palette.md"))
    assert meta.name == "colourist-palette"
    assert meta.declared_tools == ("brand_palette",)
    assert meta.metadata == {"author": "teo", "domain": "colour"}
    # The body is not an attribute of the metadata object at all.
    assert not hasattr(meta, "body")

    # A body that would break a full parse must not break a metadata-only read, because
    # the body is never touched. Same valid frontmatter, garbage body.
    frontmatter = _read("colourist-palette.md").split("---", 2)[1]
    corrupt = f"---{frontmatter}---\n\n\x00\x00 not valid anything \x00 : : :"
    meta2 = load_metadata(corrupt)
    assert meta2.name == "colourist-palette"
    assert meta2.declared_tools == ("brand_palette",)


# ======================================================================================
# 2. SKILL + TOOLS THROUGH A TURN — granted, then narrowed
# ======================================================================================


def _colourist_agent(allowed: tuple[str, ...]) -> AgentSpec:
    return AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        instructions="Use brand_palette for the exact hex values. Never invent a colour.",
        allowed_tools=allowed,
    )


async def test_skill_tool_is_called_and_its_output_is_used() -> None:
    """Grant brand_palette, give the colourist skill, run a turn that calls the tool.

    Assert the tool was invoked and the final text used the tool's output (the hex code).
    """
    skill = _load("colourist-palette.md")
    agent = _colourist_agent(("brand_palette",))

    # The guard narrows declared ∩ allowlist. Here both contain brand_palette, so it survives.
    narrowed = effective_tools(agent.allowed_tools, skill.declared_tools)
    assert narrowed.tools == ("brand_palette",)

    tool = ToolSpec(name="brand_palette", description="House palette.", cost_credits=0)
    run = Run(id="run:t2a", agent_name=agent.name, max_iterations=20)
    # Script: the model calls the tool, then answers quoting the tool's output.
    backend = FakeBackend(
        [
            ScriptedTurn(tool_calls=(ScriptedToolCall("brand_palette"),)),
            ScriptedTurn(text="The signal colour is amber #FFB300, per brand_palette."),
        ],
        run=run,
        tools=(tool,),
    )
    request = TurnRequest(
        message="What is our signal colour?",
        agent=agent,
        skill=skill,
        effective_tools=narrowed.tools,
        run_id=run.id,
    )

    result = await backend.run_turn(request)

    called = [c["tool"] for c in result.tool_calls]
    assert called == ["brand_palette"], f"tool was not invoked: {called}"
    assert "FFB300" in result.text.upper(), f"answer did not use tool output: {result.text!r}"
    assert result.iterations == 2


async def test_same_skill_degrades_when_tool_removed_from_allowlist() -> None:
    """Remove brand_palette from the allowlist; the guard omits it and the turn still completes.

    This is the key P3 demonstration on the happy path: the skill STILL declares the tool,
    but the agent's fixed allowlist no longer grants it, so effective_tools narrows it away.
    The turn must complete (degraded), not crash.
    """
    skill = _load("colourist-palette.md")
    assert skill.declared_tools == ("brand_palette",)  # the skill is unchanged

    agent = _colourist_agent(())  # allowlist grants NOTHING now

    narrowed = effective_tools(agent.allowed_tools, skill.declared_tools)
    assert narrowed.tools == (), "guard failed to narrow: skill widened its own authority"
    assert "brand_palette" in narrowed.removed_tools
    assert narrowed.reasons["brand_palette"] == "not in fixed allowlist"

    run = Run(id="run:t2b", agent_name=agent.name, max_iterations=20)
    # Degraded script: no tool call, because the tool is not available. The model falls
    # back to a neutral answer exactly as the skill body instructs.
    backend = FakeBackend(
        [ScriptedTurn(text="I don't have the palette tool, so grading to a neutral scheme.")],
        run=run,
    )
    request = TurnRequest(
        message="What is our signal colour?",
        agent=agent,
        skill=skill,
        effective_tools=narrowed.tools,
        run_id=run.id,
    )

    result = await backend.run_turn(request)  # must NOT raise
    assert result.text  # the turn produced an answer
    assert result.tool_calls == ()  # no tool was called
    assert result.iterations == 1


async def test_backend_refuses_a_tool_the_guard_did_not_grant() -> None:
    """Belt-and-braces: even if a script tries to call a denied tool, the backend refuses.

    The guard is the only source of authority. A scripted call to a tool outside
    effective_tools is refused at the backend rather than silently widened.
    """
    agent = _colourist_agent(())  # no tools granted
    run = Run(id="run:t2c", agent_name=agent.name)
    backend = FakeBackend(
        [ScriptedTurn(tool_calls=(ScriptedToolCall("brand_palette"),))],
        run=run,
    )
    request = TurnRequest(
        message="Call it anyway.", agent=agent, effective_tools=(), run_id=run.id
    )
    with pytest.raises(Exception) as excinfo:  # BudgetExceeded, per FakeBackend contract
        await backend.run_turn(request)
    assert "brand_palette" in str(excinfo.value)


# ======================================================================================
# 3. DECLARATIVE FUNCTION TOOLS — docstring becomes description, toolset cached
# ======================================================================================
#
# The toolset build + cache lives on PydanticAIBackend. It requires an API key to
# construct but NEVER touches the network to build a FunctionToolset — schema generation
# is pure. So we construct the backend with a dummy key (no run is executed) and drive
# _toolset_for directly. This is the same code path a live turn uses, exercised offline.


def _toolset_backend() -> object:
    from pikachu.backends.pydantic_ai import PydanticAIBackend

    return PydanticAIBackend(
        api_key="offline-dummy-key-never-used",
        tool_registry={
            "brand_palette": brand_palette,
            "shot_count": shot_count,
            "storyboard": storyboard,
        },
    )


def test_function_docstring_becomes_the_tool_description() -> None:
    """A plain function's docstring is what the model sees as the tool description."""
    backend = _toolset_backend()
    agent = AgentSpec(name="director", allowed_tools=("brand_palette", "shot_count", "storyboard"))
    request = TurnRequest(
        message="build the toolset",
        agent=agent,
        effective_tools=("brand_palette", "shot_count", "storyboard"),
    )
    toolset = backend._toolset_for(request)  # type: ignore[attr-defined]
    assert toolset is not None

    tools = toolset.tools  # dict: name -> Tool, with .description from the docstring
    assert set(tools) == {"brand_palette", "shot_count", "storyboard"}
    assert tools["brand_palette"].description == (
        "Return the house colour palette that all output must conform to."
    )
    assert tools["shot_count"].description == (
        "Return how many shots a scene should be broken into."
    )
    assert tools["storyboard"].description == (
        "Draft a three-beat storyboard from a one-line premise."
    )


def test_toolset_is_built_once_and_reused_for_the_same_permission_set() -> None:
    """The cache returns the SAME toolset object for an identical permitted-name tuple."""
    backend = _toolset_backend()
    agent = AgentSpec(name="director", allowed_tools=("brand_palette", "shot_count"))
    request = TurnRequest(
        message="turn one",
        agent=agent,
        effective_tools=("brand_palette", "shot_count"),
    )

    first = backend._toolset_for(request)  # type: ignore[attr-defined]
    second = backend._toolset_for(request)  # type: ignore[attr-defined]
    assert first is second, "identical permission set must reuse the cached toolset"


def test_a_different_permission_set_is_a_different_cache_entry() -> None:
    """P3-preserving cache key: a narrower permission set must NOT reuse the wider toolset."""
    backend = _toolset_backend()
    agent = AgentSpec(name="director", allowed_tools=("brand_palette", "shot_count"))

    wide = backend._toolset_for(  # type: ignore[attr-defined]
        TurnRequest(message="a", agent=agent, effective_tools=("brand_palette", "shot_count"))
    )
    narrow = backend._toolset_for(  # type: ignore[attr-defined]
        TurnRequest(message="b", agent=agent, effective_tools=("brand_palette",))
    )
    assert wide is not None and narrow is not None
    assert wide is not narrow
    assert set(narrow.tools) == {"brand_palette"}


# ======================================================================================
# 4. STREAMING — event order and result equality
# ======================================================================================


def _streaming_setup() -> tuple[FakeBackend, TurnRequest]:
    """A turn with one tool call and some text, shared by the streaming + parity tests."""
    agent = _colourist_agent(("brand_palette",))
    tool = ToolSpec(name="brand_palette", description="House palette.", cost_credits=0)
    run = Run(id="run:t4", agent_name=agent.name, max_iterations=20)
    backend = FakeBackend(
        [
            ScriptedTurn(tool_calls=(ScriptedToolCall("brand_palette"),)),
            ScriptedTurn(text="Signal amber #FFB300."),
        ],
        run=run,
        tools=(tool,),
    )
    request = TurnRequest(
        message="What is our signal colour?",
        agent=agent,
        skill=_load("colourist-palette.md"),
        effective_tools=("brand_palette",),
        run_id=run.id,
    )
    return backend, request


async def test_stream_event_order_and_tool_pairing() -> None:
    """TurnStarted first, TurnFinished last-and-once, tool call is started-then-finished."""
    backend, request = _streaming_setup()

    events = [event async for event in stream_turn(backend, request)]

    assert isinstance(events[0], TurnStarted), "TurnStarted must be first"
    assert isinstance(events[-1], TurnFinished), "TurnFinished must be last"
    finishes = [e for e in events if isinstance(e, TurnFinished)]
    assert len(finishes) == 1, "TurnFinished must appear exactly once"

    # The tool call is a started/finished pair, in that order, before the terminal event.
    started = next(i for i, e in enumerate(events) if isinstance(e, ToolCallStarted))
    finished = next(i for i, e in enumerate(events) if isinstance(e, ToolCallFinished))
    assert started < finished, "ToolCallStarted must precede ToolCallFinished"
    assert events[started].tool == "brand_palette"
    assert events[finished].tool == "brand_palette"
    assert events[finished].outcome is ToolOutcome.SUCCESS

    # Degraded (reconstructed) path is announced on the wire, not hidden.
    assert events[0].streaming is False

    # The text delta carries the assistant text.
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "FFB300" in text.upper()


async def test_turnfinished_result_equals_the_non_streaming_result_with_timing() -> None:
    """TurnFinished.result must equal what run_turn returns for the same request, timing included.

    Two separate backends (each drains its own script once) built from identical setup, so
    comparing the streamed result against the blocking result is apples-to-apples.
    """
    blocking_backend, blocking_request = _streaming_setup()
    blocking_result = await blocking_backend.run_turn(blocking_request)

    stream_backend, stream_request = _streaming_setup()
    events = [event async for event in stream_turn(stream_backend, stream_request)]
    streamed_result = next(e for e in events if isinstance(e, TurnFinished)).result

    # Full structural equality: text, tool_calls, tokens, iterations AND the timing model.
    assert streamed_result == blocking_result
    assert streamed_result.timing == blocking_result.timing
    assert streamed_result.tool_calls == blocking_result.tool_calls
    assert streamed_result.iterations == blocking_result.iterations


# ======================================================================================
# Bonus — the credit path a metered tool exercises, offline, as a user would hit it.
# ======================================================================================


async def test_metered_tool_reserves_and_captures_offline() -> None:
    """A metered tool call runs reserve→capture through a FakeBiller; the charge lands once."""
    agent = _colourist_agent(("generate_image",))
    tool = ToolSpec(name="generate_image", description="Costs credits.", cost_credits=35)
    biller = FakeBiller()
    run = Run(id="run:t5", agent_name=agent.name, max_iterations=20)
    backend = FakeBackend(
        [ScriptedTurn(tool_calls=(ScriptedToolCall("generate_image"),), text="done")],
        run=run,
        biller=biller,
        tools=(tool,),
    )
    request = TurnRequest(
        message="make it", agent=agent, effective_tools=("generate_image",), run_id=run.id
    )

    result = await backend.run_turn(request)
    assert biller.charged == 35
    assert biller.refunded == 0
    assert result.cost_credits == 35
