"""S2 — one admission path for every untrusted-input boundary. Earns **Thunder**.

Success criterion S2 is the claim the product positioning rests on: *we supply the permission
layer the standards leave out*. That is only a guarantee if a hostile skill, a hostile plugin
**and** a hostile MCP server are refused by the SAME code path — proven for arbitrary input,
not demonstrated on three examples. Three mechanisms that each happen to work is not a one-path
guarantee, and "each happens to work" is exactly how S2 broke: ``skills/``, ``plugins/`` and
``webmcp/`` had no guard reference at all, relying on the type contract instead.

This file proves two things:

1. **Behavioural** (hypothesis, arbitrary hostile input): for each of the three source kinds
   the SAME :func:`pikachu.guard.untrusted.admit` call narrows identically, and no source kind
   can ever yield a tool outside ``allowlist ∩ declared``. The taint differs per kind — a
   plugin, a foreign skill and an MCP server are distinguishable in lineage — but the NARROWING
   does not.
2. **Structural** (introspection): ``plugins/loader.py``, ``webmcp/tools.py`` and
   ``skills/loader.py`` each actually reference ``admit``. This is the test that stops a future
   module quietly bypassing the path — the precise regression that made S2 false before.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pikachu.core.types import Taint, TrustTier, normalize_tool_name
from pikachu.guard.untrusted import Admission, SourceKind, admit

pytestmark = pytest.mark.thunder


# The three source kinds S2 names explicitly. WEB_PAGE is exercised too (it was also a gap),
# but skill / plugin / MCP server are the trio the criterion is written around.
_S2_KINDS = (SourceKind.FOREIGN_SKILL, SourceKind.PLUGIN, SourceKind.MCP_SERVER)

# The modules that MUST route through admit. These are the three Lane T boundaries; mcp/ and
# a2a/ already route through effective_tools and are converged separately.
_ROUTED_MODULES = (
    "src/pikachu/plugins/loader.py",
    "src/pikachu/webmcp/tools.py",
    "src/pikachu/skills/loader.py",
)


def _repo_root() -> Path:
    """The pikachu project root, found by walking up to the dir that holds ``src/pikachu``."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "pikachu").is_dir():
            return parent
    raise AssertionError("could not locate the pikachu repo root from the test file")


def _norm_set(names: object) -> set[str]:
    assert isinstance(names, (list, tuple))
    return {normalize_tool_name(str(n)) for n in names} - {""}


# A deliberately hostile alphabet: the punctuation the normalizer keeps and strips, whitespace,
# mixed case, unicode that normalizes to empty, and the literal dangerous names.
_raw_name = st.one_of(
    st.text(alphabet="abcABC_.- 019!@#\t\u00e9\u4e2d", min_size=0, max_size=12),
    st.sampled_from(["terminal", " TERMINAL ", "bash", "read_file", "browser", "web", "web"]),
)
_name_lists = st.lists(_raw_name, min_size=0, max_size=10)


# ---------------------------------------------------------------------------------------
# Behavioural: one narrowing, three kinds
# ---------------------------------------------------------------------------------------


@given(fixed=_name_lists, declared=st.one_of(st.none(), _name_lists))
def test_all_three_kinds_narrow_identically(
    fixed: list[str], declared: list[str] | None
) -> None:
    """The SAME admit call narrows a hostile toolset identically across skill, plugin, MCP.

    This is the core of S2: not three parallel mechanisms, one path. The tool tuple, the removed
    set and the reasons must be byte-for-byte identical regardless of which untrusted boundary
    the input arrived from.
    """
    admissions = [
        admit("hostile", declared_tools=declared, fixed_allowlist=fixed, kind=k)
        for k in _S2_KINDS
    ]
    first = admissions[0]
    for other in admissions[1:]:
        assert other.tools == first.tools, (first.tools, other.tools)
        assert other.removed_tools == first.removed_tools
        assert other.reasons == first.reasons


@given(fixed=_name_lists, declared=st.one_of(st.none(), _name_lists))
def test_no_kind_yields_a_tool_outside_allowlist_intersect_declared(
    fixed: list[str], declared: list[str] | None
) -> None:
    """For EVERY source kind, admitted tools ⊆ allowlist ∩ declared (P3 across the boundary)."""
    allow = _norm_set(fixed)
    for kind in (*_S2_KINDS, SourceKind.WEB_PAGE):
        adm = admit("hostile", declared_tools=declared, fixed_allowlist=fixed, kind=kind)
        got = set(adm.tools)
        upper = allow if declared is None else allow & _norm_set(declared)
        assert got <= upper, (kind, fixed, declared, adm.tools)


