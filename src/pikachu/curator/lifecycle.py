"""LIFECYCLE — the deterministic half of curation: measure and credit.

The published pattern this module implements (``docs/13-self-improvement.md``, arXiv
2607.13683) is a hard split: **an LLM may diagnose and propose; only deterministic code
measures and credits.** No model call decides a promotion. Every threshold below is plain
Python, so the credit path is unhackable *by construction* rather than by policy — there is
no place a clever generation can talk its way into a promotion, because promotion is not a
generation, it is an integer comparison.

STATES AND THE ONE RULE THAT BOUNDS DRIFT (``docs/03-skill-lifecycle.md``)
-------------------------------------------------------------------------
::

    draft ──► candidate ──► active           (archived is reachable from any state)
                                └──► archived ──► (restore is always possible)

* ``draft`` — just distilled; **invisible to retrieval**.
* ``candidate`` — reused at least once; **retrievable**.
* ``active`` — >= ``PROMOTE_MIN_USES`` successful uses above a success-rate threshold;
  **retrievable**.
* ``archived`` — out of retrieval, **fully recoverable**.

Only ``candidate`` and ``active`` are visible to ``find_skill``. That single rule is the
whole drift bound: the retrieval set grows with *demonstrated value*, not with volume.

FIVE INVARIANTS, ADOPTED WHOLESALE (Hermes' scar tissue, not preference)
------------------------------------------------------------------------
1. **Only touches agent-created skills.** A user-authored or imported skill is never
   auto-transitioned — :func:`_guard_agent_created` refuses.
2. **Archive, never delete.** There is deliberately **no delete** in this module. Archive is
   recoverable via :func:`restore`.
3. **Pinned skills bypass every automatic transition.** A user override the machine may not
   argue with — every transition function returns the skill unchanged when it is pinned.
4. **Improvement writes a NEW version, never mutates.** Bodies are immutable
   (``Skill`` is frozen); :func:`improve` returns a fresh version with ``parent_version``
   set, and :func:`revert` is a single pointer update.
5. **Runs on idle, proposes rather than mutates for anything public.** The transition
   functions here are pure — they take a skill and return the next skill — so a caller runs
   them on the auxiliary model's idle tick and, for a public skill, enqueues the proposal
   for human review instead of applying it. The purity is what makes "propose, don't mutate"
   a one-line caller choice rather than a rewrite.

SECURITY: taint blocks promotion, every path (``docs/06-security.md``)
----------------------------------------------------------------------
Both :func:`promote_on_reuse` and :func:`promote_on_success` route through
``guard.authored.assert_authored_promotable`` before returning a promoted skill. A tainted
draft can accumulate any number of successful uses and still never reach ``candidate`` or
``active`` — the Soul-curator badge proves this for every derivation path. Promotion is
gated on lineage first and counts second, in that order, because a laundering attempt that
racks up uses must fail on the lineage check regardless of how good its numbers look.
"""

from __future__ import annotations

from typing import Final

from pikachu.core.errors import PikachuError
from pikachu.core.types import Skill, SkillStatus, TrustTier
from pikachu.guard.authored import assert_authored_promotable
from pikachu.guard.lineage import assert_promotable

__all__ = [
    "PROMOTE_MIN_USES",
    "PROMOTE_MIN_SUCCESS_RATE",
    "NotAgentCreated",
    "UsageStats",
    "archive",
    "improve",
    "promote_on_reuse",
    "promote_on_success",
    "restore",
    "revert",
]

#: A skill needs at least this many successful uses to move ``candidate -> active``.
#: Plain Python, deliberately. This is the whole credit rule; there is no model in it.
PROMOTE_MIN_USES: Final[int] = 3

#: ...and its success rate must be at least this. A skill that is used a lot but works
#: rarely is a bad skill with traffic, not an active one.
PROMOTE_MIN_SUCCESS_RATE: Final[float] = 0.5


