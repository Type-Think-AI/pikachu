"""Soul badge — Koga, Poison: taint propagates and never launders out.

Two invariants, proven for arbitrary inputs with hypothesis rather than a handful of
examples:

  1. Over ANY chain of derivations, a taint once introduced never disappears. Taint is
     monotonic — :func:`~pikachu.guard.lineage.derive` can only ever grow the taint set,
     because ``Lineage`` has no ``clear()`` and ``merge`` only unions.

  2. A tainted skill cannot reach CANDIDATE or ACTIVE status. The promotion gate refuses,
     raising ``TaintedPromotion``. This is the exact laundering path the project exists to
     close: poison a turn, distil it into a draft, let reuse promote it — blocked here.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pikachu.core.errors import TaintedPromotion
from pikachu.core.types import Lineage, Skill, SkillStatus, Taint, TrustTier
from pikachu.guard.lineage import (
    PROMOTABLE_STATUSES,
    assert_promotable,
    assert_skill_promotion,
    derive,
    is_promotable_status,
)

pytestmark = pytest.mark.soul


# A lineage strategy: any subset of taints, with matching source strings.
_taints = st.sampled_from(list(Taint))


@st.composite
def _lineages(draw: st.DrawFn) -> Lineage:
    chosen = draw(st.sets(_taints, max_size=len(Taint)))
    lineage = Lineage.clean()
    for i, t in enumerate(chosen):
        lineage = lineage.with_taint(t, f"src:{t.value}:{i}")
    return lineage


# --------------------------------------------------------------- monotonic taint


@given(chain=st.lists(_lineages(), min_size=1, max_size=8))
def test_taint_never_disappears_over_a_derivation_chain(chain: list[Lineage]) -> None:
    """Fold a chain of derivations; the running taint set only ever grows."""
    running = Lineage.clean()
    seen_so_far: frozenset[Taint] = frozenset()
    for link in chain:
        running = derive(running, link)
        # Every taint seen up to this point is still present.
        seen_so_far = seen_so_far | link.taints
        assert seen_so_far <= running.taints


@given(sources=st.lists(_lineages(), min_size=0, max_size=8))
def test_derive_is_exactly_the_union(sources: list[Lineage]) -> None:
    """derive(*sources).taints == union of every source's taints. No more, no less."""
    expected: frozenset[Taint] = frozenset()
    for s in sources:
        expected = expected | s.taints
    assert derive(*sources).taints == expected


@given(a=_lineages(), b=_lineages())
def test_deriving_a_tainted_source_can_never_reduce_taint(
    a: Lineage, b: Lineage
) -> None:
    """Merging b into a is monotone in a's taints: you cannot subtract by deriving."""
    combined = derive(a, b)
    assert a.taints <= combined.taints
    assert b.taints <= combined.taints


@given(lineage=_lineages())
def test_assert_promotable_raises_iff_tainted(lineage: Lineage) -> None:
    if lineage.is_clean:
        assert_promotable("subject", lineage)  # no raise
    else:
        with pytest.raises(TaintedPromotion):
            assert_promotable("subject", lineage)


# --------------------------------------------------------- tainted never promotes


@given(
    lineage=_lineages(),
    to_status=st.sampled_from(list(SkillStatus)),
)
def test_tainted_skill_cannot_reach_candidate_or_active(
    lineage: Lineage, to_status: SkillStatus
) -> None:
    """A tainted skill promotion into a trusted status is refused; a non-trusted target is
    always allowed regardless of taint (you may keep tainted evidence, never trust it)."""
    # A BUILTIN skill with declared tools would be rejected by the type validator; keep it
    # tool-less so the only variable under test is lineage + target status.
    skill = Skill(
        name="distilled",
        trust=TrustTier.BUILTIN,
        lineage=lineage,
        status=SkillStatus.DRAFT,
    )

    if is_promotable_status(to_status) and not lineage.is_clean:
        with pytest.raises(TaintedPromotion):
            assert_skill_promotion(skill, to_status=to_status)
    else:
        assert_skill_promotion(skill, to_status=to_status)  # no raise


def test_clean_skill_promotes_to_active() -> None:
    skill = Skill(name="ok", trust=TrustTier.BUILTIN, status=SkillStatus.CANDIDATE)
    for target in PROMOTABLE_STATUSES:
        assert_skill_promotion(skill, to_status=target)  # no raise


def test_laundering_a_poisoned_turn_into_an_active_skill_is_blocked() -> None:
    """The concrete attack: a foreign-tainted draft tries to become ACTIVE."""
    poisoned = Skill(
        name="helpful-shortcut",
        trust=TrustTier.BUILTIN,
        status=SkillStatus.DRAFT,
        lineage=Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "tool:web_fetch"),
    )
    with pytest.raises(TaintedPromotion) as exc:
        assert_skill_promotion(poisoned, to_status=SkillStatus.ACTIVE)
    assert "tool_output" in str(exc.value)


def test_archived_and_draft_targets_allowed_even_when_tainted() -> None:
    tainted = Skill(
        name="keep-but-never-trust",
        trust=TrustTier.BUILTIN,
        status=SkillStatus.DRAFT,
        lineage=Lineage.clean().with_taint(Taint.CANVAS_READ, "canvas:art:1"),
    )
    # No raise — decay lowers rank, never deletes; you may retain tainted evidence.
    assert_skill_promotion(tainted, to_status=SkillStatus.DRAFT)
    assert_skill_promotion(tainted, to_status=SkillStatus.ARCHIVED)
