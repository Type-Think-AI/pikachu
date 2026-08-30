"""In-memory fakes for every Protocol in ``pikachu.core.protocols``.

These are load-bearing: every other lane tests against them, so a bug here is a bug
everywhere. The rules they must uphold are the same structural guarantees the real
implementations owe, restated small enough to hold in one file:

  * ``FakeSkillStore.find`` returns ONLY ``status.is_retrievable`` skills — enforced
    structurally, not via a caller-supplied filter argument.
  * ``FakeBiller.capture`` is idempotent on ``reservation_id`` and raises
    ``DoubleCaptureError`` on a second capture of the same id.
  * ``FakeCanvasStore.append`` rejects an id that already exists rather than overwrite.

Everything is async, deterministic, and touches no network and no clock-dependent branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pikachu.core.errors import DoubleCaptureError
from pikachu.core.types import (
    Artifact,
    MemoryRecord,
    MemoryScope,
    Run,
    Signal,
    Skill,
    ToolOutcome,
)

__all__ = [
    "FakeBiller",
    "FakeCanvasStore",
    "FakeMemoryStore",
    "FakeReservation",
    "FakeRunStore",
    "FakeSignalLedger",
    "FakeSkillStore",
]


# --------------------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------------------


class FakeSkillStore:
    """In-memory skill persistence keyed by ``(name, version)``.

    ``find`` filters to retrievable skills as a structural property of the store: there is
    no ``include_drafts`` parameter a caller could set wrong. A draft simply cannot come
    back out of ``find``, only out of ``get`` by explicit name.
    """

    def __init__(self, skills: tuple[Skill, ...] = ()) -> None:
        self._by_key: dict[tuple[str, int], Skill] = {}
        for skill in skills:
            self._by_key[(skill.name, skill.version)] = skill

    async def get(self, name: str, version: int | None = None) -> Skill | None:
        if version is not None:
            return self._by_key.get((name, version))
        versions = [s for (n, _), s in self._by_key.items() if n == name]
        if not versions:
            return None
        return max(versions, key=lambda s: s.version)

    async def find(
        self, query: str, *, partition: str | None = None, limit: int = 5
    ) -> tuple[Skill, ...]:
        matches = [
            s
            for s in self._by_key.values()
            if s.status.is_retrievable
            and (partition is None or s.partition == partition)
            and (query == "" or query.lower() in (s.name + " " + s.description).lower())
        ]
        # Deterministic ordering: newest version of a name first, then by name.
        matches.sort(key=lambda s: (s.name, -s.version))
        return tuple(matches[: max(limit, 0)])

    async def put(self, skill: Skill) -> Skill:
        self._by_key[(skill.name, skill.version)] = skill
        return skill

    async def archive(self, name: str, version: int) -> None:
        from pikachu.core.types import SkillStatus

        existing = self._by_key.get((name, version))
        if existing is not None:
            self._by_key[(name, version)] = existing.model_copy(
                update={"status": SkillStatus.ARCHIVED}
            )


# --------------------------------------------------------------------------------------
# Billing
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeReservation:
    """Concrete held credit. Satisfies the ``Reservation`` structural protocol."""

    _id: str
    _amount: int

    @property
    def id(self) -> str:
        return self._id

    @property
    def amount(self) -> int:
        return self._amount


class FakeBiller:
    """Reserve / capture / release with idempotent capture.

    State machine per reservation id: RESERVED -> CAPTURED, or RESERVED -> RELEASED.
    A second capture of an already-captured id is the double-charge bug this whole layer
    exists to prevent, so it raises ``DoubleCaptureError`` rather than being tolerated.
    """

    def __init__(self) -> None:
        self._counter = 0
        self._reservations: dict[str, FakeReservation] = {}
        self._captured: dict[str, ToolOutcome] = {}
        self._released: set[str] = set()

    async def reserve(self, *, run_id: str, tool: str, amount: int) -> FakeReservation:
        self._counter += 1
        rid = f"res-{run_id}-{tool}-{self._counter}"
        reservation = FakeReservation(_id=rid, _amount=amount)
        self._reservations[rid] = reservation
        return reservation

    async def capture(self, reservation_id: str, *, outcome: ToolOutcome) -> None:
        if reservation_id in self._captured:
            raise DoubleCaptureError(reservation_id)
        if reservation_id not in self._reservations:
            raise KeyError(f"unknown reservation {reservation_id!r}")
        if reservation_id in self._released:
            raise KeyError(f"reservation {reservation_id!r} was already released")
        self._captured[reservation_id] = outcome

    async def release(self, reservation_id: str) -> None:
        # Idempotent: releasing an already-released or unknown reservation is a no-op.
        if reservation_id in self._captured:
            return
        self._released.add(reservation_id)

    # Test-only introspection (not part of the Biller protocol).
    def captured_amount(self) -> int:
        return sum(
            self._reservations[rid].amount
            for rid in self._captured
            if rid in self._reservations
        )

    def is_captured(self, reservation_id: str) -> bool:
        return reservation_id in self._captured


# --------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------


class FakeRunStore:
    """Durable run state in a dict. ``checkpoint`` overwrites, ``create`` does not."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    async def create(self, run: Run) -> Run:
        if run.id in self._runs:
            raise KeyError(f"run {run.id!r} already exists")
        self._runs[run.id] = run
        return run

    async def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    async def checkpoint(self, run: Run) -> Run:
        self._runs[run.id] = run
        return run


