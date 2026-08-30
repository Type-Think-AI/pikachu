"""Canvas invariants, proven for arbitrary operation sequences (hypothesis).

Two properties this file exists to prove, over any sequence of appends, revisions, gate
grants and guarded reads:

1. **No operation ever mutates an existing artifact.** Once an id is on the board, the node
   stored under that id is byte-for-byte the one that was appended — no later append,
   revision, read or gate operation changes it. This is the append-only guarantee and the
   whole reason the board removes the overwrite vector.

2. **Every artifact reachable by traversal was appended exactly once.** Whatever
   ``children`` / ``ancestors`` / ``descendants`` / ``lineage_of`` return is drawn only from
   the set of ids that were appended (each id at most once, because a duplicate append is
   rejected), and every reachable node is identical to the one appended under that id.

A third property guards the cycle claim: traversal **terminates** on arbitrary (possibly
malformed, possibly cyclic) parent pointers. Hypothesis is free to build a graph whose
parents point anywhere, including into cycles; every traversal must still return.
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from pikachu.canvas.graph import CanvasGraph, DuplicateArtifactError
from pikachu.core.errors import ApprovalRequired
from pikachu.core.types import Artifact, ArtifactKind, Lineage, Provenance, Taint

# A small id alphabet so collisions (duplicate appends) and real parent hits both occur
# often, rather than every id being unique and every parent dangling.
_IDS = ["a", "b", "c", "d", "e", "f"]
_id = st.sampled_from(_IDS)
_taint = st.sampled_from(list(Taint))


@st.composite
def _op(draw: st.DrawFn) -> tuple[str, dict[str, object]]:
    """One operation drawn from the canvas surface."""
    kind = draw(st.sampled_from(["append", "revise", "gate", "grant", "read"]))
    if kind == "append":
        return "append", {
            "id": draw(_id),
            # parent may be any id (a real hit, a dangling pointer, or self) or None.
            "parent": draw(st.one_of(st.none(), _id)),
            "taints": frozenset(draw(st.sets(_taint, max_size=2))),
            "produced_by": draw(st.sampled_from(["ag1", "ag2", None])),
            "requires_approval": draw(st.booleans()),
        }
    if kind == "revise":
        return "revise", {"parent": draw(_id), "new_id": draw(_id)}
    return kind, {"id": draw(_id)}


def _make_artifact(spec: dict[str, object]) -> Artifact:
    taints = spec["taints"]
    assert isinstance(taints, frozenset)
    lineage = Lineage(taints=taints, sources=tuple(f"src:{t.value}" for t in taints))
    aid = spec["id"]
    parent = spec["parent"]
    produced_by = spec["produced_by"]
    assert isinstance(aid, str)
    assert parent is None or isinstance(parent, str)
    assert produced_by is None or isinstance(produced_by, str)
    return Artifact(
        id=aid,
        kind=ArtifactKind.DATA,
        payload_ref=f"ref://{aid}",
        parent=parent,
        provenance=Provenance(produced_by=produced_by),
        lineage=lineage,
    )


async def _run_sequence(
    ops: list[tuple[str, dict[str, object]]],
) -> tuple[CanvasGraph, dict[str, Artifact]]:
    """Apply the op sequence, returning the graph and a snapshot of the first artifact ever
    stored under each id (what the append-only rule says must never change)."""
    g = CanvasGraph()
    first_seen: dict[str, Artifact] = {}

    for name, spec in ops:
        if name == "append":
            art = _make_artifact(spec)
            req = spec["requires_approval"]
            assert isinstance(req, bool)
            try:
                await g.append(art, requires_approval=req)
            except DuplicateArtifactError:
                continue
            first_seen.setdefault(art.id, art)
        elif name == "revise":
            parent = spec["parent"]
            new_id = spec["new_id"]
            assert isinstance(parent, str) and isinstance(new_id, str)
            try:
                rev = await g.revise(parent, new_id=new_id, payload_ref=f"ref://{new_id}")
            except Exception:
                continue  # unknown parent or duplicate new_id — both legitimately rejected
            first_seen.setdefault(rev.id, rev)
        elif name == "gate":
            aid = spec["id"]
            assert isinstance(aid, str)
            g.require_approval(aid)
        elif name == "grant":
            aid = spec["id"]
            assert isinstance(aid, str)
            g.grant_approval(aid)
        elif name == "read":
            aid = spec["id"]
            assert isinstance(aid, str)
            try:
                await g.read(aid)
            except (ApprovalRequired, KeyError):
                pass  # gated or absent — reading must not mutate anything either way
    return g, first_seen


_sequences = st.lists(_op(), min_size=0, max_size=40)


async def _stored_ids(g: CanvasGraph) -> tuple[str, ...]:
    """Ids still retrievable via the public ``get`` surface."""
    out: list[str] = []
    for candidate in _IDS:
        if await g.get(candidate) is not None:
            out.append(candidate)
    return tuple(out)


@settings(max_examples=200, deadline=None)
@given(ops=_sequences)
def test_no_operation_mutates_an_existing_artifact(
    ops: list[tuple[str, dict[str, object]]],
) -> None:
    """Every id that was ever stored still holds the exact node first stored under it."""
    g, first_seen = asyncio.run(_run_sequence(ops))
    for aid, original in first_seen.items():
        current = asyncio.run(g.get(aid))
        assert current is not None, aid
        # Frozen models: value equality is structural equality. Nothing rewrote the node.
        assert current == original, (aid, current, original)


@settings(max_examples=200, deadline=None)
@given(ops=_sequences)
def test_reachable_artifacts_were_appended_exactly_once(
    ops: list[tuple[str, dict[str, object]]],
) -> None:
    """Everything traversal returns is a node that was appended, identical to what was
    appended, and the store holds exactly one node per id."""
    g, first_seen = asyncio.run(_run_sequence(ops))
    appended_ids = set(first_seen)

    async def _check() -> None:
        for aid in appended_ids:
            reached: list[Artifact] = []
            reached.extend(await g.children(aid))
            reached.extend(await g.ancestors(aid))
            reached.extend(await g.descendants(aid))
            reached.extend(await g.lineage_of(aid))
            for node in reached:
                # Only ever-appended ids are reachable...
                assert node.id in appended_ids, node.id
                # ...and each reachable node equals the one appended (no mutation slipped in).
                assert node == first_seen[node.id], node.id

    asyncio.run(_check())
    # "Exactly once": a duplicate append is rejected, so the store maps each id to a single
    # node. The dict keying by id makes multiplicity structurally impossible; assert the set
    # of stored ids equals the set of first-seen ids (no phantom nodes appeared).
    stored_ids = set(asyncio.run(_stored_ids(g)))
    assert stored_ids == appended_ids


@settings(max_examples=300, deadline=None)
@given(ops=_sequences)
def test_traversal_always_terminates_on_arbitrary_parent_pointers(
    ops: list[tuple[str, dict[str, object]]],
) -> None:
    """The cycle guard: no traversal hangs, whatever the parent pointers are.

    ``asyncio.run`` here would hang the whole test process if any walk looped forever, so
    reaching the assertions at all IS the proof of termination."""
    g, first_seen = asyncio.run(_run_sequence(ops))

    async def _walk_all() -> None:
        for aid in list(first_seen) + ["z"]:  # include an absent id
            await g.children(aid)
            await g.ancestors(aid)
            await g.descendants(aid)
            await g.lineage_of(aid)

    asyncio.run(_walk_all())
