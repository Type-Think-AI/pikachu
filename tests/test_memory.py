"""Memory store semantics: scopes, the recall budget, decay, and crew-shared LONG.

These are the plain unit tests for the reference store. The security-shaped invariants
(taint never disappears, memory never widens authority, cross-tenant isolation) live in the
Soul and Marsh badge suites; this file covers the everyday behaviour the store owes.
"""

from __future__ import annotations

import pytest

from pikachu.core.protocols import MemoryStore
from pikachu.core.types import MemoryRecord, MemoryScope
from pikachu.memory import DEFAULT_RECALL_LIMIT, CrewMemory, InMemoryMemoryStore


def _rec(
    key: str,
    value: str = "",
    *,
    scope: MemoryScope = MemoryScope.SHORT,
    confidence: float = 0.5,
    evidence: int = 0,
) -> MemoryRecord:
    return MemoryRecord(
        key=key,
        value=value or key,
        scope=scope,
        confidence=confidence,
        evidence_count=evidence,
    )


def test_satisfies_memory_store_protocol() -> None:
    """The reference store is structurally a MemoryStore."""
    store = InMemoryMemoryStore(tenant="house-a")
    assert isinstance(store, MemoryStore)


async def test_scopes_are_addressable_independently() -> None:
    store = InMemoryMemoryStore(tenant="house-a")
    await store.remember(_rec("s", scope=MemoryScope.SHORT))
    await store.remember(_rec("m", scope=MemoryScope.MID))
    await store.remember(_rec("l", scope=MemoryScope.LONG))

    short = await store.recall("", scope=MemoryScope.SHORT)
    mid = await store.recall("", scope=MemoryScope.MID)
    long = await store.recall("", scope=MemoryScope.LONG)

    assert {r.key for r in short} == {"s"}
    assert {r.key for r in mid} == {"m"}
    assert {r.key for r in long} == {"l"}


async def test_recall_none_scope_sees_all_three() -> None:
    store = InMemoryMemoryStore(tenant="house-a")
    await store.remember(_rec("s", scope=MemoryScope.SHORT))
    await store.remember(_rec("m", scope=MemoryScope.MID))
    await store.remember(_rec("l", scope=MemoryScope.LONG))

    everything = await store.recall("")
    assert {r.key for r in everything} == {"s", "m", "l"}


async def test_recall_matches_on_key_and_value() -> None:
    store = InMemoryMemoryStore(tenant="house-a")
    await store.remember(_rec("brand-voice", "warm, never corporate"))
    await store.remember(_rec("palette", "no pure black"))

    on_key = await store.recall("brand")
    on_value = await store.recall("corporate")
    assert {r.key for r in on_key} == {"brand-voice"}
    assert {r.key for r in on_value} == {"brand-voice"}


# --------------------------------------------------------------------------- budget


async def test_recall_budget_is_a_hard_cap() -> None:
    """A caller asking for more than the store's cap still gets at most the cap."""
    store = InMemoryMemoryStore(tenant="house-a", recall_limit=3)
    for i in range(10):
        await store.remember(_rec(f"k{i}", confidence=0.5))

    # Ask for far more than the cap.
    got = await store.recall("", limit=1000)
    assert len(got) == 3


async def test_recall_limit_can_only_narrow_not_widen() -> None:
    store = InMemoryMemoryStore(tenant="house-a", recall_limit=5)
    for i in range(10):
        await store.remember(_rec(f"k{i}"))

    # A smaller per-call limit wins.
    assert len(await store.recall("", limit=2)) == 2
    # A larger per-call limit is clamped to the store cap.
    assert len(await store.recall("", limit=99)) == 5


async def test_default_recall_limit_applied() -> None:
    store = InMemoryMemoryStore(tenant="house-a")
    assert store.recall_limit == DEFAULT_RECALL_LIMIT
    for i in range(DEFAULT_RECALL_LIMIT + 5):
        await store.remember(_rec(f"k{i}"))
    got = await store.recall("", limit=DEFAULT_RECALL_LIMIT + 5)
    assert len(got) == DEFAULT_RECALL_LIMIT


async def test_budget_keeps_highest_confidence_first() -> None:
    """The cap keeps the most trustworthy records, not the first-inserted ones."""
    store = InMemoryMemoryStore(tenant="house-a", recall_limit=2)
    await store.remember(_rec("low", confidence=0.1))
    await store.remember(_rec("high", confidence=0.9))
    await store.remember(_rec("mid", confidence=0.5))

    got = await store.recall("", limit=2)
    assert [r.key for r in got] == ["high", "mid"]


