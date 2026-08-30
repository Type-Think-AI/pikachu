"""Lane F tests — the backend seam and FakeBackend.

``tests/fakes.py`` (Lane C) does not exist while this lane is written, so everything these
tests need is defined locally here. When Lane C's fakes land, the shared helpers can be
lifted out; nothing here depends on them.

House rules: no network (enforced by conftest's autouse fixture), deterministic, no
wall-clock in any assertion.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pikachu.backends import (
    BaseBackend,
    FakeBackend,
    FakeBiller,
    ScriptedToolCall,
    ScriptedTurn,
)
from pikachu.core.errors import BudgetExceeded, DoubleCaptureError
from pikachu.core.protocols import AgentBackend, Biller
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

# --------------------------------------------------------------------------------------
# Local fixtures (would move to tests/fakes.py once Lane C writes it)
# --------------------------------------------------------------------------------------


@pytest.fixture
def agent() -> AgentSpec:
    return AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        allowed_tools=("generate_image", "read_canvas"),
    )


@pytest.fixture
def run() -> Run:
    return Run(id="run:test", agent_name="colourist", max_iterations=5)


@pytest.fixture
def metered_tool() -> ToolSpec:
    return ToolSpec(name="generate_image", description="Costs credits.", cost_credits=35)


def make_request(
    *,
    agent: AgentSpec,
    effective_tools: tuple[str, ...],
    message: str = "grade this frame",
    run_id: str = "run:test",
) -> TurnRequest:
    """Build a TurnRequest as the guard would hand it to a backend.

    ``effective_tools`` is supplied explicitly and is treated as already narrowed — this
    lane never computes it, and neither does the backend under test.
    """
    return TurnRequest(
        message=message,
        agent=agent,
        effective_tools=effective_tools,
        run_id=run_id,
    )


def image_artifact(artifact_id: str) -> Artifact:
    return Artifact(id=artifact_id, kind=ArtifactKind.IMAGE, payload_ref=f"ref://{artifact_id}")


# --------------------------------------------------------------------------------------
# Protocol conformance — feeds Cascade
# --------------------------------------------------------------------------------------


def test_fake_backend_is_agent_backend() -> None:
    """FakeBackend satisfies isinstance against the runtime_checkable protocol."""
    backend = FakeBackend()
    assert isinstance(backend, AgentBackend)
    assert isinstance(backend, BaseBackend)


def test_fake_biller_is_biller() -> None:
    """The bundled biller satisfies the Biller protocol so the credit path is real."""
    assert isinstance(FakeBiller(), Biller)


# --------------------------------------------------------------------------------------
# The seam stays one method wide
# --------------------------------------------------------------------------------------


def test_agent_backend_protocol_has_exactly_one_method() -> None:
    """The seam is one method on purpose — guard against a second one creeping in."""
    members = {
        name
        for name in vars(AgentBackend).get("__protocol_attrs__", set())
        if not name.startswith("_")
    }
    # __protocol_attrs__ is populated by typing for runtime_checkable protocols.
    assert members == {"run_turn"}, members


# --------------------------------------------------------------------------------------
# Full multi-iteration turn (unit-level; the badge version lives in tests/badges)
# --------------------------------------------------------------------------------------


async def test_multi_iteration_turn_folds_into_one_result(agent: AgentSpec, run: Run) -> None:
    backend = FakeBackend(
        script=[
            ScriptedTurn(text="thinking", output_tokens=3, input_tokens=100),
            ScriptedTurn(
                text="calling tool",
                tool_calls=(ScriptedToolCall("read_canvas"),),
                output_tokens=2,
                cache_read_tokens=50,
            ),
            ScriptedTurn(
                text="done",
                artifacts=(image_artifact("art-1"),),
                output_tokens=5,
            ),
        ],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=("read_canvas", "generate_image"))
    result = await backend.run_turn(req)

    assert result.text == "thinking\ncalling tool\ndone"
    assert result.iterations == 3
    assert len(result.artifacts) == 1 and result.artifacts[0].id == "art-1"
    assert result.tool_calls == ({"tool": "read_canvas", "outcome": "success", "args": {}},)
    assert result.input_tokens == 100
    assert result.output_tokens == 10
    assert result.cache_read_tokens == 50


# --------------------------------------------------------------------------------------
# It records what it was handed — including the narrowed toolset
# --------------------------------------------------------------------------------------


async def test_records_every_request_it_received(agent: AgentSpec, run: Run) -> None:
    backend = FakeBackend(script=[ScriptedTurn(text="ok")], run=run)
    req = make_request(agent=agent, effective_tools=("read_canvas",))
    await backend.run_turn(req)
    assert backend.received_requests == [req]
    assert backend.received_requests[0].effective_tools == ("read_canvas",)


async def test_backend_never_widens_effective_tools(agent: AgentSpec, run: Run) -> None:
    """What it was given is what it used — a scripted call outside the set is refused.

    The agent's allowlist is ('generate_image', 'read_canvas') but the guard narrowed the
    turn to ('read_canvas',). A script that calls generate_image must NOT be silently
    executed against the wider allowlist — that would defeat the permission layer.
    """
    backend = FakeBackend(
        script=[ScriptedTurn(tool_calls=(ScriptedToolCall("generate_image"),))],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=("read_canvas",))
    with pytest.raises(BudgetExceeded) as exc:
        await backend.run_turn(req)
    assert exc.value.limit_kind == "tool_authority"


async def test_used_tools_are_a_subset_of_effective_tools(agent: AgentSpec, run: Run) -> None:
    backend = FakeBackend(
        script=[
            ScriptedTurn(tool_calls=(ScriptedToolCall("read_canvas"),)),
            ScriptedTurn(tool_calls=(ScriptedToolCall("generate_image"),)),
        ],
        run=run,
    )
    effective = ("read_canvas", "generate_image")
    req = make_request(agent=agent, effective_tools=effective)
    result = await backend.run_turn(req)
    used = {c["tool"] for c in result.tool_calls}
    assert used <= set(effective)


# --------------------------------------------------------------------------------------
# The credit path: reserve -> capture / release
# --------------------------------------------------------------------------------------


async def test_metered_success_reserves_then_captures_once(
    agent: AgentSpec, run: Run, metered_tool: ToolSpec
) -> None:
    biller = FakeBiller()
    backend = FakeBackend(
        script=[ScriptedTurn(tool_calls=(ScriptedToolCall("generate_image"),))],
        run=run,
        biller=biller,
        tools=(metered_tool,),
    )
    req = make_request(agent=agent, effective_tools=("generate_image",))
    result = await backend.run_turn(req)

    assert len(biller.reservations) == 1
    (rid,) = biller.reservations
    assert biller.captured == {rid: ToolOutcome.SUCCESS}
    assert biller.released == set()
    assert biller.charged == 35
    assert result.cost_credits == 35


async def test_metered_failure_releases_and_refund_holds(
    agent: AgentSpec, run: Run, metered_tool: ToolSpec
) -> None:
    biller = FakeBiller()
    backend = FakeBackend(
        script=[
            ScriptedTurn(
                tool_calls=(ScriptedToolCall("generate_image", outcome=ToolOutcome.FAILED),)
            )
        ],
        run=run,
        biller=biller,
        tools=(metered_tool,),
    )
    req = make_request(agent=agent, effective_tools=("generate_image",))
    await backend.run_turn(req)

    (rid,) = biller.reservations
    assert biller.captured == {}
    assert biller.released == {rid}
    assert biller.charged == 0
    assert biller.refunded == 35


async def test_free_tool_never_touches_the_biller(agent: AgentSpec, run: Run) -> None:
    biller = FakeBiller()
    backend = FakeBackend(
        script=[ScriptedTurn(tool_calls=(ScriptedToolCall("read_canvas"),))],
        run=run,
        biller=biller,
        tools=(ToolSpec(name="read_canvas", cost_credits=0),),
    )
    req = make_request(agent=agent, effective_tools=("read_canvas",))
    await backend.run_turn(req)
    assert biller.reservations == {}
    assert biller.charged == 0


# --------------------------------------------------------------------------------------
# Idempotency and the double-capture guard
# --------------------------------------------------------------------------------------


async def test_capture_is_idempotent_on_same_outcome() -> None:
    biller = FakeBiller()
    resv = await biller.reserve(run_id="run:test", tool="generate_image", amount=35)
    await biller.capture(resv.id, outcome=ToolOutcome.SUCCESS)
    await biller.capture(resv.id, outcome=ToolOutcome.SUCCESS)  # resume-safe no-op
    assert biller.charged == 35


async def test_second_capture_of_same_reservation_raises_double_capture() -> None:
    biller = FakeBiller()
    resv = await biller.reserve(run_id="run:test", tool="generate_image", amount=35)
    await biller.capture(resv.id, outcome=ToolOutcome.SUCCESS)
    with pytest.raises(DoubleCaptureError) as exc:
        await biller.capture(resv.id, outcome=ToolOutcome.INTERRUPTED)
    assert exc.value.reservation_id == resv.id


async def test_capture_after_release_raises_double_capture() -> None:
    biller = FakeBiller()
    resv = await biller.reserve(run_id="run:test", tool="generate_image", amount=35)
    await biller.release(resv.id)
    with pytest.raises(DoubleCaptureError):
        await biller.capture(resv.id, outcome=ToolOutcome.SUCCESS)


async def test_pre_captured_reservation_is_not_recharged_on_resume(
    agent: AgentSpec, metered_tool: ToolSpec
) -> None:
    """Resume must not re-capture a reservation the Run already recorded as captured.

    The reservation id is deterministic (resv:<run_id>:<tool>:<seq>), so a resume can name
    the already-captured one in Run.captured_reservations and the backend skips it.
    """
    biller = FakeBiller()
    already = "resv:run:test:generate_image:0"
    run = Run(
        id="run:test",
        agent_name="colourist",
        max_iterations=5,
        captured_reservations=frozenset({already}),
    )
    backend = FakeBackend(
        script=[ScriptedTurn(tool_calls=(ScriptedToolCall("generate_image"),))],
        run=run,
        biller=biller,
        tools=(metered_tool,),
    )
    req = make_request(agent=agent, effective_tools=("generate_image",))
    await backend.run_turn(req)
    assert biller.charged == 0  # already captured on the prior run — not charged again


# --------------------------------------------------------------------------------------
# INTERRUPTED is representable and does not silently release
# --------------------------------------------------------------------------------------


async def test_interrupted_outcome_captures_never_releases(
    agent: AgentSpec, run: Run, metered_tool: ToolSpec
) -> None:
    """INTERRUPTED means a paid side effect MAY have happened — capture, do not release.

    Collapsing it into FAILED (and releasing) is exactly how a double-charge on resume gets
    written: the release refunds a charge whose side effect already fired.
    """
    biller = FakeBiller()
    backend = FakeBackend(
        script=[
            ScriptedTurn(
                text="side effect may have fired",
                tool_calls=(
                    ScriptedToolCall("generate_image", outcome=ToolOutcome.INTERRUPTED),
                ),
            )
        ],
        run=run,
        biller=biller,
        tools=(metered_tool,),
    )
    req = make_request(agent=agent, effective_tools=("generate_image",))
    result = await backend.run_turn(req)

    (rid,) = biller.reservations
    assert biller.captured == {rid: ToolOutcome.INTERRUPTED}
    assert biller.released == set()
    assert biller.charged == 35
    assert result.tool_calls[0]["outcome"] == "interrupted"


# --------------------------------------------------------------------------------------
# Budgets bite
# --------------------------------------------------------------------------------------


async def test_exceeding_max_iterations_raises_budget_exceeded(agent: AgentSpec) -> None:
    run = Run(id="run:test", agent_name="colourist", max_iterations=2)
    backend = FakeBackend(
        script=[ScriptedTurn(text="1"), ScriptedTurn(text="2"), ScriptedTurn(text="3")],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=())
    with pytest.raises(BudgetExceeded) as exc:
        await backend.run_turn(req)
    assert exc.value.limit_kind == "iterations"


async def test_at_max_iterations_is_allowed(agent: AgentSpec) -> None:
    run = Run(id="run:test", agent_name="colourist", max_iterations=2)
    backend = FakeBackend(
        script=[ScriptedTurn(text="1"), ScriptedTurn(text="2")],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=())
    result = await backend.run_turn(req)
    assert result.iterations == 2


# --------------------------------------------------------------------------------------
# Cache metrics are exercisable
# --------------------------------------------------------------------------------------


async def test_cache_hit_ratio_is_exercisable(agent: AgentSpec, run: Run) -> None:
    backend = FakeBackend(
        script=[ScriptedTurn(input_tokens=250, cache_read_tokens=750)],
        run=run,
    )
    req = make_request(agent=agent, effective_tools=())
    result = await backend.run_turn(req)
    assert result.cache_read_tokens == 750
    assert result.cache_hit_ratio == pytest.approx(0.75)


# --------------------------------------------------------------------------------------
# Determinism — byte-identical results across runs
# --------------------------------------------------------------------------------------


def _script() -> list[ScriptedTurn]:
    # Pin the artifact's provenance timestamp: Artifact.provenance.at defaults to utcnow(),
    # so a wall-clock value would leak into the byte comparison and make the determinism
    # check test the clock, not the backend. Fixed input in, byte-identical output out.
    fixed = Provenance(produced_by="colourist", at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    artifact = Artifact(
        id="art-9", kind=ArtifactKind.IMAGE, payload_ref="ref://art-9", provenance=fixed
    )
    return [
        ScriptedTurn(text="a", input_tokens=100, cache_read_tokens=20),
        ScriptedTurn(
            text="b",
            tool_calls=(ScriptedToolCall("generate_image"),),
            artifacts=(artifact,),
            output_tokens=4,
        ),
        ScriptedTurn(text="c", output_tokens=1),
    ]


async def test_same_script_produces_identical_results(
    agent: AgentSpec, metered_tool: ToolSpec
) -> None:
    def build() -> FakeBackend:
        return FakeBackend(
            script=_script(),
            run=Run(id="run:det", agent_name="colourist", max_iterations=10),
            biller=FakeBiller(),
            tools=(metered_tool,),
        )

    req = make_request(agent=agent, effective_tools=("generate_image",), run_id="run:det")
    first = await build().run_turn(req)
    second = await build().run_turn(req)

    # model_dump_json is byte-level: identical bytes means identical result.
    assert first.model_dump_json() == second.model_dump_json()
    assert first.cost_credits == second.cost_credits == 35
