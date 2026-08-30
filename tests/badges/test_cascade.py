"""Cascade badge (Misty, Water) — contracts flow.

Proves: every fake satisfies ``isinstance()`` against its ``runtime_checkable`` Protocol,
each fake round-trips its primary operation, and the ``SignalLedger`` protocol exposes no
score/read method — scores must never be readable from anywhere that could place them in
an agent's context.
"""

from __future__ import annotations

import inspect

import pytest

from pikachu.core.errors import DoubleCaptureError
from pikachu.core.protocols import (
    Biller,
    CanvasStore,
    MemoryStore,
    RunStore,
    SignalLedger,
    SkillStore,
)
from pikachu.core.types import (
    Artifact,
    ArtifactKind,
    MemoryRecord,
    MemoryScope,
    Run,
    RunPhase,
    Signal,
    SignalKind,
    SignalSubject,
    Skill,
    SkillStatus,
    ToolOutcome,
    TrustTier,
)
from tests.fakes import (
    FakeBiller,
    FakeCanvasStore,
    FakeMemoryStore,
    FakeRunStore,
    FakeSignalLedger,
    FakeSkillStore,
)

pytestmark = pytest.mark.cascade


# --------------------------------------------------------------------------------------
# Every fake satisfies isinstance() against its runtime_checkable Protocol.
# --------------------------------------------------------------------------------------


def test_fake_skill_store_is_a_skill_store() -> None:
    assert isinstance(FakeSkillStore(), SkillStore)


def test_fake_biller_is_a_biller() -> None:
    assert isinstance(FakeBiller(), Biller)


def test_fake_run_store_is_a_run_store() -> None:
    assert isinstance(FakeRunStore(), RunStore)


def test_fake_memory_store_is_a_memory_store() -> None:
    assert isinstance(FakeMemoryStore(), MemoryStore)


def test_fake_canvas_store_is_a_canvas_store() -> None:
    assert isinstance(FakeCanvasStore(), CanvasStore)


def test_fake_signal_ledger_is_a_signal_ledger() -> None:
    assert isinstance(FakeSignalLedger(), SignalLedger)


# --------------------------------------------------------------------------------------
# Each fake round-trips its primary operation.
# --------------------------------------------------------------------------------------


async def test_skill_store_put_get_round_trip() -> None:
    store = FakeSkillStore()
    skill = Skill(name="s", trust=TrustTier.BUILTIN, status=SkillStatus.ACTIVE, version=1)
    await store.put(skill)
    assert await store.get("s") == skill


async def test_skill_store_find_returns_only_retrievable() -> None:
    """The structural guarantee: drafts and archived skills never come out of find."""
    retrievable = (
        Skill(name="cand", status=SkillStatus.CANDIDATE, trust=TrustTier.BUILTIN),
        Skill(name="act", status=SkillStatus.ACTIVE, trust=TrustTier.BUILTIN),
    )
    hidden = (
        Skill(name="draft", status=SkillStatus.DRAFT, trust=TrustTier.BUILTIN),
        Skill(name="arch", status=SkillStatus.ARCHIVED, trust=TrustTier.BUILTIN),
    )
    store = FakeSkillStore(retrievable + hidden)
    found = await store.find("", limit=10)
    names = {s.name for s in found}
    assert names == {"cand", "act"}
    assert all(s.status.is_retrievable for s in found)


async def test_skill_store_find_scopes_to_partition() -> None:
    store = FakeSkillStore(
        (
            Skill(name="a", status=SkillStatus.ACTIVE, trust=TrustTier.BUILTIN, partition="p1"),
            Skill(name="b", status=SkillStatus.ACTIVE, trust=TrustTier.BUILTIN, partition="p2"),
        )
    )
    found = await store.find("", partition="p1", limit=10)
    assert {s.name for s in found} == {"a"}


async def test_biller_reserve_capture_round_trip() -> None:
    biller = FakeBiller()
    res = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)
    assert res.amount == 35
    await biller.capture(res.id, outcome=ToolOutcome.SUCCESS)
    assert biller.is_captured(res.id)
    assert biller.captured_amount() == 35


async def test_biller_capture_is_idempotent_and_raises_on_double() -> None:
    """A second capture of the same reservation is the double-charge bug — it raises."""
    biller = FakeBiller()
    res = await biller.reserve(run_id="run-1", tool="generate_image", amount=35)
    await biller.capture(res.id, outcome=ToolOutcome.SUCCESS)
    with pytest.raises(DoubleCaptureError) as exc:
        await biller.capture(res.id, outcome=ToolOutcome.SUCCESS)
    assert exc.value.reservation_id == res.id
    # The charge was not doubled.
    assert biller.captured_amount() == 35


async def test_biller_release_then_capture_is_rejected() -> None:
    biller = FakeBiller()
    res = await biller.reserve(run_id="run-1", tool="t", amount=10)
    await biller.release(res.id)
    with pytest.raises(KeyError):
        await biller.capture(res.id, outcome=ToolOutcome.SUCCESS)


