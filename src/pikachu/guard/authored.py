"""The agent-generated-skill gate — closing the distil laundering path.

WHY THIS MODULE EXISTS, AND WHY IT SHIPS BEFORE ``curator/``
-----------------------------------------------------------
``skills/scanner.py`` runs on **imported** skills. It did not run on **agent-generated**
ones. That gap turns the distil step in ``docs/13-self-improvement.md`` into a **privilege
laundering path**:

    poison one turn  ->  it is distilled into a ``draft``  ->  reuse promotes it  ->
    the injection is now durable *and* carries our own ``agent_created`` provenance.

The literature is explicit that per-session prompt filtering does not cover this — "memory
evolution can convert one-time indirect injection into persistent compromise"
(arXiv 2602.15654). So ``docs/06-security.md`` records a **hard prerequisite**: ``guard/``
must cover agent-generated skills *before* ``curator/`` may ship. This module is that
coverage. Everything in ``curator/`` routes a skill through here before it can be created or
promoted.

THE THREE REQUIREMENTS FROM ``docs/06-security.md`` (all enforced here)
----------------------------------------------------------------------
1. **Same scanner, no provenance trust.** An agent-generated body goes through the exact
   ``skills.scanner`` used for imported skills. ``agent_created`` provenance confers **no**
   trust: :func:`scan_authored_body` calls ``reject_or_raise`` identically for both.
2. **Taint is inherited, and taint blocks promotion.** A skill distilled from a turn that
   consumed untrusted tool output or a foreign skill body inherits that turn's taint via
   :func:`inherit_turn_lineage`, and :func:`assert_authored_promotable` refuses to promote
   anything tainted, raising :class:`~pikachu.core.errors.TaintedPromotion`.
3. **Authority is never widened.** A distilled skill cannot self-grant tools; that is the
   allowlist's job (invariant P3), enforced elsewhere. We do not derive authority here.

WHAT IS REUSED, NOT REIMPLEMENTED
---------------------------------
* Scanning: ``pikachu.skills.scanner`` (``scan`` / ``reject_or_raise``). No second scanner.
* Taint propagation: ``pikachu.guard.lineage`` (``derive`` / ``assert_skill_promotion``).
  This module is a thin, distil-specific wrapper over those primitives — it adds the
  "run the scanner over an agent body" and "gather a turn's taints" pieces the lineage
  layer does not know about, and delegates every monotonicity/promotion decision to it.

The scanner is imported lazily inside the functions that use it: a turn that never distils a
skill must not pull the scanner ruleset at import time (wave-2 lazy-import rule).
"""

from __future__ import annotations

from collections.abc import Iterable

from pikachu.core.errors import InjectionDetected
from pikachu.core.types import Lineage, Skill, SkillStatus, Taint, TrustTier
from pikachu.guard.lineage import assert_promotable, assert_skill_promotion, derive

__all__ = [
    "assert_authored_promotable",
    "authored_is_clean",
    "inherit_turn_lineage",
    "scan_authored_body",
]


def scan_authored_body(body: str, *, skill_name: str | None = None) -> None:
    """Run the SAME injection scanner over an agent-generated body as over imported ones.

    Provenance ``agent_created`` confers no trust, so this is the identical enforcement
    call the importer makes: on any finding at or above the scanner's rejection threshold it
    raises :class:`~pikachu.core.errors.InjectionDetected`. There is deliberately no
    "sanitise and accept" path — the scanner does not understand a payload well enough to
    neutralise it, so a detected payload is rejected outright.

    Imported lazily so a turn that never distils a skill does not load the ruleset.
    """
    from pikachu.skills.scanner import reject_or_raise

    reject_or_raise(body, skill_name=skill_name)


def inherit_turn_lineage(*turn_sources: Lineage) -> Lineage:
    """The lineage a skill distilled from a turn inherits.

    A distilled skill is *derived from* everything the turn consumed, so its lineage is the
    monotonic union of those sources' lineages — exactly what
    :func:`pikachu.guard.lineage.derive` computes. If any consumed source was tainted (a
    foreign skill body, untrusted tool output, a canvas read), the distilled skill inherits
    that taint and can never be promoted.

    Called with no sources this returns a clean lineage: a skill distilled from a turn that
    consumed nothing untrusted is clean, which is the correct base case.
    """
    return derive(*turn_sources)


def authored_is_clean(*turn_sources: Lineage) -> bool:
    """Whether a skill distilled from these turn sources would be promotable on lineage.

    A convenience for the distil gate's own bookkeeping: it lets the curator record *why* a
    candidate is or is not eligible without catching an exception. The authoritative refusal
    is still :func:`assert_authored_promotable`, which raises.
    """
    return inherit_turn_lineage(*turn_sources).is_clean


def assert_authored_promotable(
    skill: Skill,
    *,
    to_status: SkillStatus = SkillStatus.CANDIDATE,
    extra_sources: Iterable[Lineage] = (),
) -> None:
    """Refuse to promote an agent-generated skill that is (or would become) tainted.

    Two checks, both raising :class:`~pikachu.core.errors.TaintedPromotion`:

      * the skill's *own* lineage against the target status — delegated to
        :func:`pikachu.guard.lineage.assert_skill_promotion`, so the rule that a tainted
        skill can never reach a retrievable status is enforced by exactly one place; and
      * any ``extra_sources`` (e.g. a re-derivation at promotion time) merged in, so a skill
        that became clean on paper but is being promoted alongside a tainted source is still
        refused.

    Promotion into a non-trusted status (DRAFT, ARCHIVED) is always allowed regardless of
    taint — you may keep tainted evidence, you just may never trust it. This mirrors the
    "decay lowers rank, never deletes" philosophy and is inherited straight from
    ``assert_skill_promotion``.
    """
    # The skill's own lineage vs. the target status (the primary laundering gate).
    assert_skill_promotion(skill, to_status=to_status)

    # A promotion carrying additional tainted context is refused too, but only when the
    # target is actually a trusted status — keeping a tainted draft is fine.
    combined = derive(skill.lineage, *extra_sources)
    if to_status in (SkillStatus.CANDIDATE, SkillStatus.ACTIVE):
        assert_promotable(f"skill {skill.name!r} v{skill.version}", combined)
