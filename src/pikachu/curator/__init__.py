"""Curator subpackage — self-improvement's *earning* half.

The headline capability is not "generate skills"; it is "generate, then make them earn their
place" (``docs/13-self-improvement.md``). This package is that earning half:

* :mod:`pikachu.curator.distil` — the four-check creation gate. Most turns produce no skill.
* :mod:`pikachu.curator.lifecycle` — deterministic promotion, immutable versioning,
  archive-never-delete, and the pinned-bypass override.

The security prerequisite lives in :mod:`pikachu.guard.authored` (agent-generated skills are
scanned like imported ones and inherit turn taint); both modules here route through it, so a
tainted draft can never be laundered into a trusted, retrievable skill.

Import-light by construction: nothing here pulls ``pydantic_ai``. The scanner and
confusability checks are imported lazily inside the functions that use them, so a turn that
never distils a skill pays nothing for this package.
"""

from __future__ import annotations

from pikachu.curator.distil import (
    DistilCandidate,
    DistilOutcome,
    DistilRejection,
    RejectionReason,
    distil,
)
from pikachu.curator.lifecycle import (
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_USES,
    NotAgentCreated,
    UsageStats,
    archive,
    improve,
    promote_on_reuse,
    promote_on_success,
    restore,
    revert,
)

__all__ = [
    # distil (creation gate)
    "DistilCandidate",
    "DistilOutcome",
    "DistilRejection",
    "RejectionReason",
    "distil",
    # lifecycle (deterministic transitions)
    "PROMOTE_MIN_SUCCESS_RATE",
    "PROMOTE_MIN_USES",
    "NotAgentCreated",
    "UsageStats",
    "archive",
    "improve",
    "promote_on_reuse",
    "promote_on_success",
    "restore",
    "revert",
]