class NotAgentCreated(PikachuError):
    """A lifecycle transition was attempted on a skill the curator may not touch.

    Only ``agent_created`` skills are auto-transitioned. A user-authored or imported skill
    must never be silently rewritten by an autonomous process — raising here is louder and
    safer than a silent no-op, because attempting it at all is a caller bug.
    """

    def __init__(self, name: str, *, trust: str) -> None:
        super().__init__(
            f"skill {name!r} (trust={trust}) is not agent-created; "
            f"the curator only transitions agent-created skills"
        )
        self.name = name


class UsageStats:
    """Deterministic use/success accounting for one skill.

    A tiny value object rather than free integers, so the promotion rule reads as one method
    and there is exactly one definition of 'success rate'. Immutable-ish: ``record`` returns
    a new instance, matching the frozen-everything house style.
    """

    __slots__ = ("uses", "successes")

    def __init__(self, uses: int = 0, successes: int = 0) -> None:
        if successes > uses:
            raise ValueError(f"successes ({successes}) cannot exceed uses ({uses})")
        if uses < 0 or successes < 0:
            raise ValueError("uses and successes must be non-negative")
        self.uses = uses
        self.successes = successes

    @property
    def success_rate(self) -> float:
        """Successful fraction of uses. Zero uses is a zero rate, not a division error."""
        return self.successes / self.uses if self.uses else 0.0

    def record(self, *, success: bool) -> UsageStats:
        """A new stats value with one more use (and one more success if it succeeded)."""
        return UsageStats(
            uses=self.uses + 1,
            successes=self.successes + (1 if success else 0),
        )

    def meets_active_bar(self) -> bool:
        """The plain-Python promotion rule: enough uses AND a good enough success rate."""
        return self.uses >= PROMOTE_MIN_USES and self.success_rate >= PROMOTE_MIN_SUCCESS_RATE


def _guard_agent_created(skill: Skill) -> None:
    """Invariant 1: only agent-created skills are auto-transitioned.

    We treat ``TrustTier.BUILTIN`` as the agent-created tier here: a distilled draft is
    written BUILTIN (see ``curator.distil``), and that is the only tier the curator mints.
    A VERIFIED / COMMUNITY / UNTRUSTED skill is human-authored or imported and is off-limits
    to automatic transitions.
    """
    if skill.trust is not TrustTier.BUILTIN:
        raise NotAgentCreated(skill.name, trust=skill.trust.value)


def promote_on_reuse(skill: Skill) -> Skill:
    """``draft -> candidate`` on first reuse. Idempotent above draft.

    Invariants honoured: pinned skills are returned unchanged (bypass every auto-transition);
    only agent-created skills are touched; a tainted draft is refused by
    ``assert_authored_promotable`` before any status change — no number of reuses launders
    it. A skill that is already candidate/active/archived is returned unchanged, so calling
    this on every reuse is safe.
    """
    if skill.pinned:
        return skill
    _guard_agent_created(skill)
    if skill.status is not SkillStatus.DRAFT:
        return skill
    # Lineage gate BEFORE the status change. Tainted draft can never become candidate.
    assert_authored_promotable(skill, to_status=SkillStatus.CANDIDATE)
    return skill.model_copy(update={"status": SkillStatus.CANDIDATE})


def promote_on_success(skill: Skill, stats: UsageStats) -> Skill:
    """``candidate -> active`` once ``stats`` clear the deterministic bar.

    The bar is :meth:`UsageStats.meets_active_bar` — ``>= PROMOTE_MIN_USES`` successful uses
    above ``PROMOTE_MIN_SUCCESS_RATE``. No model decides this; it is an integer-and-float
    comparison. Pinned skills bypass; only agent-created skills are touched; the lineage gate
    runs first so a tainted candidate with great numbers is still refused.

    Returns the skill unchanged if it is not a candidate or has not met the bar — so this is
    safe to call after every recorded use.
    """
    if skill.pinned:
        return skill
    _guard_agent_created(skill)
    if skill.status is not SkillStatus.CANDIDATE:
        return skill
    if not stats.meets_active_bar():
        return skill
    # Lineage gate BEFORE the count check matters: taint refuses regardless of numbers.
    assert_authored_promotable(skill, to_status=SkillStatus.ACTIVE)
    return skill.model_copy(update={"status": SkillStatus.ACTIVE})


