"""In-memory reference implementation of the :class:`MemoryStore` Protocol.

This is the *reference semantics*, not the production backend — Lane L owns the SQLite
implementation. Everything the SQLite version must do, this file does in a dict so the rules
are readable in one place and testable with no I/O:

  * **Three scopes** (:class:`~pikachu.core.types.MemoryScope`): SHORT is one turn, MID is
    one conversation, LONG is **shared across the whole crew of a house**. LONG is the answer
    to day-one emptiness — a newly created agent joins a house whose LONG memory already
    knows the brand. The sharing boundary is made explicit and testable (see
    :meth:`InMemoryMemoryStore.for_agent`).

  * **Confidence decays without reinforcement; evidence rises with it.** :meth:`decay`
    lowers ``confidence`` and never removes a record — the Protocol has no ``delete`` on
    purpose. Punishment is done safely: rank drops, nothing is lost.

  * **Recall is budgeted.** A hard cap set at construction bounds every recall, even when a
    caller passes a larger ``limit``. Unbounded recall destroys the stable prompt prefix,
    which silently disables prompt caching (invariant P10). The cap is not negotiable.

  * **Cross-tenant isolation is structural.** A store is bound to one ``tenant`` (house) at
    construction. There is no ``tenant=`` argument on :meth:`recall` a caller could get
    wrong: one house's private memory *cannot* be addressed from another house's store,
    because the private records live in a per-store dict and LONG records are filed under
    the store's own tenant key. The safest access control is the kind that cannot be called
    wrongly (the same principle as ``SkillStore.find``).

Import-light and lazy: only the stdlib and ``core``. A turn that never touches memory never
imports this module — nothing at package scope pulls it in.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from pikachu.core.types import MemoryRecord, MemoryScope

__all__ = [
    "DEFAULT_RECALL_LIMIT",
    "CrewMemory",
    "InMemoryMemoryStore",
]


#: Default hard cap on how many records a single recall may return. Sized to leave the
#: stable prompt prefix stable: a handful of high-value memories, not an unbounded dump.
#: Chosen conservatively — a store may lower it, and a caller may only ever ask for *fewer*.
DEFAULT_RECALL_LIMIT: int = 10


class CrewMemory:
    """The LONG-scope store, shared across every agent in a house.

    One :class:`CrewMemory` instance is the crew's shared brain. Multiple
    :class:`InMemoryMemoryStore` instances belonging to the *same tenant* are handed the
    *same* :class:`CrewMemory`, so a LONG memory written by one agent is recalled by all of
    them — that is the crew-sharing boundary, made a concrete object rather than an implicit
    convention.

    Records are filed under their tenant key. A store only ever reads its own tenant's slot,
    so even a single shared :class:`CrewMemory` handed to two different houses keeps them
    isolated — but the intended, safe usage is one :class:`CrewMemory` per house, which the
    :meth:`InMemoryMemoryStore.for_agent` factory arranges automatically.
    """

    def __init__(self) -> None:
        # tenant -> list of LONG records for that tenant. defaultdict so a fresh house
        # reads an empty list rather than KeyError-ing.
        self._by_tenant: defaultdict[str, list[MemoryRecord]] = defaultdict(list)

    def add(self, tenant: str, record: MemoryRecord) -> None:
        self._by_tenant[tenant].append(record)

    def records(self, tenant: str) -> tuple[MemoryRecord, ...]:
        """LONG records visible to ``tenant``, and no other tenant's."""
        return tuple(self._by_tenant[tenant])

    def replace(self, tenant: str, records: list[MemoryRecord]) -> None:
        self._by_tenant[tenant] = records


