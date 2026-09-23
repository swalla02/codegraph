"""SQLite persistence. Never parses, never shells out to git."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 6

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
    parser_version TEXT NOT NULL,
    module_body_hash TEXT NOT NULL DEFAULT ''
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
-- How each name in each scope was bound, as the text states it: declared with
-- an annotation, assigned from a call, or bound by something no type can be
-- read off ('opaque', `type` NULL). `scope` is spelled like
-- `blob_refs.from_qualname`; an instance attribute is `self.x` under its class.
-- Read by the resolver's receiver step to answer `catalog.fingerprint()` from
-- `catalog: Catalog` (#47). See `parse.ParsedBinding`.
CREATE TABLE IF NOT EXISTS blob_bindings (
    blob_sha TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    type TEXT,
    line INTEGER NOT NULL,
    PRIMARY KEY (blob_sha, ordinal)
);

-- Layer 2: materialized per revision, evictable.
-- `fingerprint` pins everything OUTSIDE the tree that the materialized graph
-- depends on: parser version, source roots, the effect catalog's own digest,
-- and a digest of the source of the modules that decide what gets stored
-- (`indexer.RESOLVER_SOURCES`). A reconcile whose tree is unchanged can only
-- skip its work if these are unchanged too -- editing codegraph.toml changes
-- no file in the tree but can change every edge and every effect, and so does
-- upgrading codegraph itself.
CREATE TABLE IF NOT EXISTS revisions (
    rev TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    materialized_at INTEGER NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT ''
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
    -- Comma-joined decorator names, copied through from `blob_nodes`. A
    -- decorator runs at definition time and can register, wrap or replace
    -- the thing it decorates, which is one of the ways a symbol is invoked
    -- with no call site naming it anywhere (#27); `query/islands.py` reads
    -- this to say so. Layer 2 carries it rather than joining back to Layer
    -- 1 because a node id encodes `shadow_index` and `blob_nodes` does not,
    -- so the join key would have to reconstruct the id format by hand.
    decorators TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (rev, id)
);
-- `provenance` is the other axis from `confidence`, and the two answer
-- different questions (`resolve.STATIC`, `resolve.RUNTIME`). Confidence says
-- how sure the resolver is that this reference means that symbol; provenance
-- says who says so -- the text, or a run that was watched. They are
-- orthogonal, so a pair both deduced and observed holds TWO rows rather than
-- one merged verdict: the static row keeps the call site and the tier the
-- text earns, the runtime row keeps the observation, and a query that wants
-- either can have it. See `trace.project`.
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
-- A reference that produced no edge, and why.
--
-- 'unknown' means no candidate was found at all -- the resolver is blind to
-- something. 'builtin' means a call the resolver understood and deliberately
-- did not link to a repo symbol, kept out of the gap count so the real gaps
-- stay visible. 'external' is the same choice one boundary further out: a call
-- through an import of a module the repository does not contain (`pytest.main`),
-- which no node in this graph can be. 'ambiguous' is the opposite of 'unknown': the resolver saw
-- too much. The last-resort step matches a bare name against every live
-- definition in the revision, and when more than one answers, that fan-out is
-- recorded HERE, once, instead of as N low-confidence edges.
--
-- That is not a truncation. The candidate set is `every live node whose
-- qualname's last segment is this name`, which the `nodes` table already
-- holds, so `ambiguity.py` recomputes it exactly at query time from
-- (`src`, `raw_name`) -- see #25. `candidates` is the count as of indexing,
-- kept for reporting only; nothing reads it to decide anything. `src` is the
-- node the reference was made from, and is the one thing about the reference
-- that is NOT derivable from the name index.
CREATE TABLE IF NOT EXISTS unresolved (
    rev TEXT NOT NULL,
    src TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    raw_name TEXT NOT NULL,
    ref_kind TEXT NOT NULL DEFAULT 'call',
    reason TEXT NOT NULL DEFAULT 'unknown',
    candidates INTEGER NOT NULL DEFAULT 0
);

-- Layer 3: what a run was observed to do, imported rather than derived.
--
-- Not Layer 1 and not Layer 2. Layer 1 is a function of a blob's bytes and
-- Layer 2 is a function of a revision's tree, so either can be thrown away
-- and recomputed; this cannot. It is evidence, and the only way to get it
-- back is to run the program again. `gc` therefore never touches it, and
-- `index --rebuild` does not either.
--
-- Keyed by `rev`, because a trace describes one revision of one repository
-- and nothing else. Making that the primary key is what stops it leaking:
-- there is no query that could read a trace under a revision it was not
-- imported for, because no such row exists. One trace per revision -- a
-- second import replaces the first rather than accumulating, since two runs
-- of the same suite are not two independent bodies of evidence a reader
-- would ever want to tell apart.
--
-- `trace_files` is what makes staleness detectable instead of silent. It
-- records the blob sha of every file the trace named a symbol in, AS OF the
-- import. An observation is about the code that ran; when the file it ran in
-- has a different sha, that code is gone and the observation is not evidence
-- about what is there now. See `trace.project`.
--
-- The counts on `trace_runs` after `projected_at` are derived -- `project`
-- recomputes and rewrites them in the same transaction that writes the edge
-- rows they describe, so they cannot drift from the graph they summarize.
-- They are stored rather than recomputed because every report's summary line
-- prints them, and a query should not rescan the whole trace to say one
-- sentence.
CREATE TABLE IF NOT EXISTS trace_runs (
    rev TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    source TEXT NOT NULL,
    imported_at INTEGER NOT NULL,
    observed INTEGER NOT NULL,
    executed INTEGER NOT NULL,
    projected_at INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    stale INTEGER NOT NULL DEFAULT 0,
    unmodelable INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trace_edges (
    rev TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (rev, src, dst)
);
-- Every in-repo function the run entered, whether or not any in-repo caller
-- was found for it. A framework-dispatched view function has no caller in
-- this tree at all, so it appears in no edge -- and "this ran" is precisely
-- the fact `islands` has never had about such a symbol.
CREATE TABLE IF NOT EXISTS trace_executed (
    rev TEXT NOT NULL,
    node_id TEXT NOT NULL,
    PRIMARY KEY (rev, node_id)
);
CREATE TABLE IF NOT EXISTS trace_files (
    rev TEXT NOT NULL,
    path TEXT NOT NULL,
    blob_sha TEXT NOT NULL,
    PRIMARY KEY (rev, path)
);

CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(rev, dst);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(rev, src);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(rev, module);
CREATE INDEX IF NOT EXISTS idx_nodes_qualname ON nodes(rev, qualname);
CREATE INDEX IF NOT EXISTS idx_effects_node ON effects(rev, node_id);
-- Query-time ambiguity expansion reads every ambiguous row for a revision
-- in one pass; without this it is a full scan of a table that also holds
-- the (much larger) 'unknown' and 'builtin' rows.
CREATE INDEX IF NOT EXISTS idx_unresolved_reason ON unresolved(rev, reason);
-- `unknowns` asks for one symbol's unresolved references, and `query/path.py`
-- asks for one node's ambiguous ones to put a call site on a derived hop.
-- Both filter on `src`, and without this each is a full scan of a table with
-- ~50k rows on django -- a per-symbol question whose cost is the size of the
-- repository rather than the size of the symbol. The index is additive, so an
-- existing database picks it up on the next open with no rebuild.
CREATE INDEX IF NOT EXISTS idx_unresolved_src ON unresolved(rev, src);
"""

