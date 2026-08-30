"""In-memory append-only artifact graph implementing the ``CanvasStore`` protocol.

This is the reference implementation of the shared blackboard. The SQLite-backed
``CanvasStore`` (Lane L) enforces the same invariants with a PRIMARY KEY instead of a dict
check; this one is what every other lane tests against, so its guarantees must be exactly
the ones the storage backend owes.

The invariants, stated once:

* **Append-only.** ``append`` rejects an id that already exists — it never overwrites.
  Once a node is in the graph its fields never change (``Artifact`` is frozen, and we never
  replace an entry in place).
* **A revision is a new node.** ``revise`` mints a fresh artifact whose ``parent`` points at
  the one it supersedes. The superseded node still stands and is still reachable.
* **Provenance names the producing agent.** On a shared board "who made this frame" is a
  first-class question — it is how an agent's output quality is judged after the fact.
* **Taint on read.** Append-only stops overwrites but not injection: a poisoned artifact a
  second agent reads is still poison. Reading a tainted artifact taints the reader.
* **Approval gates fail closed.** A gated artifact cannot be read by a downstream agent until
  it is granted; an ungated-but-unapproved read raises rather than leaking the payload.

Cycles: an append-only graph *should* be acyclic (a parent must already exist before its
child is appended, so a back-edge is impossible to create through ``append``). But a
hand-constructed ``Artifact`` can carry a malformed ``parent`` pointer — including one that
names itself or a descendant — and traversal must never hang on it. Every walk here is
bounded by a visited set.

Lazy imports: the module depends only on the core types, which are already loaded whenever
anything in this package runs, so there is nothing heavy to defer. The rule still holds —
no database driver, no ``pydantic_ai``, nothing a canvas-free turn would not need.
"""

from __future__ import annotations

from collections import deque

from pikachu.core.errors import ApprovalRequired, PikachuError
from pikachu.core.types import (
    Artifact,
    Lineage,
    Provenance,
    Taint,
)

__all__ = [
    "CanvasGraph",
    "DuplicateArtifactError",
    "taint_for_reader",
]


class DuplicateArtifactError(PikachuError):
    """``append`` was handed an id that already exists.

    The canvas is append-only. Overwriting an existing node is exactly the attack the
    append-only design removes, so a duplicate id is a typed error rather than a silent
    replace. Defined here rather than in the reserved ``core.errors`` so the canvas owns its
    own failure mode; it still subclasses ``PikachuError`` so a host catches it with the one
    package-wide clause.
    """

    def __init__(self, artifact_id: str) -> None:
        super().__init__(
            f"artifact {artifact_id!r} already exists; the canvas is append-only and never "
            f"overwrites — a change is a new artifact with parent set"
        )
        self.artifact_id = artifact_id


def taint_for_reader(reader: Lineage, artifact: Artifact) -> Lineage:
    """The lineage an agent inherits by reading ``artifact``.

    This is the helper ``guard/`` must apply on **every** canvas read, not only on tool
    grants: the guard's job of confining authority is undone if a poisoned artifact can be
    read into an agent's working state without carrying its taint forward.

    Two things are folded in, monotonically (``Lineage.merge`` never drops a taint):

    * the artifact's own lineage — whatever it was already tainted by travels to the reader;
    * a fresh ``Taint.CANVAS_READ`` sourced at the artifact id — so that even reading a
      *clean* artifact records that this value came off the shared board, which is itself a
      lower-trust origin than something the agent produced in isolation.

    Pure and side-effect free: it computes the new lineage, it does not mutate the reader,
    the artifact, or the graph.
    """
    inherited = reader.merge(artifact.lineage)
    return inherited.with_taint(Taint.CANVAS_READ, f"canvas:{artifact.id}")


