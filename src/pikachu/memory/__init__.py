"""Memory: scoped, decaying, budgeted recall — plus the crew-shared LONG scope.

The public surface is the in-memory reference store and its crew-shared backing. The SQLite
backend that satisfies the same Protocol lives in ``pikachu.storage`` (Lane L); this package
holds the *semantics* — scopes, the decay-never-deletes rule, the hard recall budget, and the
structural cross-tenant isolation.

Nothing heavy is imported at module scope, so a turn that never touches memory pays nothing
for this package existing. The concrete classes are imported directly from
``pikachu.memory.store`` — they carry no third-party imports themselves, so re-exporting them
here is free.
"""

from __future__ import annotations

from pikachu.memory.store import (
    DEFAULT_RECALL_LIMIT,
    CrewMemory,
    InMemoryMemoryStore,
)

__all__ = [
    "DEFAULT_RECALL_LIMIT",
    "CrewMemory",
    "InMemoryMemoryStore",
]
