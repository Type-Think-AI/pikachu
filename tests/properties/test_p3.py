"""P3, proven for arbitrary input (hypothesis). Earns **Thunder**.

The one invariant this file exists to prove:

    effective_tools(fixed_allowlist, declared).tools
        ⊆ set(fixed_allowlist) ∩ set(declared)      (when declared is not None)
        ⊆ set(fixed_allowlist)                       (when declared is None)

— always, for every input, including the adversarial shapes that broke the parent system:
empty, unicode, whitespace-padded, mixed case, and duplicated names.

Everything is compared in NORMALIZED form, because that is the only form in which "the same
tool" is a well-defined question — the whole point of :func:`normalize_tool_name` is that
``" TERMINAL "`` and ``"terminal"`` are one tool, not two.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from pikachu.core.types import normalize_tool_name
from pikachu.guard.allowlist import DANGEROUS_TOOLS, effective_tools

pytestmark = __import__("pytest").mark.thunder


def _norm_set(names: object) -> set[str]:
    """Normalized, non-empty set of an iterable of raw names."""
    assert isinstance(names, (list, tuple))
    return {normalize_tool_name(str(n)) for n in names} - {""}


# Deliberately nasty tool-name alphabet: ASCII letters, digits, the punctuation
# normalize_tool_name keeps and strips, spaces, mixed case, and a couple of unicode chars
# that normalize to empty. Include the literal dangerous names so they actually occur.
_raw_name = st.one_of(
    st.text(
        alphabet="abcABC_.- 019!@#\t\u00e9\u4e2d",
        min_size=0,
        max_size=12,
    ),
    st.sampled_from(["terminal", " TERMINAL ", "bash", "read_file", "browser", "web", "web"]),
)

_name_lists = st.lists(_raw_name, min_size=0, max_size=10)


@given(fixed=_name_lists, declared=st.one_of(st.none(), _name_lists))
def test_p3_subset(fixed: list[str], declared: list[str] | None) -> None:
    """effective ⊆ allowlist ∩ declared, for every input."""
    result = effective_tools(fixed, declared)
    got = set(result.tools)

    allow = _norm_set(fixed)
    if declared is None:
        upper_bound = allow
    else:
        upper_bound = allow & _norm_set(declared)

    assert got <= upper_bound, (fixed, declared, result.tools)


@given(fixed=_name_lists, declared=st.one_of(st.none(), _name_lists))
def test_no_dangerous_tool_ever_appears(
    fixed: list[str], declared: list[str] | None
) -> None:
    """No dangerous tool is ever in .tools, on either the None or the declared path."""
    result = effective_tools(fixed, declared)
    assert not (set(result.tools) & DANGEROUS_TOOLS), result.tools


@given(fixed=_name_lists, declared=_name_lists)
def test_removed_accounts_for_every_dropped_item(
    fixed: list[str], declared: list[str]
) -> None:
    """Every normalized, non-empty declared item is either kept or removed — nothing
    vanishes unaccounted for, and every removed tool has a reason."""
    result = effective_tools(fixed, declared)
    declared_norm = _norm_set(declared)
    accounted = set(result.tools) | set(result.removed_tools)
    # Everything that survived normalization is accounted for one way or the other.
    assert declared_norm <= accounted, (declared, result)
    # reasons has an entry for exactly the removed tools.
    assert set(result.reasons) == set(result.removed_tools)
    # Nothing appears in the output that was not declared (after normalization).
    assert accounted <= declared_norm


@given(fixed=_name_lists, declared=st.one_of(st.none(), _name_lists))
def test_idempotent(fixed: list[str], declared: list[str] | None) -> None:
    """Feeding .tools back in as `declared` yields the same tool set.

    Once narrowed, re-narrowing changes nothing: the guard has reached a fixed point. This
    is what lets a toolset be re-validated at any boundary without drift.
    """
    first = effective_tools(fixed, declared)
    second = effective_tools(fixed, first.tools)
    assert set(second.tools) == set(first.tools), (first.tools, second.tools)


@given(fixed=_name_lists, declared=st.one_of(st.none(), _name_lists))
def test_multiplicity_preserved_for_kept_tools(
    fixed: list[str], declared: list[str] | None
) -> None:
    """For the declared path, the count of each kept tool equals its normalized count in
    the declared input (deduping is forbidden)."""
    if declared is None:
        return
    result = effective_tools(fixed, declared)
    for tool in set(result.tools):
        declared_count = sum(1 for d in declared if normalize_tool_name(str(d)) == tool)
        assert result.tools.count(tool) == declared_count
