"""Phase 2: unresolved references -> edges, against a revision's symbol table.

Phase 1 (`parse.py`) is path-independent and records a reference as it was
written. This module is the opposite half: it never parses Python and never
shells out to git, but it does know every path in one revision. It turns
`blob_refs` into `edges`.

The `Resolver` protocol is the seam. `resolve_revision` builds the symbol
table for a revision and drives the writes; a resolver only answers
"what could this name mean here", so a smarter engine (or another language)
is a swap-in rather than a rewrite.

Over-approximation is the deliberate bias: a candidate is never dropped to
improve precision, only recorded at a lower confidence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from codegraph.config import Config
from codegraph.parse import ParsedRef
from codegraph.store import Store

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"

PROVENANCE = "static"

#: `src` for a reference made at module scope, which owns no node of its own.
MODULE_SCOPE = "<module>"


def module_for_path(path: str, source_roots: tuple[str, ...]) -> str:
    """'src/pay/service.py' -> 'pay.service'; 'pay/__init__.py' -> 'pay'."""
    trimmed = path
    for root in sorted(source_roots, key=len, reverse=True):
        prefix = f"{root}/" if root else ""
        if prefix and trimmed.startswith(prefix):
            trimmed = trimmed[len(prefix) :]
            break
    trimmed = trimmed.removesuffix(".py").removesuffix("/__init__")
    return trimmed.replace("/", ".")


def package_for_module(module: str, path: str) -> str:
    """The package a module's relative imports are resolved against.

    A regular module's package is its parent; `pkg/__init__.py` *is* `pkg`,
    so its own name is the package.
    """
    if path.endswith("__init__.py"):
        return module
    return module.rpartition(".")[0]


def absolute_module(module: str, level: int, package: str) -> str:
    """Expand a `from ... import` target to an absolute module name.

    `level=1` is the importing file's own package, `level=2` its parent, and
    so on; `level=0` is already absolute.
    """
    if level == 0:
        return module
    parts = package.split(".") if package else []
    ascend = level - 1
    base = ".".join(parts[: len(parts) - ascend] if ascend else parts)
    if not module:
        return base
    return f"{base}.{module}" if base else module


@dataclass
class ResolveContext:
    """Everything a resolver may look at for one file in one revision.

    `qualname_index` and `name_index` are shared across every file of the
    revision and hold *live* bindings only: a shadowed definition keeps its
    node and can still be an edge target by another route, but it never wins
    a name lookup.
    """

    rev: str
    path: str
    module: str
    module_to_path: dict[str, str]
    qualname_index: dict[tuple[str, str], str]  # (path, qualname) -> node id, live only
    name_index: dict[str, list[str]]  # bare name -> node ids, live only
    import_map: dict[str, str]  # local alias -> dotted target
    bases: dict[str, list[str]]  # class node id -> base class node ids
    enclosing_class: dict[str, str] = field(default_factory=dict)  # node id -> class node id


class Resolver(Protocol):
    def resolve_call(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        """Return `(node_id, confidence)` candidates for `ref`; [] if unresolved."""
        ...


class AstResolver:
    """Scope-aware heuristics over the revision's symbol table.

    First match wins, in the order the spec fixes: imported name, module-local
    name, `self.X` through the class and its bases, then a repo-wide match on
    the final dotted segment.
    """

    def resolve_call(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        for step in (self._imported, self._module_local, self._through_self, self._by_last_segment):
            hits = step(ref, ctx)
            if hits:
                return hits
        return []

    # -- step 1: an imported name ---------------------------------------
    def _imported(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        head, _, rest = ref.raw_name.partition(".")
        target = ctx.import_map.get(head)
        if target is None:
            return []
        dotted = f"{target}.{rest}" if rest else target
        node_id = self._lookup_dotted(dotted, ctx)
        return [(node_id, HIGH)] if node_id else []

    @staticmethod
    def _lookup_dotted(dotted: str, ctx: ResolveContext) -> str | None:
        """Split `a.b.c` at every module/qualname boundary, longest module first."""
        parts = dotted.split(".")
        for split in range(len(parts) - 1, 0, -1):
            path = ctx.module_to_path.get(".".join(parts[:split]))
            if path is None:
                continue
            node_id = ctx.qualname_index.get((path, ".".join(parts[split:])))
            if node_id:
                return node_id
        return None

    # -- step 2: a name defined in the same module ------------------------
    def _module_local(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        if ref.dotted:
            return []
        node_id = ctx.qualname_index.get((ctx.path, ref.raw_name))
        return [(node_id, HIGH)] if node_id else []

    # -- step 3: self.X through the class and its bases -------------------
    def _through_self(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        head, _, attribute = ref.raw_name.partition(".")
        if head != "self" or not attribute or "." in attribute:
            return []
        owner = ctx.qualname_index.get((ctx.path, ref.from_qualname))
        start = ctx.enclosing_class.get(owner) if owner else None
        if start is None:
            return []
        for class_id in self._mro(start, ctx):
            class_path, _, class_qualname = class_id.partition("::")
            node_id = ctx.qualname_index.get((class_path, f"{class_qualname}.{attribute}"))
            if node_id:
                return [(node_id, HIGH)]
        return []

    @staticmethod
    def _mro(start: str, ctx: ResolveContext) -> list[str]:
        """Breadth-first walk of the class and its known bases, cycle-safe."""
        order, seen, queue = [], {start}, [start]
        while queue:
            current = queue.pop(0)
            order.append(current)
            for base in ctx.bases.get(current, ()):
                if base not in seen:
                    seen.add(base)
                    queue.append(base)
        return order

    # -- steps 4 and 5: a repo-wide match on the last segment -------------
    def _by_last_segment(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        candidates = ctx.name_index.get(ref.raw_name.rpartition(".")[2], ())
        if len(candidates) == 1:
            return [(candidates[0], MEDIUM)]
        return [(node_id, LOW) for node_id in candidates]


@dataclass(frozen=True)
class ResolveStats:
    edges: int = 0
    unresolved: int = 0


class _SymbolTable:
    """The revision's live symbol table, plus the per-file import maps."""

    def __init__(self, store: Store, rev: str, config: Config) -> None:
        connection = store.connection
        self.paths: list[str] = sorted(
            row["path"] for row in connection.execute("SELECT path FROM tree WHERE rev=?", (rev,))
        )
        self.module_for: dict[str, str] = {
            path: module_for_path(path, config.source_roots) for path in self.paths
        }
        self.module_to_path: dict[str, str] = {}
        for path in self.paths:
            # Sorted paths, so a module reachable from two source roots
            # deterministically binds to the first one.
            self.module_to_path.setdefault(self.module_for[path], path)

        self.qualname_index: dict[tuple[str, str], str] = {}
        self.name_index: dict[str, list[str]] = {}
        class_ids: dict[tuple[str, str], str] = {}
        rows = connection.execute(
            "SELECT id, path, qualname, kind FROM nodes WHERE rev=? AND name_binding='live'"
            " ORDER BY id",
            (rev,),
        ).fetchall()
        for row in rows:
            key = (row["path"], row["qualname"])
            self.qualname_index[key] = row["id"]
            self.name_index.setdefault(row["qualname"].rpartition(".")[2], []).append(row["id"])
            if row["kind"] == "class":
                class_ids[key] = row["id"]

        self.enclosing_class: dict[str, str] = {}
        for row in rows:
            found = _nearest_class(row["path"], row["qualname"], class_ids)
            if found:
                self.enclosing_class[row["id"]] = found

        self.import_maps: dict[str, dict[str, str]] = {path: {} for path in self.paths}
        self.imported_modules: dict[str, set[str]] = {path: set() for path in self.paths}
        self._build_imports(connection, rev)

    def _build_imports(self, connection: sqlite3.Connection, rev: str) -> None:
        """One pass over the revision's imports, expanding relative ones."""
        rows = connection.execute(
            "SELECT t.path, i.module, i.level, i.name, i.alias FROM blob_imports i"
            " JOIN tree t ON t.blob_sha = i.blob_sha"
            " WHERE t.rev=? ORDER BY t.path, i.ordinal",
            (rev,),
        )
        for row in rows:
            path = row["path"]
            alias_map = self.import_maps[path]
            modules = self.imported_modules[path]
            package = package_for_module(self.module_for[path], path)
            module = absolute_module(row["module"], row["level"], package)
            if row["name"] is None:
                # `import a.b` / `import a.b as c`: the alias names the module,
                # and a plain import also makes the full dotted path usable.
                alias_map[row["alias"] or module] = module
                if row["alias"] is None:
                    alias_map.setdefault(module.partition(".")[0], module.partition(".")[0])
            else:
                target = f"{module}.{row['name']}" if module else row["name"]
                alias_map[row["alias"] or row["name"]] = target
                # `from a.b import c` may name a module or a symbol; record both.
                modules.add(target)
            if module:
                modules.add(module)

    def context(self, rev: str, path: str, bases: dict[str, list[str]]) -> ResolveContext:
        return ResolveContext(
            rev=rev,
            path=path,
            module=self.module_for[path],
            module_to_path=self.module_to_path,
            qualname_index=self.qualname_index,
            name_index=self.name_index,
            import_map=self.import_maps[path],
            bases=bases,
            enclosing_class=self.enclosing_class,
        )


