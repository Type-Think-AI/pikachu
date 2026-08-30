"""Backends — the framework seam.

The whole coupling surface to any agent framework is one method: ``run_turn``. Swapping
frameworks is writing one more subclass of :class:`BaseBackend`, not a rewrite. The parent
repo proves this holds — the equivalent abstraction is 379 of 8,180 lines and already has a
second implementation.

``FakeBackend`` is the deterministic, no-network, no-model implementation every other lane
tests against, and it is what earns the Volcano badge. ``PydanticAIBackend`` is wave 2 and
lives in ``backends/pydantic_ai.py`` once Lane E's verification verdict is in — it is not
created here.
"""

from __future__ import annotations

from pikachu.backends.base import BaseBackend
from pikachu.backends.fake import (
    FakeBackend,
    FakeBiller,
    FakeReservation,
    ScriptedToolCall,
    ScriptedTurn,
)

__all__ = [
    "BaseBackend",
    "FakeBackend",
    "FakeBiller",
    "FakeReservation",
    "ScriptedToolCall",
    "ScriptedTurn",
]