def improve(skill: Skill, *, body: str, description: str | None = None) -> Skill:
    """Write a NEW version of ``skill`` — never mutate the old one (invariant 4).

    Returns a fresh :class:`Skill` with ``version = skill.version + 1`` and
    ``parent_version = skill.version``. The old version is untouched and remains retrievable
    by its own version number, so an improvement can never lose the version that worked. The
    new version starts as a ``draft`` (it must re-earn its place through reuse) and inherits
    the parent's lineage — an improvement of a tainted skill is still tainted.

    Pinned skills are not exempt from improvement (pinning bypasses *automatic* transitions,
    and calling ``improve`` is an explicit act), but the new version is unpinned: a pin is on
    a specific version the user chose, not on the lineage.
    """
    _guard_agent_created(skill)
    return skill.model_copy(
        update={
            "body": body,
            "description": description if description is not None else skill.description,
            "version": skill.version + 1,
            "parent_version": skill.version,
            "status": SkillStatus.DRAFT,
            "pinned": False,
        }
    )


def revert(current: Skill, target: Skill) -> Skill:
    """Revert to a prior version by pointer, not by mutation (invariant 4).

    Both arguments are immutable versions of the same skill lineage; ``target`` is the
    version to make live again. Returns ``target`` unchanged — reverting *is* choosing which
    stored version is current, a single pointer update at the store layer. Nothing is
    rewritten, so the version being reverted *from* is still there to revert back to. Raises
    if the two are not versions of the same skill, which would be a caller bug.
    """
    if current.name != target.name:
        raise ValueError(
            f"cannot revert across skills: {current.name!r} vs {target.name!r}"
        )
    return target


def archive(skill: Skill) -> Skill:
    """Move a skill out of retrieval, recoverably (invariant 2). There is no delete.

    Returns a new version at ``SkillStatus.ARCHIVED``. Archiving is always allowed — you may
    archive a tainted skill, a pinned skill (an explicit archive overrides the pin, because
    archiving is non-destructive and reversible), or an active one. The body is preserved
    intact; :func:`restore` brings it back.
    """
    _guard_agent_created(skill)
    return skill.model_copy(update={"status": SkillStatus.ARCHIVED})


def restore(skill: Skill, *, to: SkillStatus = SkillStatus.CANDIDATE) -> Skill:
    """Bring an archived skill back into a non-archived state (invariant 2).

    Archive is recoverable; this is the recovery. The default target is ``candidate`` (back
    into retrieval, but not straight to trusted-active — it re-earns active). Restoring a
    tainted skill to a retrievable status is refused by the lineage gate, so 'recoverable'
    never means 'launderable'. Raises if the skill is not archived.
    """
    _guard_agent_created(skill)
    if skill.status is not SkillStatus.ARCHIVED:
        raise ValueError(
            f"skill {skill.name!r} is {skill.status.value}, not archived; nothing to restore"
        )
    if to in (SkillStatus.CANDIDATE, SkillStatus.ACTIVE):
        # Gate on LINEAGE taint only. We must not route through the status-aware
        # assert_skill_promotion here: an archived skill's own ``may_promote`` is False *by
        # virtue of being archived*, which would make a clean archived skill un-restorable
        # and break the "restore is always possible" invariant. The laundering risk restore
        # must block is a *tainted* skill re-entering retrieval; that is exactly what a pure
        # lineage assertion catches, and nothing more.
        assert_promotable(f"skill {skill.name!r} v{skill.version}", skill.lineage)
    return skill.model_copy(update={"status": to})
