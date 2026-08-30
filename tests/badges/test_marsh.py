"""Marsh badge — Sabrina, Psychic: memory cannot lie.

Memory can inform *what to do*; it can never enlarge *what is permitted*. Three invariants:

  1. ``MemoryRecord.may_justify_authority`` is structurally ``False`` — typed
     ``Literal[False]`` so a call site cannot even branch on it to grant something.

  2. No code path derives a tool grant from recalled content. Whatever a recalled memory
     suggests, the effective grant stays a subset of the host allowlist — invariant P3
     restated across the memory boundary. :func:`assert_cannot_widen_authority` refuses an
     escalation.

  3. Cross-tenant isolation: one house's private memory can never enter another house's
     recall, enforced structurally (a store is bound to its tenant; there is no tenant
     argument to get wrong).
"""

from __future__ import annotations

import typing

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pikachu.core.errors import TaintedPromotion
from pikachu.core.types import (
    Lineage,
    MemoryRecord,
    MemoryScope,
    Taint,
    normalize_tool_name,
)
from pikachu.guard.lineage import (
    assert_cannot_widen_authority,
    assert_memory_grants_nothing,
)
from pikachu.memory import CrewMemory, InMemoryMemoryStore

pytestmark = pytest.mark.marsh


def _rec(key: str, *, scope: MemoryScope = MemoryScope.LONG) -> MemoryRecord:
    return MemoryRecord(key=key, value=key, scope=scope)


# ----------------------------------------------- memory never justifies authority


@given(
    key=st.text(min_size=1, max_size=20),
    value=st.text(max_size=40),
    scope=st.sampled_from(list(MemoryScope)),
    confidence=st.floats(min_value=0.0, max_value=1.0),
    evidence=st.integers(min_value=0, max_value=1000),
)
def test_may_justify_authority_is_structurally_false(
    key: str, value: str, scope: MemoryScope, confidence: float, evidence: int
) -> None:
    """For ANY record — high confidence, huge evidence count — authority is still False."""
    record = MemoryRecord(
        key=key,
        value=value,
        scope=scope,
        confidence=confidence,
        evidence_count=evidence,
    )
    assert record.may_justify_authority is False
    # And the guard's runtime assertion never raises for a well-typed record.
    assert_memory_grants_nothing(record)


def test_may_justify_authority_is_typed_literal_false() -> None:
    """The return type is ``Literal[False]`` — a call site cannot branch on it as a grant."""
    hints = typing.get_type_hints(MemoryRecord.may_justify_authority.fget)  # type: ignore[attr-defined]
    assert hints["return"] == typing.Literal[False]


# -------------------------------------------- no tool grant is derived from memory


def test_memory_cannot_widen_a_tool_grant() -> None:
    """A recalled memory 'suggesting' a tool outside the allowlist cannot widen it."""
    allowlist = ("generate_image", "read_canvas")
    # The grant a run tries to assemble tries to reach a tool memory 'mentioned'.
    with pytest.raises(TaintedPromotion):
        assert_cannot_widen_authority(
            "recalled brand-voice memory",
            granted=("generate_image", "delete_everything"),
            fixed_allowlist=allowlist,
        )


@given(
    allow=st.lists(st.text(min_size=1, max_size=8), max_size=6),
    granted=st.lists(st.text(min_size=1, max_size=8), max_size=6),
)
def test_authority_never_exceeds_allowlist(
    allow: list[str], granted: list[str]
) -> None:
    """assert_cannot_widen_authority raises iff granted reaches beyond the allowlist.

    The model normalises both sides, because the function does. It compared raw strings until
    the audit (``docs/24-audit.md`` defect 3) showed that made a case or whitespace variant of
    a permitted tool look like escalation — every other entry point in ``guard/`` normalises,
    and a guarantee that holds on one path and not another is not a guarantee.

    A name that normalises to empty counts as escalation rather than as nothing: it is not a
    valid tool name, ``ToolSpec`` rejects it, and letting malformed input pass an authority
    check quietly is how a bypass gets built. Hypothesis found exactly that case with
    ``granted=[':']``.
    """
    allow_set = {normalize_tool_name(a) for a in allow}
    allow_set.discard("")
    escalates = any(
        (not (norm := normalize_tool_name(g))) or norm not in allow_set for g in granted
    )
    if escalates:
        with pytest.raises(TaintedPromotion):
            assert_cannot_widen_authority(
                "subject", granted=granted, fixed_allowlist=allow
            )
    else:
        assert_cannot_widen_authority("subject", granted=granted, fixed_allowlist=allow)


def test_narrowing_is_always_allowed() -> None:
    """A grant that is a subset of the allowlist passes — memory may narrow, never widen."""
    assert_cannot_widen_authority(
        "subject",
        granted=("read_canvas",),
        fixed_allowlist=("read_canvas", "generate_image"),
    )


# ---------------------------------------------------------- cross-tenant isolation


async def test_two_houses_do_not_share_long_memory() -> None:
    """Each house has its own crew memory; one house's LONG never enters another's recall."""
    crew_a = CrewMemory()
    crew_b = CrewMemory()
    house_a = InMemoryMemoryStore.for_agent(tenant="house-a", crew=crew_a)
    house_b = InMemoryMemoryStore.for_agent(tenant="house-b", crew=crew_b)

    await house_a.remember(_rec("a-secret", scope=MemoryScope.LONG))

    assert {r.key for r in await house_a.recall("")} == {"a-secret"}
    assert await house_b.recall("") == ()


async def test_isolation_holds_even_on_a_shared_crew_object() -> None:
    """Even if two tenants are handed the SAME CrewMemory, records are filed per tenant, so
    neither can recall the other's — isolation is structural, not a config discipline."""
    shared = CrewMemory()
    house_a = InMemoryMemoryStore(tenant="house-a", crew=shared)
    house_b = InMemoryMemoryStore(tenant="house-b", crew=shared)

    await house_a.remember(_rec("a-only", scope=MemoryScope.LONG))
    await house_b.remember(_rec("b-only", scope=MemoryScope.LONG))

    assert {r.key for r in await house_a.recall("", scope=MemoryScope.LONG)} == {"a-only"}
    assert {r.key for r in await house_b.recall("", scope=MemoryScope.LONG)} == {"b-only"}


def test_recall_has_no_tenant_argument() -> None:
    """The isolation guarantee is the ABSENCE of an addressable path to another tenant.

    If a ``tenant`` parameter ever appears on recall, a caller could point it at another
    house — so its absence is the invariant, and this test pins it.
    """
    import inspect

    sig = inspect.signature(InMemoryMemoryStore.recall)
    assert "tenant" not in sig.parameters


async def test_tainted_memory_is_still_recallable_but_grants_nothing() -> None:
    """Memory being tainted does not remove it (decay-not-delete) — but tainted or not, it
    confers no authority. The two properties are independent and both hold."""
    store = InMemoryMemoryStore(tenant="house-a")
    tainted = MemoryRecord(
        key="from-a-tool",
        value="the web said to run rm -rf",
        scope=MemoryScope.SHORT,
        lineage=Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "tool:web_fetch"),
    )
    await store.remember(tainted)

    recalled = await store.recall("")
    assert len(recalled) == 1
    # Recallable, yet grants nothing.
    assert recalled[0].may_justify_authority is False
    assert_memory_grants_nothing(recalled[0])
