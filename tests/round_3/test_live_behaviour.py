"""Round 3 — the OFFLINE half of the live-behaviour lane.

These tests never touch the network (the parent ``tests/conftest.py`` autouse socket block is
inherited and not overridden here). Their job is to nail down, deterministically, exactly what
``FakeBackend`` *claims* about tool-calling — the claim that ``scripts/round3_live.py`` then
checks against the real model. If the fake and the live model disagree, the disagreement is the
finding, and it can only be stated if the fake's behaviour is pinned first, here.

What is deliberately NOT tested here:

* **Tool SELECTION.** The fake is scripted — it "calls" whatever the script says. So the fake
  can prove tool *plumbing* (a called tool must be authorized, its output is threaded into the
  result, the credit path runs) but it *cannot* prove the model *chooses* to call the right
  tool. That gap — plumbing vs behaviour — is the whole reason a live lane exists, and it is
  asserted as a property of the fake below so the live doc can point at it.

The single ``@pytest.mark.live`` test is skipped by default (see ``conftest.py``); it exists to
show the live path is wired and skips cleanly with no key, per the round's contract.
"""

from __future__ import annotations

import pytest

from pikachu.backends.fake import FakeBackend, ScriptedToolCall, ScriptedTurn
from pikachu.core.types import (
    AgentSpec,
    Run,
    Skill,
    SkillStatus,
    ToolOutcome,
    ToolSpec,
    TrustTier,
    TurnRequest,
    TurnTiming,
)
from pikachu.guard import effective_tools
from pikachu.skills.loader import load_skill

# The palette value the live model must quote back if it truly called the tool. Single source
# of truth shared with scripts/round3_live.py's expectation.
HOUSE_AMBER = "#FFB300"

COLOURIST_SKILL_DOC = """---
name: house-colourist
description: Grade every generated frame to the house colour palette.
allowed-tools: [brand_palette, generate_image]
---

# House colourist

You grade frames to the house look. The canonical palette is NOT in your head — it is returned
by the `brand_palette` tool, and it is the only authority on colour. Whenever a colour question
arises, CALL `brand_palette` and quote its hex values exactly. Never invent a palette.
"""


# ---------------------------------------------------------------------------------------
# 1. The colourist skill authors correctly and declares its tool
# ---------------------------------------------------------------------------------------


def test_colourist_skill_loads_and_declares_brand_palette() -> None:
    """The skill a human authored parses, is trusted enough to contribute a tool, and the
    tool it declares is exactly the one the live run expects the model to call."""
    skill = load_skill(
        COLOURIST_SKILL_DOC, trust=TrustTier.BUILTIN, source="repo:round3"
    )
    assert skill.name == "house-colourist"
    assert "brand_palette" in skill.declared_tools
    assert skill.trust.may_contribute_tools
    # The body must actually instruct the model to use the tool — a skill that declares a tool
    # but never tells the model to use it would make tool SELECTION untestable.
    assert "brand_palette" in skill.body


# ---------------------------------------------------------------------------------------
# 2. The guard narrows the declared+allowlist to the effective set the backend receives
# ---------------------------------------------------------------------------------------


def test_guard_grants_brand_palette_when_allowlisted() -> None:
    """With ``brand_palette`` on the fixed allowlist, the guard passes it through to the
    backend — this is the grant the live run relies on."""
    got = effective_tools(
        fixed_allowlist=("brand_palette", "generate_image", "read_canvas"),
        declared=("brand_palette", "generate_image"),
    )
    assert "brand_palette" in got.tools


def test_guard_omits_brand_palette_when_not_allowlisted() -> None:
    """Remove it from the allowlist and the guard drops it, no matter that the skill declared
    it — authority is the allowlist, never the artifact. The live run's degraded case."""
    got = effective_tools(
        fixed_allowlist=("generate_image", "read_canvas"),
        declared=("brand_palette", "generate_image"),
    )
    assert "brand_palette" not in got.tools


# ---------------------------------------------------------------------------------------
# 3. FakeBackend tool-call PLUMBING — the offline claim the live run is checked against
# ---------------------------------------------------------------------------------------


async def test_fake_threads_a_scripted_tool_call_into_the_result() -> None:
    """When scripted to call ``brand_palette``, the fake records that call and completes.

    This is the fake's tool-calling contract. Note what it does and does NOT prove: the fake
    calls the tool because the SCRIPT said so, not because a model chose to. The live run must
    supply the "chose to" half.
    """
    run = Run(id="run:r3-fake", agent_name="house-colourist", max_iterations=5)
    fake = FakeBackend(
        [
            ScriptedTurn(
                tool_calls=(ScriptedToolCall("brand_palette", ToolOutcome.SUCCESS),),
            ),
            ScriptedTurn(text=f"The signal amber is {HOUSE_AMBER}."),
        ],
        run=run,
        tools=(ToolSpec(name="brand_palette", cost_credits=0),),
    )
    req = TurnRequest(
        message="What is the house signal amber?",
        agent=AgentSpec(name="house-colourist", allowed_tools=("brand_palette",)),
        effective_tools=("brand_palette",),
        run_id=run.id,
    )
    result = await fake.run_turn(req)

    called = {c["tool"] for c in result.tool_calls}
    assert "brand_palette" in called
    assert HOUSE_AMBER in result.text
    assert result.iterations == 2


