"""Protocols — what a host must supply.

Storage and billing are **Protocols, not implementations**. The product plugs in Postgres
and a credit ledger; someone running this open-source plugs in SQLite and a no-op biller.
Nothing in this package imports a database driver.

All protocols are ``runtime_checkable`` so a fake can be asserted against them in a test —
that round-trip is what earns the Cascade badge.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pikachu.core.types import (
    Artifact,
    MemoryRecord,
    MemoryScope,
    Run,
    Signal,
    Skill,
    ToolOutcome,
    TurnRequest,
    TurnResult,
)

__all__ = [
    "AgentBackend",
    "Biller",
    "CanvasStore",
    "Embedder",
    "MemoryStore",
    "Reservation",
    "RunStore",
    "SignalLedger",
    "SkillStore",
]


@runtime_checkable
class SkillStore(Protocol):
    """Skill persistence and retrieval."""

    async def get(self, name: str, version: int | None = None) -> Skill | None: ...

    async def find(
        self, query: str, *, partition: str | None = None, limit: int = 5
    ) -> tuple[Skill, ...]:
        """Retrieve candidate skills.

        MUST return only ``status.is_retrievable`` skills. Implement this as a structural
        guarantee rather than a caller-supplied filter argument: the safest access control
        is one that is impossible to get wrong, not one that depends on passing the right
        parameter.
        """
        ...

    async def put(self, skill: Skill) -> Skill:
        """Persist a NEW version. Never mutates an existing one."""
        ...

    async def archive(self, name: str, version: int) -> None:
        """Archive recoverably. There is deliberately no ``delete``."""
        ...


class Reservation(Protocol):
    """A held-but-not-yet-charged amount of credit."""

    @property
    def id(self) -> str: ...

    @property
    def amount(self) -> int: ...


@runtime_checkable
class Biller(Protocol):
    """Metered tool accounting: reserve, then capture or release.

    Exactly one charging point, with refund on failure. Generic durable execution is
    at-least-once, which is unsafe to repeat for a paid generation — so capture must be
    idempotent on ``reservation_id`` and a resume must never re-capture a captured one.
    """

    async def reserve(self, *, run_id: str, tool: str, amount: int) -> Reservation: ...

    async def capture(self, reservation_id: str, *, outcome: ToolOutcome) -> None:
        """Commit the charge. MUST be idempotent on ``reservation_id``.

        ``ToolOutcome.INTERRUPTED`` means the side effect MAY have happened. Do not treat
        it as failure and silently release — that path double-charges on the retry.
        """
        ...

    async def release(self, reservation_id: str) -> None:
        """Return a reservation unspent. Idempotent."""
        ...


@runtime_checkable
class RunStore(Protocol):
    """Durable run state, so a turn survives a process restart."""

    async def create(self, run: Run) -> Run: ...

    async def get(self, run_id: str) -> Run | None: ...

    async def checkpoint(self, run: Run) -> Run:
        """Persist current state. Called after every iteration."""
        ...


@runtime_checkable
class MemoryStore(Protocol):
    async def remember(self, record: MemoryRecord) -> None: ...

    async def recall(
        self, query: str, *, scope: MemoryScope | None = None, limit: int = 10
    ) -> tuple[MemoryRecord, ...]:
        """Retrieve memory under a budget.

        The retrieval budget is not negotiable: unbounded recall is how a stable prompt
        prefix stops being stable, which breaks caching.
        """
        ...

    async def decay(self, *, older_than_days: int) -> int:
        """Lower confidence on unreinforced records. Returns count affected.

        Lowers rank. Never deletes.
        """
        ...


@runtime_checkable
class CanvasStore(Protocol):
    """Append-only artifact graph."""

    async def append(self, artifact: Artifact) -> Artifact:
        """Add an artifact. MUST reject an id that already exists rather than overwrite."""
        ...

    async def get(self, artifact_id: str) -> Artifact | None: ...

    async def children(self, artifact_id: str) -> tuple[Artifact, ...]: ...


@runtime_checkable
class SignalLedger(Protocol):
    """Feedback evidence. Write-only from the agent's perspective.

    There is intentionally no ``score()`` method on this protocol. Scores drive retrieval
    rank; they must never be readable from anywhere that could place them in an agent's
    context.
    """

    async def record(self, signal: Signal) -> None: ...


class Embedder(Protocol):
    """Text -> vector. A parameter, never a hardcoded provider.

    Passed in so confusability checks and skill retrieval can be tested with a
    deterministic stub and zero network calls.
    """

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


@runtime_checkable
class AgentBackend(Protocol):
    """The framework seam — one method.

    This is the whole coupling surface to any agent framework. In the parent repo the
    equivalent abstraction is 379 of 8,180 lines and a second implementation already
    proves it holds, which is why swapping frameworks is writing one more subclass rather
    than a rewrite.
    """

    async def run_turn(self, request: TurnRequest) -> TurnResult: ...