@dataclass
class InMemoryMemoryStore:
    """Reference :class:`MemoryStore`, bound to exactly one tenant (house).

    Construct one per (tenant, conversation) pair. SHORT and MID records live in this
    instance; LONG records live in the shared :class:`CrewMemory`, filed under this store's
    ``tenant``. Because the tenant is fixed at construction and never passed to
    :meth:`recall`, a caller has no way to reach another house's memory through this object.

    :param tenant: the house this store belongs to. The isolation boundary.
    :param crew: the shared LONG-scope store for this house. When omitted a private one is
        created, which means this store's LONG memory is shared with nobody — use
        :meth:`for_agent` to get correct crew sharing.
    :param recall_limit: hard cap on records returned by any single :meth:`recall`. A caller
        may request fewer via ``limit`` but never more. Must be >= 0.
    """

    tenant: str
    crew: CrewMemory = field(default_factory=CrewMemory)
    recall_limit: int = DEFAULT_RECALL_LIMIT

    # Per-store (per conversation) records for the non-shared scopes.
    _short: list[MemoryRecord] = field(default_factory=list)
    _mid: list[MemoryRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.recall_limit < 0:
            raise ValueError(f"recall_limit must be >= 0, got {self.recall_limit}")

    # ------------------------------------------------------------------ factories

    @classmethod
    def for_agent(
        cls,
        *,
        tenant: str,
        crew: CrewMemory,
        recall_limit: int = DEFAULT_RECALL_LIMIT,
    ) -> InMemoryMemoryStore:
        """Build a store for one agent in a house, sharing the house's crew memory.

        Two agents in the same house get two stores that share one :class:`CrewMemory`, so
        their LONG memory is common while their SHORT/MID memory is not. This is the intended
        construction and the one the crew-sharing test exercises.
        """
        return cls(tenant=tenant, crew=crew, recall_limit=recall_limit)

    # ------------------------------------------------------------------ MemoryStore

    async def remember(self, record: MemoryRecord) -> None:
        """Store ``record`` in the bucket its scope dictates.

        A LONG record is filed in the shared crew store under this store's tenant, which is
        what makes it visible to the rest of the house. SHORT and MID stay local to this
        store. The record's own tenant is never taken from the record — it is always this
        store's tenant, so a record cannot smuggle itself into another house.
        """
        if record.scope is MemoryScope.LONG:
            self.crew.add(self.tenant, record)
        elif record.scope is MemoryScope.MID:
            self._mid.append(record)
        else:
            self._short.append(record)

    async def recall(
        self,
        query: str,
        *,
        scope: MemoryScope | None = None,
        limit: int = 10,
    ) -> tuple[MemoryRecord, ...]:
        """Retrieve records under a hard budget.

        The effective cap is ``min(limit, recall_limit)`` and is applied *after* ranking, so
        a caller asking for more than the store allows still gets at most ``recall_limit``.
        A negative ``limit`` yields nothing.

        ``scope=None`` searches all three scopes this store can see (its own SHORT/MID plus
        the crew's LONG for this tenant). A specific scope narrows to that bucket. No scope
        or argument can reach another tenant's records — that is not a filter, it is the
        absence of any addressable path to them.

        Ranking is deterministic: highest confidence first, then most evidence, then key —
        so the budget always keeps the *most* trustworthy records, and ties never depend on
        insertion order or the clock.
        """
        candidates = self._visible(scope)

        q = query.lower()
        matches = [
            r
            for r in candidates
            if q == "" or q in (r.key + " " + r.value).lower()
        ]
        matches.sort(key=lambda r: (r.confidence, r.evidence_count, r.key), reverse=True)

        effective_cap = min(limit, self.recall_limit)
        if effective_cap <= 0:
            return ()
        return tuple(matches[:effective_cap])

    async def decay(self, *, older_than_days: int) -> int:
        """Lower confidence on records, returning how many were affected.

        Applies to every scope this store owns *and* to this tenant's slice of the crew's
        LONG memory. Confidence drops toward zero; a record at zero confidence stays (it is
        outranked by everything, but it is not deleted — there is no delete). ``evidence_count``
        is untouched, because evidence is a historical fact that decay does not erase.

        ``older_than_days`` is part of the Protocol; this reference implementation treats it
        as "decay everything eligible" because it holds no wall-clock index. The SQLite
        backend (Lane L) is where the age predicate becomes a real ``WHERE created_at <``
        clause. The invariant this file pins is *decay lowers rank and never deletes* — the
        age selectivity is a backend concern, documented here so it is not mistaken for a
        gap.
        """
        affected = 0

        def _decayed(records: list[MemoryRecord]) -> list[MemoryRecord]:
            nonlocal affected
            out: list[MemoryRecord] = []
            for r in records:
                if r.confidence > 0.0:
                    new_conf = max(0.0, r.confidence - 0.1)
                    out.append(r.model_copy(update={"confidence": new_conf}))
                    affected += 1
                else:
                    out.append(r)
            return out

        self._short = _decayed(self._short)
        self._mid = _decayed(self._mid)
        self.crew.replace(self.tenant, _decayed(list(self.crew.records(self.tenant))))
        return affected

    # ------------------------------------------------------------------ internals

    def _visible(self, scope: MemoryScope | None) -> list[MemoryRecord]:
        """All records this store may see for ``scope`` (or every scope when None).

        The crew's LONG records are pulled for *this store's tenant only*. There is no branch
        that could substitute another tenant — the isolation is that this method never takes
        a tenant argument.
        """
        long_records = list(self.crew.records(self.tenant))
        if scope is MemoryScope.SHORT:
            return list(self._short)
        if scope is MemoryScope.MID:
            return list(self._mid)
        if scope is MemoryScope.LONG:
            return long_records
        # scope is None: everything visible to this store.
        return [*self._short, *self._mid, *long_records]
