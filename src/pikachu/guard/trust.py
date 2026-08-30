"""The trust-tiered execution ladder — the load gate.

Two decisions live here, and only these two:

* :func:`resolve_trust` — the single, authoritative answer to "may a skill at this tier
  contribute tools at all?". It delegates to
  :attr:`~pikachu.core.types.TrustTier.may_contribute_tools`, which already encodes the rule
  (BUILTIN and VERIFIED may; COMMUNITY and UNTRUSTED never do). We do NOT duplicate the
  rule here — a second copy is a second thing that can drift out of sync with the first.

* :func:`may_load` — whether a skill may be loaded into a run given the caller's review
  policy.

This is necessary-but-not-sufficient authority. Even when :func:`resolve_trust` says a skill
*may* contribute tools, the allowlist intersection in :mod:`pikachu.guard.allowlist` still
applies. Authority is never derived from the artifact requesting it.
"""

from __future__ import annotations

from pikachu.core.types import Skill, TrustTier

__all__ = [
    "may_load",
    "resolve_trust",
]


def resolve_trust(tier: TrustTier) -> bool:
    """Whether a skill at ``tier`` may contribute tools to a run.

    Thin, deliberate delegation to :attr:`TrustTier.may_contribute_tools`. The rule
    (BUILTIN and VERIFIED yes; COMMUNITY and UNTRUSTED no) is defined exactly once, in the
    frozen core contract, and read from there.
    """
    return tier.may_contribute_tools


def may_load(skill: Skill, *, require_review: bool) -> bool:
    """Whether ``skill`` may be loaded into a run.

    :param skill: The skill under consideration.
    :param require_review: The caller's policy. When ``True``, only tiers that have actually
        been human-reviewed — BUILTIN (reviewed at commit time) and VERIFIED (scanned AND
        human-reviewed) — may load. When ``False``, COMMUNITY may also load on the strength
        of a clean scan alone.

    UNTRUSTED never loads: it is foreign, mid-run, or of unknown provenance, and the
    :class:`Skill` model already makes it structurally impossible for it to declare tools.

    **Honest limitation:** auto-approving a COMMUNITY skill on a clean scan
    (``require_review=False``) is UNSAFE. The scanner is pattern-based; it catches literal
    "ignore previous instructions" and misses paraphrased injection entirely. A clean scan
    is evidence of nothing more than the absence of the specific strings we test for. This
    function does not — cannot — certify a skill as safe; it only applies the policy the
    caller chose, and the caller should treat ``require_review=False`` as a knowing
    acceptance of that risk, not as a safety guarantee.
    """
    if skill.trust is TrustTier.UNTRUSTED:
        return False

    if require_review:
        # Only tiers that carry an actual human review.
        return skill.trust in (TrustTier.BUILTIN, TrustTier.VERIFIED)

    # Review not required: COMMUNITY may load on a clean scan alone. See the docstring —
    # this is a policy choice with a known blind spot, not a safety claim.
    return skill.trust in (
        TrustTier.BUILTIN,
        TrustTier.VERIFIED,
        TrustTier.COMMUNITY,
    )
