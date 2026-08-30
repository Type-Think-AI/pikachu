"""Taint propagation over the frozen :class:`~pikachu.core.types.Lineage`.

This module is the enforcement half of the laundering defence. ``Lineage`` (in
``core/types``) is the *data*: an immutable, monotonic set of taints with no ``clear()``,
so a taint that has been introduced cannot be expressed away. This module is the *rules*
that read that data at the two boundaries where laundering would otherwise happen:

  * **promotion** — a tainted value must never reach a trusted position (CANDIDATE /
    ACTIVE skill status, or any "now trusted" transition). :func:`assert_promotable`.
  * **authority** — a tainted value, or *any* recalled memory, must never widen a tool
    grant. Authority comes only from the host allowlist (invariant P3); this restates P3
    across the memory boundary. :func:`assert_cannot_widen_authority`.

Why this is not a nice-to-have (docs/06-security.md): per-turn prompt filtering is not
enough for a self-evolving agent, because memory evolution can turn a single injected turn
into a durable, self-provenanced skill. Taint is what makes that path unreachable.

Design mirrors ``guard/allowlist.py``:

  * **Monotonic by construction.** :func:`derive` only ever unions taint. There is no
    subtract path, deliberately, because ``Lineage`` offers none.
  * **Fails closed.** When something is not promotable the answer is a raised
    ``TaintedPromotion``, not a silent drop — promotion is a deliberate act, so refusing it
    loudly is correct (unlike a tool filter, which must omit silently).

Everything is pure and import-light: only the stdlib and ``core``. Nothing here is needed
by a turn that never touches memory or lineage, so nothing imports it at package scope.
"""

from __future__ import annotations

from collections.abc import Iterable

from pikachu.core.errors import TaintedPromotion
from pikachu.core.types import Lineage, MemoryRecord, Skill, SkillStatus, Taint

__all__ = [
    "PROMOTABLE_STATUSES",
    "assert_cannot_widen_authority",
    "assert_promotable",
    "derive",
    "is_promotable_status",
    "taints_of",
]


#: The skill lifecycle states that mean "visible to retrieval / trusted to run". Kept in
#: sync with :meth:`SkillStatus.is_retrievable` but named here because promotion is about
#: *reaching* these states, which is a lineage question, not a status question.
PROMOTABLE_STATUSES: frozenset[SkillStatus] = frozenset(
    {SkillStatus.CANDIDATE, SkillStatus.ACTIVE}
)


def is_promotable_status(status: SkillStatus) -> bool:
    """Whether ``status`` is a trusted, retrieval-visible state.

    A promotion is any transition *into* one of these. Reaching one with a tainted lineage
    is the laundering event this module exists to block.
    """
    return status in PROMOTABLE_STATUSES


def derive(*sources: Lineage) -> Lineage:
    """Combine source lineages into the lineage of a value derived from all of them.

    The result's taints are the **union** of every source's taints, and its sources are the
    ordered union of every source string. This is the only construction rule taint needs:
    a value is exactly as tainted as the dirtiest thing that fed it, and never cleaner.

    Called with no arguments this returns a clean lineage — a value derived from nothing
    untrusted is clean, which is the correct base case (e.g. a constant, or host config).

    Monotonicity is structural: :meth:`Lineage.merge` only unions, and there is no code path
    here that removes a taint. Feeding a tainted lineage in can only ever keep or grow the
    taint set of the output — proven for arbitrary chains by the Soul badge.
    """
    result = Lineage.clean()
    for source in sources:
        result = result.merge(source)
    return result


def taints_of(*sources: Lineage) -> frozenset[Taint]:
    """The union of taints across ``sources`` — a convenience over :func:`derive`."""
    return derive(*sources).taints


def assert_promotable(subject: str, lineage: Lineage) -> None:
    """Raise :class:`TaintedPromotion` if ``lineage`` carries any taint.

    ``subject`` is a human-facing identifier (a skill name, a record key) used only for the
    error message. A clean lineage passes silently; any taint at all is disqualifying — the
    rule is not "how tainted", it is "tainted at all", because a single injected source is
    enough to compromise a promoted skill.

    This is the promotion gate. Call it before moving anything into a CANDIDATE/ACTIVE
    status, distilling a turn into a durable skill, or otherwise conferring trust.
    """
    if not lineage.is_clean:
        raise TaintedPromotion(
            subject,
            taints=frozenset(t.value for t in lineage.taints),
        )


def assert_skill_promotion(skill: Skill, *, to_status: SkillStatus) -> None:
    """Guard a specific skill status transition.

    Two ways this refuses, both raising :class:`TaintedPromotion`:

      * the target status is trusted (:data:`PROMOTABLE_STATUSES`) **and** the skill's
        lineage is tainted — the direct laundering event; or
      * the skill's own :attr:`Skill.may_promote` is already ``False`` (tainted or
        archived) and the target is a trusted status.

    A transition into a non-trusted status (DRAFT, ARCHIVED) is always allowed regardless of
    taint: you may keep tainted evidence around, you just may never trust it. That is the
    "decay lowers rank, never deletes" philosophy applied to skills.
    """
    if not is_promotable_status(to_status):
        return
    if not skill.lineage.is_clean or not skill.may_promote:
        raise TaintedPromotion(
            f"skill {skill.name!r} v{skill.version}",
            taints=frozenset(t.value for t in skill.lineage.taints),
        )


def assert_cannot_widen_authority(
    subject: str,
    *,
    granted: Iterable[str],
    fixed_allowlist: Iterable[str],
) -> None:
    """Restate invariant P3 across the memory boundary.

    Whatever a recalled memory (or any derived content) *suggests* a run should be allowed
    to do, the effective grant must remain a subset of the host's fixed allowlist. Memory
    can inform *what to do*; it can never enlarge *what is permitted*. If ``granted`` tries
    to reach beyond ``fixed_allowlist`` this raises :class:`TaintedPromotion` — the same
    class, because widening authority from content is a laundering event of the same kind.

    Comparison is on the raw provided strings; callers upstream already normalise through
    :func:`~pikachu.core.types.normalize_tool_name` at the allowlist boundary. This function
    is the memory-side assertion, not a second normaliser.

    Note this is a *belt* around a structural guarantee, not the only line of defence:
    :attr:`MemoryRecord.may_justify_authority` is typed ``Literal[False]`` so a call site
    cannot branch on memory to grant anything in the first place. This function catches a
    grant that was assembled some other way and still tried to exceed the allowlist.
    """
    allow = set(fixed_allowlist)
    escalated = {g for g in granted if g not in allow}
    if escalated:
        raise TaintedPromotion(
            f"{subject} attempted to widen authority to {sorted(escalated)} "
            f"beyond the fixed allowlist",
            taints=frozenset({"authority_escalation"}),
        )


def assert_memory_grants_nothing(record: MemoryRecord) -> None:
    """Assert structurally that a memory record confers no authority.

    :attr:`MemoryRecord.may_justify_authority` is ``Literal[False]`` in the type system, so
    this cannot fail at runtime for a well-typed record — and that is the point. The Marsh
    badge calls it to prove the property holds for arbitrary records, and its existence
    documents the rule at the boundary where a future contributor might be tempted to break
    it.
    """
    # ``may_justify_authority`` is typed ``Literal[False]``; asserting it keeps the
    # invariant honest at runtime and gives the Marsh suite something concrete to exercise.
    if record.may_justify_authority:  # pragma: no cover - unreachable by type
        raise TaintedPromotion(
            f"memory {record.key!r} claimed to justify authority",
            taints=frozenset({"memory_authority"}),
        )