def _nearest_class(path: str, qualname: str, class_ids: dict[tuple[str, str], str]) -> str | None:
    """The innermost enclosing class of `qualname`, if any."""
    parts = qualname.split(".")
    for split in range(len(parts) - 1, 0, -1):
        found = class_ids.get((path, ".".join(parts[:split])))
        if found:
            return found
    return None


def _refs_by_path(store: Store, rev: str, ref_kind: str) -> dict[str, list[ParsedRef]]:
    """Every reference of one kind in the revision, grouped by owning path."""
    rows = store.connection.execute(
        "SELECT t.path, r.ordinal, r.from_qualname, r.ref_kind, r.raw_name, r.dotted, r.line"
        " FROM blob_refs r JOIN tree t ON t.blob_sha = r.blob_sha"
        " WHERE t.rev=? AND r.ref_kind=? ORDER BY t.path, r.ordinal",
        (rev, ref_kind),
    )
    grouped: dict[str, list[ParsedRef]] = {}
    for row in rows:
        grouped.setdefault(row["path"], []).append(
            ParsedRef(
                ordinal=row["ordinal"],
                from_qualname=row["from_qualname"],
                ref_kind=row["ref_kind"],
                raw_name=row["raw_name"],
                dotted=row["dotted"],
                line=row["line"],
            )
        )
    return grouped


