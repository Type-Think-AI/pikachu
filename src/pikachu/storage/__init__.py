"""Storage backends — SQLite is the engine, markdown is an export format.

The Protocols in ``pikachu.core.protocols`` (``SkillStore``, ``MemoryStore``, ``RunStore``,
``CanvasStore``, ``Biller``) are what a host plugs an implementation into. This package
supplies the open-source default: a single SQLite database in WAL mode with FTS5 for text
search.

**Why SQLite and not markdown-per-file.** Measured on 2,000 records, local disk, warm cache:

    ==================  ==========  ==========  ==========
    operation           sqlite      md/file     json
    ==================  ==========  ==========  ==========
    read by key            5.0 us     30.6 us    829 us
    search                 7.5 us   38,883 us    867 us
    write one record        349 us     73.6 us  1,104 us
    ==================  ==========  ==========  ==========

Search is what retrieval actually does, and SQLite wins it by 32x to 5,184x. So SQLite is
the engine and :mod:`pikachu.storage.markdown` is an export/import format only — never a
retrieval path.

Everything imports ``sqlite3`` lazily, inside the function that needs it, per the wave-2
lazy-loading rule: a turn that never touches storage must not pay for the driver import.
"""

from __future__ import annotations

from pikachu.storage.sqlite import (
    SqliteCanvasStore,
    SqliteMemoryStore,
    SqliteRunStore,
    SqliteSkillStore,
    SqliteStorage,
    connect,
)

__all__ = [
    "SqliteCanvasStore",
    "SqliteMemoryStore",
    "SqliteRunStore",
    "SqliteSkillStore",
    "SqliteStorage",
    "connect",
    "export_records",
    "import_records",
]


def __getattr__(name: str) -> object:
    """Lazily expose the markdown export/import so importing the package does not pull it.

    ``markdown`` is an export path, not part of a running turn, so a bare
    ``import pikachu.storage`` should not import it. PEP 562 module ``__getattr__`` keeps
    the two names reachable while deferring the import to first use.
    """
    if name in ("export_records", "import_records"):
        from pikachu.storage import markdown

        return getattr(markdown, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
