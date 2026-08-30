"""DISTIL — should a finished turn become a skill? Almost always: no.

This is the creation gate from ``docs/03-skill-lifecycle.md`` / ``docs/13-self-improvement.md``.
Its whole reason to exist is stated bluntly in both docs and worth restating: **most turns
must produce no skill.** A system that writes a skill per turn is the failure mode the
curation literature diagnoses as *library drift* — the retrieval set fills with
near-duplicates, ``find_skill``'s top results become noise, and every later turn silently
degrades. Two independent measurements (SkillsBench, and the SoK survey) find self-generated
skills give no benefit or actively degrade performance *without* this gate. The gate is the
feature.

THE FOUR CHECKS (all must pass; failing any one records a reason and produces no skill)
--------------------------------------------------------------------------------------
1. **SUCCEEDED** — the turn produced an artifact and was not refunded. A refunded turn is a
   failure however it looks, and a failure is not a recipe.
2. **NON-TRIVIAL** — a multi-step recipe, not a single tool call. A one-shot request that
   any prompt would satisfy is not worth a skill.
3. **NOT A NEAR-DUPLICATE** — the single most important anti-drift check. It reuses
   ``skills.confusability`` (which reuses the caller's ``Embedder``); it is **not**
   reimplemented here. This is the check that bounds the library.
4. **PARAMETERISABLE** — the recipe generalises past the literal prompt. A skill pinned to
   one exact input is a cache entry, not a skill.

Every rejection is recorded **with its reason** (:class:`DistilRejection`). The rejection
log is the tuning signal for the gate itself — a gate you cannot see rejecting is a gate you
cannot calibrate.

SECURITY IS NOT OPTIONAL HERE (``docs/06-security.md``)
-------------------------------------------------------
Even a turn that passes all four checks is run through ``guard.authored`` before a draft is
written:

  * its body is scanned with the **same** scanner imported skills get — ``agent_created``
    provenance buys no trust; and
  * the draft **inherits the turn's lineage**, so a turn that consumed untrusted tool output
    or a foreign skill body yields a *tainted* draft that can exist but can never be
    promoted (``guard.lineage`` / the Soul badge enforce that downstream).

A draft is written at ``TrustTier.BUILTIN`` because it is our own generated content, but the
scanner still runs and the taint still attaches — trust in the *tier* sense (may it declare
tools) is separate from trust in the *lineage* sense (is its content clean). A tainted
builtin draft is exactly the case the promotion gate must refuse, and does.

No model call lives on this path. The whole gate is deterministic Python: an LLM may
*propose* a skill body upstream, but whether that proposal becomes a draft is decided here,
by code, so the creation path cannot be argued into by a clever generation.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from pikachu.core.errors import InjectionDetected
from pikachu.core.protocols import Embedder
from pikachu.core.types import Lineage, Skill, SkillStatus, TrustTier
from pikachu.guard.authored import inherit_turn_lineage, scan_authored_body

__all__ = [
    "DistilCandidate",
    "DistilOutcome",
    "DistilRejection",
    "RejectionReason",
    "distil",
]


class RejectionReason(str, Enum):
    """Why a turn did not become a skill. The value doubles as a log label.

    ``CLEAN_SCAN_REQUIRED`` is a *security* rejection: the four content checks passed but the
    body tripped the injection scanner, so no draft is written. It is distinct from the four
    quality reasons because it is not a tuning signal for the gate — it is a block.
    """

    NOT_SUCCEEDED = "not_succeeded"
    TRIVIAL = "trivial"
    NEAR_DUPLICATE = "near_duplicate"
    NOT_PARAMETERISABLE = "not_parameterisable"
    INJECTION_DETECTED = "injection_detected"


class DistilCandidate(BaseModel):
    """The proposed skill plus the turn evidence the gate needs to judge it.

    This is the input to :func:`distil`. The four flags (``succeeded``, ``tool_call_count``,
    ``parameterisable``) come from turn telemetry, not from a model's opinion — the gate
    measures, it does not ask. ``turn_lineage`` is every lineage the turn consumed; the draft
    inherits their union.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    body: str = ""
    declared_tools: tuple[str, ...] = Field(default_factory=tuple)
    partition: str | None = None

    succeeded: bool
    """The turn produced an artifact and was not refunded. Check 1."""

    tool_call_count: int = 0
    """How many tool calls the turn made. ``>= min_tool_calls`` is 'non-trivial'. Check 2."""

    parameterisable: bool = False
    """Whether the recipe generalises past the literal prompt. Check 4. Supplied by turn
    analysis upstream; the gate does not re-derive it, it enforces it."""

    turn_lineage: tuple[Lineage, ...] = Field(default_factory=tuple)
    """Every lineage the turn consumed. The draft inherits their monotonic union — a turn
    that touched anything tainted yields a tainted (unpromotable) draft."""


