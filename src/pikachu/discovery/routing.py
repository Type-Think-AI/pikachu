"""Conservative routing and partition management.

ROUTING IS CONSERVATIVE BY DESIGN
---------------------------------
The one behaviour this module refuses to have is *guessing*. Three rules, taken verbatim
from KiroCrew's ``select_crew`` discipline (docs/14-multi-agent.md):

1. **Trigger-match only.** An agent is a candidate iff one of its declared ``triggers``
   matches the message. An agent with **no triggers is never auto-selected** — it stays
   reachable only by name through the registry.
2. **Ambiguity does not resolve to a pick.** If two or more agents match, ``route`` returns
   :attr:`RouteDecision.AMBIGUOUS` with *all* candidates and lets the caller decide. It never
   silently chooses the first, the most-triggered, or the "best" — there is no scoring model
   here to be wrong.
3. **Default is a single agent.** When nothing matches, the decision is
   :attr:`RouteDecision.DEFAULT` and the caller runs its one default agent. Multi-agent is
   opt-in; it exists only because a user configured a crew.

There is deliberately **no supervisor, no delegation graph, no topology**. ``route`` is a
pure function over specs and a message. Coordination between agents is the canvas, and
re-introducing message passing here is precisely the failure the multi-agent design rejects.

PARTITION MANAGEMENT — THE LOAD-BEARING PART
--------------------------------------------
An agent's ``skill_tags`` define its **partition**: the set of skills the model selects from
when that agent runs. Skill selection does not degrade gracefully as a partition grows — it
stays stable, then **drops sharply**, and the driver is *semantic confusability among similar
descriptions*, not the count. The failure is **silent**: the wrong skill is chosen and
nothing errors.

So this module wires in :mod:`pikachu.skills.confusability` (it does not reimplement it):

* :func:`check_partition_addition` — before a new skill joins a partition, warn if its
  description is too close to one already there. **WARN, never reject.** A human may have a
  good reason for two similar skills; substituting our judgement for theirs would get the
  check switched off.
* :func:`audit_partition` — the "this agent should be split" signal, computed as the **max
  pairwise description similarity within the partition** (constraint C7). The number is
  exposed on :class:`PartitionAudit` so telemetry/ can trend it as a leading indicator,
  *before* selection accuracy drops rather than after.

EVIDENCE CAVEAT
---------------
The confusability-cliff mechanism comes from a **single-author preliminary technical
report**. The qualitative claim (a sharp, confusability-driven cliff) is plausible; the
specific threshold at which it triggers is **unconfirmed**. The ``0.85`` split threshold
below is **our own default, not a published figure**. It is a starting point to tune against
real selection data, and this docstring exists so no reader mistakes it for a validated
constant. We act on the mechanism anyway only because the failure it predicts is silent, and
the guard is cheap.

NETWORK
-------
No network here. The :class:`~pikachu.core.protocols.Embedder` is a parameter, passed
through to the confusability helpers; in tests it is the deterministic hash-based stub. The
stub carries no real semantics — it is for testing plumbing and thresholds, never for
asserting a semantic judgement.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from pikachu.core.protocols import Embedder
from pikachu.core.types import AgentSpec

__all__ = [
    "RouteDecision",
    "RouteResult",
    "route",
    "PartitionAudit",
    "audit_partition",
    "check_partition_addition",
    "DEFAULT_SPLIT_THRESHOLD",
]


# Our own default, NOT a published number. See the module docstring's evidence caveat.
DEFAULT_SPLIT_THRESHOLD = 0.85


# --------------------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------------------


class RouteDecision(str, Enum):
    """What ``route`` concluded. The caller branches on this, never on a guessed pick."""

    MATCHED = "matched"
    """Exactly one agent matched a trigger. ``candidates`` holds that single agent."""

    AMBIGUOUS = "ambiguous"
    """Two or more agents matched. ``candidates`` holds all of them; the caller decides.
    We do NOT pick one — there is no scoring here to be wrong about."""

    DEFAULT = "default"
    """No agent matched. The caller runs its single default agent. ``candidates`` is empty.
    This is the conservative, single-agent default, not a failure."""


class RouteResult(BaseModel):
    """The outcome of routing one message. Frozen — a decision is a value, not a mutable
    accumulator.
    """

    model_config = ConfigDict(frozen=True)

    decision: RouteDecision
    candidates: tuple[AgentSpec, ...] = Field(default_factory=tuple)
    matched_triggers: tuple[str, ...] = Field(default_factory=tuple)
    """The distinct triggers that fired, for explainability. Empty on DEFAULT."""

    @property
    def selected(self) -> AgentSpec | None:
        """The single agent to run, or ``None``.

        Populated ONLY on :attr:`RouteDecision.MATCHED`. On AMBIGUOUS it is ``None`` on
        purpose — reading a "the" selection off an ambiguous route is the guess this design
        refuses, so the caller must look at ``candidates`` instead. On DEFAULT it is ``None``
        because the default agent is the caller's, not one of these specs.
        """
        if self.decision is RouteDecision.MATCHED and self.candidates:
            return self.candidates[0]
        return None


def _trigger_hits(message: str, spec: AgentSpec) -> tuple[str, ...]:
    """Triggers of ``spec`` that appear in ``message``, matched case-insensitively.

    Substring containment, deliberately simple: a trigger is a hint a user typed, not a
    regex, and there is no model in the loop. An agent with no triggers returns nothing, so
    it can never be a candidate — the by-name-only guarantee falls straight out of this.
    """
    lowered = message.lower()
    hits: list[str] = []
    for trig in spec.triggers:
        needle = trig.strip().lower()
        if needle and needle in lowered:
            hits.append(trig)
    return tuple(hits)


def route(message: str, agents: Iterable[AgentSpec]) -> RouteResult:
    """Route a message to at most one agent, conservatively.

    Returns a :class:`RouteResult` whose ``decision`` is:

    * ``MATCHED`` — exactly one agent had a trigger in the message;
    * ``AMBIGUOUS`` — two or more did (all returned as candidates, none chosen);
    * ``DEFAULT`` — none did (run the caller's single default agent).

    Agents with no triggers are skipped entirely: they are by-name-invocation only and are
    never auto-selected. This function is pure — it reads specs and a string and returns a
    value, with no I/O and no model call.
    """
    matched: list[AgentSpec] = []
    fired: list[str] = []
    for spec in agents:
        hits = _trigger_hits(message, spec)
        if hits:
            matched.append(spec)
            fired.extend(hits)

    if not matched:
        return RouteResult(decision=RouteDecision.DEFAULT)

    distinct_triggers = tuple(dict.fromkeys(fired))
    if len(matched) == 1:
        return RouteResult(
            decision=RouteDecision.MATCHED,
            candidates=tuple(matched),
            matched_triggers=distinct_triggers,
        )
    return RouteResult(
        decision=RouteDecision.AMBIGUOUS,
        candidates=tuple(matched),
        matched_triggers=distinct_triggers,
    )


# --------------------------------------------------------------------------------------
# Partition management
# --------------------------------------------------------------------------------------


class PartitionAudit(BaseModel):
    """The confusability state of one partition, as a trendable value.

    ``max_pairwise_similarity`` is the leading indicator (constraint C7): the closest any two
    skill descriptions in the partition are to each other. When it crosses ``threshold``,
    ``should_split`` is ``True`` — a WARNING that this agent can no longer reliably tell its
    own skills apart, not an instruction to split automatically.
    """

    model_config = ConfigDict(frozen=True)

    partition: str | None = None
    skill_count: Annotated[int, Field(ge=0)] = 0
    threshold: float = DEFAULT_SPLIT_THRESHOLD

    max_pairwise_similarity: float = 0.0
    """Max cosine similarity between any two descriptions in the partition. ``0.0`` when
    fewer than two skills exist (nothing to confuse), which correctly never trips a split."""

    nearest_pair: tuple[str, str] | None = None
    """The two descriptions that are most similar, for a human to look at. ``None`` when the
    partition has fewer than two skills."""

    should_split: bool = False
    """Exposed for telemetry to trend. WARN-only: a human decides whether to split."""


async def audit_partition(
    descriptions: Sequence[str],
    *,
    embedder: Embedder,
    partition: str | None = None,
    threshold: float = DEFAULT_SPLIT_THRESHOLD,
) -> PartitionAudit:
    """Compute the split signal for a partition: its max pairwise description similarity.

    Delegates the pairwise work to :func:`pikachu.skills.confusability.max_pairwise_similarity`
    (imported lazily, not reimplemented). A partition with fewer than two descriptions has
    nothing to confuse, so it reports similarity ``0.0`` and never suggests a split.

    ``should_split`` is ``True`` iff the max similarity meets or exceeds ``threshold``. That
    threshold is our own default (see the module docstring); pass a tuned value once you have
    selection data.
    """
    from pikachu.skills.confusability import max_pairwise_similarity

    descs = tuple(descriptions)
    best = await max_pairwise_similarity(descs, embedder=embedder)
    if best is None:
        return PartitionAudit(
            partition=partition,
            skill_count=len(descs),
            threshold=threshold,
            max_pairwise_similarity=0.0,
            nearest_pair=None,
            should_split=False,
        )
    return PartitionAudit(
        partition=partition,
        skill_count=len(descs),
        threshold=threshold,
        max_pairwise_similarity=best.score,
        nearest_pair=(best.text_a, best.text_b),
        should_split=best.score >= threshold,
    )


async def check_partition_addition(
    new_description: str,
    existing_descriptions: Sequence[str],
    *,
    embedder: Embedder,
    partition: str | None = None,
    threshold: float = DEFAULT_SPLIT_THRESHOLD,
) -> "ConfusabilityReport":
    """Warn if a skill about to join a partition is too close to one already in it.

    A thin, intent-named wrapper over
    :func:`pikachu.skills.confusability.check_new_skill` (imported lazily). It exists so a
    caller adding a skill to an agent's partition reads a verb that names *what it is doing*
    rather than the generic confusability API, and so the "same partition only" scoping is
    explicit at the call site: pass only the descriptions already in this agent's partition.

    Returns the confusability report unchanged. ``breaches_threshold`` being ``True`` is a
    **warning** — the caller (a human, ultimately) decides whether to admit the skill anyway.
    A skill whose look-alike lives in a *different* partition is not passed here, so it
    correctly does not warn: the model never chooses between partitions.
    """
    from pikachu.skills.confusability import check_new_skill

    return await check_new_skill(
        new_description,
        tuple(existing_descriptions),
        embedder=embedder,
        threshold=threshold,
        partition=partition,
    )


# Re-exported type name for the return annotation above, resolved lazily to keep this
# module's import cost off callers that only route.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from pikachu.skills.confusability import ConfusabilityReport