_IGNORE_TEXT = "*\n"

#: Every Layer 2 table, each keyed by `rev`. What `seed_revision` and
#: `drop_revision` treat as "one revision's graph". The trace tables are
#: deliberately absent: they are evidence about one revision, not something
#: derived from its tree, so they are never carried to another revision or
#: discarded along with one.
LAYER2_TABLES: tuple[str, ...] = (
    "revisions",
    "tree",
    "nodes",
    "edges",
    "effects",
    "imports",
    "unresolved",
)


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
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def seed_revision(self, src: str, dst: str, *, move: bool) -> None:
        """Make `dst`'s Layer 2 rows a copy of `src`'s -- or, with `move`,
        rekey `src`'s rows to `dst` and leave nothing under `src`.

        Not a materialization. The rows are `src`'s graph filed under
        another name, and the next `Indexer.reconcile(dst)` treats them as
        `dst`'s previous state: it diffs `dst`'s real tree against them and
        rewrites what moved, narrowed exactly as an edit to the working tree
        would be. That is how `history` pays per commit for the files the
        commit touched rather than for a cold build of every revision.
        Whatever `dst` held before is discarded first.
        """
        connection = self.connection
        with connection:
            for table in LAYER2_TABLES:
                connection.execute(f"DELETE FROM {table} WHERE rev=?", (dst,))
                if move:
                    connection.execute(f"UPDATE {table} SET rev=? WHERE rev=?", (dst, src))
                    continue
                columns = [
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                    if row["name"] != "rev"
                ]
                listed = ", ".join(columns)
                connection.execute(
                    f"INSERT INTO {table}(rev, {listed}) SELECT ?, {listed} FROM {table}"
                    " WHERE rev=?",
                    (dst, src),
                )

    def drop_revision(self, rev: str) -> None:
        """Discard `rev`'s Layer 2 rows. Layer 1 is untouched, so the blobs
        it parsed stay cached for whichever revision sees them next."""
        with self.connection:
            for table in LAYER2_TABLES:
                self.connection.execute(f"DELETE FROM {table} WHERE rev=?", (rev,))

    def revisions(self) -> set[str]:
        """Every revision with a materialized graph."""
        return {row["rev"] for row in self.connection.execute("SELECT rev FROM revisions")}

    def close(self) -> None:
        self.connection.close()