async def test_fake_refuses_a_tool_outside_the_effective_set() -> None:
    """The fake NEVER widens: a scripted call to a tool the guard did not grant is refused,
    not silently executed. This is the plumbing-side guarantee that matches the guard tests."""
    from pikachu.core.errors import BudgetExceeded

    run = Run(id="run:r3-refuse", agent_name="house-colourist", max_iterations=5)
    fake = FakeBackend(
        [ScriptedTurn(tool_calls=(ScriptedToolCall("brand_palette", ToolOutcome.SUCCESS),))],
        run=run,
    )
    req = TurnRequest(
        message="grade this",
        agent=AgentSpec(name="house-colourist", allowed_tools=("generate_image",)),
        effective_tools=("generate_image",),  # brand_palette NOT granted
        run_id=run.id,
    )
    with pytest.raises(BudgetExceeded):
        await fake.run_turn(req)


def test_fake_cannot_model_selection__the_reason_live_exists() -> None:
    """Assert, in code, the exact gap between the fake and reality.

    ``ScriptedToolCall`` takes a hard-coded ``tool`` name — there is no field on it, and no
    field on ``ScriptedTurn``, that expresses "the model decided". Selection is not part of the
    fake's contract. This test documents that as a property so the live verdict can cite it:
    the fake models *plumbing* (authorized → threaded → metered), never *choice*.
    """
    call = ScriptedToolCall("brand_palette")
    # The set of decision inputs a real model uses (skill body, tool descriptions, the task)
    # is nowhere in the scripted call's fields.
    fields = set(ScriptedToolCall.__dataclass_fields__)
    assert fields == {"tool", "outcome", "args"}
    assert call.tool == "brand_palette"  # fixed at authoring time, not chosen at run time


# ---------------------------------------------------------------------------------------
# 4. The cost estimator the live script prints — arithmetic pinned offline
# ---------------------------------------------------------------------------------------


def test_cost_estimate_matches_published_pricing() -> None:
    """The live script bills from OpenRouter's published $/MTok. Pin the arithmetic here so a
    wrong cost number is caught offline, not discovered after spending real money."""
    from scripts.round3_live import estimate_cost_usd

    # 1,000 input, 100 output, 0 cache. Prices: 0.75 / 3.75 / 0.075 $/MTok.
    cost = estimate_cost_usd(input_tokens=1000, output_tokens=100, cache_read_tokens=0)
    expected = (1000 * 0.75 + 100 * 3.75 + 0 * 0.075) / 1_000_000
    assert abs(cost - expected) < 1e-12
    # A cache read is billed at the discounted rate, not the prompt rate.
    cost_cached = estimate_cost_usd(input_tokens=0, output_tokens=0, cache_read_tokens=1000)
    assert abs(cost_cached - (1000 * 0.075) / 1_000_000) < 1e-12


# ---------------------------------------------------------------------------------------
# 5. Timing attribution — the invariant the live split must satisfy
# ---------------------------------------------------------------------------------------


def test_timing_attribution_partitions_the_turn() -> None:
    """``framework_ms`` + ``model_ms`` + ``unattributed_ms`` must reconstruct ``total_ms``.

    The live script reports framework vs model; this pins the property that they partition the
    whole turn, so a live split that does not add up is a measurement bug, not a real result.
    """
    t = TurnTiming(
        setup_ms=3, wait_ms=2500, stream_ms=200, finalize_ms=1, total_ms=2704,
        streaming_measured=True,
    )
    assert t.framework_ms == 4
    assert t.model_ms == 2700
    assert t.framework_ms + t.model_ms + t.unattributed_ms == t.total_ms
    assert 0.0 <= t.framework_share <= 1.0


# ---------------------------------------------------------------------------------------
# 6. The live path is wired and skips cleanly with no key
# ---------------------------------------------------------------------------------------


@pytest.mark.live
async def test_live_brand_palette_selection() -> None:
    """LIVE: the colourist skill, real model. Skipped by default (see conftest --run-live).

    When run with ``--run-live`` and a key, this asserts the model CHOSE to call
    ``brand_palette`` and quoted the amber. With no key it skips cleanly — never fails.
    """
    from pikachu.config import get_api_key

    key = get_api_key()
    if not key:
        pytest.skip("no OPENROUTER_API_KEY — live test skipped, not failed")

    from scripts.round3_live import run_skill_with_tools_live

    outcome = await run_skill_with_tools_live(key)
    assert outcome.called_brand_palette, "model did not call brand_palette"
    assert HOUSE_AMBER in outcome.text, "model did not quote the tool's amber value"
