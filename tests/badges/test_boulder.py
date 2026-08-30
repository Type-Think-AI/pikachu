"""Boulder badge (Brock, Rock) — types are solid.

Proves: every model in ``core.types`` round-trips ``model_dump`` -> ``model_validate``
unchanged, every frozen model rejects mutation, enum ``.value`` strings are stable (they
end up in a database, so a rename must break a test), and the documented derived
properties compute correctly.

``mypy --strict`` cleanliness is the other half of Boulder and is asserted by the badge
runner, which invokes mypy separately — a static check has no pytest assertion to make.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import BaseModel, ValidationError

from pikachu.config import DEFAULT_MODEL
from pikachu.core.types import (
    AgentSpec,
    Artifact,
    ArtifactKind,
    Lineage,
    MemoryRecord,
    MemoryScope,
    Provenance,
    Run,
    RunPhase,
    Signal,
    SignalKind,
    SignalSubject,
    Skill,
    SkillStatus,
    Taint,
    ToolOutcome,
    ToolSpec,
    TrustTier,
    TurnRequest,
    TurnResult,
)

pytestmark = pytest.mark.boulder


# --------------------------------------------------------------------------------------
# Representative instances — one of every model, populated with non-default values so a
# round-trip that silently drops a field is caught.
# --------------------------------------------------------------------------------------


def _tainted_lineage() -> Lineage:
    return Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "tool:web_search")


def _every_model() -> list[BaseModel]:
    lineage = _tainted_lineage()
    prov = Provenance(
        prompt="a red bicycle", model="x", cost_credits=35, seed=7, produced_by="colourist"
    )
    return [
        lineage,
        Skill(
            name="brand-palette",
            description="apply the house palette",
            body="# body",
            declared_tools=("generate_image", "generate_image"),
            status=SkillStatus.ACTIVE,
            trust=TrustTier.BUILTIN,
            version=3,
            parent_version=2,
            pinned=True,
            partition="colour",
            stripped_scripts=("setup.sh",),
        ),
        ToolSpec(name="generate_image", description="d", cost_credits=35, requires_approval=True),
        AgentSpec(
            name="colourist",
            role="grade stills",
            instructions="match palette",
            skill_tags=("colour",),
            allowed_tools=("generate_image", "read_canvas"),
            model=DEFAULT_MODEL,
            triggers=("grade",),
        ),
        Run(
            id="run-1",
            agent_name="colourist",
            phase=RunPhase.RUNNING,
            iteration=2,
            max_iterations=20,
            charged_credits=70,
            refunded_credits=35,
            captured_reservations=frozenset({"res-a"}),
        ),
        prov,
        Artifact(
            id="art-1",
            kind=ArtifactKind.IMAGE,
            payload_ref="r2://x",
            parent="art-0",
            provenance=prov,
            lineage=lineage,
        ),
        MemoryRecord(
            key="brand.primary",
            value="#FCFCFC",
            scope=MemoryScope.LONG,
            confidence=0.8,
            evidence_count=4,
            lineage=lineage,
        ),
        Signal(
            subject=SignalSubject.SKILL,
            subject_id="brand-palette",
            kind=SignalKind.KEPT,
            strength=0.2,
            run_id="run-1",
        ),
        TurnRequest(
            message="grade this",
            agent=AgentSpec(name="a", allowed_tools=("generate_image",)),
            history=({"role": "user", "content": "hi"},),
            effective_tools=("generate_image",),
            run_id="run-1",
        ),
        TurnResult(
            text="done",
            artifacts=(),
            tool_calls=({"tool": "generate_image"},),
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=800,
            cache_write_tokens=0,
            cost_credits=35,
            iterations=3,
        ),
    ]


@pytest.mark.parametrize("model", _every_model(), ids=lambda m: type(m).__name__)
def test_model_dump_validate_round_trips_unchanged(model: BaseModel) -> None:
    """model_dump -> model_validate reconstructs an equal instance."""
    dumped = model.model_dump()
    restored = type(model).model_validate(dumped)
    assert restored == model


@pytest.mark.parametrize("model", _every_model(), ids=lambda m: type(m).__name__)
def test_json_round_trips_unchanged(model: BaseModel) -> None:
    """The JSON path (what actually hits a database) also round-trips."""
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


@pytest.mark.parametrize("model", _every_model(), ids=lambda m: type(m).__name__)
def test_every_model_is_frozen(model: BaseModel) -> None:
    """Every core model is frozen; mutating any field raises."""
    field_name = next(iter(type(model).model_fields))
    with pytest.raises(ValidationError):
        setattr(model, field_name, getattr(model, field_name))


# --------------------------------------------------------------------------------------
# Enum value strings are stable — these literals end up in a database.
# A rename becomes a test failure, on purpose.
# --------------------------------------------------------------------------------------


def test_trust_tier_value_strings_are_stable() -> None:
    assert {t.name: t.value for t in TrustTier} == {
        "BUILTIN": "builtin",
        "VERIFIED": "verified",
        "COMMUNITY": "community",
        "UNTRUSTED": "untrusted",
    }


def test_taint_value_strings_are_stable() -> None:
    assert {t.name: t.value for t in Taint} == {
        "FOREIGN_SKILL": "foreign_skill",
        "TOOL_OUTPUT": "tool_output",
        "CANVAS_READ": "canvas_read",
        "USER_UNVERIFIED": "user_unverified",
    }


def test_skill_status_value_strings_are_stable() -> None:
    assert {s.name: s.value for s in SkillStatus} == {
        "DRAFT": "draft",
        "CANDIDATE": "candidate",
        "ACTIVE": "active",
        "ARCHIVED": "archived",
    }


def test_tool_outcome_value_strings_are_stable() -> None:
    assert {o.name: o.value for o in ToolOutcome} == {
        "SUCCESS": "success",
        "FAILED": "failed",
        "DENIED": "denied",
        "INTERRUPTED": "interrupted",
    }


def test_run_phase_value_strings_are_stable() -> None:
    assert {p.name: p.value for p in RunPhase} == {
        "PENDING": "pending",
        "RUNNING": "running",
        "AWAITING_APPROVAL": "awaiting_approval",
        "SUCCEEDED": "succeeded",
        "FAILED": "failed",
        "CANCELLED": "cancelled",
    }


def test_artifact_kind_value_strings_are_stable() -> None:
    assert {k.name: k.value for k in ArtifactKind} == {
        "TEXT": "text",
        "IMAGE": "image",
        "VIDEO": "video",
        "AUDIO": "audio",
        "DOCUMENT": "document",
        "DATA": "data",
    }


def test_memory_scope_value_strings_are_stable() -> None:
    assert {s.name: s.value for s in MemoryScope} == {
        "SHORT": "short",
        "MID": "mid",
        "LONG": "long",
    }


def test_signal_subject_value_strings_are_stable() -> None:
    assert {s.name: s.value for s in SignalSubject} == {
        "AGENT": "agent",
        "SKILL": "skill",
        "MEMORY": "memory",
        "ARTIFACT": "artifact",
        "TOOL": "tool",
        "RUN": "run",
    }


def test_signal_kind_value_strings_are_stable() -> None:
    assert {k.name: k.value for k in SignalKind} == {
        "KEPT": "kept",
        "EXPORTED": "exported",
        "REGENERATED_AWAY": "regenerated_away",
        "EDITED_THEN_KEPT": "edited_then_kept",
        "ABANDONED": "abandoned",
        "RATED": "rated",
        "CORRECTED": "corrected",
        "REUSED": "reused",
    }


# --------------------------------------------------------------------------------------
# Documented derived properties compute correctly.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (TrustTier.BUILTIN, True),
        (TrustTier.VERIFIED, True),
        (TrustTier.COMMUNITY, False),
        (TrustTier.UNTRUSTED, False),
    ],
)
def test_trust_tier_may_contribute_tools(tier: TrustTier, expected: bool) -> None:
    assert tier.may_contribute_tools is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (SkillStatus.DRAFT, False),
        (SkillStatus.CANDIDATE, True),
        (SkillStatus.ACTIVE, True),
        (SkillStatus.ARCHIVED, False),
    ],
)
def test_skill_status_is_retrievable(status: SkillStatus, expected: bool) -> None:
    assert status.is_retrievable is expected


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (RunPhase.PENDING, False),
        (RunPhase.RUNNING, False),
        (RunPhase.AWAITING_APPROVAL, False),
        (RunPhase.SUCCEEDED, True),
        (RunPhase.FAILED, True),
        (RunPhase.CANCELLED, True),
    ],
)
def test_run_phase_is_terminal(phase: RunPhase, expected: bool) -> None:
    assert phase.is_terminal is expected


def test_lineage_is_clean_and_merge() -> None:
    clean = Lineage.clean()
    assert clean.is_clean is True

    tainted = clean.with_taint(Taint.FOREIGN_SKILL, "catalog:community")
    assert tainted.is_clean is False
    assert Taint.FOREIGN_SKILL in tainted.taints
    assert "catalog:community" in tainted.sources

    # Merge is monotonic: it never drops a taint or a source.
    other = Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "tool:web")
    merged = tainted.merge(other)
    assert merged.taints == frozenset({Taint.FOREIGN_SKILL, Taint.TOOL_OUTPUT})
    assert set(merged.sources) == {"catalog:community", "tool:web"}
    # Sources deduplicate but preserve first-seen order.
    assert merged.sources == ("catalog:community", "tool:web")


def test_lineage_merge_is_monotonic_cannot_launder() -> None:
    """There is no clear(); merging a clean lineage never removes an existing taint."""
    tainted = Lineage.clean().with_taint(Taint.CANVAS_READ, "canvas:art-1")
    still_tainted = tainted.merge(Lineage.clean())
    assert still_tainted.is_clean is False
    assert still_tainted.taints == tainted.taints


@pytest.mark.parametrize(
    ("read", "input_", "expected"),
    [
        (0, 0, 0.0),
        (800, 200, 0.8),
        (1000, 0, 1.0),
        (0, 1000, 0.0),
    ],
)
def test_turn_result_cache_hit_ratio(read: int, input_: int, expected: float) -> None:
    result = TurnResult(text="", cache_read_tokens=read, input_tokens=input_)
    assert result.cache_hit_ratio == pytest.approx(expected)


@pytest.mark.parametrize(
    ("charged", "refunded", "expected"),
    [(0, 0, 0), (70, 0, 70), (70, 35, 35), (35, 35, 0)],
)
def test_run_net_credits(charged: int, refunded: int, expected: int) -> None:
    run = Run(
        id="r", agent_name="a", charged_credits=charged, refunded_credits=refunded
    )
    assert run.net_credits == expected


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (SignalKind.KEPT, True),
        (SignalKind.EXPORTED, True),
        (SignalKind.EDITED_THEN_KEPT, True),
        (SignalKind.REUSED, True),
        (SignalKind.REGENERATED_AWAY, False),
        (SignalKind.ABANDONED, False),
        (SignalKind.RATED, False),
        (SignalKind.CORRECTED, False),
    ],
)
def test_signal_kind_is_positive(kind: SignalKind, expected: bool) -> None:
    assert kind.is_positive is expected


# --------------------------------------------------------------------------------------
# Structural guarantees the design comments promise.
# --------------------------------------------------------------------------------------


def test_untrusted_skill_cannot_declare_tools() -> None:
    """The type refuses to construct an untrusted skill that declares a toolset."""
    with pytest.raises(ValidationError):
        Skill(name="x", declared_tools=("web_search",), trust=TrustTier.UNTRUSTED)


def test_declared_tools_normalized_but_not_deduped() -> None:
    """Order and multiplicity survive — a pinned parent-repo test depends on it."""
    skill = Skill(
        name="x",
        declared_tools=(" WEB ", "web", "web"),
        trust=TrustTier.BUILTIN,
    )
    assert skill.declared_tools == ("web", "web", "web")


def test_memory_may_justify_authority_is_literal_false() -> None:
    """Memory never justifies a sensitive action — the property is a constant False."""
    record = MemoryRecord(key="k", value="v")
    assert record.may_justify_authority is False


def test_skill_may_promote_requires_clean_lineage() -> None:
    clean = Skill(name="x", trust=TrustTier.BUILTIN, status=SkillStatus.CANDIDATE)
    assert clean.may_promote is True

    tainted = Skill(
        name="y",
        trust=TrustTier.BUILTIN,
        status=SkillStatus.CANDIDATE,
        lineage=Lineage.clean().with_taint(Taint.FOREIGN_SKILL, "s"),
    )
    assert tainted.may_promote is False


def test_frozen_reservation_dataclass_is_immutable() -> None:
    """Sanity check that the fakes' Reservation is also frozen (used across lanes)."""
    from tests.fakes import FakeReservation

    res = FakeReservation(_id="r", _amount=10)
    with pytest.raises(dataclasses.FrozenInstanceError):
        res._amount = 20  # type: ignore[misc]
