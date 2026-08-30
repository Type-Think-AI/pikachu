"""The eval case set — deterministic verifiers first, LLM-judge only where nothing else works.

Every case here answers ONE question against a fake, offline, with no clock or random
dependence. A :class:`DeterministicCase` scores exactly 1.0 (held) or 0.0 (broken) — there is
no middle, because the thing it checks is true or false. A :class:`JudgeCase` scores in
``[0.0, 1.0]`` and is flagged :attr:`EvalCase.noisy`; with no judge wired it does not run at
all (the runner records it as skipped).

The five deterministic cases map onto real permission-layer surfaces, so a case that "passes"
proves a live guarantee rather than a tautology:

  * ``skill-body-reaches-model``  — the composed :class:`TurnRequest` actually carries the
    skill body the backend sees (``FakeBackend.received_requests``).
  * ``denied-tool-absent``        — a tool the allowlist excludes never appears in
    ``effective_tools``, so the backend never receives authority for it.
  * ``guard-narrows-over-broad``  — an over-broad declaration is narrowed to
    ``allowlist ∩ declared`` by ``guard.allowlist.effective_tools``.
  * ``tainted-stays-unpromoted``  — a tainted draft is refused promotion by the curator's
    ``promote_on_reuse`` (``TaintedPromotion``), regardless of reuse.
  * ``near-duplicate-refused``    — a description identical to one already in the partition
    breaches the confusability threshold (deterministic cosine 1.0 via the stub embedder).

Each is paired in the tests with a deliberately broken stub that makes it score 0.0 — a case
that cannot fail is not a test.

The scoring surface is deliberately NOT a pytest marker. These are tier-2 trend signals; a
marker would make them a tier-1 gate, which is the exact conflation docs/12-evaluation.md
forbids. The runner turns these scores into a Pokédex entry, never a badge.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "EvalCase",
    "EvalKind",
    "EvalScore",
    "Judge",
    "all_cases",
    "deterministic_cases",
    "judge_cases",
]


class EvalKind(str, Enum):
    """What kind of signal a case produces.

    ``DETERMINISTIC`` is a hard true/false checked against a fake — it scores 1.0 or 0.0 and
    is trustworthy on a single run. ``JUDGE`` is model-scored, continuous, and noisy; it is
    presented as a trend and never as a verdict (docs/12-evaluation.md).
    """

    DETERMINISTIC = "deterministic"
    JUDGE = "judge"


class Judge:
    """Structural type for an LLM-as-judge callable.

    A judge maps a candidate answer plus a rubric to a score in ``[0.0, 1.0]``. It is a
    parameter, never a hardcoded provider — passed in so a real judge can be wired for a live
    trend run while the whole case set stays offline-testable with a scripted stub. The design
    constraint from docs/12-evaluation.md ("never judge with the generating model") is the
    caller's to honour when it supplies one; nothing here calls a model on its own.
    """

    async def score(self, *, answer: str, rubric: str) -> float:  # pragma: no cover - Protocol-ish
        raise NotImplementedError


@dataclass(frozen=True)
class EvalScore:
    """One case's outcome. Recorded, trended — never gates.

    ``skipped`` is a first-class outcome, distinct from a 0.0 score: a judge case with no
    judge, or a case whose optional harness is absent, is SKIPPED (no signal), not FAILED (a
    zero signal). Collapsing the two would let "we didn't measure it" read as "it scored
    badly", which is the opposite of honest.
    """

    case_id: str
    kind: EvalKind
    score: float
    noisy: bool
    skipped: bool = False
    detail: str = ""

    @property
    def held(self) -> bool:
        """For a deterministic case: did the invariant hold? Meaningless for a judge case.

        A judge score is a trend point, not a pass/fail, so ``held`` is only ever read for
        deterministic cases (where 1.0 == held). Exposed so a reporter can count how many
        hard checks passed without re-deriving the rule.
        """
        return self.kind is EvalKind.DETERMINISTIC and not self.skipped and self.score >= 1.0


# A verifier runs offline and returns ``(score, detail)``. It takes an optional judge so the
# same signature covers both deterministic cases (which ignore it) and judge cases (which need
# it and skip when it is ``None``).
Verifier = Callable[["Judge | None"], Awaitable[tuple[float, str]]]


@dataclass(frozen=True)
class EvalCase:
    """A single evaluation: an id, a kind, a rubric, and an offline verifier.

    ``noisy`` is ``True`` exactly for judge cases and is surfaced in every rendering so a
    reader never mistakes a model score for a hard result. ``run`` invokes the verifier and
    wraps the outcome in an :class:`EvalScore`; a judge case with ``judge is None`` returns a
    skipped score rather than scoring 0.0.
    """

    case_id: str
    kind: EvalKind
    rubric: str
    verifier: Verifier
    noisy: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    async def run(self, judge: Judge | None = None) -> EvalScore:
        if self.kind is EvalKind.JUDGE and judge is None:
            return EvalScore(
                case_id=self.case_id,
                kind=self.kind,
                score=0.0,
                noisy=self.noisy,
                skipped=True,
                detail="no judge wired in — judge cases skip rather than fail (tier-2 rule)",
            )
        score, detail = await self.verifier(judge)
        return EvalScore(
            case_id=self.case_id,
            kind=self.kind,
            score=score,
            noisy=self.noisy,
            skipped=False,
            detail=detail,
        )


# --------------------------------------------------------------------------------------
# Deterministic verifiers — each checks one live surface against a fake, offline.
# --------------------------------------------------------------------------------------


async def _verify_skill_body_reaches_model(_judge: Judge | None) -> tuple[float, str]:
    """The composed TurnRequest carries the skill body the backend actually sees.

    A skill whose body never reaches the model is a skill that does nothing. We build a
    request carrying a known body, run it through ``FakeBackend``, and read what the backend
    received back out of ``received_requests`` — the same seam every other lane asserts on.
    """
    from pikachu.backends.fake import FakeBackend
    from pikachu.core.types import (
        AgentSpec,
        Skill,
        SkillStatus,
        TrustTier,
        TurnRequest,
    )

    marker = "NEVER-USE-PURE-BLACK-sentinel-42"
    skill = Skill(
        name="brand-palette",
        description="Apply the house palette.",
        body=f"# Brand palette\n\n{marker}\n",
        declared_tools=("generate_image",),
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
    )
    agent = AgentSpec(name="colourist", allowed_tools=("generate_image",))
    request = TurnRequest(
        message="grade this still",
        agent=agent,
        skill=skill,
        effective_tools=("generate_image",),
    )
    backend = FakeBackend(script=[])
    await backend.run_turn(request)

    got = backend.received_requests
    if not got:
        return 0.0, "backend received no request"
    seen = got[0].skill
    if seen is not None and seen.body is not None and marker in seen.body:
        return 1.0, "skill body reached the backend intact"
    return 0.0, f"skill body did not reach the backend (skill={seen!r})"


async def _verify_denied_tool_absent(_judge: Judge | None) -> tuple[float, str]:
    """A tool outside the allowlist never becomes authority the backend can act on.

    The backend refuses to widen its toolset, so a scripted call to a denied tool raises. But
    the eval-level fact we score is stronger and quieter: the denied tool is simply absent
    from what the request grants. We build a request whose ``effective_tools`` omit the denied
    tool and confirm the backend's own view of authority never contains it.
    """
    from pikachu.backends.base import BaseBackend
    from pikachu.backends.fake import FakeBackend
    from pikachu.core.types import AgentSpec, TurnRequest

    denied = "read_file"
    agent = AgentSpec(name="colourist", allowed_tools=("generate_image",))
    request = TurnRequest(
        message="do the thing",
        agent=agent,
        effective_tools=("generate_image",),  # read_file deliberately not granted
    )
    backend = FakeBackend(script=[])
    await backend.run_turn(request)

    authorized = BaseBackend.authorized_tools(backend.received_requests[0])
    if denied in authorized:
        return 0.0, f"denied tool {denied!r} was present in authority {authorized!r}"
    return 1.0, f"denied tool {denied!r} absent from authority {authorized!r}"


async def _verify_guard_narrows_over_broad(_judge: Judge | None) -> tuple[float, str]:
    """An over-broad declaration is narrowed to allowlist ∩ declared, dangerous stripped.

    Runs the real ``guard.allowlist.effective_tools`` (the P3 gate) with a declaration that
    asks for more than the allowlist grants plus a dangerous tool, and asserts the kept set is
    exactly the intersection minus dangerous — the narrowing the whole permission layer rests
    on.
    """
    from pikachu.guard.allowlist import effective_tools

    allowlist = ("web_search", "generate_image")
    declared = ("web_search", "generate_image", "read_canvas", "bash")  # over-broad + dangerous
    result = effective_tools(allowlist, declared)

    kept = set(result.tools)
    expected = {"web_search", "generate_image"}  # read_canvas not allowed; bash dangerous
    if kept != expected:
        return 0.0, f"guard kept {sorted(kept)}, expected {sorted(expected)}"
    # Every kept tool must be inside allowlist ∩ declared — the P3 subset property.
    intersect = set(allowlist) & set(declared)
    if not kept.issubset(intersect):
        return 0.0, f"kept {sorted(kept)} is not a subset of allowlist ∩ declared {sorted(intersect)}"
    return 1.0, f"guard narrowed to {sorted(kept)} (dropped {sorted(result.removed_tools)})"


async def _verify_tainted_stays_unpromoted(_judge: Judge | None) -> tuple[float, str]:
    """A tainted draft is refused promotion no matter how it is reused.

    Runs the curator's real ``promote_on_reuse`` over a tainted, agent-created draft and
    scores 1.0 only when it raises ``TaintedPromotion`` — the laundering gate holding. A skill
    that promoted despite taint scores 0.0.
    """
    from pikachu.core.errors import TaintedPromotion
    from pikachu.core.types import Lineage, Skill, SkillStatus, Taint, TrustTier
    from pikachu.curator.lifecycle import promote_on_reuse

    tainted_draft = Skill(
        name="distilled-from-a-poisoned-turn",
        description="Looks helpful.",
        body="# ...\n",
        status=SkillStatus.DRAFT,
        trust=TrustTier.BUILTIN,  # agent-created, so the curator would otherwise touch it
        lineage=Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "turn:evil"),
    )
    try:
        promoted = promote_on_reuse(tainted_draft)
    except TaintedPromotion:
        return 1.0, "tainted draft was refused promotion (TaintedPromotion raised)"
    return 0.0, f"tainted draft promoted to {promoted.status.value} — laundering gate failed"


async def _verify_near_duplicate_refused(_judge: Judge | None) -> tuple[float, str]:
    """A near-duplicate description breaches the confusability threshold at authoring time.

    Uses a description IDENTICAL to one already in the partition, which gives cosine 1.0
    against any embedder deterministically (the hash-based stub included), so this scores the
    plumbing — a breach is reported — not a semantic judgement. The check WARNS; scoring 1.0
    means the warning fired, which is the behaviour we want a trend to confirm keeps working.
    """
    from pikachu.skills.confusability import check_new_skill

    class _StubEmbedder:
        dimensions = 16

        async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            import hashlib

            out: list[tuple[float, ...]] = []
            for text in texts:
                digest = hashlib.sha256(text.encode("utf-8")).digest()
                raw = [digest[i] / 255.0 for i in range(self.dimensions)]
                norm = sum(v * v for v in raw) ** 0.5 or 1.0
                out.append(tuple(v / norm for v in raw))
            return tuple(out)

    description = "apply the house palette to a generated still"
    report = await check_new_skill(
        description,
        (description,),  # identical existing skill in the SAME partition
        embedder=_StubEmbedder(),
        partition="colour",
    )
    if report.breaches_threshold and report.nearest_score >= report.threshold:
        return 1.0, f"near-duplicate breached threshold (score={report.nearest_score:.3f})"
    return 0.0, f"near-duplicate did not breach (score={report.nearest_score:.3f})"


# --------------------------------------------------------------------------------------
# Judge verifier — the ONE place nothing deterministic works. Clearly labelled noisy.
# --------------------------------------------------------------------------------------


async def _verify_body_helpfulness_JUDGE(judge: Judge | None) -> tuple[float, str]:
    """Whether a skill body reads as helpful, self-contained guidance — a JUDGE call.

    There is no deterministic verifier for "is this prose good", so this is the one case that
    needs a model. It is flagged ``noisy=True`` and its score is a TREND POINT, never a
    verdict (docs/12-evaluation.md). With no judge wired the case never reaches here — the
    case's ``run`` returns a skipped score.
    """
    if judge is None:  # pragma: no cover - EvalCase.run skips before calling
        return 0.0, "no judge"
    body = (
        "# Brand palette\n\nNever use pure black. Never crop tighter than 16:9. "
        "State the palette hex codes up front so the model does not have to guess."
    )
    score = await judge.score(
        answer=body,
        rubric="Score 0-1 how self-contained and actionable this skill body is.",
    )
    return score, f"judge scored body helpfulness {score:.3f} (NOISY — trend only, not a verdict)"


# --------------------------------------------------------------------------------------
# Case registry
# --------------------------------------------------------------------------------------


def deterministic_cases() -> tuple[EvalCase, ...]:
    """The hard, offline, true/false case set. Each scores 1.0 or 0.0 with zero noise."""
    return (
        EvalCase(
            case_id="skill-body-reaches-model",
            kind=EvalKind.DETERMINISTIC,
            rubric="The composed TurnRequest carries the skill body the backend receives.",
            verifier=_verify_skill_body_reaches_model,
            tags=("skill", "backend"),
        ),
        EvalCase(
            case_id="denied-tool-absent",
            kind=EvalKind.DETERMINISTIC,
            rubric="A tool outside the allowlist is absent from the backend's authority.",
            verifier=_verify_denied_tool_absent,
            tags=("guard", "backend"),
        ),
        EvalCase(
            case_id="guard-narrows-over-broad",
            kind=EvalKind.DETERMINISTIC,
            rubric="effective_tools narrows an over-broad declaration to allowlist ∩ declared.",
            verifier=_verify_guard_narrows_over_broad,
            tags=("guard",),
        ),
        EvalCase(
            case_id="tainted-stays-unpromoted",
            kind=EvalKind.DETERMINISTIC,
            rubric="A tainted draft is refused promotion by the curator.",
            verifier=_verify_tainted_stays_unpromoted,
            tags=("curator", "taint"),
        ),
        EvalCase(
            case_id="near-duplicate-refused",
            kind=EvalKind.DETERMINISTIC,
            rubric="A near-duplicate description breaches the confusability threshold.",
            verifier=_verify_near_duplicate_refused,
            tags=("confusability",),
        ),
    )


def judge_cases() -> tuple[EvalCase, ...]:
    """The noisy, model-scored case set. Skipped when no judge is wired in.

    Kept small and explicitly separate from the deterministic set so a reader never confuses a
    trend point for a hard result. Every case here has ``noisy=True``.
    """
    return (
        EvalCase(
            case_id="body-helpfulness",
            kind=EvalKind.JUDGE,
            rubric="How self-contained and actionable a skill body reads.",
            verifier=_verify_body_helpfulness_JUDGE,
            noisy=True,
            tags=("skill", "judge"),
        ),
    )


def all_cases() -> tuple[EvalCase, ...]:
    """Every case, deterministic first then judge. Order is stable for readable output."""
    return deterministic_cases() + judge_cases()


def filter_cases(
    cases: Sequence[EvalCase], *, tag: str | None = None, kind: EvalKind | None = None
) -> tuple[EvalCase, ...]:
    """Narrow a case set by tag and/or kind. Zero matches is a valid, honest empty result."""
    out = tuple(
        c
        for c in cases
        if (tag is None or tag in c.tags) and (kind is None or c.kind is kind)
    )
    return out
