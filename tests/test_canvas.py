"""Canvas graph — example-based tests.

Covers every behaviour the lane spec names explicitly:

* a duplicate id is rejected with a typed error;
* a revision creates a NEW node with ``parent`` set and mutates nothing;
* the producing agent is recorded in provenance;
* traversal (children / ancestors / descendants / lineage_of) is correct on a multi-level
  graph;
* a cyclic / self-referential parent pointer does not hang the process;
* reading a tainted artifact taints the reader;
* an unapproved gated artifact cannot be read (fails closed).

The property that no operation *ever* mutates an existing artifact, over arbitrary op
sequences, lives in ``tests/properties/test_canvas.py``.
"""

from __future__ import annotations

import pytest

from pikachu.canvas.graph import CanvasGraph, DuplicateArtifactError, taint_for_reader
from pikachu.core.errors import ApprovalRequired
from pikachu.core.protocols import CanvasStore
from pikachu.core.types import (
    Artifact,
    ArtifactKind,
    Lineage,
    Provenance,
    Taint,
)


def _art(
    aid: str,
    *,
    parent: str | None = None,
    lineage: Lineage | None = None,
    produced_by: str | None = None,
    kind: ArtifactKind = ArtifactKind.IMAGE,
) -> Artifact:
    return Artifact(
        id=aid,
        kind=kind,
        payload_ref=f"ref://{aid}",
        parent=parent,
        provenance=Provenance(produced_by=produced_by),
        lineage=lineage or Lineage.clean(),
    )


# -- Protocol conformance ---------------------------------------------------------------


def test_graph_is_a_canvas_store() -> None:
    """CanvasGraph structurally satisfies the CanvasStore protocol (Cascade-style check)."""
    assert isinstance(CanvasGraph(), CanvasStore)


# -- Append-only: duplicate id rejected -------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_id_rejected_with_typed_error() -> None:
    g = CanvasGraph()
    original = _art("a")
    await g.append(original)

    with pytest.raises(DuplicateArtifactError) as ei:
        await g.append(_art("a", produced_by="imposter"))
    assert ei.value.artifact_id == "a"
    # The original is untouched — the append-only rule held.
    stored = await g.get("a")
    assert stored is original


@pytest.mark.asyncio
async def test_duplicate_id_does_not_overwrite() -> None:
    g = CanvasGraph()
    await g.append(_art("a", produced_by="agent-1"))
    imposter = _art("a", produced_by="agent-2")
    with pytest.raises(DuplicateArtifactError):
        await g.append(imposter)
    stored = await g.get("a")
    assert stored is not None
    assert stored.provenance.produced_by == "agent-1"


# -- Revision is a new node with parent set ---------------------------------------------


@pytest.mark.asyncio
async def test_revision_creates_new_node_with_parent_set() -> None:
    g = CanvasGraph()
    root = await g.append(_art("v1", produced_by="agent-1"))

    rev = await g.revise("v1", new_id="v2", payload_ref="ref://v2", produced_by="agent-2")

    assert rev.id == "v2"
    assert rev.parent == "v1"
    assert rev.kind == root.kind  # inherited
    # Both nodes coexist; nothing was replaced.
    assert (await g.get("v1")) is root
    assert (await g.get("v2")) is rev


@pytest.mark.asyncio
async def test_revision_records_producing_agent() -> None:
    g = CanvasGraph()
    await g.append(_art("v1", produced_by="agent-1"))
    rev = await g.revise("v1", new_id="v2", payload_ref="ref://v2", produced_by="colourist")
    assert rev.provenance.produced_by == "colourist"


@pytest.mark.asyncio
async def test_revision_inherits_parent_taint() -> None:
    g = CanvasGraph()
    tainted = Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "tool:scrape")
    await g.append(_art("v1", lineage=tainted))
    rev = await g.revise("v1", new_id="v2", payload_ref="ref://v2")
    # A revision of a tainted artifact stays tainted.
    assert Taint.TOOL_OUTPUT in rev.lineage.taints


@pytest.mark.asyncio
async def test_revise_unknown_parent_raises() -> None:
    g = CanvasGraph()
    with pytest.raises(Exception):
        await g.revise("nope", new_id="x", payload_ref="ref://x")


# -- Provenance records the producing agent ---------------------------------------------


@pytest.mark.asyncio
async def test_provenance_producing_agent_recorded() -> None:
    g = CanvasGraph()
    await g.append(_art("a", produced_by="renderer"))
    a = await g.get("a")
    assert a is not None
    assert a.provenance.produced_by == "renderer"


# -- Traversal on a multi-level graph ---------------------------------------------------
#
# Shape:
#     root
#     ├── b
#     │   ├── d
#     │   └── e
#     └── c


async def _multi_level() -> CanvasGraph:
    g = CanvasGraph()
    await g.append(_art("root"))
    await g.append(_art("b", parent="root"))
    await g.append(_art("c", parent="root"))
    await g.append(_art("d", parent="b"))
    await g.append(_art("e", parent="b"))
    return g


@pytest.mark.asyncio
async def test_children() -> None:
    g = await _multi_level()
    assert [a.id for a in await g.children("root")] == ["b", "c"]
    assert [a.id for a in await g.children("b")] == ["d", "e"]
    assert await g.children("d") == ()


@pytest.mark.asyncio
async def test_ancestors_nearest_first() -> None:
    g = await _multi_level()
    assert [a.id for a in await g.ancestors("d")] == ["b", "root"]
    assert await g.ancestors("root") == ()


