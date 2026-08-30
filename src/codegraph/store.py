"""SQLite persistence. Never parses, never shells out to git."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

WORKTREE = "WORKTREE"


class _Row(sqlite3.Row):
    """sqlite3.Row subclass with string representation that includes data."""

    def __str__(self) -> str:
        return str(tuple(self))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Layer 1: immutable, content-addressed, shared across every revision.
CREATE TABLE IF NOT EXISTS blobs (
    blob_sha TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    error TEXT,
    parser_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blob_nodes (
    blob_sha TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    qualname TEXT NOT NULL,
    kind TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    body_hash TEXT NOT NULL,
    name_binding TEXT NOT NULL,
    shadow_index INTEGER,
    conditional INTEGER NOT NULL DEFAULT 0,
    decorators TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (blob_sha, ordinal)
);
CREATE TABLE IF NOT EXISTS blob_refs (
    blob_sha TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    from_qualname TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    raw_name TEXT NOT NULL,
    dotted TEXT,
    line INTEGER NOT NULL,
    PRIMARY KEY (blob_sha, ordinal)
);
CREATE TABLE IF NOT EXISTS blob_imports (
    blob_sha TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    module TEXT NOT NULL,
    level INTEGER NOT NULL,
    name TEXT,
    alias TEXT,
    PRIMARY KEY (blob_sha, ordinal)
);

-- Layer 2: materialized per revision, evictable.
CREATE TABLE IF NOT EXISTS revisions (
    rev TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    materialized_at INTEGER NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tree (
    rev TEXT NOT NULL, path TEXT NOT NULL, blob_sha TEXT NOT NULL,
    PRIMARY KEY (rev, path)
);
CREATE TABLE IF NOT EXISTS nodes (
    rev TEXT NOT NULL,
    id TEXT NOT NULL,
    path TEXT NOT NULL,
    qualname TEXT NOT NULL,
    kind TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    body_hash TEXT NOT NULL,
    name_binding TEXT NOT NULL,
    PRIMARY KEY (rev, id)
);
CREATE TABLE IF NOT EXISTS edges (
    rev TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'static',
    callsite_path TEXT NOT NULL,
    callsite_line INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
    rev TEXT NOT NULL,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    direct INTEGER NOT NULL,
    evidence_path TEXT,
    evidence_line INTEGER,
    confidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    rev TEXT NOT NULL, importer_path TEXT NOT NULL, module TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unresolved (
    rev TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL, raw_name TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(rev, dst);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(rev, src);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(rev, module);
CREATE INDEX IF NOT EXISTS idx_nodes_qualname ON nodes(rev, qualname);
CREATE INDEX IF NOT EXISTS idx_effects_node ON effects(rev, node_id);
"""

_IGNORE_TEXT = "*\n"


class Store:
    """Owns the SQLite connection for one repository."""

    def __init__(self, connection: sqlite3.Connection, directory: Path) -> None:
        self.connection = connection
        self.directory = directory

    @classmethod
    def open(cls, root: Path) -> Store:
        directory = root / ".codegraph"
        directory.mkdir(exist_ok=True)
        ignore = directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text(_IGNORE_TEXT)

        db_path = directory / "graph.db"
        connection = sqlite3.connect(db_path)
        connection.row_factory = _Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")

        store = cls(connection, directory)
        store._migrate()
        return store

    def _migrate(self) -> None:
        self.connection.executescript(_SCHEMA)
        self.connection.commit()
        recorded = self.get_meta("schema_version")
        if recorded != str(SCHEMA_VERSION):
            self._drop_all()
            self.connection.executescript(_SCHEMA)
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _drop_all(self) -> None:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        for row in rows:
            self.connection.execute(f"DROP TABLE IF EXISTS {row['name']}")
        self.connection.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