def _source_id(ref: ParsedRef, table: _SymbolTable, path: str) -> str:
    """The node that owns a reference; module scope gets a stable pseudo-id."""
    if ref.from_qualname == MODULE_SCOPE:
        return f"{path}::{MODULE_SCOPE}"
    return table.qualname_index.get((path, ref.from_qualname), f"{path}::{ref.from_qualname}")


def resolve_revision(
    store: Store, rev: str, config: Config, resolver: Resolver | None = None
) -> ResolveStats:
    """Rewrite `edges`, `imports` and `unresolved` for one revision.

    v1 re-resolves the whole revision rather than narrowing to
    `dependents()`; resolution is cheap next to parsing and a whole-revision
    rewrite cannot leave a stale edge behind.
    """
    resolver = resolver or AstResolver()
    connection = store.connection
    table = _SymbolTable(store, rev, config)

    connection.execute("DELETE FROM edges WHERE rev=?", (rev,))
    connection.execute("DELETE FROM imports WHERE rev=?", (rev,))
    connection.execute("DELETE FROM unresolved WHERE rev=?", (rev,))
    connection.executemany(
        "INSERT INTO imports(rev, importer_path, module) VALUES(?, ?, ?)",
        [
            (rev, path, module)
            for path in table.paths
            for module in sorted(table.imported_modules[path])
        ],
    )

    edge_rows: list[tuple] = []
    unresolved_rows: list[tuple] = []

    # Inheritance first: `self.X` walks the class hierarchy, so the hierarchy
    # has to exist before any call is resolved.
    bases: dict[str, list[str]] = {}
    base_refs = _refs_by_path(store, rev, "base")
    for path in table.paths:
        ctx = table.context(rev, path, bases)
        for ref in base_refs.get(path, ()):
            src = _source_id(ref, table, path)
            for node_id, confidence in resolver.resolve_call(ref, ctx):
                edge_rows.append(
                    (rev, src, node_id, "INHERITS", confidence, PROVENANCE, path, ref.line)
                )
                # Only a certain link feeds the MRO walk, which claims HIGH.
                # A weaker one still gets its edge, and a `self.X` that misses
                # the walk falls through to the repo-wide name match anyway.
                if confidence == HIGH:
                    bases.setdefault(src, []).append(node_id)

    call_refs = _refs_by_path(store, rev, "call")
    for path in table.paths:
        ctx = table.context(rev, path, bases)
        for ref in call_refs.get(path, ()):
            src = _source_id(ref, table, path)
            hits = resolver.resolve_call(ref, ctx)
            for node_id, confidence in hits:
                edge_rows.append(
                    (rev, src, node_id, "CALLS", confidence, PROVENANCE, path, ref.line)
                )
            if not hits:
                # Never dropped: the ref stays in `blob_refs` for effect
                # detection, and the gap is counted as a health signal.
                unresolved_rows.append((rev, path, ref.line, ref.raw_name))

    connection.executemany(
        "INSERT INTO edges(rev, src, dst, kind, confidence, provenance, callsite_path,"
        " callsite_line) VALUES(?,?,?,?,?,?,?,?)",
        edge_rows,
    )
    connection.executemany(
        "INSERT INTO unresolved(rev, path, line, raw_name) VALUES(?, ?, ?, ?)",
        unresolved_rows,
    )
    return ResolveStats(edges=len(edge_rows), unresolved=len(unresolved_rows))


def dependents(store: Store, rev: str, modules: set[str]) -> set[str]:
    """Paths whose imports name any of these modules — the re-resolve set."""
    if not modules:
        return set()
    placeholders = ",".join("?" * len(modules))
    rows = store.connection.execute(
        f"SELECT DISTINCT importer_path FROM imports WHERE rev=? AND module IN ({placeholders})",
        (rev, *modules),
    )
    return {row["importer_path"] for row in rows}


def find_symbol(store: Store, rev: str, query: str) -> list[sqlite3.Row]:
    """Fuzzy lookup: exact id, then exact qualname, then suffix match."""
    columns = "id, path, qualname, kind, line_start, line_end, name_binding"
    for clause, parameters in (
        ("id=?", (query,)),
        ("qualname=?", (query,)),
        ("qualname LIKE ? ESCAPE '\\'", (f"%.{_escape_like(query)}",)),
    ):
        rows = store.connection.execute(
            f"SELECT {columns} FROM nodes WHERE rev=? AND {clause} ORDER BY id", (rev, *parameters)
        ).fetchall()
        if rows:
            return rows
    return []


def _escape_like(value: str) -> str:
    for character in ("\\", "%", "_"):
        value = value.replace(character, f"\\{character}")
    return value
