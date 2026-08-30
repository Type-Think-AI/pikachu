"""Typed errors.

Every failure a caller might reasonably branch on gets its own class. Nothing here inherits
from ``Exception`` directly except the base, so a host can catch everything from this
package with one clause.
"""

from __future__ import annotations

__all__ = [
    "ApprovalRequired",
    "BudgetExceeded",
    "DoubleCaptureError",
    "InjectionDetected",
    "PikachuError",
    "SkillParseError",
    "TaintedPromotion",
    "ToolDenied",
]


class PikachuError(Exception):
    """Base for everything this package raises."""


class SkillParseError(PikachuError):
    """A SKILL.md document could not be parsed.

    Malformed frontmatter is an error, never a silent default — a skill that loads with
    half its metadata missing is worse than one that fails loudly.
    """

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class InjectionDetected(PikachuError):
    """A skill body contained a detected injection payload.

    Detected payloads are REJECTED, not sanitised-and-accepted. Sanitising implies the
    scanner understands the payload well enough to neutralise it, which it does not — it
    misses paraphrased injection entirely.
    """

    def __init__(self, message: str, *, pattern: str, skill_name: str | None = None) -> None:
        super().__init__(message)
        self.pattern = pattern
        self.skill_name = skill_name


class ToolDenied(PikachuError):
    """A tool call was refused by the guard.

    Note this is raised at the CALL boundary, never from inside a tool-filter function.
    Filters must fail closed by omitting the tool: raising from a prepare/filter hook is
    not supported by the underlying framework and produces a hard error instead of a
    graceful denial.
    """

    def __init__(self, tool: str, *, reason: str) -> None:
        super().__init__(f"tool {tool!r} denied: {reason}")
        self.tool = tool
        self.reason = reason


class TaintedPromotion(PikachuError):
    """Something tried to promote a tainted artifact into a trusted position.

    This is the laundering path: poison one turn, distil it into a draft, let reuse promote
    it, and the injection is durable and carries our own provenance.
    """

    def __init__(self, subject: str, *, taints: frozenset[str]) -> None:
        super().__init__(f"{subject} carries taint {sorted(taints)} and may not be promoted")
        self.subject = subject
        self.taints = taints


class BudgetExceeded(PikachuError):
    """A run hit its iteration, token or credit ceiling."""

    def __init__(self, message: str, *, limit_kind: str) -> None:
        super().__init__(message)
        self.limit_kind = limit_kind


class ApprovalRequired(PikachuError):
    """A tool call needs human approval before proceeding.

    Not a failure — the run is expected to suspend and resume.
    """

    def __init__(self, tool: str, *, run_id: str | None = None) -> None:
        super().__init__(f"tool {tool!r} requires approval")
        self.tool = tool
        self.run_id = run_id


class DoubleCaptureError(PikachuError):
    """A resume tried to capture a reservation that was already captured.

    Raised rather than tolerated: silently allowing it is charging a user twice for one
    generation, which is the exact failure durable execution is supposed to prevent.
    """

    def __init__(self, reservation_id: str) -> None:
        super().__init__(f"reservation {reservation_id!r} was already captured")
        self.reservation_id = reservation_id
