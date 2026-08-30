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
from pathlib import Path
from typing import Protocol

from codegraph import gitio
from codegraph.config import Config
from codegraph.parse import PARSER_VERSION, parse_blob
from codegraph.store import WORKTREE, Store


@dataclass(frozen=True)
class IndexStats:
    paths_total: int = 0
    paths_dirty: int = 0
    blobs_parsed: int = 0
    blobs_cached: int = 0
    parse_errors: int = 0
    shadowed: int = 0


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

        # One transaction for the whole reconcile: Layer 1 fill-in and the
        # Layer 2 rewrite either both land or neither does.
        with connection:
            parsed, cached, errors = self._ensure_parsed(set(tree.values()))

            connection.execute("DELETE FROM tree WHERE rev=?", (rev,))
            connection.executemany(
                "INSERT INTO tree(rev, path, blob_sha) VALUES(?, ?, ?)",
                [(rev, path, sha) for path, sha in tree.items()],
            )
            connection.execute("DELETE FROM nodes WHERE rev=?", (rev,))
            shadowed = self._materialize_nodes(rev, tree)

            connection.execute(
                "INSERT INTO revisions(rev, kind, materialized_at) VALUES(?, ?, ?) "
                "ON CONFLICT(rev) DO UPDATE SET materialized_at=excluded.materialized_at",
                (rev, "worktree" if rev == WORKTREE else "commit", int(time.time())),
            )

        return IndexStats(
            paths_total=len(tree),
            paths_dirty=len(dirty) + len(removed),
            blobs_parsed=parsed,
            blobs_cached=cached,
            parse_errors=errors,
            shadowed=shadowed,
        )

    def _ensure_parsed(self, shas: set[str]) -> tuple[int, int, int]:
        """Fill Layer 1 for any sha not already parsed at the current parser
        version. Returns (blobs_parsed, blobs_cached, parse_errors)."""
        connection = self.store.connection
        known = {
            row["blob_sha"]
            for row in connection.execute(
                "SELECT blob_sha FROM blobs WHERE parser_version=?", (PARSER_VERSION,)
            )
        }
        missing = sorted(shas - known)
        errors = 0

        for sha, data in self.source.read(missing):
            result = parse_blob(data)
            errors += int(result.error is not None)
            connection.execute(
                "INSERT OR REPLACE INTO blobs(blob_sha, status, error, parser_version) "
                "VALUES(?, ?, ?, ?)",
                (sha, "error" if result.error else "ok", result.error, PARSER_VERSION),
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

        return len(missing), len(shas) - len(missing), errors

    def _materialize_nodes(self, rev: str, tree: dict[str, str]) -> int:
        """Rebuild Layer 2's `nodes` rows for `rev` from Layer 1, and return
        the count of non-conditional shadowed definitions found."""
        connection = self.store.connection
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
                    )
                )
        connection.executemany(
            "INSERT OR REPLACE INTO nodes(rev, id, path, qualname, kind, line_start,"
            " line_end, body_hash, name_binding) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return shadowed
