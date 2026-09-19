"""Reconciles a revision into Layer 2 from the Layer 1 parse cache.

`TreeSource` implementations answer "what does this revision look like"
(`gitio`'s job, or a bare filesystem walk when there is no repo). The
`Indexer` never shells out to git and never parses Python itself; it only
diffs trees, asks Layer 1 to fill in anything it hasn't seen, and
materializes Layer 2 rows for the revision.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from codegraph import gitio
from codegraph.config import Config
from codegraph.effects.catalog import Catalog
from codegraph.effects.detect import detect_direct
from codegraph.effects.propagate import propagate
from codegraph.parse import MODULE_SCOPE, PARSER_VERSION, parse_blob
from codegraph.resolve import resolve_revision
from codegraph.store import WORKTREE, Store

#: The modules whose *source* decides what a materialized revision contains,
#: relative to the package directory.
#:
#: Derived rather than declared, for the reason `Catalog.fingerprint` hashes
#: its rules instead of trusting a version number written beside them: a
#: constant only describes the code while somebody remembers to bump it, and
#: forgetting is precisely how #44 happened -- a pure resolver change left the
#: fingerprint untouched, the unchanged-tree fast path fired, and every user
#: who upgraded kept being served the previous resolver's edges, with no error
#: and no warning.
#:
#: Membership is one question asked per module: does this code decide what
#: gets *stored* for a revision?
#:
#: - `resolve.py` decides every edge. It is the module #44 was about.
#: - `effects/detect.py` and `effects/propagate.py` decide every row in
#:   `effects`, which is materialized in the same transaction as the edges.
#: - `ambiguity.py` is in because `propagate` reads its hub edges: the
#:   bare-name fan-out is not stored, but what it reaches is.
#: - `effects/catalog.py` is in even though `Catalog.fingerprint()` is
#:   already folded in below -- that pins the *rules*, this pins the code
#:   that matches them (precedence, confidence derivation).
#: - `indexer.py` is in because `_materialize_nodes` writes `nodes` and
#:   `_narrowable` decides how much of a revision a reconcile may keep. It
#:   also means an edit to THIS LIST invalidates, which a list that exempted
#:   its own file would not.
#:
#: Deliberately out:
#:
#: - `parse.py`, and Layer 1 generally. The parse cache is keyed separately,
#:   on blob sha and `PARSER_VERSION` (see `_ensure_parsed`), and pinning
#:   parser source here would buy nothing anyway: re-resolving unchanged
#:   `blob_*` rows under a new parser produces the same graph, so the cost
#:   would be a rebuild with no possible change in the answer.
#: - `store.py`. A schema change already discards the whole database
#:   (`SCHEMA_VERSION`), which is strictly stronger than this.
#: - `query/*`, `render.py`, `cli.py`. They read the graph and never write a
#:   row, so a change there cannot make a stored row wrong -- and they are
#:   the modules edited most often. Pinning them would make this digest mean
#:   "any commit to codegraph rebuilds every revision", which is a cost with
#:   nothing on the other side of it.
#: - `config.py` and `gitio.py`. Inputs, not logic: what they produce is
#:   already pinned by value (`source_roots` below, the tree diff above).
RESOLVER_SOURCES: tuple[str, ...] = (
    "ambiguity.py",
    "effects/catalog.py",
    "effects/detect.py",
    "effects/propagate.py",
    "indexer.py",
    "resolve.py",
)

_PACKAGE = Path(__file__).resolve().parent


def digest_sources(package: Path) -> str:
    """Hash `RESOLVER_SOURCES` as they sit under `package`.

    Raw file bytes, deliberately -- not a normalized AST, not the code
    objects. Hashing the bytes costs a rebuild for an edit that changes no
    behavior (a comment, a docstring, a reflow), and this repository's
    comments are long. That is still the cheaper side of the trade: a
    normalization is code that can be wrong, and the way it goes wrong is by
    declaring two different resolvers identical -- a *missed* rebuild, which
    is the bug being fixed here, reintroduced in a form that is harder to
    see. The failure mode of raw bytes is one re-resolve nobody needed: ~12s
    on django (2,932 files, 109k edges), against 34s to index the same tree
    cold, and it never touches the parse cache.

    Note who actually pays that. For an installed copy these bytes change
    only when the package does, which is exactly when the graph has to be
    rebuilt; the spurious rebuilds fall on this repository's own developers,
    who are also the people a stale graph would mislead worst.

    Each file's name goes into the digest with its bytes, so moving code
    between two pinned modules changes the result rather than concatenating
    to the same stream.
    """
    digest = hashlib.blake2b(digest_size=16)
    for name in RESOLVER_SOURCES:
        digest.update(name.encode())
        digest.update(b"\x00")
        digest.update((package / name).read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


@lru_cache(maxsize=1)
def resolver_fingerprint() -> str:
    """The installed resolver's identity, as `_fingerprint` folds it in.

    Cached for the life of the process: these files cannot change under a
    running interpreter in any way the already-imported modules would honor,
    and every query reconciles, so this would otherwise be re-read from disk
    on every one of them.
    """
    return digest_sources(_PACKAGE)


@dataclass(frozen=True)
class IndexStats:
    paths_total: int = 0
    paths_dirty: int = 0
    blobs_parsed: int = 0
    blobs_cached: int = 0
    parse_errors: int = 0
    shadowed: int = 0
    edges: int = 0
    unresolved: int = 0
    ambiguous: int = 0


class TreeSource(Protocol):
    def tree(self, rev: str) -> dict[str, str]:
        """Map repo-relative path -> content-addressed blob sha for `rev`."""
        ...

    def read(self, shas: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        """Yield (sha, content) for each requested sha this source can supply."""
        ...


class GitTreeSource:
    """Reads trees via git; `WORKTREE` overlays uncommitted changes onto HEAD.

    `gitio.hash_object` computes a blob sha without writing it into git's
    object database, so an uncommitted file's content is never fetchable
    via `cat-file` under that sha. This source keeps its own small cache of
    those bytes (keyed by the sha it just computed) so `read` can serve them
    directly instead of asking git for an object it was never given.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._worktree_contents: dict[str, bytes] = {}

    def tree(self, rev: str) -> dict[str, str]:
        if rev != WORKTREE:
            return gitio.ls_tree(self.root, rev)
        tree = gitio.ls_tree(self.root, "HEAD")
        self._worktree_contents.clear()
        for path, code in gitio.status_paths(self.root).items():
            full = self.root / path
            if code == "D" or not full.exists():
                tree.pop(path, None)
            else:
                data = full.read_bytes()
                sha = gitio.hash_object(self.root, data)
                tree[path] = sha
                self._worktree_contents[sha] = data
        return tree

    def read(self, shas: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        remaining = []
        for sha in shas:
            if sha in self._worktree_contents:
                yield sha, self._worktree_contents[sha]
            else:
                remaining.append(sha)
        yield from gitio.cat_file_batch(self.root, remaining)


class FsTreeSource:
    """Fallback for directories that are not git repositories.

    Hashes file content with blake2b to get a stable, content-addressed key
    for Layer 1 -- there is no git blob sha to use instead.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._contents: dict[str, bytes] = {}

    def tree(self, rev: str) -> dict[str, str]:
        tree: dict[str, str] = {}
        self._contents.clear()
        for path in sorted(self.root.rglob("*.py")):
            if ".codegraph" in path.parts or ".git" in path.parts:
                continue
            data = path.read_bytes()
            sha = hashlib.blake2b(data, digest_size=20).hexdigest()
            rel = path.relative_to(self.root).as_posix()
            tree[rel] = sha
            self._contents[sha] = data
        return tree

    def read(self, shas: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        for sha in shas:
            if sha in self._contents:
                yield sha, self._contents[sha]


class Indexer:
    """Reconciles one revision's tree into Layer 2, filling Layer 1 as needed."""

    def __init__(
        self, root: Path, store: Store, source: TreeSource, config: Config | None = None
    ) -> None:
        self.root = root
        self.store = store
        self.source = source
        self.config = config or Config.load(root)

    def reconcile(self, rev: str = WORKTREE) -> IndexStats:
        connection = self.store.connection
        tree = self.source.tree(rev)

        stored = {
            row["path"]: row["blob_sha"]
            for row in connection.execute("SELECT path, blob_sha FROM tree WHERE rev=?", (rev,))
        }
        dirty = {path for path, sha in tree.items() if stored.get(path) != sha}
        removed = set(stored) - set(tree)

        catalog = Catalog.load(self.config)
        fingerprint = self._fingerprint(catalog)
        fingerprint_ok = self._is_current(rev, fingerprint)
        if not dirty and not removed and fingerprint_ok:
            # Nothing in the tree moved and nothing outside it did either, so
            # the materialized graph is already the answer.
            #
            # This is the hot path, not an edge case: every query reconciles
            # the working tree first (the `git status` model -- see the README),
            # so without this a repeated query pays a full rebuild of a
            # revision that did not change. On django that was ~20s per query.
            return self._unchanged_stats(rev, tree)

        # One transaction for the whole reconcile: Layer 1 fill-in and the
        # Layer 2 rewrite either both land or neither does.
        with connection:
            blob_shas = set(tree.values())
            parsed, cached = self._ensure_parsed(blob_shas)
            errors = self._error_count(blob_shas)

            connection.execute("DELETE FROM tree WHERE rev=?", (rev,))
            connection.executemany(
                "INSERT INTO tree(rev, path, blob_sha) VALUES(?, ?, ?)",
                [(rev, path, sha) for path, sha in tree.items()],
            )

            # `narrow` is the set of paths this reconcile may confine itself to,
            # or None to rebuild the whole revision. See `_narrowable`.
            narrow = self._narrowable(tree, stored, dirty, removed, fingerprint_ok)
            if narrow is None:
                connection.execute("DELETE FROM nodes WHERE rev=?", (rev,))
                shadowed = self._materialize_nodes(rev, tree)
            else:
                marks = ",".join("?" * len(narrow))
                connection.execute(
                    f"DELETE FROM nodes WHERE rev=? AND path IN ({marks})",
                    (rev, *sorted(narrow)),
                )
                self._materialize_nodes(rev, {p: tree[p] for p in narrow})
                shadowed = self._shadowed_count(rev)

            connection.execute(
                "INSERT INTO revisions(rev, kind, materialized_at, fingerprint)"
                " VALUES(?, ?, ?, ?) ON CONFLICT(rev) DO UPDATE SET"
                " materialized_at=excluded.materialized_at,"
                " fingerprint=excluded.fingerprint",
                (
                    rev,
                    "worktree" if rev == WORKTREE else "commit",
                    int(time.time()),
                    fingerprint,
                ),
            )

            # Phase 2 runs inside the same transaction: a revision is never
            # visible with materialized nodes but stale edges.
            # Propagation reads exactly three things: the revision's node ids,
            # its CALLS edges, and the (node, kind, confidence) of its direct
            # effects. A narrowed pass can only change those within `narrow`,
            # and `_narrowable` has already guaranteed the node ids are
            # identical -- so snapshotting the other two before and after says
            # whether propagation has any work to do at all. An edit that
            # changes a literal or shifts lines usually changes neither.
            before = None if narrow is None else self._propagation_inputs(rev, narrow)

            resolved = resolve_revision(self.store, rev, self.config, only_paths=narrow)

            # Effect detection and propagation close out the same
            # transaction: a revision is never visible with edges but
            # stale (or missing) effects.
            detect_direct(self.store, rev, catalog, self.config, only_paths=narrow)

            # When it does have work, propagation is still whole-revision: an
            # effect flows along edges, so a change anywhere can reach anywhere
            # and there is no cheap frontier to start from. That remains the
            # non-proportional phase -- see #7.
            if before is None or before != self._propagation_inputs(rev, narrow):
                propagate(self.store, rev)

        return IndexStats(
            paths_total=len(tree),
            paths_dirty=len(dirty) + len(removed),
            blobs_parsed=parsed,
            blobs_cached=cached,
            parse_errors=errors,
            shadowed=shadowed,
            edges=resolved.edges,
            unresolved=resolved.unresolved,
            ambiguous=resolved.ambiguous,
        )

    def _narrowable(
        self,
        tree: dict[str, str],
        stored: dict[str, str],
        dirty: set[str],
        removed: set[str],
        fingerprint_ok: bool,
    ) -> set[str] | None:
        """The paths this reconcile may confine itself to, or None for all.

        Resolution is global by nature: a bare-name call matches against every
        definition in the revision, and `self.X` walks a class hierarchy that
        spans files. So narrowing is only sound when the revision's SYMBOL
        TABLE is provably unchanged -- then nothing outside the edited files
        can resolve differently, and only those files' own references need
        rebuilding.

        The conditions, each of them load-bearing:

        - the revision is already materialized under this fingerprint, so
          there is a correct previous state to keep;
        - no path was added or removed, since either changes `module_to_path`
          and the repo-wide name index;
        - every dirty path declares exactly the same symbols as before -- same
          qualnames, kinds, live/shadowed bindings. A new `def` changes what
          bare-name calls anywhere in the repo can match;
        - every dirty path's base references are unchanged. A changed base
          class moves `self.X` resolution in every subclass, wherever it lives,
          and lets `_load_bases` read the hierarchy back from the existing
          INHERITS edges instead of recomputing it;
        - every dirty path's `self.x` attribute bindings are unchanged. The
          receiver step reads them across the hierarchy, so a changed
          attribute type moves `self.x.m()` in subclasses in other files.

        Anything else falls back to a whole-revision rewrite, which cannot
        leave a stale edge behind. Getting this wrong is worse than being slow,
        so the check is deliberately conservative -- a body-only edit is the
        case worth catching, and it is by far the common one.
        """
        if not fingerprint_ok or removed or set(tree) != set(stored):
            return None
        if not dirty:
            return set()
        for path in dirty:
            before, after = stored[path], tree[path]
            if self._symbol_signature(before) != self._symbol_signature(after):
                return None
            if self._base_signature(before) != self._base_signature(after):
                return None
            if self._attribute_signature(before) != self._attribute_signature(after):
                return None
        return set(dirty)

    def _propagation_inputs(
        self, rev: str, paths: set[str]
    ) -> tuple[frozenset, frozenset, frozenset]:
        """What `propagate` would see differently if these paths changed.

        Deliberately projected: `evidence_line` is excluded because propagation
        never reads it, so a call moving down a line must not be mistaken for a
        change in what that call means.
        """
        if not paths:
            return frozenset(), frozenset(), frozenset()
        connection = self.store.connection
        marks = ",".join("?" * len(paths))
        args = (rev, *sorted(paths))
        edges = frozenset(
            tuple(row)
            for row in connection.execute(
                "SELECT src, dst, confidence FROM edges"
                f" WHERE rev=? AND kind='CALLS' AND callsite_path IN ({marks})",
                args,
            )
        )
        # The ambiguous references are half of propagation's LOW subgraph
        # and are NOT in `edges` (#25). Leaving them out here would let a
        # narrowed reconcile that changed only which names a file calls
        # ambiguously decide propagation had nothing to do, and keep serving
        # effects derived from the previous revision's fan-out.
        ambiguous = frozenset(
            tuple(row)
            for row in connection.execute(
                "SELECT src, raw_name, ref_kind FROM unresolved"
                f" WHERE rev=? AND reason='ambiguous' AND path IN ({marks})",
                args,
            )
        )
        direct = frozenset(
            tuple(row)
            for row in connection.execute(
                "SELECT node_id, kind, confidence FROM effects"
                f" WHERE rev=? AND direct=1 AND evidence_path IN ({marks})",
                args,
            )
        )
        return edges, ambiguous, direct

    def _symbol_signature(self, blob_sha: str) -> frozenset[tuple]:
        """What a blob DECLARES, ignoring what any of it does.

        `body_hash` is deliberately absent: a changed body is exactly the edit
        this is trying to let through.
        """
        return frozenset(
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT qualname, kind, name_binding, shadow_index, conditional"
                " FROM blob_nodes WHERE blob_sha=?",
                (blob_sha,),
            )
        )

    def _base_signature(self, blob_sha: str) -> frozenset[tuple]:
        """The blob's base-class references, ignoring line positions."""
        return frozenset(
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT from_qualname, raw_name, dotted FROM blob_refs"
                " WHERE blob_sha=? AND ref_kind='base'",
                (blob_sha,),
            )
        )

    def _attribute_signature(self, blob_sha: str) -> frozenset[tuple]:
        """What the blob says its classes' `self.x` attributes hold, ignoring
        line positions.

        The receiver step resolves `self.x.m()` through every binding of
        `self.x` across the class hierarchy (#47), which spans files: changing
        `thing: Item` to `thing: Other` in a base class's `__init__` declares no
        new symbol and no new base, yet moves every `self.thing.save()` in every
        subclass. Local bindings are absent on purpose -- they are only ever
        read by references in their own file, which the narrowed pass rebuilds.
        """
        return frozenset(
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT scope, name, kind, type FROM blob_bindings"
                " WHERE blob_sha=? AND name LIKE 'self.%'",
                (blob_sha,),
            )
        )

    def _shadowed_count(self, rev: str) -> int:
        return self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM blob_nodes b JOIN tree t ON t.blob_sha = b.blob_sha"
            " WHERE t.rev=? AND b.shadow_index IS NOT NULL AND b.conditional=0",
            (rev,),
        ).fetchone()["n"]

    def _fingerprint(self, catalog: Catalog) -> str:
        """Digest of everything the graph depends on that is NOT in the tree.

        A reconcile is allowed to skip its work when the tree is unchanged, so
        anything else that can change an edge or an effect has to be pinned
        here or that skip becomes a stale-answer bug. Editing `codegraph.toml`
        touches no file in the revision's tree and can change every effect in
        the graph.

        That includes codegraph's own code, not just its configuration:
        upgrading the tool changes how the same tree resolves, and until #44
        nothing here said so.

        `Catalog.fingerprint` was written for exactly this and had no caller
        until now.
        """
        # `ambiguity_limit` used to be pinned here and no longer is: since
        # #25 it changes nothing about the graph, so making it invalidate a
        # materialized revision would be a rebuild bought with nothing.
        parts = (
            PARSER_VERSION,
            # The resolver is the half of "outside the tree" that upgrading
            # codegraph changes, and it went unpinned until #44: a new
            # resolver was served the previous one's edges forever. See
            # `RESOLVER_SOURCES` for what that digest covers.
            resolver_fingerprint(),
            catalog.fingerprint(),
            ",".join(self.config.source_roots),
        )
        return hashlib.blake2b("\x00".join(parts).encode(), digest_size=16).hexdigest()

    def _is_current(self, rev: str, fingerprint: str) -> bool:
        """Has `rev` been materialized under this exact fingerprint?"""
        row = self.store.connection.execute(
            "SELECT fingerprint FROM revisions WHERE rev=?", (rev,)
        ).fetchone()
        return row is not None and row["fingerprint"] == fingerprint

    def _unchanged_stats(self, rev: str, tree: dict[str, str]) -> IndexStats:
        """Stats for a reconcile that did nothing, read back from the store.

        Counted rather than remembered: the numbers have to describe the graph
        as it stands, not the pass that happened to build it, or `status` would
        report zeroes whenever nothing changed.
        """
        connection = self.store.connection
        shas = set(tree.values())

        def count(sql: str) -> int:
            return connection.execute(sql, (rev,)).fetchone()["n"]

        return IndexStats(
            paths_total=len(tree),
            paths_dirty=0,
            blobs_parsed=0,
            blobs_cached=len(shas),
            parse_errors=self._error_count(shas),
            # Counted from Layer 1 the same way `_materialize_nodes` counts it
            # -- `nodes` keeps neither `shadow_index` nor `conditional`, so
            # counting non-live rows there would quietly include the
            # conditional definitions that `_materialize_nodes` excludes.
            shadowed=count(
                "SELECT COUNT(*) AS n FROM blob_nodes b JOIN tree t"
                " ON t.blob_sha = b.blob_sha"
                " WHERE t.rev=? AND b.shadow_index IS NOT NULL AND b.conditional=0"
            ),
            edges=count("SELECT COUNT(*) AS n FROM edges WHERE rev=?"),
            unresolved=count(
                "SELECT COUNT(*) AS n FROM unresolved WHERE rev=? AND reason='unknown'"
            ),
            ambiguous=count(
                "SELECT COUNT(*) AS n FROM unresolved WHERE rev=? AND reason='ambiguous'"
            ),
        )

    def _ensure_parsed(self, shas: set[str]) -> tuple[int, int]:
        """Fill Layer 1 for any sha not already parsed at the current parser
        version. Returns (blobs_parsed, blobs_cached)."""
        connection = self.store.connection
        known = {
            row["blob_sha"]
            for row in connection.execute(
                "SELECT blob_sha FROM blobs WHERE parser_version=?", (PARSER_VERSION,)
            )
        }
        missing = sorted(shas - known)

        for sha, data in self.source.read(missing):
            result = parse_blob(data)
            connection.execute(
                "INSERT OR REPLACE INTO blobs(blob_sha, status, error, parser_version,"
                " module_body_hash) VALUES(?, ?, ?, ?, ?)",
                (
                    sha,
                    "error" if result.error else "ok",
                    result.error,
                    PARSER_VERSION,
                    result.module_body_hash,
                ),
            )
            connection.executemany(
                "INSERT OR REPLACE INTO blob_nodes(blob_sha, ordinal, qualname, kind,"
                " line_start, line_end, body_hash, name_binding, shadow_index,"
                " conditional, decorators) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        sha,
                        n.ordinal,
                        n.qualname,
                        n.kind,
                        n.line_start,
                        n.line_end,
                        n.body_hash,
                        n.name_binding,
                        n.shadow_index,
                        n.conditional,
                        ",".join(n.decorators),
                    )
                    for n in result.nodes
                ],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO blob_refs(blob_sha, ordinal, from_qualname,"
                " ref_kind, raw_name, dotted, line) VALUES(?,?,?,?,?,?,?)",
                [
                    (sha, r.ordinal, r.from_qualname, r.ref_kind, r.raw_name, r.dotted, r.line)
                    for r in result.refs
                ],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO blob_imports(blob_sha, ordinal, module, level,"
                " name, alias) VALUES(?,?,?,?,?,?)",
                [(sha, i.ordinal, i.module, i.level, i.name, i.alias) for i in result.imports],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO blob_bindings(blob_sha, ordinal, scope, name, kind,"
                " type, line) VALUES(?,?,?,?,?,?,?)",
                [
                    (sha, b.ordinal, b.scope, b.name, b.kind, b.type, b.line)
                    for b in result.bindings
                ],
            )

        return len(missing), len(shas) - len(missing)

    def _error_count(self, shas: set[str]) -> int:
        """Count of `shas` (the current revision's own blobs) whose Layer 1
        parse is recorded as an error, regardless of whether that parse ran
        this pass or an earlier one.

        `_ensure_parsed` only fills in blobs Layer 1 hasn't seen yet, so
        counting errors only over blobs parsed THIS pass (the old approach)
        made `parse_errors` report correctly on the first run and then
        silently vanish on every later run, since a broken file's blob is
        cached (status='error') after run 1 and never re-parsed -- while the
        file stays exactly as broken and excluded from the graph. Re-reading
        the `blobs` table for the revision's current tree, instead of
        trusting this pass's own counter, makes the number reflect reality
        regardless of cache state.
        """
        if not shas:
            return 0
        connection = self.store.connection
        placeholders = ",".join("?" * len(shas))
        row = connection.execute(
            f"SELECT COUNT(*) AS n FROM blobs WHERE status='error' AND blob_sha IN"
            f" ({placeholders})",
            tuple(shas),
        ).fetchone()
        return row["n"]

    def _materialize_nodes(self, rev: str, tree: dict[str, str]) -> int:
        """Rebuild Layer 2's `nodes` rows for `rev` from Layer 1, and return
        the count of non-conditional shadowed definitions found.

        Every path also gets a synthetic module node (`path::<module>`), so
        that a module-scope call's edge `src` — an import-time side effect
        like `app = create_app()` — has a real row in `nodes` rather than a
        dangling id. Its `body_hash` is `parse.py`'s `module_body_hash`: a
        structural hash of the module's top-level statements with nested
        def/class bodies elided, computed once per blob and cached on the
        `blobs` row alongside it -- whitespace-insensitive like every other
        `body_hash`, but still sensitive to a module-scope statement (an
        import, a top-level call) changing. Falls back to the blob sha
        itself when a blob has no cached hash (a parse error leaves it
        empty), so a broken file's module node still changes identity
        when its content does. Its span is a placeholder (`1..1`): the
        file's true last line isn't available from Layer 1's parsed
        tables without re-reading blob content, and nothing downstream
        depends on it yet.
        """
        connection = self.store.connection
        module_hashes: dict[str, str] = {}
        shas = set(tree.values())
        if shas:
            placeholders = ",".join("?" * len(shas))
            for row in connection.execute(
                f"SELECT blob_sha, module_body_hash FROM blobs WHERE blob_sha IN ({placeholders})",
                tuple(shas),
            ):
                module_hashes[row["blob_sha"]] = row["module_body_hash"]

        shadowed = 0
        rows: list[tuple] = []
        for path, sha in tree.items():
            for node in connection.execute(
                "SELECT * FROM blob_nodes WHERE blob_sha=? ORDER BY ordinal", (sha,)
            ):
                suffix = "" if node["shadow_index"] is None else f"#{node['shadow_index']}"
                if node["shadow_index"] is not None and not node["conditional"]:
                    shadowed += 1
                rows.append(
                    (
                        rev,
                        f"{path}::{node['qualname']}{suffix}",
                        path,
                        node["qualname"],
                        node["kind"],
                        node["line_start"],
                        node["line_end"],
                        node["body_hash"],
                        node["name_binding"],
                        node["decorators"],
                    )
                )
            rows.append(
                (
                    rev,
                    f"{path}::{MODULE_SCOPE}",
                    path,
                    MODULE_SCOPE,
                    "module",
                    1,
                    1,
                    module_hashes.get(sha) or sha,
                    "live",
                    "",
                )
            )
        connection.executemany(
            "INSERT OR REPLACE INTO nodes(rev, id, path, qualname, kind, line_start,"
            " line_end, body_hash, name_binding, decorators) VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return shadowed
