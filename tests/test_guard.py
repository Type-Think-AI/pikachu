"""Example tests for the guard — the specific historical failure cases.

Earns **Thunder** (badge 3) together with ``tests/properties/test_p3.py``. Where that file
proves P3 for arbitrary input, this file pins the concrete behaviours a refactor might
quietly break: the None-vs-empty distinction, order/multiplicity survival, dangerous-tool
stripping, normalization on every entry point, and the trust ladder.
"""

from __future__ import annotations

import pytest

from pikachu.core.types import Skill, SkillStatus, TrustTier
from pikachu.guard.allowlist import (
    DANGEROUS_TOOLS,
    EffectiveToolset,
    effective_tools,
)
from pikachu.guard.trust import may_load, resolve_trust

pytestmark = pytest.mark.thunder


# --------------------------------------------------------------------------------------
# effective_tools — the None vs () distinction
# --------------------------------------------------------------------------------------


def test_declared_none_yields_full_allowlist(fixed_allowlist: tuple[str, ...]) -> None:
    """None means 'declared nothing' -> inherit the whole allowlist."""
    result = effective_tools(fixed_allowlist, None)
    assert result.tools == fixed_allowlist
    assert result.removed_tools == ()
    assert result.reasons == {}


def test_declared_empty_yields_no_tools(fixed_allowlist: tuple[str, ...]) -> None:
    """() means 'declared an empty set' -> no tools. Distinct from None."""
    result = effective_tools(fixed_allowlist, ())
    assert result.tools == ()
    assert result.removed_tools == ()


def test_none_and_empty_are_different(fixed_allowlist: tuple[str, ...]) -> None:
    assert effective_tools(fixed_allowlist, None).tools != effective_tools(
        fixed_allowlist, ()
    ).tools


# --------------------------------------------------------------------------------------
# P3 — a skill can only narrow, never widen
# --------------------------------------------------------------------------------------


def test_narrows_to_intersection(fixed_allowlist: tuple[str, ...]) -> None:
    result = effective_tools(fixed_allowlist, ("generate_image", "read_canvas"))
    assert result.tools == ("generate_image", "read_canvas")


def test_declared_tool_not_in_allowlist_is_denied(
    fixed_allowlist: tuple[str, ...],
) -> None:
    """The core escalation defence: a declared tool absent from the fixed allowlist is
    denied and recorded — it can never be granted."""
    result = effective_tools(fixed_allowlist, ("generate_image", "delete_everything"))
    assert result.tools == ("generate_image",)
    assert "delete_everything" in result.removed_tools
    assert result.reasons["delete_everything"] == "not in fixed allowlist"


def test_cannot_widen_beyond_allowlist(fixed_allowlist: tuple[str, ...]) -> None:
    """No declared set, however large, produces a tool outside the allowlist."""
    result = effective_tools(fixed_allowlist, ("a", "b", "c", "web_search"))
    assert set(result.tools) <= set(fixed_allowlist)


# --------------------------------------------------------------------------------------
# Order and multiplicity survive — pinned. Do NOT dedupe or sort.
# --------------------------------------------------------------------------------------


def test_duplicates_survive() -> None:
    """('web','web') must stay ('web','web'). A pinned parent-repo test depends on this."""
    result = effective_tools(("web",), ("web", "web"))
    assert result.tools == ("web", "web")


def test_order_survives() -> None:
    """Declaration order is preserved; the output is not sorted."""
    allow = ("zebra", "alpha", "mango")
    result = effective_tools(allow, ("mango", "zebra", "alpha"))
    assert result.tools == ("mango", "zebra", "alpha")


def test_multiplicity_and_order_survive_together() -> None:
    allow = ("web", "canvas")
    result = effective_tools(allow, ("canvas", "web", "canvas", "web"))
    assert result.tools == ("canvas", "web", "canvas", "web")


# --------------------------------------------------------------------------------------
# Normalization on EVERY entry point — the terminal/TERMINAL/" terminal " incident
# --------------------------------------------------------------------------------------


def test_whitespace_padded_declared_is_normalized() -> None:
    result = effective_tools(("web_search",), (" web_search ",))
    assert result.tools == ("web_search",)


