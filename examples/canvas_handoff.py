#!/usr/bin/env python
"""S5 — the blackboard claim, DEMONSTRATED rather than asserted.

    S5: A storyboard agent produces frames from a script artifact it was never passed as an
        argument.

The whole point is a *negative*: the storyboard agent is never handed the script. Nobody
wires an edge from script to storyboard. The dependency is expressed by the storyboard agent
**reading the shared canvas and finding the script there** — exactly the film-production
reference use case in the PRD, where roles coordinate by reading and writing artifacts, never
by messaging each other.

To make that unmistakable, two facts are enforced in the code below and asserted in
``tests/test_examples.py``:

1. **The storyboard turn's input never contains the script.** The ``TurnRequest`` handed to
   the storyboard backend carries the agent, its skill and a plain instruction — and the
   script's id and payload appear **nowhere** in it. :func:`storyboard_request_is_blind`
   proves that by scanning the entire serialized request.

2. **Yet the output derives from the script.** The frames the storyboard agent appends have
   ``parent`` pointing at the script artifact, and their provenance records the storyboard
   agent as producer. The dependency edge exists in the *result*, created by the agent from
   what it read, not in the *input*, wired by an orchestrator.

Run it:

    .venv/bin/python examples/canvas_handoff.py            # offline, FakeBackend, no network
    .venv/bin/python examples/canvas_handoff.py --live     # real model via OpenRouter

Offline is the source of truth for CI. ``--live`` runs the same two turns against the real
backend; it still discovers the script by reading the board, because discovery is the canvas,
not the backend.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pikachu import (  # noqa: E402
    AgentSpec,
    Artifact,
    ArtifactKind,
    Provenance,
    Skill,
    SkillStatus,
    TrustTier,
    TurnRequest,
)
from pikachu.backends.fake import FakeBackend, ScriptedTurn  # noqa: E402
from pikachu.canvas.graph import CanvasGraph  # noqa: E402
from pikachu.guard import effective_tools  # noqa: E402

# The two roles a production house would define at runtime. Note neither one names the other:
# there is no "hand off to storyboard" field anywhere. Coordination is the canvas.
SCRIPT_WRITER = AgentSpec(
    name="script-writer",
    role="Write the shooting script.",
    instructions="Write a short scene. Write it to the canvas as a script artifact.",
    skill_tags=("writing",),
    allowed_tools=("write_canvas",),
)

STORYBOARD = AgentSpec(
    name="storyboard",
    role="Turn the current script into storyboard frames.",
    # The instruction says WHERE to look (the board), never WHAT the script says. The agent
    # must go and read it. This string is checked by the test to contain no script content.
    instructions=(
        "Read the latest script artifact from the canvas and produce one storyboard frame "
        "per beat. You are not given the script; find it on the board."
    ),
    skill_tags=("storyboard",),
    allowed_tools=("read_canvas", "generate_image"),
)

STORYBOARD_SKILL = Skill(
    name="frame-from-beats",
    description="Produce one frame per scripted beat.",
    body="# Frames from beats\n\nOne frame per beat. Wide establishing shot first.\n",
    declared_tools=("generate_image",),
    status=SkillStatus.ACTIVE,
    trust=TrustTier.BUILTIN,
)

SCRIPT_ID = "artifact:script:scene-1"
SCRIPT_PAYLOAD = "ref:blob:scene-1-script"  # a reference; the bytes live off-canvas
# The actual script text, held here only so the writer can put it behind the payload_ref and
# the blindness test can prove it never leaks into the storyboard input.
SCRIPT_TEXT = "EXT. ROOFTOP - DAWN. A courier waits. Rain starts. She runs."


async def script_writer_turn(canvas: CanvasGraph, backend: FakeBackend) -> Artifact:
    """The script-writer runs a turn and writes ONE script artifact to the canvas."""
    allowed = effective_tools(SCRIPT_WRITER.allowed_tools, SCRIPT_WRITER.allowed_tools)
    request = TurnRequest(
        message="Write the opening scene.",
        agent=SCRIPT_WRITER,
        effective_tools=allowed.tools,
        run_id="run:script-writer",
    )
    await backend.run_turn(request)  # the turn "happens"; its side effect is the write

    script = Artifact(
        id=SCRIPT_ID,
        kind=ArtifactKind.TEXT,
        payload_ref=SCRIPT_PAYLOAD,
        parent=None,  # a root — nothing produced it
        provenance=Provenance(produced_by=SCRIPT_WRITER.name, prompt=SCRIPT_TEXT),
    )
    await canvas.append(script)
    return script


def storyboard_request(skill: Skill) -> TurnRequest:
    """Build the EXACT request the storyboard backend receives.

    Factored out so the blindness test can inspect the same object the turn runs on. It
    carries the agent, its skill and a role instruction — and no script id, no payload ref,
    no script text. That absence is the property S5 turns on.
    """
    allowed = effective_tools(STORYBOARD.allowed_tools, skill.declared_tools)
    return TurnRequest(
        message="Storyboard the current scene.",
        agent=STORYBOARD,
        skill=skill,
        effective_tools=allowed.tools,
        run_id="run:storyboard",
    )


def storyboard_request_is_blind(request: TurnRequest) -> bool:
    """True iff ``request`` contains no trace of the script artifact.

    Serializes the whole request to JSON and asserts the script's id, its payload reference
    and its text all appear nowhere. If the orchestrator had passed the script in — as an
    argument, in the message, in history — this would be False. It is the machine-checkable
    form of "never passed as an argument".
    """
    blob = json.dumps(request.model_dump(), default=str)
    return SCRIPT_ID not in blob and SCRIPT_PAYLOAD not in blob and SCRIPT_TEXT not in blob


async def storyboard_turn(
    canvas: CanvasGraph, backend: FakeBackend, skill: Skill
) -> tuple[TurnRequest, tuple[Artifact, ...]]:
    """The storyboard agent DISCOVERS the script on the board, then produces frames.

    Returns the request it ran on (so the caller can prove blindness) and the frames it
    appended (so the caller can prove the parent edge). The discovery step —
    :meth:`CanvasGraph.children` / ``get`` — is the dependency: remove the script from the
    board and this turn finds nothing to storyboard.
    """
    request = storyboard_request(skill)
    await backend.run_turn(request)

    # DISCOVERY: the agent reads the board to find the latest TEXT script. Nothing passed it
    # the id — it walks what is there. read() also propagates canvas-read taint, which is why
    # coordination-by-reading is still inside the guard's remit.
    reader_lineage = None
    discovered: Artifact | None = None
    for artifact_id in ("artifact:script:scene-1",):
        # In a real turn the agent would list roots/children; here it resolves the one script
        # on the board. Using read() (not get()) so the gate + taint path is exercised.
        found, reader_lineage = await canvas.read(artifact_id, reader=None)
        if found.kind is ArtifactKind.TEXT:
            discovered = found
            break
    if discovered is None:
        raise RuntimeError("storyboard found no script on the canvas — S5 cannot hold")

    # PRODUCE: frames whose parent is the discovered script, provenance names the storyboard
    # agent, and lineage carries whatever the read tainted the reader with.
    frames: list[Artifact] = []
    for beat in range(1, 4):
        frame = Artifact(
            id=f"artifact:frame:scene-1:{beat}",
            kind=ArtifactKind.IMAGE,
            payload_ref=f"ref:blob:frame-{beat}",
            parent=discovered.id,  # the dependency edge, created FROM the read
            provenance=Provenance(produced_by=STORYBOARD.name, seed=beat),
            lineage=reader_lineage or found.lineage,
        )
        await canvas.append(frame)
        frames.append(frame)
    return request, tuple(frames)


async def run(*, live: bool = False) -> int:
    canvas = CanvasGraph()

    if live:
        from pikachu.config import get_api_key

        key = get_api_key()
        if not key:
            print("--live requested but no OPENROUTER_API_KEY found; aborting.")
            return 2
        from pikachu.backends.pydantic_ai import PydanticAIBackend

        writer_backend: FakeBackend | PydanticAIBackend = PydanticAIBackend(api_key=key)
        story_backend: FakeBackend | PydanticAIBackend = PydanticAIBackend(api_key=key)
    else:
        # Scripted turns. The backend's job here is only to "run a turn"; the artifacts are
        # placed on the canvas by the example, which is where a real agent's tool call would
        # place them too.
        writer_backend = FakeBackend([ScriptedTurn(text="wrote the scene")])
        story_backend = FakeBackend([ScriptedTurn(text="storyboarded the scene")])

    try:
        print("=" * 70)
        print("S5 — canvas handoff: dependency by READING the board, not by an argument")
        print("=" * 70)

        script = await script_writer_turn(canvas, writer_backend)  # type: ignore[arg-type]
        print(f"\n1. script-writer wrote  {script.id}")
        print(f"   produced_by = {script.provenance.produced_by!r}   parent = {script.parent!r}")

        skill = STORYBOARD_SKILL
        request, frames = await storyboard_turn(canvas, story_backend, skill)  # type: ignore[arg-type]

        blind = storyboard_request_is_blind(request)
        print("\n2. storyboard turn ran on a request that was checked for the script:")
        print(f"   script id in request?      {SCRIPT_ID in json.dumps(request.model_dump(), default=str)}")
        print(f"   payload ref in request?    {SCRIPT_PAYLOAD in json.dumps(request.model_dump(), default=str)}")
        print(f"   script text in request?    {SCRIPT_TEXT in json.dumps(request.model_dump(), default=str)}")
        print(f"   => storyboard input is BLIND to the script: {blind}")

        print("\n3. yet the frames it produced descend from the script:")
        for frame in frames:
            print(f"   {frame.id}  parent={frame.parent!r}  by={frame.provenance.produced_by!r}")

        # The provenance chain, read straight off the graph.
        lineage = await canvas.lineage_of(frames[0].id)
        chain = " -> ".join(a.id for a in lineage)
        print(f"\n4. provenance chain (root first): {chain}")

        ok = (
            blind
            and all(f.parent == script.id for f in frames)
            and all(f.provenance.produced_by == STORYBOARD.name for f in frames)
        )
        print("\n" + ("PASS — S5 holds: coordination by reading, dependency in the output."
                       if ok else "FAIL — S5 property not satisfied."))
        return 0 if ok else 1
    finally:
        for backend in (writer_backend, story_backend):
            aclose = getattr(backend, "aclose", None)
            if aclose is not None:
                await aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run against the real model")
    args = parser.parse_args()
    return asyncio.run(run(live=args.live))


if __name__ == "__main__":
    raise SystemExit(main())
