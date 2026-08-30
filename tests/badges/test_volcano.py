"""Volcano badge — Blaine, Fire: it actually runs.

A full multi-iteration turn runs end to end through the backend seam and returns a coherent
TurnResult. This is a genuine exercise of the seam, not a stub assertion: the turn thinks,
calls a free tool and a metered tool through the real reserve→capture credit path, produces
an artifact, reports cache-bearing token counts, and the result is checked for internal
coherence.

``tests/fakes.py`` (Lane C) does not exist yet, so the request/run scaffolding is built
locally.
"""

from __future__ import annotations

import pytest

from pikachu.backends import FakeBackend, FakeBiller, ScriptedToolCall, ScriptedTurn
from pikachu.core.protocols import AgentBackend
from pikachu.core.types import (
    AgentSpec,
    Artifact,
    ArtifactKind,
    Provenance,
    Run,
    ToolOutcome,
    ToolSpec,
    TurnRequest,
)

pytestmark = pytest.mark.volcano


@pytest.mark.volcano
async def test_full_turn_runs_end_to_end_on_fake_backend() -> None:
    agent = AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        instructions="Match the brand palette.",
        allowed_tools=("read_canvas", "generate_image"),
    )
    run = Run(id="run:volcano", agent_name="colourist", max_iterations=8)
    biller = FakeBiller()
    generate = ToolSpec(name="generate_image", description="Costs credits.", cost_credits=35)
    read = ToolSpec(name="read_canvas", cost_credits=0)

    graded = Artifact(
        id="art:graded",
        kind=ArtifactKind.IMAGE,
        payload_ref="ref://art:graded",
        provenance=Provenance(prompt="grade to house look", produced_by="colourist"),
    )

    backend = FakeBackend(
        script=[
            # 1. Read the current frame (free tool).
            ScriptedTurn(
                text="reading the current frame",
                tool_calls=(ScriptedToolCall("read_canvas"),),
                input_tokens=1200,
                cache_read_tokens=800,
                output_tokens=6,
            ),
            # 2. Generate the graded still (metered — exercises reserve→capture).
            ScriptedTurn(
                text="grading to the house palette",
                tool_calls=(ScriptedToolCall("generate_image", outcome=ToolOutcome.SUCCESS),),
                artifacts=(graded,),
                cache_read_tokens=1200,
                input_tokens=300,
                output_tokens=40,
            ),
            # 3. Wrap up.
            ScriptedTurn(text="graded and posted to the canvas", output_tokens=12),
        ],
        run=run,
        biller=biller,
        tools=(read, generate),
    )

    # The guard already narrowed the toolset — the backend is downstream of it.
    request = TurnRequest(
        message="grade this still to the house look",
        agent=agent,
        effective_tools=("read_canvas", "generate_image"),
        run_id="run:volcano",
    )

    # Precondition: it is a real backend.
    assert isinstance(backend, AgentBackend)

    result = await backend.run_turn(request)

    # --- It actually ran, all the way through. ---
    assert result.iterations == 3
    assert result.text == (
        "reading the current frame\n"
        "grading to the house palette\n"
        "graded and posted to the canvas"
    )

    # --- The tools it used are exactly the narrowed set, in order, nothing widened. ---
    used = tuple(c["tool"] for c in result.tool_calls)
    assert used == ("read_canvas", "generate_image")
    assert set(used) <= set(request.effective_tools)

    # --- The artifact came through with provenance intact. ---
    assert len(result.artifacts) == 1
    assert result.artifacts[0].id == "art:graded"
    assert result.artifacts[0].provenance.produced_by == "colourist"

    # --- The credit path ran once, and the metered call was charged exactly once. ---
    assert len(biller.reservations) == 1
    (rid,) = biller.reservations
    assert biller.captured == {rid: ToolOutcome.SUCCESS}
    assert biller.released == set()
    assert biller.charged == 35
    assert result.cost_credits == 35

    # --- Cache metrics are real and coherent. ---
    assert result.cache_read_tokens == 2000
    assert result.input_tokens == 1500
    assert result.cache_hit_ratio > 0.0
    assert result.cache_hit_ratio == pytest.approx(2000 / (1500 + 2000))

    # --- The backend recorded what it was handed. ---
    assert backend.received_requests == [request]
    assert backend.received_requests[0].effective_tools == ("read_canvas", "generate_image")