async def test_biller_release_is_idempotent() -> None:
    biller = FakeBiller()
    res = await biller.reserve(run_id="run-1", tool="t", amount=10)
    await biller.release(res.id)
    await biller.release(res.id)  # no raise
    assert biller.captured_amount() == 0


async def test_run_store_create_checkpoint_round_trip() -> None:
    store = FakeRunStore()
    run = Run(id="run-1", agent_name="a", phase=RunPhase.PENDING)
    await store.create(run)
    assert await store.get("run-1") == run

    advanced = run.model_copy(update={"phase": RunPhase.RUNNING, "iteration": 1})
    await store.checkpoint(advanced)
    got = await store.get("run-1")
    assert got is not None
    assert got.phase is RunPhase.RUNNING
    assert got.iteration == 1


async def test_run_store_create_rejects_duplicate() -> None:
    store = FakeRunStore()
    run = Run(id="run-1", agent_name="a")
    await store.create(run)
    with pytest.raises(KeyError):
        await store.create(run)


async def test_memory_store_remember_recall_round_trip() -> None:
    store = FakeMemoryStore()
    record = MemoryRecord(key="brand.primary", value="#FCFCFC", scope=MemoryScope.LONG)
    await store.remember(record)
    recalled = await store.recall("brand", scope=MemoryScope.LONG)
    assert record in recalled


async def test_memory_store_decay_lowers_never_deletes() -> None:
    store = FakeMemoryStore()
    await store.remember(MemoryRecord(key="k", value="v", confidence=0.5))
    affected = await store.decay(older_than_days=30)
    assert affected == 1
    survivors = await store.recall("k")
    assert len(survivors) == 1
    assert survivors[0].confidence == pytest.approx(0.4)


async def test_canvas_store_append_get_round_trip() -> None:
    store = FakeCanvasStore()
    art = Artifact(id="art-1", kind=ArtifactKind.IMAGE, payload_ref="r2://x")
    await store.append(art)
    assert await store.get("art-1") == art


async def test_canvas_store_append_rejects_duplicate_id() -> None:
    """Append-only: a duplicate id is rejected rather than overwriting."""
    store = FakeCanvasStore()
    art = Artifact(id="art-1", kind=ArtifactKind.TEXT, payload_ref="r2://a")
    await store.append(art)
    clash = Artifact(id="art-1", kind=ArtifactKind.TEXT, payload_ref="r2://b")
    with pytest.raises(ValueError):
        await store.append(clash)
    # Original survives, unmodified.
    got = await store.get("art-1")
    assert got is not None
    assert got.payload_ref == "r2://a"


async def test_canvas_store_children_by_parent() -> None:
    store = FakeCanvasStore()
    await store.append(Artifact(id="root", kind=ArtifactKind.TEXT, payload_ref="r"))
    await store.append(
        Artifact(id="rev-1", kind=ArtifactKind.TEXT, payload_ref="r1", parent="root")
    )
    await store.append(
        Artifact(id="rev-2", kind=ArtifactKind.TEXT, payload_ref="r2", parent="root")
    )
    kids = await store.children("root")
    assert {a.id for a in kids} == {"rev-1", "rev-2"}


async def test_signal_ledger_record_accepts_signal() -> None:
    ledger = FakeSignalLedger()
    await ledger.record(
        Signal(subject=SignalSubject.SKILL, subject_id="s", kind=SignalKind.KEPT)
    )
    assert ledger._test_recorded_count() == 1


# --------------------------------------------------------------------------------------
# The SignalLedger must NOT expose a score/read path. Scores drive retrieval rank; if one
# can be read, it can end up in an agent's context and be optimised against.
# --------------------------------------------------------------------------------------

_FORBIDDEN_READBACK_NAMES = frozenset(
    {"score", "scores", "read", "get", "read_score", "get_score", "rank", "ranking", "query"}
)


def test_signal_ledger_protocol_has_no_score_or_read_method() -> None:
    """If someone adds a getter to the SignalLedger protocol, this fails."""
    members = {
        name
        for name, _ in inspect.getmembers(SignalLedger)
        if not name.startswith("_")
    }
    leaked = members & _FORBIDDEN_READBACK_NAMES
    assert not leaked, f"SignalLedger must be write-only; found read path(s): {sorted(leaked)}"
    # The only public method the protocol should expose is `record`.
    assert members == {"record"}, f"unexpected public members on SignalLedger: {sorted(members)}"


def test_fake_signal_ledger_exposes_no_public_score_or_read_method() -> None:
    """The fake must not smuggle a readable score in either."""
    public = {name for name in dir(FakeSignalLedger) if not name.startswith("_")}
    leaked = public & _FORBIDDEN_READBACK_NAMES
    assert not leaked, f"FakeSignalLedger leaks a readable score via: {sorted(leaked)}"
