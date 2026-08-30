"""Tests for discovery/ — the AgentSpec registry, conservative routing, and partition
confusability.

Offline only. The ``embedder`` fixture from ``conftest.py`` is the deterministic hash-based
:class:`StubEmbedder`, so these tests exercise **plumbing and thresholds** — they never
assert a semantic judgement, because the stub gives semantically-similar strings unrelated
vectors on purpose.
"""

from __future__ import annotations

import pytest

from pikachu.core.types import AgentSpec
from pikachu.discovery.registry import (
    AgentRegistry,
    DuplicateAgentError,
    InvalidAgentName,
    UnknownAgentError,
    is_valid_agent_name,
)
from pikachu.discovery.routing import (
    DEFAULT_SPLIT_THRESHOLD,
    RouteDecision,
    audit_partition,
    check_partition_addition,
    route,
)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _agent(name: str, *, triggers: tuple[str, ...] = (), skill_tags: tuple[str, ...] = ()) -> AgentSpec:
    return AgentSpec(name=name, triggers=triggers, skill_tags=skill_tags)


# --------------------------------------------------------------------------------------
# Registry — create / get / list / retire / restore
# --------------------------------------------------------------------------------------


def test_create_then_get_returns_same_spec() -> None:
    reg = AgentRegistry()
    spec = _agent("designer")
    reg.create(spec)
    assert reg.get("designer") is spec


def test_list_is_name_sorted_and_live_only() -> None:
    reg = AgentRegistry()
    reg.create(_agent("marketer"))
    reg.create(_agent("designer"))
    assert [a.name for a in reg.list()] == ["designer", "marketer"]


def test_duplicate_name_rejected() -> None:
    reg = AgentRegistry()
    reg.create(_agent("designer"))
    with pytest.raises(DuplicateAgentError):
        reg.create(_agent("designer"))


def test_get_unknown_raises() -> None:
    reg = AgentRegistry()
    with pytest.raises(UnknownAgentError):
        reg.get("nope")


def test_retire_is_reversible_nothing_deleted() -> None:
    reg = AgentRegistry()
    spec = _agent("designer")
    reg.create(spec)

    reg.retire("designer")
    # Hidden from the live list and from a plain get...
    assert reg.list() == ()
    assert reg.is_retired("designer") is True
    with pytest.raises(UnknownAgentError):
        reg.get("designer")
    # ...but retained, reachable when asked, and restorable unchanged.
    assert reg.get("designer", include_retired=True) is spec
    assert reg.restore("designer") is spec
    assert reg.get("designer") is spec
    assert reg.is_retired("designer") is False


def test_retired_name_still_reserved_against_duplicate() -> None:
    reg = AgentRegistry()
    reg.create(_agent("designer"))
    reg.retire("designer")
    with pytest.raises(DuplicateAgentError):
        reg.create(_agent("designer"))


def test_retire_unknown_or_already_retired_raises() -> None:
    reg = AgentRegistry()
    with pytest.raises(UnknownAgentError):
        reg.retire("ghost")
    reg.create(_agent("designer"))
    reg.retire("designer")
    with pytest.raises(UnknownAgentError):
        reg.retire("designer")  # already retired -> not live -> error, not a silent no-op


def test_restore_unknown_raises() -> None:
    reg = AgentRegistry()
    with pytest.raises(UnknownAgentError):
        reg.restore("ghost")


def test_list_include_retired() -> None:
    reg = AgentRegistry()
    reg.create(_agent("designer"))
    reg.create(_agent("marketer"))
    reg.retire("marketer")
    assert [a.name for a in reg.list()] == ["designer"]
    assert [a.name for a in reg.list(include_retired=True)] == ["designer", "marketer"]


# --------------------------------------------------------------------------------------
# Registry — Agent Plugins name grammar (incl. '--'/'..' lookahead)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["a", "designer", "social-media", "v1.2", "agent-2", "a.b-c"],
)
def test_valid_agent_names(name: str) -> None:
    assert is_valid_agent_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Designer",          # uppercase
        "-leading",          # leading separator
        "trailing-",         # trailing separator
        ".dot",              # leading dot
        "double--dash",      # '--' forbidden by the lookahead
        "path..traversal",   # '..' forbidden by the lookahead
        "has space",
        "under_score",       # '_' not in the grammar
    ],
)
def test_invalid_agent_names_rejected(name: str) -> None:
    assert is_valid_agent_name(name) is False


def test_create_rejects_invalid_name() -> None:
    reg = AgentRegistry()
    with pytest.raises(InvalidAgentName):
        reg.create(AgentSpec(name="double--dash"))


# --------------------------------------------------------------------------------------
# Routing — conservative, trigger-match only
# --------------------------------------------------------------------------------------


def test_no_trigger_agent_is_never_auto_selected_but_reachable_by_name() -> None:
    reg = AgentRegistry()
    silent = _agent("designer", triggers=())  # no triggers
    reg.create(silent)

    # Routing never picks it, whatever the message.
    result = route("please design a logo", reg.list())
    assert result.decision is RouteDecision.DEFAULT
    assert result.candidates == ()

    # But it is fully reachable by name.
    assert reg.get("designer") is silent


def test_single_trigger_match_routes() -> None:
    a = _agent("designer", triggers=("logo", "banner"))
    b = _agent("marketer", triggers=("campaign",))
    result = route("make me a logo", [a, b])
    assert result.decision is RouteDecision.MATCHED
    assert result.selected is a
    assert result.candidates == (a,)
    assert "logo" in result.matched_triggers


