"""Soul badge (curator) — Koga, Poison: the distil laundering path stays closed.

``test_soul.py`` proves taint monotonicity over the raw ``guard.lineage`` primitives. This
module proves the same property *through the curator's own public surface* — the place an
attacker actually reaches. The distinction matters: a laundering defence that holds in the
primitive but leaks through the convenience wrapper is no defence, so the curator's own
``promote_on_reuse`` / ``promote_on_success`` / ``restore`` / ``distil`` are exercised here,
not the lineage helpers directly.

The one sentence this suite defends: **a tainted draft can NEVER reach candidate or active,
however many successful uses it accumulates, and the taint survives every derivation path.**
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pikachu.core.errors import TaintedPromotion
from pikachu.core.types import Lineage, Skill, SkillStatus, Taint, TrustTier
from pikachu.curator.distil import DistilCandidate, RejectionReason, distil
from pikachu.curator.lifecycle import (
    PROMOTE_MIN_USES,
    UsageStats,
    promote_on_reuse,
    promote_on_success,
    restore,
)
from pikachu.guard.authored import authored_is_clean, inherit_turn_lineage

pytestmark = pytest.mark.soul


_taints = st.sampled_from(list(Taint))


@st.composite
def _tainted_lineage(draw: st.DrawFn) -> Lineage:
    """A lineage guaranteed to carry at least one taint."""
    chosen = draw(st.sets(_taints, min_size=1, max_size=len(Taint)))
    lineage = Lineage.clean()
    for i, t in enumerate(sorted(chosen, key=lambda x: x.value)):
        lineage = lineage.with_taint(t, f"src:{t.value}:{i}")
    return lineage


def _tainted_draft(lineage: Lineage, *, name: str = "laundered") -> Skill:
    """A BUILTIN (agent-created) draft carrying ``lineage`` — the exact attack shape."""
    return Skill(
        name=name,
        description="a shortcut distilled from a poisoned turn",
        body="# shortcut\n\nstep one\nstep two\n",
        status=SkillStatus.DRAFT,
        trust=TrustTier.BUILTIN,
        lineage=lineage,
    )


# --------------------------------------------------------- reuse can never launder


@given(lineage=_tainted_lineage())
def test_tainted_draft_never_reaches_candidate_on_reuse(lineage: Lineage) -> None:
    """First reuse promotes a clean draft; a tainted draft is refused, not promoted."""
    with pytest.raises(TaintedPromotion):
        promote_on_reuse(_tainted_draft(lineage))


@given(lineage=_tainted_lineage())
def test_no_number_of_successful_uses_promotes_a_tainted_candidate(
    lineage: Lineage,
) -> None:
    """The headline: pile on successful uses; a tainted skill still cannot reach active.

    We even force the skill into CANDIDATE (bypassing the reuse gate that would already have
    refused it) to prove the *success* gate refuses independently — defence in depth, so a
    single missed call site upstream cannot open the path.
    """
    forced_candidate = _tainted_draft(lineage).model_copy(
        update={"status": SkillStatus.CANDIDATE}
    )
    # Way past the threshold, perfect success rate — numbers are irrelevant to taint.
    stats = UsageStats(uses=PROMOTE_MIN_USES * 10, successes=PROMOTE_MIN_USES * 10)
    assert stats.meets_active_bar()
    with pytest.raises(TaintedPromotion):
        promote_on_success(forced_candidate, stats)


@given(lineage=_tainted_lineage(), uses=st.integers(min_value=0, max_value=50))
def test_tainted_never_promotes_for_any_use_count(
    lineage: Lineage, uses: int
) -> None:
    """For ANY use/success count, a tainted candidate is refused promotion to active."""
    forced_candidate = _tainted_draft(lineage).model_copy(
        update={"status": SkillStatus.CANDIDATE}
    )
    stats = UsageStats(uses=uses, successes=uses)
    if stats.meets_active_bar():
        with pytest.raises(TaintedPromotion):
            promote_on_success(forced_candidate, stats)
    else:
        # Below the bar it is simply returned unchanged — still never active.
        assert promote_on_success(forced_candidate, stats).status is SkillStatus.CANDIDATE


# ------------------------------------------------- restore is not a laundering door


@given(
    lineage=_tainted_lineage(),
    to=st.sampled_from([SkillStatus.CANDIDATE, SkillStatus.ACTIVE]),
)
def test_restoring_a_tainted_archived_skill_to_a_trusted_status_is_refused(
    lineage: Lineage, to: SkillStatus
) -> None:
    """Archive is recoverable, but recovery is not a way to launder taint into retrieval."""
    archived = _tainted_draft(lineage).model_copy(
        update={"status": SkillStatus.ARCHIVED}
    )
    with pytest.raises(TaintedPromotion):
        restore(archived, to=to)


# ------------------------------------------------- taint survives every derivation


@given(
    turn_sources=st.lists(_tainted_lineage(), min_size=1, max_size=6),
)
def test_taint_survives_distillation_from_a_tainted_turn(
    turn_sources: list[Lineage],
) -> None:
    """A draft distilled from a tainted turn inherits the taint — monotonically."""
    inherited = inherit_turn_lineage(*turn_sources)
    assert not inherited.is_clean
    assert not authored_is_clean(*turn_sources)
    # Every taint present in any source is present in the inherited lineage.
    union: frozenset[Taint] = frozenset()
    for src in turn_sources:
        union = union | src.taints
    assert union <= inherited.taints


async def test_distil_from_a_poisoned_turn_yields_an_unpromotable_draft(
    embedder: object,
) -> None:
    """End-to-end: a clean-bodied recipe distilled from a tool-output-tainted turn passes
    the four quality checks, is written as a draft, and yet can never be promoted."""
    poisoned_turn = Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "tool:web_fetch")
    candidate = DistilCandidate(
        name="tainted-recipe",
        description="tile a subject across a sheet then recolour it",
        body="# recipe\n\nstep one: cut out subject\nstep two: tile six times\n",
        succeeded=True,
        tool_call_count=3,
        parameterisable=True,
        turn_lineage=(poisoned_turn,),
    )
    outcome = await distil(candidate, (), embedder=embedder)  # type: ignore[arg-type]

    # It WAS created — a poisoned turn can still produce a draft; that is expected.
    assert outcome.created
    assert outcome.skill is not None
    draft = outcome.skill
    assert draft.status is SkillStatus.DRAFT
    assert not draft.lineage.is_clean  # inherited the tool-output taint

    # ...but it can never be promoted out of draft.
    with pytest.raises(TaintedPromotion):
        promote_on_reuse(draft)


def test_clean_distilled_draft_promotes_but_tainted_sibling_does_not() -> None:
    """The contrast case, concretely: identical drafts, one clean, one tainted."""
    clean = _tainted_draft(Lineage.clean(), name="clean-one").model_copy(
        update={"lineage": Lineage.clean()}
    )
    promoted = promote_on_reuse(clean)
    assert promoted.status is SkillStatus.CANDIDATE

    tainted = _tainted_draft(
        Lineage.clean().with_taint(Taint.FOREIGN_SKILL, "catalog:evil"),
        name="tainted-one",
    )
    with pytest.raises(TaintedPromotion):
        promote_on_reuse(tainted)
