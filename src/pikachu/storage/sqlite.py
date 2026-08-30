"""SQLite backends for the storage Protocols.

One module, one connection factory, WAL mode, FTS5 for text search. Implements
``SkillStore``, ``MemoryStore``, ``RunStore`` and ``CanvasStore`` (and the ``Biller`` seam,
folded into ``SqliteRunStore`` since a capture is durable run accounting) from
``pikachu.core.protocols``.

Structural guarantees this module owes, each enforced by the schema rather than by a Python
check a future refactor could skip:

  * ``SqliteSkillStore.find`` filters to ``status.is_retrievable`` in the SQL itself. There
    is no parameter a caller can set to see a draft.
  * capture is idempotent on ``reservation_id`` via a UNIQUE constraint; a second capture of
    the same id raises :class:`DoubleCaptureError`.
  * ``SqliteCanvasStore.append`` relies on a PRIMARY KEY to refuse a duplicate id — the
    database rejects it, not an ``if`` statement.

``sqlite3`` is imported lazily inside every function, per the wave-2 rule: a turn that never
touches storage does not pay for the driver.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pikachu.core.errors import DoubleCaptureError
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
    utcnow,
)

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "SCHEMA_VERSION",
    "SqliteCanvasStore",
    "SqliteMemoryStore",
    "SqliteRunStore",
    "SqliteSkillStore",
    "SqliteStorage",
    "connect",
    "migrate",
]

SCHEMA_VERSION = 1
"""Current schema version. The migrator reads ``PRAGMA user_version`` and applies each step
above the stored value up to this number. Bump it and add a migration function; never
hand-write an ALTER without a version guard."""


# --------------------------------------------------------------------------------------
# Connection factory + migration
# --------------------------------------------------------------------------------------


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open a connection with the pragmas this backend depends on.

    WAL mode for concurrent readers during a write; foreign keys on; row factory set so
    reads come back as ``sqlite3.Row``. Runs the migrator so the returned connection is at
    ``SCHEMA_VERSION``.

    ``:memory:`` is the test default. A file path persists.
    """
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL is a no-op on :memory: but harmless; skip it there to avoid a needless pragma.
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def _user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Bring ``conn`` up to ``SCHEMA_VERSION`` via ``PRAGMA user_version``.

    Idempotent: running it against an already-current database applies nothing and returns
    the same version. Each migration step is guarded by the stored version, so there is no
    unconditional ALTER chain.
    """
    version = _user_version(conn)
    if version >= SCHEMA_VERSION:
        return version
    # Steps are ordered; each brings the schema from (n-1) to (n).
    if version < 1:
        _migrate_to_v1(conn)
        conn.execute("PRAGMA user_version=1")
    conn.commit()
    return _user_version(conn)


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """Initial schema: skills, memory (+FTS5), runs, captures, artifacts."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS skills (
            name           TEXT    NOT NULL,
            version        INTEGER NOT NULL,
            description    TEXT    NOT NULL DEFAULT '',
            body           TEXT    NOT NULL DEFAULT '',
            declared_tools TEXT    NOT NULL DEFAULT '[]',
            status         TEXT    NOT NULL,
            trust          TEXT    NOT NULL,
            lineage        TEXT    NOT NULL DEFAULT '{}',
            parent_version INTEGER,
            pinned         INTEGER NOT NULL DEFAULT 0,
            partition      TEXT,
            stripped_scripts TEXT  NOT NULL DEFAULT '[]',
            created_at     TEXT    NOT NULL,
            PRIMARY KEY (name, version)
        );
        CREATE INDEX IF NOT EXISTS idx_skills_find
            ON skills (status, partition);

        CREATE TABLE IF NOT EXISTS memory (
            rowid_alias    INTEGER PRIMARY KEY AUTOINCREMENT,
            key            TEXT    NOT NULL,
            value          TEXT    NOT NULL,
            scope          TEXT    NOT NULL,
            confidence     REAL    NOT NULL,
            evidence_count INTEGER NOT NULL,
            lineage        TEXT    NOT NULL DEFAULT '{}',
            created_at     TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory (scope);

        -- FTS5 mirror of memory key+value for MATCH search. content-less external table
        -- kept in sync by triggers so the FTS index cannot drift from the base rows.
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            key, value, content='memory', content_rowid='rowid_alias'
        );
        CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
            INSERT INTO memory_fts (rowid, key, value)
            VALUES (new.rowid_alias, new.key, new.value);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
            INSERT INTO memory_fts (memory_fts, rowid, key, value)
            VALUES ('delete', old.rowid_alias, old.key, old.value);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
            INSERT INTO memory_fts (memory_fts, rowid, key, value)
            VALUES ('delete', old.rowid_alias, old.key, old.value);
            INSERT INTO memory_fts (rowid, key, value)
            VALUES (new.rowid_alias, new.key, new.value);
        END;

        CREATE TABLE IF NOT EXISTS runs (
            id                    TEXT PRIMARY KEY,
            agent_name            TEXT    NOT NULL,
            phase                 TEXT    NOT NULL,
            iteration             INTEGER NOT NULL,
            max_iterations        INTEGER NOT NULL,
            charged_credits       INTEGER NOT NULL,
            refunded_credits      INTEGER NOT NULL,
            captured_reservations TEXT    NOT NULL DEFAULT '[]',
            started_at            TEXT    NOT NULL,
            ended_at              TEXT
        );

        -- One row per captured reservation. The UNIQUE (in fact PRIMARY KEY) on
        -- reservation_id is the no-double-charge invariant: a second capture violates it
        -- and we translate the integrity error into DoubleCaptureError.
        CREATE TABLE IF NOT EXISTS captures (
            reservation_id TEXT PRIMARY KEY,
            run_id         TEXT,
            outcome        TEXT NOT NULL,
            amount         INTEGER NOT NULL DEFAULT 0,
            captured_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id          TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            payload_ref TEXT NOT NULL,
            parent      TEXT,
            provenance  TEXT NOT NULL DEFAULT '{}',
            lineage     TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_parent ON artifacts (parent);
        """
    )


# --------------------------------------------------------------------------------------
# (de)serialisation helpers for the frozen Pydantic types
# --------------------------------------------------------------------------------------


def _dump_lineage(lineage: Lineage) -> str:
    return json.dumps(
        {"taints": sorted(t.value for t in lineage.taints), "sources": list(lineage.sources)}
    )


def _load_lineage(raw: str) -> Lineage:
    data = json.loads(raw) if raw else {}
    taints = frozenset(Taint(t) for t in data.get("taints", ()))
    sources = tuple(data.get("sources", ()))
    return Lineage(taints=taints, sources=sources)


def _dump_provenance(prov: Provenance) -> str:
    return prov.model_dump_json()


def _load_provenance(raw: str) -> Provenance:
    return Provenance.model_validate_json(raw) if raw else Provenance()


def _row_to_skill(row: sqlite3.Row) -> Skill:
    return Skill(
        name=row["name"],
        description=row["description"],
        body=row["body"],
        declared_tools=tuple(json.loads(row["declared_tools"])),
        status=SkillStatus(row["status"]),
        trust=TrustTier(row["trust"]),
        lineage=_load_lineage(row["lineage"]),
        version=row["version"],
        parent_version=row["parent_version"],
        pinned=bool(row["pinned"]),
        partition=row["partition"],
        stripped_scripts=tuple(json.loads(row["stripped_scripts"])),
        created_at=utcnow() if row["created_at"] is None else _parse_dt(row["created_at"]),
    )


def _parse_dt(raw: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(raw)


def _row_to_memory(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        key=row["key"],
        value=row["value"],
        scope=MemoryScope(row["scope"]),
        confidence=row["confidence"],
        evidence_count=row["evidence_count"],
        lineage=_load_lineage(row["lineage"]),
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        agent_name=row["agent_name"],
        phase=RunPhase(row["phase"]),
        iteration=row["iteration"],
        max_iterations=row["max_iterations"],
        charged_credits=row["charged_credits"],
        refunded_credits=row["refunded_credits"],
        captured_reservations=frozenset(json.loads(row["captured_reservations"])),
        started_at=_parse_dt(row["started_at"]),
        ended_at=_parse_dt(row["ended_at"]) if row["ended_at"] else None,
    )


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        kind=ArtifactKind(row["kind"]),
        payload_ref=row["payload_ref"],
        parent=row["parent"],
        provenance=_load_provenance(row["provenance"]),
        lineage=_load_lineage(row["lineage"]),
    )


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Each whitespace-separated term is wrapped in double quotes (FTS5 phrase syntax) with
    embedded quotes doubled, so punctuation in user text cannot be read as MATCH operators
    or break the query. Terms are OR-ed. An empty query yields ``""`` which the callers
    special-case before ever reaching MATCH.
    """
    terms = [t for t in query.split() if t]
    if not terms:
        return ""
    quoted = ['"' + t.replace('"', '""') + '"' for t in terms]
    return " OR ".join(quoted)


# --------------------------------------------------------------------------------------
# SkillStore
# --------------------------------------------------------------------------------------


class SqliteSkillStore:
    """Skill persistence keyed by ``(name, version)``.

    ``find`` filters to retrievable skills in the SQL. There is deliberately no
    ``include_drafts`` argument: a draft cannot be returned by ``find`` however it is
    called. It comes back only from ``get`` by explicit name+version.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def get(self, name: str, version: int | None = None) -> Skill | None:
        if version is not None:
            row = self._conn.execute(
                "SELECT * FROM skills WHERE name = ? AND version = ?", (name, version)
            ).fetchone()
            return _row_to_skill(row) if row is not None else None
        row = self._conn.execute(
            "SELECT * FROM skills WHERE name = ? ORDER BY version DESC LIMIT 1", (name,)
        ).fetchone()
        return _row_to_skill(row) if row is not None else None

    async def find(
        self, query: str, *, partition: str | None = None, limit: int = 5
    ) -> tuple[Skill, ...]:
        # The status filter is part of the SQL, not a caller-controlled argument. Only the
        # retrievable statuses are ever named here, so a draft is structurally unreachable.
        retrievable = tuple(
            s.value for s in SkillStatus if s.is_retrievable
        )  # ('candidate', 'active')
        placeholders = ",".join("?" for _ in retrievable)
        sql = f"SELECT * FROM skills WHERE status IN ({placeholders})"
        params: list[Any] = list(retrievable)
        if partition is not None:
            sql += " AND partition = ?"
            params.append(partition)
        if query:
            sql += " AND (LOWER(name) LIKE ? OR LOWER(description) LIKE ?)"
            like = f"%{query.lower()}%"
            params.extend((like, like))
        sql += " ORDER BY name ASC, version DESC LIMIT ?"
        params.append(max(limit, 0))
        rows = self._conn.execute(sql, params).fetchall()
        return tuple(_row_to_skill(r) for r in rows)

    async def put(self, skill: Skill) -> Skill:
        self._put_many((skill,))
        return skill

    async def put_many(self, skills: tuple[Skill, ...]) -> tuple[Skill, ...]:
        """Persist many versions in ONE transaction.

        SQLite's only measured loss is per-write fsync; one transaction pays it once for
        the whole batch.
        """
        self._put_many(skills)
        return skills

    def _put_many(self, skills: tuple[Skill, ...]) -> None:
        rows = [
            (
                s.name,
                s.version,
                s.description,
                s.body,
                json.dumps(list(s.declared_tools)),
                s.status.value,
                s.trust.value,
                _dump_lineage(s.lineage),
                s.parent_version,
                int(s.pinned),
                s.partition,
                json.dumps(list(s.stripped_scripts)),
                s.created_at.isoformat(),
            )
            for s in skills
        ]
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO skills (
                    name, version, description, body, declared_tools, status, trust,
                    lineage, parent_version, pinned, partition, stripped_scripts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    async def archive(self, name: str, version: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE skills SET status = ? WHERE name = ? AND version = ?",
                (SkillStatus.ARCHIVED.value, name, version),
            )


# --------------------------------------------------------------------------------------
# MemoryStore
# --------------------------------------------------------------------------------------


class SqliteMemoryStore:
    """Memory records with FTS5-backed recall under a hard budget cap."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def remember(self, record: MemoryRecord) -> None:
        self._remember_many((record,))

    async def remember_many(self, records: tuple[MemoryRecord, ...]) -> None:
        """Insert many records in one transaction — one fsync for the batch."""
        self._remember_many(records)

    def _remember_many(self, records: tuple[MemoryRecord, ...]) -> None:
        rows = [
            (
                r.key,
                r.value,
                r.scope.value,
                r.confidence,
                r.evidence_count,
                _dump_lineage(r.lineage),
                r.created_at.isoformat(),
            )
            for r in records
        ]
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO memory (key, value, scope, confidence, evidence_count,
                                    lineage, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    async def recall(
        self, query: str, *, scope: MemoryScope | None = None, limit: int = 10
    ) -> tuple[MemoryRecord, ...]:
        # The budget cap is non-negotiable: unbounded recall destroys the stable prompt
        # prefix and breaks caching. limit is clamped >= 0 and always applied.
        cap = max(limit, 0)
        if cap == 0:
            return ()
        match = _fts_query(query)
        if not match:
            # No search terms -> rank by confidence then evidence, still capped.
            sql = "SELECT m.* FROM memory m"
            params: list[Any] = []
            if scope is not None:
                sql += " WHERE m.scope = ?"
                params.append(scope.value)
            sql += " ORDER BY m.confidence DESC, m.evidence_count DESC LIMIT ?"
            params.append(cap)
            rows = self._conn.execute(sql, params).fetchall()
            return tuple(_row_to_memory(r) for r in rows)
        sql = (
            "SELECT m.* FROM memory_fts f JOIN memory m ON m.rowid_alias = f.rowid "
            "WHERE memory_fts MATCH ?"
        )
        params = [match]
        if scope is not None:
            sql += " AND m.scope = ?"
            params.append(scope.value)
        # Rank: FTS relevance first, then our confidence/evidence, capped.
        sql += " ORDER BY f.rank, m.confidence DESC, m.evidence_count DESC LIMIT ?"
        params.append(cap)
        rows = self._conn.execute(sql, params).fetchall()
        return tuple(_row_to_memory(r) for r in rows)

    async def decay(self, *, older_than_days: int) -> int:
        # Lowers confidence, never deletes. Records already at 0.0 are left alone so the
        # affected count reflects real change.
        #
        # The age predicate is the point of the parameter and was missing (docs/24-audit.md
        # defect 2): every record was decayed regardless of age, so a memory created one
        # second ago lost rank to decay(older_than_days=99999). That inverts the intent —
        # decay is meant to demote the STALE, not the fresh — and quietly degrades recall.
        #
        # created_at is stored as an ISO-8601 UTC string, and ISO strings compare
        # lexicographically in the same order as the instants they denote, so a plain string
        # `<` is correct here and avoids parsing every row.
        from datetime import timedelta  # local, matching this module's lazy-import style

        cutoff = (utcnow() - timedelta(days=older_than_days)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE memory SET confidence = MAX(0.0, confidence - 0.1) "
                "WHERE confidence > 0.0 AND created_at < ?",
                (cutoff,),
            )
            return cur.rowcount


# --------------------------------------------------------------------------------------
# RunStore + Biller (durable run accounting, so capture lives with the run)
# --------------------------------------------------------------------------------------


class SqliteRunStore:
    """Durable run state, plus the idempotent capture that makes billing safe.

    ``create`` refuses an existing id; ``checkpoint`` overwrites current state. ``capture``
    inserts one row per reservation id under a PRIMARY KEY, so a second capture of the same
    id violates the constraint and is translated into :class:`DoubleCaptureError` rather
    than silently double-charging.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def create(self, run: Run) -> Run:
        import sqlite3

        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO runs (id, agent_name, phase, iteration, max_iterations,
                                      charged_credits, refunded_credits,
                                      captured_reservations, started_at, ended_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._run_row(run),
                )
        except sqlite3.IntegrityError as exc:
            raise KeyError(f"run {run.id!r} already exists") from exc
        return run

    async def get(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row is not None else None

    async def checkpoint(self, run: Run) -> Run:
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO runs (id, agent_name, phase, iteration,
                    max_iterations, charged_credits, refunded_credits,
                    captured_reservations, started_at, ended_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._run_row(run),
            )
        return run

    async def capture(
        self,
        reservation_id: str,
        *,
        outcome: ToolOutcome,
        run_id: str | None = None,
        amount: int = 0,
    ) -> None:
        """Commit a charge exactly once.

        Idempotent by identity: the same ``reservation_id`` with the same recorded outcome
        is a no-op (the resume case). A second capture with a DIFFERENT outcome, or any
        capture after the row exists with a conflicting record, raises
        :class:`DoubleCaptureError`. Enforced by the PRIMARY KEY, not a read-then-write.
        """
        import sqlite3

        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO captures (reservation_id, run_id, outcome, amount,
                                          captured_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (reservation_id, run_id, outcome.value, amount, utcnow().isoformat()),
                )
        except sqlite3.IntegrityError as exc:
            # Row already exists. Idempotent resume iff the recorded outcome matches;
            # otherwise it is a genuine double-capture and must be refused.
            existing = self._conn.execute(
                "SELECT outcome FROM captures WHERE reservation_id = ?", (reservation_id,)
            ).fetchone()
            if existing is not None and existing["outcome"] == outcome.value:
                return
            raise DoubleCaptureError(reservation_id) from exc

    def is_captured(self, reservation_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM captures WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _run_row(run: Run) -> tuple[Any, ...]:
        return (
            run.id,
            run.agent_name,
            run.phase.value,
            run.iteration,
            run.max_iterations,
            run.charged_credits,
            run.refunded_credits,
            json.dumps(sorted(run.captured_reservations)),
            run.started_at.isoformat(),
            run.ended_at.isoformat() if run.ended_at else None,
        )


# --------------------------------------------------------------------------------------
# CanvasStore
# --------------------------------------------------------------------------------------


class SqliteCanvasStore:
    """Append-only artifact graph. Duplicate ids are refused by the PRIMARY KEY."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def append(self, artifact: Artifact) -> Artifact:
        import sqlite3

        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO artifacts (id, kind, payload_ref, parent, provenance,
                                           lineage)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.id,
                        artifact.kind.value,
                        artifact.payload_ref,
                        artifact.parent,
                        _dump_provenance(artifact.provenance),
                        _dump_lineage(artifact.lineage),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"artifact {artifact.id!r} already exists; the canvas is append-only"
            ) from exc
        return artifact

    async def append_many(self, artifacts: tuple[Artifact, ...]) -> tuple[Artifact, ...]:
        """Append many artifacts in one transaction.

        If any id already exists the whole batch rolls back and :class:`ValueError` is
        raised — an append-only store must not half-commit.
        """
        import sqlite3

        try:
            with self._conn:
                self._conn.executemany(
                    """
                    INSERT INTO artifacts (id, kind, payload_ref, parent, provenance,
                                           lineage)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            a.id,
                            a.kind.value,
                            a.payload_ref,
                            a.parent,
                            _dump_provenance(a.provenance),
                            _dump_lineage(a.lineage),
                        )
                        for a in artifacts
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "append_many rejected: a duplicate artifact id in the batch; "
                "the canvas is append-only"
            ) from exc
        return artifacts

    async def get(self, artifact_id: str) -> Artifact | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return _row_to_artifact(row) if row is not None else None

    async def children(self, artifact_id: str) -> tuple[Artifact, ...]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE parent = ? ORDER BY id ASC", (artifact_id,)
        ).fetchall()
        return tuple(_row_to_artifact(r) for r in rows)


# --------------------------------------------------------------------------------------
# Facade
# --------------------------------------------------------------------------------------


class SqliteStorage:
    """One connection, all four stores. Convenience for a host wiring the whole backend.

    Every store shares the single connection, so they see one another's writes without a
    second file handle. Close with :meth:`close`.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = connect(path)
        self.skills = SqliteSkillStore(self._conn)
        self.memory = SqliteMemoryStore(self._conn)
        self.runs = SqliteRunStore(self._conn)
        self.canvas = SqliteCanvasStore(self._conn)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteStorage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