def test_two_matches_return_both_candidates_not_a_pick() -> None:
    a = _agent("designer", triggers=("logo",))
    b = _agent("brander", triggers=("logo",))  # same trigger, deliberate overlap
    result = route("i need a logo", [a, b])
    assert result.decision is RouteDecision.AMBIGUOUS
    assert set(result.candidates) == {a, b}
    # An ambiguous route must NOT resolve to a single agent.
    assert result.selected is None


def test_default_single_agent_when_nothing_matches() -> None:
    a = _agent("designer", triggers=("logo",))
    result = route("what is the weather", [a])
    assert result.decision is RouteDecision.DEFAULT
    assert result.candidates == ()
    assert result.selected is None


def test_trigger_match_is_case_insensitive() -> None:
    a = _agent("designer", triggers=("Logo",))
    result = route("MAKE A LOGO", [a])
    assert result.decision is RouteDecision.MATCHED
    assert result.selected is a


def test_empty_roster_is_default() -> None:
    result = route("anything", [])
    assert result.decision is RouteDecision.DEFAULT


# --------------------------------------------------------------------------------------
# Partition confusability — warn, never reject; scoped per partition
# --------------------------------------------------------------------------------------


async def test_adding_near_duplicate_warns_but_does_not_reject(embedder: object) -> None:
    # StubEmbedder is hash-based, so we cannot rely on two *different* strings being close.
    # To exercise the breach path deterministically, the "new" description is IDENTICAL to
    # an existing one -> cosine similarity 1.0 -> guaranteed to cross any threshold. This
    # tests the plumbing (a breach is reported), not a semantic judgement.
    report = await check_partition_addition(
        "apply the house palette",
        ("apply the house palette",),  # identical existing skill in the SAME partition
        embedder=embedder,  # type: ignore[arg-type]
        partition="colour",
    )
    assert report.breaches_threshold is True          # WARNED
    assert report.nearest_description == "apply the house palette"
    assert report.nearest_score == pytest.approx(1.0)
    assert report.partition == "colour"
    # The report is advisory only: the skill is not rejected, no exception is raised.


async def test_same_skill_in_different_partition_does_not_warn(embedder: object) -> None:
    # Different partition => the look-alike is simply not passed in (the model never chooses
    # across partitions), so there is nothing to be confusable with and no breach.
    report = await check_partition_addition(
        "apply the house palette",
        (),  # this partition is empty of that skill
        embedder=embedder,  # type: ignore[arg-type]
        partition="marketing",
    )
    assert report.breaches_threshold is False
    assert report.nearest_description is None


async def test_non_identical_below_threshold_does_not_breach(embedder: object) -> None:
    # Again, do not lean on stub geometry (all-positive vectors are never near-orthogonal).
    # With a threshold above the cosine ceiling, a non-identical pair cannot breach, and the
    # nearest match is still surfaced for a human to inspect.
    report = await check_partition_addition(
        "write launch tweet copy",
        ("grade a still to the house look",),
        embedder=embedder,  # type: ignore[arg-type]
        partition="colour",
        threshold=1.01,
    )
    assert report.breaches_threshold is False
    assert report.nearest_description == "grade a still to the house look"
    assert report.nearest_score < 1.0  # not identical


# --------------------------------------------------------------------------------------
# Partition audit — max pairwise similarity is computed per partition and exposed
# --------------------------------------------------------------------------------------


async def test_max_pairwise_similarity_exposed_and_split_signal(embedder: object) -> None:
    # A partition containing two IDENTICAL descriptions has max pairwise similarity 1.0,
    # which crosses the default threshold and raises the split signal. Deterministic under
    # the stub because identical text hashes to the same vector.
    audit = await audit_partition(
        ("apply the house palette", "apply the house palette", "add film grain"),
        embedder=embedder,  # type: ignore[arg-type]
        partition="colour",
    )
    assert audit.skill_count == 3
    assert audit.max_pairwise_similarity == pytest.approx(1.0)
    assert audit.should_split is True
    assert audit.threshold == DEFAULT_SPLIT_THRESHOLD
    assert audit.nearest_pair is not None


async def test_partition_under_two_skills_never_splits(embedder: object) -> None:
    audit = await audit_partition(
        ("apply the house palette",),
        embedder=embedder,  # type: ignore[arg-type]
        partition="colour",
    )
    assert audit.skill_count == 1
    assert audit.max_pairwise_similarity == 0.0
    assert audit.should_split is False
    assert audit.nearest_pair is None


async def test_split_signal_gated_by_threshold_not_stub_geometry(embedder: object) -> None:
    # DO NOT assert a semantic judgement with the hash-based stub: its vectors are all-
    # positive (each component in [0, 1]), so cosine similarity between any two is high and
    # non-identical strings are NOT reliably below 0.85. Instead prove the threshold GATE:
    # with an impossibly high threshold (> 1.0, the cosine ceiling), no non-identical pair
    # can breach, so should_split must be False regardless of the stub's geometry.
    descs = ("grade a still", "write ad copy", "tile a sticker sheet")
    high = await audit_partition(
        descs, embedder=embedder, partition="mixed", threshold=1.01  # type: ignore[arg-type]
    )
    assert high.should_split is False
    assert high.threshold == 1.01
    # The similarity value is still computed and exposed for telemetry to trend...
    assert high.max_pairwise_similarity > 0.0
    assert high.nearest_pair is not None
    # ...and the same value at a threshold it clears DOES trip the signal, confirming the
    # gate is `score >= threshold` and nothing else.
    below = await audit_partition(
        descs,
        embedder=embedder,  # type: ignore[arg-type]
        partition="mixed",
        threshold=high.max_pairwise_similarity,
    )
    assert below.should_split is True
