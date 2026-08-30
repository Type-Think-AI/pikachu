"""Tests for the SQLite backends and the markdown export format.

No network, no clock-dependent assertions. Every SQLite store is driven against ``:memory:``
or a ``tmp_path`` file. The invariants under test are the same ones ``tests/fakes.py`` upholds
for the in-memory reference implementations — the SQLite versions must satisfy them too.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pikachu.core.errors import DoubleCaptureError
from pikachu.core.protocols import (
    CanvasStore,
    MemoryStore,
    RunStore,
    SkillStore,
)
from pikachu.core.types import (
    Artifact,
    ArtifactKind,
    Lineage,
    MemoryRecord,
    MemoryScope,
    Provenance,
    Run,
    RunPhase,
    Skill,
    SkillStatus,
    Taint,
    ToolOutcome,
    TrustTier,
)
from pikachu.storage import export_records, import_records
from pikachu.storage.markdown import (
    artifact_from_markdown,
    artifact_to_markdown,
    memory_from_markdown,
    memory_to_markdown,
    skill_from_markdown,
    skill_to_markdown,
)
from pikachu.storage.sqlite import (
    SCHEMA_VERSION,
    SqliteStorage,
    connect,
    migrate,
)

# The project runs pytest-asyncio in auto mode (pyproject: asyncio_mode = "auto"), so an
# async def test needs no marker.


@pytest.fixture
def storage() -> Iterator[SqliteStorage]:
    s = SqliteStorage(":memory:")
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------------------
# Protocol conformance — every SQLite store isinstance() its runtime_checkable Protocol
# --------------------------------------------------------------------------------------


def test_stores_satisfy_their_protocols(storage: SqliteStorage) -> None:
    assert isinstance(storage.skills, SkillStore)
    assert isinstance(storage.memory, MemoryStore)
    assert isinstance(storage.runs, RunStore)
    assert isinstance(storage.canvas, CanvasStore)


# --------------------------------------------------------------------------------------
# SkillStore.find cannot return a draft, however it is called
# --------------------------------------------------------------------------------------


async def test_find_never_returns_a_draft(storage: SqliteStorage) -> None:
    draft = Skill(name="secret", description="draft one", status=SkillStatus.DRAFT)
    active = Skill(name="shipped", description="active one", status=SkillStatus.ACTIVE)
    candidate = Skill(name="trial", description="candidate one", status=SkillStatus.CANDIDATE)
    archived = Skill(name="gone", description="archived one", status=SkillStatus.ARCHIVED)
    await storage.skills.put(draft)
    await storage.skills.put(active)
    await storage.skills.put(candidate)
    await storage.skills.put(archived)

    # Empty query returns everything retrievable — and nothing else.
    names = {s.name for s in await storage.skills.find("", limit=100)}
    assert names == {"shipped", "trial"}
    assert "secret" not in names
    assert "gone" not in names

    # Even a query matching the draft's text cannot surface it.
    for q in ("secret", "draft", "one", "SECRET"):
        got = await storage.skills.find(q, limit=100)
        assert all(s.status.is_retrievable for s in got)
        assert "secret" not in {s.name for s in got}


async def test_find_respects_partition_and_get_still_reaches_drafts(
    storage: SqliteStorage,
) -> None:
    await storage.skills.put(
        Skill(name="a", status=SkillStatus.ACTIVE, partition="p1", description="alpha")
    )
    await storage.skills.put(
        Skill(name="b", status=SkillStatus.ACTIVE, partition="p2", description="beta")
    )
    p1 = await storage.skills.find("", partition="p1", limit=10)
    assert {s.name for s in p1} == {"a"}

    # get() by explicit name may still reach a draft — only find() is filtered.
    draft = Skill(name="d", status=SkillStatus.DRAFT, description="d")
    await storage.skills.put(draft)
    assert (await storage.skills.get("d")) is not None


async def test_skill_round_trips_through_sqlite(storage: SqliteStorage) -> None:
    s = Skill(
        name="brand-palette",
        description="apply the house palette",
        body="# Body\nnever pure black\n",
        declared_tools=("generate_image",),
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
        version=3,
        parent_version=2,
        pinned=True,
        partition="colour",
    )
    await storage.skills.put(s)
    got = await storage.skills.get("brand-palette", 3)
    assert got == s


# --------------------------------------------------------------------------------------
# Double capture -> DoubleCaptureError; single capture idempotent under repetition
# --------------------------------------------------------------------------------------


async def test_second_capture_raises_double_capture(storage: SqliteStorage) -> None:
    await storage.runs.capture("res-1", outcome=ToolOutcome.SUCCESS, amount=35)
    with pytest.raises(DoubleCaptureError) as exc:
        await storage.runs.capture("res-1", outcome=ToolOutcome.FAILED, amount=35)
    assert exc.value.reservation_id == "res-1"


async def test_capture_is_idempotent_for_identical_repeat(storage: SqliteStorage) -> None:
    # The resume case: same id, same outcome, called twice -> no error, one charge.
    await storage.runs.capture("res-2", outcome=ToolOutcome.SUCCESS, amount=10)
    await storage.runs.capture("res-2", outcome=ToolOutcome.SUCCESS, amount=10)
    assert storage.runs.is_captured("res-2")
    # Exactly one capture row exists.
    count = storage.connection.execute(
        "SELECT COUNT(*) FROM captures WHERE reservation_id = ?", ("res-2",)
    ).fetchone()[0]
    assert count == 1


async def test_capture_enforced_by_constraint_not_python(storage: SqliteStorage) -> None:
    # There is a UNIQUE/PRIMARY KEY on reservation_id — prove it structurally.
    cols = storage.connection.execute("PRAGMA table_info(captures)").fetchall()
    pk_cols = [c["name"] for c in cols if c["pk"]]
    assert pk_cols == ["reservation_id"]


# --------------------------------------------------------------------------------------
# RunStore basics
# --------------------------------------------------------------------------------------


async def test_run_create_get_checkpoint(storage: SqliteStorage) -> None:
    run = Run(id="r1", agent_name="colourist", phase=RunPhase.RUNNING)
    await storage.runs.create(run)
    assert (await storage.runs.get("r1")) == run

    with pytest.raises(KeyError):
        await storage.runs.create(run)

    updated = run.model_copy(update={"iteration": 5, "charged_credits": 35})
    await storage.runs.checkpoint(updated)
    got = await storage.runs.get("r1")
    assert got is not None
    assert got.iteration == 5
    assert got.charged_credits == 35


# --------------------------------------------------------------------------------------
# Duplicate canvas id rejected by the database
# --------------------------------------------------------------------------------------


async def test_duplicate_canvas_id_rejected(storage: SqliteStorage) -> None:
    a = Artifact(id="art-1", kind=ArtifactKind.IMAGE, payload_ref="r2://a")
    await storage.canvas.append(a)
    with pytest.raises(ValueError, match="append-only"):
        await storage.canvas.append(
            Artifact(id="art-1", kind=ArtifactKind.TEXT, payload_ref="r2://b")
        )


async def test_canvas_id_is_primary_key(storage: SqliteStorage) -> None:
    cols = storage.connection.execute("PRAGMA table_info(artifacts)").fetchall()
    pk_cols = [c["name"] for c in cols if c["pk"]]
    assert pk_cols == ["id"]


async def test_canvas_children(storage: SqliteStorage) -> None:
    root = Artifact(id="root", kind=ArtifactKind.TEXT, payload_ref="r2://root")
    child_a = Artifact(id="c-a", kind=ArtifactKind.TEXT, payload_ref="r2://ca", parent="root")
    child_b = Artifact(id="c-b", kind=ArtifactKind.TEXT, payload_ref="r2://cb", parent="root")
    await storage.canvas.append(root)
    await storage.canvas.append(child_a)
    await storage.canvas.append(child_b)
    kids = await storage.canvas.children("root")
    assert [k.id for k in kids] == ["c-a", "c-b"]


async def test_canvas_append_many_rolls_back_on_duplicate(storage: SqliteStorage) -> None:
    await storage.canvas.append(
        Artifact(id="x", kind=ArtifactKind.TEXT, payload_ref="r2://x")
    )
    with pytest.raises(ValueError):
        await storage.canvas.append_many(
            (
                Artifact(id="y", kind=ArtifactKind.TEXT, payload_ref="r2://y"),
                Artifact(id="x", kind=ArtifactKind.TEXT, payload_ref="r2://dup"),
            )
        )
    # 'y' must NOT have been committed — the batch is all-or-nothing.
    assert (await storage.canvas.get("y")) is None


# --------------------------------------------------------------------------------------
# recall respects the budget cap and honours FTS5 MATCH
# --------------------------------------------------------------------------------------


async def test_recall_respects_budget_cap(storage: SqliteStorage) -> None:
    for i in range(20):
        await storage.memory.remember(
            MemoryRecord(key=f"k{i}", value=f"brand guideline {i}", scope=MemoryScope.LONG)
        )
    got = await storage.memory.recall("brand", limit=5)
    assert len(got) == 5
    # A zero budget returns nothing.
    assert (await storage.memory.recall("brand", limit=0)) == ()


async def test_recall_matches_via_fts(storage: SqliteStorage) -> None:
    await storage.memory.remember(
        MemoryRecord(key="palette", value="never use pure black", scope=MemoryScope.LONG)
    )
    await storage.memory.remember(
        MemoryRecord(key="cropping", value="never crop tighter than sixteen nine")
    )
    hits = await storage.memory.recall("black", limit=10)
    assert [r.key for r in hits] == ["palette"]
    # Scope filter narrows.
    none = await storage.memory.recall("black", scope=MemoryScope.SHORT, limit=10)
    assert none == ()


async def test_recall_query_with_punctuation_is_safe(storage: SqliteStorage) -> None:
    # Punctuation in the query must not be read as FTS operators or raise.
    await storage.memory.remember(
        MemoryRecord(key="k", value="colon: and quote \" and paren )")
    )
    got = await storage.memory.recall('colon: "quote )', limit=10)
    assert len(got) >= 1


async def test_decay_lowers_confidence_never_deletes(storage: SqliteStorage) -> None:
    await storage.memory.remember(
        MemoryRecord(key="k", value="v", confidence=0.5, scope=MemoryScope.LONG)
    )
    affected = await storage.memory.decay(older_than_days=0)
    assert affected == 1
    got = await storage.memory.recall("", scope=MemoryScope.LONG, limit=10)
    assert len(got) == 1  # still present
    assert got[0].confidence == pytest.approx(0.4)


# --------------------------------------------------------------------------------------
# Batch write happens in one transaction
# --------------------------------------------------------------------------------------


async def test_batch_writes(storage: SqliteStorage) -> None:
    skills = tuple(
        Skill(name=f"s{i}", status=SkillStatus.ACTIVE, description=f"d{i}") for i in range(5)
    )
    await storage.skills.put_many(skills)
    assert len(await storage.skills.find("", limit=100)) == 5

    records = tuple(MemoryRecord(key=f"k{i}", value="brand") for i in range(5))
    await storage.memory.remember_many(records)
    assert len(await storage.memory.recall("brand", limit=100)) == 5


# --------------------------------------------------------------------------------------
# Migration: empty DB -> current schema, and idempotent when run twice
# --------------------------------------------------------------------------------------


def test_migration_from_empty(tmp_path: object) -> None:
    conn = sqlite3.connect(":memory:")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    v = migrate(conn)
    assert v == SCHEMA_VERSION
    # All expected tables exist.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"skills", "memory", "runs", "captures", "artifacts"} <= tables
    conn.close()


def test_migration_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    v1 = conn.execute("PRAGMA user_version").fetchone()[0]
    # Running it again changes nothing and does not error.
    v2 = migrate(conn)
    assert v1 == v2 == SCHEMA_VERSION
    conn.close()


def test_connect_sets_pragmas(tmp_path: object) -> None:
    from pathlib import Path

    db = Path(str(tmp_path)) / "s.db"
    conn = connect(str(db))
    # WAL on a file-backed DB.
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


# --------------------------------------------------------------------------------------
# Markdown round-trip property test
# --------------------------------------------------------------------------------------

# Awkward content: colons, newlines, unicode, frontmatter-delimiter-lookalikes.
_awkward_text = st.text(
    alphabet=st.characters(
        min_codepoint=1,
        max_codepoint=0x2FFF,
        blacklist_categories=("Cs",),  # surrogates are not valid in a UTF-8 file
    ),
    max_size=400,
)


def _fixed_dt() -> datetime:
    return datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)


@settings(max_examples=200, deadline=None)
@given(
    key=st.text(min_size=1, max_size=100).filter(lambda s: s.strip() != ""),
    value=_awkward_text,
    confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    evidence=st.integers(min_value=0, max_value=10_000),
)
def test_memory_markdown_round_trip(
    key: str, value: str, confidence: float, evidence: int
) -> None:
    original = MemoryRecord(
        key=key,
        value=value,
        scope=MemoryScope.LONG,
        confidence=confidence,
        evidence_count=evidence,
        lineage=Lineage.clean().with_taint(Taint.TOOL_OUTPUT, "src: tricky\nvalue"),
        created_at=_fixed_dt(),
    )
    restored = memory_from_markdown(memory_to_markdown(original))
    assert restored.key == original.key
    assert restored.value == original.value
    assert restored.scope == original.scope
    assert restored.confidence == pytest.approx(original.confidence)
    assert restored.evidence_count == original.evidence_count
    assert restored.lineage == original.lineage
    assert restored.created_at == original.created_at


@settings(max_examples=100, deadline=None)
@given(
    name=st.text(min_size=1, max_size=100).filter(lambda s: s.strip() != ""),
    description=_awkward_text,
    body=_awkward_text,
)
def test_skill_markdown_round_trip(name: str, description: str, body: str) -> None:
    original = Skill(
        name=name,
        description=description,
        body=body,
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
        declared_tools=("generate_image", "generate_image"),  # order + dupes survive types
        version=7,
        partition="colour: grade",
        created_at=_fixed_dt(),
    )
    restored = skill_from_markdown(skill_to_markdown(original))
    assert restored == original


def test_artifact_markdown_round_trip() -> None:
    original = Artifact(
        id="art: 1\n---",  # id that looks like a delimiter
        kind=ArtifactKind.IMAGE,
        payload_ref="r2://bucket/key",
        parent="root",
        provenance=Provenance(
            prompt="a prompt with: colon and\nnewline",
            model="google/gemini-3.7-flash",
            cost_credits=35,
            seed=42,
            produced_by="colourist",
            at=_fixed_dt(),
        ),
        lineage=Lineage.clean().with_taint(Taint.CANVAS_READ, "canvas:root"),
    )
    restored = artifact_from_markdown(artifact_to_markdown(original))
    assert restored == original


# --------------------------------------------------------------------------------------
# Directory export/import
# --------------------------------------------------------------------------------------


def test_export_import_directory(tmp_path: object) -> None:
    records: tuple[object, ...] = (
        Skill(name="s1", status=SkillStatus.ACTIVE, body="b1", created_at=_fixed_dt()),
        MemoryRecord(key="m1", value="v1", created_at=_fixed_dt()),
        Artifact(
            id="a1",
            kind=ArtifactKind.TEXT,
            payload_ref="ref",
            provenance=Provenance(at=_fixed_dt()),
        ),
    )
    paths = export_records(records, tmp_path)  # type: ignore[arg-type]
    assert len(paths) == 3
    back = import_records(tmp_path)  # type: ignore[arg-type]
    by_type = {type(r).__name__ for r in back}
    assert by_type == {"Skill", "MemoryRecord", "Artifact"}


def test_export_disambiguates_stem_collision(tmp_path: object) -> None:
    # Two records whose natural ids slugify to the same stem must not overwrite.
    r1 = MemoryRecord(key="a/b", value="one", created_at=_fixed_dt())
    r2 = MemoryRecord(key="a:b", value="two", created_at=_fixed_dt())
    paths = export_records((r1, r2), tmp_path)  # type: ignore[arg-type]
    assert len(set(paths)) == 2
    back = import_records(tmp_path)  # type: ignore[arg-type]
    assert {r.value for r in back} == {"one", "two"}  # type: ignore[attr-defined]