def test_mixed_case_declared_is_normalized() -> None:
    result = effective_tools(("web_search",), ("WEB_SEARCH",))
    assert result.tools == ("web_search",)


def test_whitespace_padded_allowlist_is_normalized() -> None:
    """Normalization applies to the allowlist too — not only the declared side."""
    result = effective_tools((" WEB_SEARCH ",), ("web_search",))
    assert result.tools == ("web_search",)


def test_padded_terminal_is_stripped_not_survived() -> None:
    """The exact historical bug: ' terminal ' and 'TERMINAL' must not survive."""
    result = effective_tools(("web_search",), (" terminal ", "TERMINAL", "web_search"))
    assert "terminal" not in result.tools
    assert result.tools == ("web_search",)
    assert result.removed_tools.count("terminal") == 2


# --------------------------------------------------------------------------------------
# Dangerous tools — stripped, recorded, never silently dropped
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("dangerous", sorted(DANGEROUS_TOOLS))
def test_dangerous_tool_stripped_even_if_declared_and_allowed(dangerous: str) -> None:
    """Even when the host allowlist contains a dangerous tool, it is stripped."""
    result = effective_tools((dangerous, "web_search"), (dangerous, "web_search"))
    assert dangerous not in result.tools
    assert dangerous in result.removed_tools
    assert result.reasons[dangerous] == "dangerous tool, always stripped"


def test_dangerous_tool_stripped_when_declared_none() -> None:
    """The None path inherits the allowlist — dangerous tools must be stripped there too,
    or the most common call (skill declares nothing) would leak them."""
    result = effective_tools(("bash", "web_search"), None)
    assert "bash" not in result.tools
    assert "bash" in result.removed_tools


# --------------------------------------------------------------------------------------
# Fail closed — never raise from the filter
# --------------------------------------------------------------------------------------


def test_never_raises_on_garbage() -> None:
    """Junk input yields a denial, not an exception. The filter must fail closed."""
    result = effective_tools(("", "  ", "web_search"), ("", "  ", "!!!", "web_search"))
    assert isinstance(result, EffectiveToolset)
    assert result.tools == ("web_search",)


# --------------------------------------------------------------------------------------
# removed_tools accounts for everything dropped
# --------------------------------------------------------------------------------------


def test_removed_accounts_for_every_dropped_declared_item() -> None:
    allow = ("web_search",)
    declared = ("web_search", "bash", "delete_all")
    result = effective_tools(allow, declared)
    # Every normalized, non-empty declared item is either kept or removed, nothing vanishes.
    assert set(result.tools) | set(result.removed_tools) == {
        "web_search",
        "bash",
        "delete_all",
    }
    assert set(result.reasons) == set(result.removed_tools)


# --------------------------------------------------------------------------------------
# Trust ladder
# --------------------------------------------------------------------------------------


def test_resolve_trust_matches_core_rule() -> None:
    assert resolve_trust(TrustTier.BUILTIN) is True
    assert resolve_trust(TrustTier.VERIFIED) is True
    assert resolve_trust(TrustTier.COMMUNITY) is False
    assert resolve_trust(TrustTier.UNTRUSTED) is False


def test_builtin_may_load_under_review(builtin_skill: Skill) -> None:
    assert may_load(builtin_skill, require_review=True) is True
    assert may_load(builtin_skill, require_review=False) is True


def test_untrusted_never_loads(foreign_skill: Skill) -> None:
    assert may_load(foreign_skill, require_review=False) is False
    assert may_load(foreign_skill, require_review=True) is False


def test_community_loads_only_without_review() -> None:
    community = Skill(
        name="community-thing",
        description="A community skill declaring nothing.",
        status=SkillStatus.CANDIDATE,
        trust=TrustTier.COMMUNITY,
    )
    assert may_load(community, require_review=False) is True
    assert may_load(community, require_review=True) is False


def test_verified_loads_under_review() -> None:
    verified = Skill(
        name="verified-thing",
        description="Verified skill.",
        declared_tools=("generate_image",),
        status=SkillStatus.ACTIVE,
        trust=TrustTier.VERIFIED,
    )
    assert may_load(verified, require_review=True) is True
    assert may_load(verified, require_review=False) is True