async def test_negative_limit_yields_nothing() -> None:
    store = InMemoryMemoryStore(tenant="house-a")
    await store.remember(_rec("k"))
    assert await store.recall("", limit=-1) == ()


def test_negative_recall_limit_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        InMemoryMemoryStore(tenant="house-a", recall_limit=-1)


# --------------------------------------------------------------------------- decay


async def test_decay_lowers_confidence_without_deleting() -> None:
    store = InMemoryMemoryStore(tenant="house-a")
    await store.remember(_rec("k", confidence=0.5, scope=MemoryScope.SHORT))

    affected = await store.decay(older_than_days=0)
    assert affected == 1

    # Still present — decay lowers rank, never removes.
    got = await store.recall("", scope=MemoryScope.SHORT)
    assert len(got) == 1
    assert got[0].confidence == pytest.approx(0.4)


async def test_decay_never_removes_and_floors_at_zero() -> None:
    store = InMemoryMemoryStore(tenant="house-a")
    await store.remember(_rec("k", confidence=0.05))

    await store.decay(older_than_days=0)  # 0.05 -> 0.0
    await store.decay(older_than_days=0)  # already 0.0, stays, not affected

    got = await store.recall("")
    assert len(got) == 1
    assert got[0].confidence == 0.0


async def test_decay_affects_every_scope_including_long() -> None:
    crew = CrewMemory()
    store = InMemoryMemoryStore(tenant="house-a", crew=crew)
    await store.remember(_rec("s", scope=MemoryScope.SHORT, confidence=0.5))
    await store.remember(_rec("m", scope=MemoryScope.MID, confidence=0.5))
    await store.remember(_rec("l", scope=MemoryScope.LONG, confidence=0.5))

    affected = await store.decay(older_than_days=0)
    assert affected == 3

    long = await store.recall("", scope=MemoryScope.LONG)
    assert long[0].confidence == pytest.approx(0.4)


async def test_no_delete_method_on_store() -> None:
    """The Protocol has no delete; the reference store must not add one."""
    store = InMemoryMemoryStore(tenant="house-a")
    assert not hasattr(store, "delete")
    assert not hasattr(store, "remove")
    assert not hasattr(store, "forget")


# ------------------------------------------------------------------ crew-shared LONG


async def test_long_scope_is_shared_across_the_crew() -> None:
    """A LONG memory written by one agent is recalled by another in the same house."""
    crew = CrewMemory()
    writer = InMemoryMemoryStore.for_agent(tenant="house-a", crew=crew)
    reader = InMemoryMemoryStore.for_agent(tenant="house-a", crew=crew)

    await writer.remember(
        _rec("brand-voice", "warm, never corporate", scope=MemoryScope.LONG)
    )

    # The reader — a different agent, different store — sees it. Day-one emptiness solved.
    seen = await reader.recall("brand", scope=MemoryScope.LONG)
    assert {r.key for r in seen} == {"brand-voice"}


async def test_short_and_mid_are_not_shared_across_the_crew() -> None:
    crew = CrewMemory()
    writer = InMemoryMemoryStore.for_agent(tenant="house-a", crew=crew)
    reader = InMemoryMemoryStore.for_agent(tenant="house-a", crew=crew)

    await writer.remember(_rec("s", scope=MemoryScope.SHORT))
    await writer.remember(_rec("m", scope=MemoryScope.MID))

    assert await reader.recall("", scope=MemoryScope.SHORT) == ()
    assert await reader.recall("", scope=MemoryScope.MID) == ()


async def test_new_agent_joins_a_house_that_already_knows_the_brand() -> None:
    """Concrete day-one story: the house's LONG memory predates the new agent's store."""
    crew = CrewMemory()
    founder = InMemoryMemoryStore.for_agent(tenant="house-a", crew=crew)
    await founder.remember(
        _rec("style", "16:9, no pure black", scope=MemoryScope.LONG, confidence=0.9)
    )

    # A brand-new agent is created later — its store did not exist when the memory was made.
    newcomer = InMemoryMemoryStore.for_agent(tenant="house-a", crew=crew)
    seen = await newcomer.recall("style", scope=MemoryScope.LONG)
    assert {r.key for r in seen} == {"style"}