class DistilRejection(BaseModel):
    """A recorded 'no'. The rejection log is how the gate gets tuned."""

    model_config = ConfigDict(frozen=True)

    name: str
    reason: RejectionReason
    detail: str = ""
    """Human-facing specifics (e.g. the near-duplicate's description and score)."""


class DistilOutcome(BaseModel):
    """The result of the gate: exactly one of ``skill`` or ``rejection`` is set.

    A ``skill`` (a DRAFT, invisible to retrieval) means all four checks passed and the body
    scanned clean. A ``rejection`` means one check failed, with its reason recorded.
    """

    model_config = ConfigDict(frozen=True)

    skill: Skill | None = None
    rejection: DistilRejection | None = None

    @property
    def created(self) -> bool:
        return self.skill is not None


async def distil(
    candidate: DistilCandidate,
    existing_descriptions: tuple[str, ...],
    *,
    embedder: Embedder,
    min_tool_calls: int = 2,
    duplicate_threshold: float = 0.85,
) -> DistilOutcome:
    """Run the four-check creation gate over one finished turn.

    Returns a :class:`DistilOutcome` carrying **either** a DRAFT skill (all checks passed and
    the body scanned clean) **or** a :class:`DistilRejection` naming which check failed.
    Never both, never neither.

    The checks run in a deliberate order — cheapest and most-common-failure first, so the
    expensive embedding call in check 3 is only reached by a turn that already looks like a
    real recipe:

      1. succeeded  2. non-trivial  3. not a near-duplicate  4. parameterisable

    then the security scan. ``existing_descriptions`` must be scoped to the candidate's
    **partition** — that is the set the model actually selects from, so it is the only set a
    near-duplicate matters against.

    This coroutine makes **no model call**. The only await is the embedder, injected by the
    caller (a deterministic stub in tests). Deterministic code measures; the model does not
    decide.
    """
    # --- Check 1: the turn SUCCEEDED -------------------------------------------------
    if not candidate.succeeded:
        return DistilOutcome(
            rejection=DistilRejection(
                name=candidate.name,
                reason=RejectionReason.NOT_SUCCEEDED,
                detail="turn did not produce an artifact, or was refunded",
            )
        )

    # --- Check 2: the turn was NON-TRIVIAL -------------------------------------------
    if candidate.tool_call_count < min_tool_calls:
        return DistilOutcome(
            rejection=DistilRejection(
                name=candidate.name,
                reason=RejectionReason.TRIVIAL,
                detail=(
                    f"{candidate.tool_call_count} tool call(s); "
                    f"need >= {min_tool_calls} for a multi-step recipe"
                ),
            )
        )

    # --- Check 3: NOT a near-duplicate (the single most important anti-drift check) --
    # Reuse skills.confusability; do not reimplement. Imported lazily so a turn that never
    # distils does not pull it. Scoped to the candidate's partition by the caller.
    from pikachu.skills.confusability import check_new_skill

    report = await check_new_skill(
        candidate.description,
        existing_descriptions,
        embedder=embedder,
        threshold=duplicate_threshold,
        partition=candidate.partition,
    )
    if report.breaches_threshold:
        return DistilOutcome(
            rejection=DistilRejection(
                name=candidate.name,
                reason=RejectionReason.NEAR_DUPLICATE,
                detail=(
                    f"description is {report.nearest_score:.3f} similar to "
                    f"{report.nearest_description!r} in partition "
                    f"{candidate.partition!r} (>= {duplicate_threshold})"
                ),
            )
        )

    # --- Check 4: PARAMETERISABLE ----------------------------------------------------
    if not candidate.parameterisable:
        return DistilOutcome(
            rejection=DistilRejection(
                name=candidate.name,
                reason=RejectionReason.NOT_PARAMETERISABLE,
                detail="recipe does not generalise past the literal prompt",
            )
        )

    # --- Security: scan the body with the SAME scanner imported skills get -----------
    # agent_created provenance confers no trust. A detected payload blocks the draft.
    try:
        scan_authored_body(candidate.body, skill_name=candidate.name)
    except InjectionDetected as exc:
        return DistilOutcome(
            rejection=DistilRejection(
                name=candidate.name,
                reason=RejectionReason.INJECTION_DETECTED,
                detail=str(exc),
            )
        )

    # --- Passed everything -> write a DRAFT, inheriting the turn's lineage -----------
    # The draft is BUILTIN (our own generated content, so it MAY declare tools subject to
    # the allowlist), but it inherits the turn's taint: a turn that consumed anything
    # untrusted yields a tainted draft that can never be promoted. Trust-tier and lineage
    # are orthogonal, exactly as docs/06-security.md requires.
    draft = Skill(
        name=candidate.name,
        description=candidate.description,
        body=candidate.body,
        declared_tools=candidate.declared_tools,
        status=SkillStatus.DRAFT,
        trust=TrustTier.BUILTIN,
        lineage=inherit_turn_lineage(*candidate.turn_lineage),
        partition=candidate.partition,
        version=1,
    )
    return DistilOutcome(skill=draft)