# --------------------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------------------


@dataclass
class FakeMemoryStore:
    """In-memory records. ``decay`` lowers confidence, never deletes."""

    _records: list[MemoryRecord] = field(default_factory=list)

    async def remember(self, record: MemoryRecord) -> None:
        self._records.append(record)

    async def recall(
        self, query: str, *, scope: MemoryScope | None = None, limit: int = 10
    ) -> tuple[MemoryRecord, ...]:
        matches = [
            r
            for r in self._records
            if (scope is None or r.scope == scope)
            and (query == "" or query.lower() in (r.key + " " + r.value).lower())
        ]
        # Deterministic: highest confidence first, then most evidence.
        matches.sort(key=lambda r: (r.confidence, r.evidence_count), reverse=True)
        return tuple(matches[: max(limit, 0)])

    async def decay(self, *, older_than_days: int) -> int:
        affected = 0
        decayed: list[MemoryRecord] = []
        for r in self._records:
            if r.confidence > 0.0:
                new_conf = max(0.0, r.confidence - 0.1)
                decayed.append(r.model_copy(update={"confidence": new_conf}))
                affected += 1
            else:
                decayed.append(r)
        self._records = decayed
        return affected


# --------------------------------------------------------------------------------------
# Canvas
# --------------------------------------------------------------------------------------


class FakeCanvasStore:
    """Append-only artifact graph. Appending a duplicate id is rejected, not overwritten."""

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}

    async def append(self, artifact: Artifact) -> Artifact:
        if artifact.id in self._artifacts:
            raise ValueError(
                f"artifact {artifact.id!r} already exists; the canvas is append-only"
            )
        self._artifacts[artifact.id] = artifact
        return artifact

    async def get(self, artifact_id: str) -> Artifact | None:
        return self._artifacts.get(artifact_id)

    async def children(self, artifact_id: str) -> tuple[Artifact, ...]:
        kids = [a for a in self._artifacts.values() if a.parent == artifact_id]
        kids.sort(key=lambda a: a.id)
        return tuple(kids)


# --------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------


class FakeSignalLedger:
    """Write-only feedback ledger.

    There is deliberately NO ``score`` and NO ``read`` here — recorded signals are not
    readable back through the protocol, because a score that can be read is a score that
    can end up in an agent's context and be optimised against. The Cascade suite asserts
    this absence so a well-meaning addition of a getter fails the build.
    """

    def __init__(self) -> None:
        self._signals: list[Signal] = []

    async def record(self, signal: Signal) -> None:
        self._signals.append(signal)

    # NOTE: intentionally no async score()/read()/get() method. See class docstring.
    # A test-only count of what was recorded, named so it cannot be mistaken for a score.
    def _test_recorded_count(self) -> int:
        return len(self._signals)
