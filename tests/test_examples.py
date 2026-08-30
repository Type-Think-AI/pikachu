"""Every example runs on FakeBackend in CI, offline — and the S4/S5 properties are asserted.

An example that only works live is an example that rots: the first refactor that breaks it
goes unnoticed until someone spends money running it by hand. So these tests drive the exact
offline code paths the example ``main()`` uses, with no network (conftest's autouse
``_no_network`` hard-fails any socket) and no key.

Two of these tests are not "does it run" but "does the CLAIM hold":

* ``test_s5_storyboard_input_is_blind_yet_output_derives`` asserts the storyboard turn's
  input never contained the script artifact, and yet the frames it produced descend from it.
  That negative-plus-positive pair IS success criterion S5.
* ``test_s4_agent_created_and_invoked_without_agent_specific_import`` asserts the agent was
  created and invoked with no import of any agent-specific module — the machine-checkable form
  of "no code change, no deploy".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The examples resolve `pikachu` via src/ on their own sys.path insert, but the test process
# needs the examples package importable too.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples import canvas_handoff, create_agent_at_runtime  # noqa: E402
from pikachu import ArtifactKind  # noqa: E402
from pikachu.backends.fake import FakeBackend, ScriptedTurn  # noqa: E402
from pikachu.canvas.graph import CanvasGraph  # noqa: E402

# asyncio_mode = "auto" (pyproject) collects every ``async def test_*`` as an asyncio test,
# so no per-test marker is needed.


# --------------------------------------------------------------------------------------
# The examples run end to end, offline.
# --------------------------------------------------------------------------------------


async def test_canvas_handoff_example_runs_offline() -> None:
    """The whole S5 example returns 0 (PASS) on FakeBackend with no network."""
    rc = await canvas_handoff.run(live=False)
    assert rc == 0


async def test_create_agent_example_runs_offline() -> None:
    """The whole S4 example returns 0 (PASS) on FakeBackend with no network."""
    rc = await create_agent_at_runtime.run(live=False)
    assert rc == 0


# --------------------------------------------------------------------------------------
# S5 — the blackboard property, asserted directly.
# --------------------------------------------------------------------------------------


async def test_s5_storyboard_input_is_blind_yet_output_derives() -> None:
    """S5: the storyboard agent was never passed the script, yet its frames descend from it.

    This drives the same steps as the example, but inspects the intermediate request and the
    produced frames rather than only the exit code — so the property is pinned, not merely
    the happy path.
    """
    canvas = CanvasGraph()
    writer = FakeBackend([ScriptedTurn(text="wrote the scene")])
    story = FakeBackend([ScriptedTurn(text="storyboarded the scene")])

    # 1. script-writer produces the script.
    script = await canvas_handoff.script_writer_turn(canvas, writer)
    assert script.id == canvas_handoff.SCRIPT_ID
    assert script.provenance.produced_by == canvas_handoff.SCRIPT_WRITER.name
    assert script.parent is None  # a root; nothing produced it

    # 2. storyboard turn — capture the exact request it ran on and the frames it produced.
    request, frames = await canvas_handoff.storyboard_turn(
        canvas, story, canvas_handoff.STORYBOARD_SKILL
    )

    # ★ THE NEGATIVE: the input the storyboard backend received contains no trace of the
    #   script — not its id, not its payload ref, not its text. It was never passed the
    #   artifact as an argument.
    assert canvas_handoff.storyboard_request_is_blind(request), (
        "storyboard request leaked the script — S5's 'never passed as an argument' is violated"
    )
    # Belt-and-braces: the raw request really has no field carrying it.
    dumped = str(request.model_dump())
    assert canvas_handoff.SCRIPT_ID not in dumped
    assert canvas_handoff.SCRIPT_PAYLOAD not in dumped
    assert canvas_handoff.SCRIPT_TEXT not in dumped
    assert request.skill is not None and request.skill.name == "frame-from-beats"

    # ★ THE POSITIVE: the output derives from the script anyway. Every frame's parent is the
    #   script, and provenance names the storyboard agent as producer.
    assert frames, "storyboard produced no frames"
    for frame in frames:
        assert frame.kind is ArtifactKind.IMAGE
        assert frame.parent == script.id, "frame does not descend from the script"
        assert frame.provenance.produced_by == canvas_handoff.STORYBOARD.name

    # 3. The dependency is real on the graph: the script's descendants ARE the frames, and
    #    each frame's lineage chain walks back to the script.
    descendants = await canvas.descendants(script.id)
    assert {a.id for a in descendants} == {f.id for f in frames}
    chain = await canvas.lineage_of(frames[0].id)
    assert chain[0].id == script.id  # root first
    assert chain[-1].id == frames[0].id


async def test_s5_removing_the_script_breaks_the_handoff() -> None:
    """Control: with no script on the board, discovery finds nothing and the turn cannot run.

    This is what proves the dependency is the READ, not a wired edge: an empty board makes the
    storyboard turn fail because there is nothing to read — exactly the failure a real missing
    dependency would produce.
    """
    canvas = CanvasGraph()  # empty — the writer never ran
    story = FakeBackend([ScriptedTurn(text="storyboarded the scene")])
    with pytest.raises((RuntimeError, KeyError)):
        await canvas_handoff.storyboard_turn(canvas, story, canvas_handoff.STORYBOARD_SKILL)


# --------------------------------------------------------------------------------------
# S4 — runtime creation, asserted directly.
# --------------------------------------------------------------------------------------


async def test_s4_agent_created_and_invoked_without_agent_specific_import() -> None:
    """S4: create an AgentSpec at runtime, register it, invoke it by name — no agent module.

    The machine-checkable claim is that making the agent exist and running it imports no
    agent-specific code. We assert two things:

    * the modules loaded to do this are the SDK's generic surface (registry, types, backend,
      guard) — there is no ``examples.agents`` or ``pikachu.agents.colourist`` to import,
      because the agent is data;
    * the round trip works: created, listed, fetched by name, invoked, produced output.
    """
    from pikachu.discovery.registry import AgentRegistry

    registry = AgentRegistry()

    # Creation is pure data — a constructor call, not a subclass or a decorator.
    colourist = registry.create(create_agent_at_runtime.user_creates_colourist())
    retoucher = registry.create(create_agent_at_runtime.user_creates_retoucher())

    # No agent-specific module was needed to define them.
    assert not any(
        name.startswith(("examples.agents", "pikachu.agents"))
        for name in sys.modules
    ), "an agent-specific module was imported — S4 requires the agent to be pure data"

    # Six declarative fields, nothing more.
    assert colourist.name == "colourist"
    assert retoucher.name == "retoucher"

    # ★ Partition boundary is visible and disjoint.
    assert set(colourist.skill_tags).isdisjoint(retoucher.skill_tags)

    # Invoke BY NAME through the generic backend seam.
    backend = FakeBackend([ScriptedTurn(text="graded to the house palette")])
    out = await create_agent_at_runtime.invoke_by_name(
        registry, "colourist", backend, "Grade this still."
    )
    assert out == "graded to the house palette"

    # The registry resolved the spec purely from the string name.
    assert registry.get("colourist") is colourist


async def test_s4_second_agent_has_a_different_partition() -> None:
    """The two runtime agents occupy disjoint skill partitions, making the boundary explicit."""
    from pikachu.discovery.registry import AgentRegistry

    registry = AgentRegistry()
    a = registry.create(create_agent_at_runtime.user_creates_colourist())
    b = registry.create(create_agent_at_runtime.user_creates_retoucher())
    assert a.skill_tags == ("colour", "grade")
    assert b.skill_tags == ("retouch", "cleanup")
    assert set(a.skill_tags) & set(b.skill_tags) == set()
    # Both are live and by-name resolvable.
    assert {s.name for s in registry.list()} == {"colourist", "retoucher"}