@given(fixed=_name_lists, declared=_name_lists)
def test_taint_differs_by_kind_but_narrowing_does_not(
    fixed: list[str], declared: list[str]
) -> None:
    """The taint distinguishes the source kind; the narrowing is invariant to it.

    A plugin, a foreign skill and an MCP server must remain distinguishable in lineage (so a
    downstream taint check can tell where a value came from), yet the *set of tools* they are
    allowed must not depend on that distinction.
    """
    web = admit("s", declared_tools=declared, fixed_allowlist=fixed, kind=SourceKind.WEB_PAGE)
    skill = admit(
        "s", declared_tools=declared, fixed_allowlist=fixed, kind=SourceKind.FOREIGN_SKILL
    )
    # Narrowing invariant to kind.
    assert web.tools == skill.tools
    assert web.removed_tools == skill.removed_tools
    # Taint distinguishes the kinds: WEB_PAGE taints CANVAS_READ, a foreign skill FOREIGN_SKILL.
    assert Taint.CANVAS_READ in web.lineage.taints
    assert Taint.FOREIGN_SKILL in skill.lineage.taints
    assert web.lineage.taints != skill.lineage.taints


@given(fixed=_name_lists, declared=st.one_of(st.none(), _name_lists))
def test_admission_lineage_is_always_tainted(
    fixed: list[str], declared: list[str] | None
) -> None:
    """Admitting untrusted input can never produce a clean lineage — taint is monotonic."""
    for kind in (*_S2_KINDS, SourceKind.WEB_PAGE):
        adm = admit("hostile", declared_tools=declared, fixed_allowlist=fixed, kind=kind)
        assert not adm.lineage.is_clean, (kind, adm.lineage)
        assert "hostile" in adm.lineage.sources


@given(fixed=_name_lists, declared=_name_lists)
def test_admission_never_raises_and_accounts_for_every_declared_tool(
    fixed: list[str], declared: list[str]
) -> None:
    """admit omits denied tools (never raises); kept ∪ removed accounts for every declared one."""
    adm = admit("hostile", declared_tools=declared, fixed_allowlist=fixed)
    assert isinstance(adm, Admission)
    declared_norm = _norm_set(declared)
    accounted = set(adm.tools) | set(adm.removed_tools)
    assert declared_norm <= accounted, (declared, adm)
    assert accounted <= declared_norm
    assert set(adm.reasons) == set(adm.removed_tools)


def test_declared_none_vs_empty_are_distinct() -> None:
    """None (declared nothing) inherits the allowlist; () (declared empty) yields no tools.

    Same distinction ``effective_tools`` makes — admit must preserve it for every kind.
    """
    allow = ("web", "search")
    for kind in (*_S2_KINDS, SourceKind.WEB_PAGE):
        inherit = admit("s", declared_tools=None, fixed_allowlist=allow, kind=kind)
        empty = admit("s", declared_tools=(), fixed_allowlist=allow, kind=kind)
        assert set(inherit.tools) == {"web", "search"}, kind
        assert empty.tools == (), kind


# ---------------------------------------------------------------------------------------
# Structural: the boundaries actually call admit (the anti-bypass guard)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", _ROUTED_MODULES)
def test_boundary_module_references_admit(rel_path: str) -> None:
    """Each of the three untrusted boundaries actually references ``admit`` in its source.

    This is the test that keeps S2 true over time. It failed-by-absence before Lane T: the
    modules simply did not mention the guard. Asserting it by introspection means a future edit
    that removes the routing — the exact way S2 broke — fails here instead of shipping a silent
    bypass. We check both an ``admit`` import from the guard AND a call to it, via AST rather
    than a substring grep so a comment mentioning the word cannot satisfy the check.
    """
    path = _repo_root() / rel_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imports_admit = any(
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("pikachu.guard")
        and any(alias.name == "admit" for alias in node.names)
        for node in ast.walk(tree)
    )
    calls_admit = any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "admit")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "admit")
        )
        for node in ast.walk(tree)
    )

    assert imports_admit, f"{rel_path} does not import admit from pikachu.guard"
    assert calls_admit, f"{rel_path} imports admit but never calls it"


def test_all_three_s2_kinds_are_distinct_source_kinds() -> None:
    """Guard against the trio silently collapsing to one enum member."""
    assert len(set(_S2_KINDS)) == 3


def test_admit_is_the_guard_public_symbol() -> None:
    """admit is exported from the guard package, so boundaries import one stable name."""
    import pikachu.guard as guard

    assert "admit" in guard.__all__
    assert guard.admit is admit
    # And it is admitted at the untrusted tier by default.
    adm = admit("s", declared_tools=("web",), fixed_allowlist=("web",))
    assert adm.trust is TrustTier.UNTRUSTED