@pytest.mark.asyncio
async def test_descendants() -> None:
    g = await _multi_level()
    assert {a.id for a in await g.descendants("root")} == {"b", "c", "d", "e"}
    assert {a.id for a in await g.descendants("b")} == {"d", "e"}
    assert await g.descendants("e") == ()


@pytest.mark.asyncio
async def test_lineage_of_root_first_inclusive() -> None:
    g = await _multi_level()
    assert [a.id for a in await g.lineage_of("d")] == ["root", "b", "d"]
    assert [a.id for a in await g.lineage_of("root")] == ["root"]
    assert await g.lineage_of("unknown") == ()


# -- Cycle guard: a malformed parent pointer must not hang ------------------------------


@pytest.mark.asyncio
async def test_self_parent_does_not_hang() -> None:
    g = CanvasGraph()
    # A hand-constructed artifact naming itself as parent. append() cannot normally create
    # this, but the type permits it, so traversal must survive it.
    await g.append(_art("loop", parent="loop"))
    assert await g.ancestors("loop") == ()  # self-edge is not followed
    assert await g.descendants("loop") == ()
    assert [a.id for a in await g.lineage_of("loop")] == ["loop"]
    assert await g.children("loop") == ()


@pytest.mark.asyncio
async def test_two_node_cycle_does_not_hang() -> None:
    g = CanvasGraph()
    # x -> y -> x. Malformed, but must terminate.
    await g.append(_art("x", parent="y"))
    await g.append(_art("y", parent="x"))
    anc = await g.ancestors("x")
    # Bounded by the visited set: at most the other node, then it stops.
    assert [a.id for a in anc] == ["y"]
    desc = await g.descendants("x")
    assert {a.id for a in desc} == {"y"}


# -- Taint on read ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reading_tainted_artifact_taints_reader() -> None:
    g = CanvasGraph()
    poisoned = Lineage.clean().with_taint(Taint.USER_UNVERIFIED, "user:anon")
    await g.append(_art("poison", lineage=poisoned))

    _artifact, reader_after = await g.read("poison")
    # The reader inherited the artifact's taint AND a canvas-read taint.
    assert Taint.USER_UNVERIFIED in reader_after.taints
    assert Taint.CANVAS_READ in reader_after.taints
    assert not reader_after.is_clean


@pytest.mark.asyncio
async def test_reading_clean_artifact_still_records_canvas_read() -> None:
    g = CanvasGraph()
    await g.append(_art("clean"))
    _artifact, reader_after = await g.read("clean")
    # Even a clean artifact marks the reader: this value came off the shared board.
    assert reader_after.taints == frozenset({Taint.CANVAS_READ})


@pytest.mark.asyncio
async def test_taint_for_reader_is_pure() -> None:
    """The helper computes a new lineage without mutating reader or artifact."""
    poisoned = Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "tool:x")
    art = _art("a", lineage=poisoned)
    reader = Lineage.clean()
    result = taint_for_reader(reader, art)
    assert reader.is_clean  # unchanged
    assert art.lineage == poisoned  # unchanged
    assert Taint.TOOL_OUTPUT in result.taints
    assert Taint.CANVAS_READ in result.taints


@pytest.mark.asyncio
async def test_read_accumulates_taint_across_reads() -> None:
    g = CanvasGraph()
    await g.append(_art("a", lineage=Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "t")))
    await g.append(_art("b", lineage=Lineage.clean().with_taint(Taint.FOREIGN_SKILL, "s")))
    _a, after_a = await g.read("a")
    _b, after_b = await g.read("b", reader=after_a)
    assert {Taint.TOOL_OUTPUT, Taint.FOREIGN_SKILL, Taint.CANVAS_READ} <= after_b.taints


# -- Approval gates: fail closed --------------------------------------------------------


@pytest.mark.asyncio
async def test_unapproved_gated_artifact_cannot_be_read() -> None:
    g = CanvasGraph()
    await g.append(_art("gated"), requires_approval=True)
    assert g.is_gated("gated")
    with pytest.raises(ApprovalRequired) as ei:
        await g.read("gated")
    assert ei.value.tool == "gated"


@pytest.mark.asyncio
async def test_gated_artifact_readable_after_grant() -> None:
    g = CanvasGraph()
    await g.append(_art("gated"), requires_approval=True)
    g.grant_approval("gated")
    assert not g.is_gated("gated")
    artifact, _reader = await g.read("gated")
    assert artifact.id == "gated"


@pytest.mark.asyncio
async def test_require_approval_after_append_also_gates() -> None:
    g = CanvasGraph()
    await g.append(_art("later"))
    # Readable now...
    await g.read("later")
    # ...then gated retroactively.
    g.require_approval("later")
    with pytest.raises(ApprovalRequired):
        await g.read("later")


@pytest.mark.asyncio
async def test_read_unknown_id_raises_keyerror() -> None:
    g = CanvasGraph()
    with pytest.raises(KeyError):
        await g.read("ghost")


@pytest.mark.asyncio
async def test_gate_check_precedes_missing_node() -> None:
    """A gated-but-never-appended id fails closed on approval, not on missing node —
    the payload is never the discriminator."""
    g = CanvasGraph()
    g.require_approval("phantom")
    with pytest.raises(ApprovalRequired):
        await g.read("phantom")
