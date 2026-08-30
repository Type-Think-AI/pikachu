"""The single admission point every untrusted-input boundary calls — S2 made one path.

The product claim is that we supply the permission layer the standards leave out. That claim
is only true if a hostile skill, a hostile plugin **and** a hostile MCP server are refused by
the *same* code path — not by three mechanisms that each happen to work. Before this module,
they were three: ``mcp/`` and ``a2a/`` routed through :func:`~pikachu.guard.effective_tools`,
while ``skills/``, ``plugins/`` and ``webmcp/`` relied on the type contract (an ``UNTRUSTED``
skill cannot declare tools, so the frozen :class:`~pikachu.core.types.Skill` model rejects it).
The type contract is real defence and stays — but "three things that each work" is not the
one-path guarantee we advertise.

:func:`admit` is that one path. Every boundary that receives tools from outside the trust
boundary calls it in one line and gets back an :class:`Admission`:

  * the **narrowed** toolset, computed by :func:`~pikachu.guard.effective_tools` — this module
    COMPOSES P3, it does not reimplement or replace it;
  * the tools that were **removed** and why;
  * the source's **lineage with the correct taint merged in** — a foreign skill, a plugin, an
    MCP server and a web page each taint the same way the boundary already tainted them;
  * the **trust tier** it was admitted at, carried through so a caller need not re-derive it.

Two rules inherited from the guard, non-negotiable here too:

  * **Never raises for a denied tool — it OMITS.** Raising from a tool-preparation filter is
    unsupported by the underlying framework and turns a graceful denial into a hard error.
    :class:`~pikachu.core.errors.ToolDenied` is for the call boundary, not for this filter.
  * **Authority is never derived from the artifact requesting it.** The fixed allowlist is the
    only source of authority; ``declared_tools`` is a request, narrowed against it.

The signature is deliberately small — ``source`` plus four keywords — so every boundary can
call it in one line. A shared path only stays shared if it is cheap to call; a filter that
needs six positional arguments at each site is a filter that gets bypassed, which is exactly
how S2 broke in the first place.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from pikachu.core.types import Lineage, Taint, TrustTier
from pikachu.guard.allowlist import effective_tools

__all__ = [
    "Admission",
    "SourceKind",
    "admit",
]


class SourceKind(str, Enum):
    """The kind of untrusted boundary a toolset arrived from.

    The kind selects the :class:`~pikachu.core.types.Taint` merged into the admitted lineage.
    Each value maps to the taint the corresponding boundary already applied on its own path, so
    routing that boundary through :func:`admit` does not change the taint it produces — it only
    makes the *narrowing* shared. The mapping lives in :data:`_TAINT_FOR_KIND`.
    """

    FOREIGN_SKILL = "foreign_skill"
    """A skill from an untrusted origin (a bundle of unknown provenance loaded mid-run)."""

    PLUGIN = "plugin"
    """A plugin directory — it ships third-party code, so it is the sharpest case."""

    MCP_SERVER = "mcp_server"
    """A remote MCP server advertising tools. Its list is a request, never a grant."""

    WEB_PAGE = "web_page"
    """A browser page exposing/consuming tools via WebMCP. Attacker-controllable content."""


#: Which taint each source kind merges into the admitted lineage. These reuse existing
#: :class:`~pikachu.core.types.Taint` values rather than inventing new ones, and they match
#: what each boundary already applied on its own path:
#:
#:   * a foreign skill and a plugin are ``FOREIGN_SKILL`` — the same taint ``skills/loader``
#:     and ``plugins/loader`` already set;
#:   * an MCP server is ``FOREIGN_SKILL`` — a remote tool descriptor is a foreign declaration
#:     whose text is attacker-controllable, exactly as ``mcp/client`` tags it;
#:   * a web page is ``CANVAS_READ`` — the closest existing taint for content read out of a
#:     shared, externally-writable surface.
_TAINT_FOR_KIND: dict[SourceKind, Taint] = {
    SourceKind.FOREIGN_SKILL: Taint.FOREIGN_SKILL,
    SourceKind.PLUGIN: Taint.FOREIGN_SKILL,
    SourceKind.MCP_SERVER: Taint.FOREIGN_SKILL,
    SourceKind.WEB_PAGE: Taint.CANVAS_READ,
}


class Admission(BaseModel):
    """The result of admitting an untrusted toolset through the one guard path.

    ``tools`` is the narrowed tuple the boundary may actually offer, in the guard's order with
    duplicates intact. ``removed_tools`` / ``reasons`` account for everything dropped.
    ``lineage`` is the source's lineage with this kind's taint merged in — monotonic, never
    cleared. ``trust`` is the tier the source was admitted at, carried through so a caller does
    not re-derive it.

    Frozen: an admission is a decision, not a mutable buffer.
    """

    model_config = ConfigDict(frozen=True)

    tools: tuple[str, ...] = Field(default_factory=tuple)
    removed_tools: tuple[str, ...] = Field(default_factory=tuple)
    reasons: dict[str, str] = Field(default_factory=dict)
    lineage: Lineage = Field(default_factory=Lineage.clean)
    trust: TrustTier = TrustTier.UNTRUSTED


def admit(
    source: str,
    *,
    declared_tools: Sequence[str] | None,
    fixed_allowlist: Sequence[str],
    trust: TrustTier = TrustTier.UNTRUSTED,
    lineage: Lineage | None = None,
    kind: SourceKind = SourceKind.FOREIGN_SKILL,
) -> Admission:
    """Admit an untrusted toolset — the single path every boundary shares.

    :param source: A human/audit label for where this came from (a skill name, a plugin path,
        an MCP server name, a page origin). Recorded in the merged lineage's sources.
    :param declared_tools: What the source ASKS for. ``None`` means *declared nothing* — which,
        per :func:`~pikachu.guard.effective_tools`, inherits the whole (dangerous-filtered)
        allowlist; an empty sequence means *declared an empty set* and yields no tools. The two
        are different and both are honoured, because ``effective_tools`` distinguishes them.
    :param fixed_allowlist: The host-supplied allowlist — the ONLY source of authority.
    :param trust: The tier this source is admitted at. Carried through onto the
        :class:`Admission`; it does not itself widen or narrow tools (P3 does that), it records
        the tier the caller decided on.
    :param lineage: The source's existing lineage, if any. This kind's taint is merged into it.
        ``None`` starts from a clean lineage.
    :param kind: Which boundary this is, selecting the taint merged in (see :class:`SourceKind`).
    :returns: An :class:`Admission`.

    **This function never raises for a denied tool.** It delegates narrowing to
    :func:`~pikachu.guard.effective_tools`, which omits denied tools into ``removed_tools`` — the
    fail-closed rule the whole guard obeys. It composes P3; it does not reimplement it.
    """
    narrowed = effective_tools(fixed_allowlist, declared_tools)

    base = lineage if lineage is not None else Lineage.clean()
    merged = base.with_taint(_TAINT_FOR_KIND[kind], source)

    return Admission(
        tools=narrowed.tools,
        removed_tools=narrowed.removed_tools,
        reasons=narrowed.reasons,
        lineage=merged,
        trust=trust,
    )