class CanvasGraph:
    """An in-memory append-only artifact graph. Structurally a ``CanvasStore``.

    Implements the protocol's ``append`` / ``get`` / ``children`` and adds the traversal,
    taint and gate surface the append-only board needs. All mutation goes through ``append``;
    there is no method that replaces or removes a node.

    Approval-gate state is held on the graph, not on the artifact. ``Artifact`` is frozen and
    immutable by design, so "this node now requires approval" and "approval was granted"
    cannot be an artifact field without mutating a node — they are graph facts about a node,
    tracked in two sets keyed by id.
    """

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}
        # ids that may not be read downstream until granted. A gate can be declared at
        # append time (a gated ArtifactKind / requires_approval producer) or set afterward.
        self._gated: set[str] = set()
        # gated ids that have been granted. Granting an id not in _gated is harmless.
        self._granted: set[str] = set()

    # -- CanvasStore protocol -----------------------------------------------------------

    async def append(self, artifact: Artifact, *, requires_approval: bool = False) -> Artifact:
        """Add ``artifact`` to the board.

        Rejects a duplicate id with :class:`DuplicateArtifactError` — the append-only rule.
        Pass ``requires_approval=True`` to gate the node at creation time so no downstream
        agent may read it until :meth:`grant_approval` is called for its id.
        """
        if artifact.id in self._artifacts:
            raise DuplicateArtifactError(artifact.id)
        self._artifacts[artifact.id] = artifact
        if requires_approval:
            self._gated.add(artifact.id)
        return artifact

    async def get(self, artifact_id: str) -> Artifact | None:
        """Raw lookup by id. No gate check, no taint — this is the graph-internal read used
        by traversal. For an agent read that must respect gates and propagate taint, use
        :meth:`read`."""
        return self._artifacts.get(artifact_id)

    async def children(self, artifact_id: str) -> tuple[Artifact, ...]:
        """Direct children — every node whose ``parent`` is ``artifact_id``.

        Deterministically ordered by id. A node is never its own child even if it names
        itself as parent (a self-parent is a malformed pointer, reported by traversal, not
        materialised as an edge here)."""
        kids = [
            a
            for a in self._artifacts.values()
            if a.parent == artifact_id and a.id != artifact_id
        ]
        kids.sort(key=lambda a: a.id)
        return tuple(kids)

    # -- Revision -----------------------------------------------------------------------

    async def revise(
        self,
        parent_id: str,
        *,
        new_id: str,
        payload_ref: str,
        produced_by: str | None = None,
        provenance: Provenance | None = None,
        lineage: Lineage | None = None,
    ) -> Artifact:
        """Append a NEW artifact that supersedes ``parent_id``. Nothing mutates.

        The new node inherits the parent's ``kind`` and, unless overridden, its lineage — a
        revision of a tainted artifact stays tainted. ``produced_by`` records which agent
        made the revision; pass a full ``provenance`` to record prompt/model/cost/seed too.
        The parent must already exist (you cannot revise a node that was never appended).
        """
        parent = self._artifacts.get(parent_id)
        if parent is None:
            raise PikachuError(
                f"cannot revise {parent_id!r}: no such artifact on the canvas"
            )
        prov = provenance
        if prov is None:
            prov = Provenance(produced_by=produced_by)
        elif produced_by is not None and prov.produced_by is None:
            prov = prov.model_copy(update={"produced_by": produced_by})

        revision = Artifact(
            id=new_id,
            kind=parent.kind,
            payload_ref=payload_ref,
            parent=parent_id,
            provenance=prov,
            lineage=parent.lineage if lineage is None else lineage,
        )
        return await self.append(revision)

    # -- Traversal (all cycle-guarded) --------------------------------------------------

    async def ancestors(self, artifact_id: str) -> tuple[Artifact, ...]:
        """Every artifact from ``artifact_id``'s parent up to the root, nearest first.

        Walks ``parent`` pointers. A missing parent ends the walk (a dangling pointer is not
        an error here — it is a root with a stale reference). A cyclic pointer is broken by
        the visited set rather than looping forever."""
        out: list[Artifact] = []
        seen: set[str] = {artifact_id}
        node = self._artifacts.get(artifact_id)
        current = node.parent if node is not None else None
        while current is not None and current not in seen:
            seen.add(current)
            parent = self._artifacts.get(current)
            if parent is None:
                break
            out.append(parent)
            current = parent.parent
        return tuple(out)

    async def descendants(self, artifact_id: str) -> tuple[Artifact, ...]:
        """Every artifact reachable by following child edges down from ``artifact_id``.

        Breadth-first, deterministically ordered within each level, each node visited once.
        The visited set makes a malformed cycle safe: a node that (transitively) names itself
        as an ancestor is enqueued at most once."""
        out: list[Artifact] = []
        seen: set[str] = {artifact_id}
        queue: deque[str] = deque([artifact_id])
        while queue:
            current = queue.popleft()
            kids = [
                a
                for a in self._artifacts.values()
                if a.parent == current and a.id != current
            ]
            kids.sort(key=lambda a: a.id)
            for kid in kids:
                if kid.id in seen:
                    continue
                seen.add(kid.id)
                out.append(kid)
                queue.append(kid.id)
        return tuple(out)

    async def lineage_of(self, artifact_id: str) -> tuple[Artifact, ...]:
        """The full chain from the root down to ``artifact_id`` inclusive, root first.

        This is the provenance answer: "what produced this, all the way back". It is the
        reversed ancestor walk with the node itself appended, so it reads top-to-bottom the
        way a human traces where a frame came from. Cycle-guarded via :meth:`ancestors`.
        Empty if the id is unknown."""
        node = self._artifacts.get(artifact_id)
        if node is None:
            return ()
        anc = await self.ancestors(artifact_id)
        # ancestors is nearest-first; reverse to root-first, then the node itself.
        return tuple(reversed(anc)) + (node,)

    # -- Approval gates -----------------------------------------------------------------

    def require_approval(self, artifact_id: str) -> None:
        """Mark an already-appended artifact as gated. Idempotent.

        A gated artifact cannot be read via :meth:`read` until :meth:`grant_approval` is
        called for its id. Marking an unknown id is allowed (the gate simply applies if that
        id is ever appended-then-read through this graph); reading an unknown id fails on the
        missing-node path regardless."""
        self._gated.add(artifact_id)

    def grant_approval(self, artifact_id: str) -> None:
        """Grant approval for a gated artifact so downstream agents may read it. Idempotent."""
        self._granted.add(artifact_id)

    def is_gated(self, artifact_id: str) -> bool:
        """Whether an id is gated and not yet granted — i.e. reads of it fail closed."""
        return artifact_id in self._gated and artifact_id not in self._granted

    # -- Guarded read (gates + taint) ---------------------------------------------------

    async def read(
        self, artifact_id: str, *, reader: Lineage | None = None
    ) -> tuple[Artifact, Lineage]:
        """Read an artifact AS AN AGENT: enforce the gate, then compute inherited taint.

        Returns ``(artifact, reader_lineage_after)`` — the node and the lineage the reading
        agent now carries. The caller (the guard, on behalf of a turn) must adopt the
        returned lineage; that is how taint crosses the canvas boundary.

        Fails closed:

        * unknown id -> ``KeyError`` (there is nothing to read);
        * gated and not granted -> :class:`ApprovalRequired` — the payload is never returned
          for an unapproved gated node, so a downstream agent cannot read past the gate.
        """
        if self.is_gated(artifact_id):
            raise ApprovalRequired(artifact_id)
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        base = Lineage.clean() if reader is None else reader
        return artifact, taint_for_reader(base, artifact)
