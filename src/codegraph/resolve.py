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

import builtins
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from codegraph.config import Config
from codegraph.parse import SUPER, ParsedRef
from codegraph.store import Store

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"

#: Canonical rank for comparing confidence tiers: higher is stronger. This
#: is the one place the tier order is defined -- `effects/propagate.py`,
#: `query/impact.py`, and `query/effects.py` all compare confidence tiers
#: and used to each redefine this table locally, with no guarantee they
#: agreed: two copies used higher-is-stronger, one used the opposite
#: polarity with the tiers spelled out as separate string literals rather
#: than these constants, so renaming a tier would have silently produced a
#: `KeyError` in whichever copy nobody happened to update. Importing this
#: one table (and `stronger`/`weaker` below) is the fix.
CONFIDENCE_RANK: dict[str, int] = {LOW: 0, MEDIUM: 1, HIGH: 2}


def stronger(a: str, b: str) -> str:
    """The more confident of two tiers (ties favor `a`)."""
    return a if CONFIDENCE_RANK[a] >= CONFIDENCE_RANK[b] else b


def weaker(a: str, b: str) -> str:
    """The less confident of two tiers (ties favor `a`)."""
    return a if CONFIDENCE_RANK[a] <= CONFIDENCE_RANK[b] else b


PROVENANCE = "static"

#: Python builtins, as the names they are actually called by.
#:
#: These matter because the last-resort step matches a call's final dotted
#: segment against every definition in the repository, and plenty of builtins
#: share a name with a plausible method: `set`, `list`, `next`, `id`, `type`,
#: `format`, `hash`, `filter`, `map`, `open`, `sum`, `iter`, `compile`, `vars`.
#: On `psf/requests` that turned `badargs = set(kwargs) - set(result)` inside
#: `create_cookie` into an edge to `RequestsCookieJar.set`, which then carried
#: an effect into a witness path presented as clickable evidence. See #17.
BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))


def is_builtin_call(ref: ParsedRef) -> bool:
    """Is this reference a call to a Python builtin rather than a repo symbol?

    Only a BARE name can be: `x.set(...)` is a method call on something, and
    must keep falling through to the name match. A bare name is safe to claim
    here because the steps before this one have already ruled out every way a
    repo symbol could legitimately shadow the builtin -- a module-local
    `def set(...)` is caught at step 2 and an imported one at step 1, both at
    HIGH. So a bare builtin name arriving at the last-resort step is the
    builtin.
    """
    return not ref.dotted and ref.raw_name in BUILTIN_NAMES

#: `src` for a reference made at module scope, which owns no node of its own.
MODULE_SCOPE = "<module>"

#: How many re-export hops `AstResolver._lookup_dotted` will follow.
#:
#: A chain is real -- `a/__init__.py` re-exports from `a/b.py`, which
#: re-exports from `a/c.py` -- but short: flask's whole public API is one hop,
#: and the deepest chain actually walked across the benchmark targets is two.
#: This bound is a cost and sanity guard with room to spare, NOT the
#: termination guarantee: that is the `seen` set in `_lookup_dotted`, since a
#: cycle necessarily reproduces a dotted name already tried. Every other walk
#: in this module (`breadth_first`, and `_mro`/`_descendants` on top of it) is
#: cycle-safe the same way.
REEXPORT_HOPS = 8


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
    #: EVERY path's alias map, not just this file's. Following a package
    #: re-export means reading what ANOTHER file imports: `flask.Flask` is
    #: answered by `src/flask/__init__.py`'s `from .app import Flask`.
    import_maps: dict[str, dict[str, str]]
    bases: dict[str, list[str]]  # class node id -> base class node ids
    enclosing_class: dict[str, str] = field(default_factory=dict)  # node id -> class node id
    subclasses: dict[str, list[str]] = field(default_factory=dict)  # inverse of `bases`
    #: Memo for `_descendants`, shared across every file of the revision. The
    #: hierarchy is fixed once inheritance has been resolved, but the walk runs
    #: per `self.X` reference -- on django that cost 3.5s of a 11.2s resolve.
    descendant_cache: dict[str, list[str]] = field(default_factory=dict)


def breadth_first(start: str, adjacency: dict[str, list[str]]) -> list[str]:
    """Breadth-first walk from `start` over `adjacency`, cycle-safe.

    Module level rather than a method because two different walks need it:
    the resolver's MRO/override walks over `bases`/`subclasses`, and
    `_inherited_constructor` below, which is not resolution of a name at
    all and so does not belong to a `Resolver`.
    """
    order, seen, queue = [], {start}, [start]
    while queue:
        current = queue.pop(0)
        order.append(current)
        for nxt in adjacency.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return order


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
        for step in (
            self._imported,
            self._module_local,
            self._through_super,
            self._through_self,
            self._by_last_segment,
        ):
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

    @classmethod
    def _lookup_dotted(cls, dotted: str, ctx: ResolveContext) -> str | None:
        """A dotted name -> the definition it names, following re-exports.

        A module attribute can be a name the module merely *imported*, and for
        a package `__init__.py` that is the normal case: `flask.Flask` is not
        defined in `src/flask/__init__.py`, it is bound there by
        `from .app import Flask`. Looking only for a definition (step one
        below) answered 218 of flask's 1039 ambiguous references with nothing
        -- the package's entire public API, the most-written form of every
        import a library user makes. See #38.

        So each round does two things, in this order:

        1. is the remaining qualname DEFINED in the module the prefix names?
        2. if not, is it IMPORTED there? Rewrite the dotted name through that
           import and go round again.

        Definition first is what keeps a re-export from shadowing a real local
        definition: an `__init__.py` that both defines `Foo` and imports a
        different `Foo` resolves to the one a reader looking it up in that file
        would find, which is the same rule `constructor_target` follows for a
        method. (Python itself would give the later binding, but `nodes` holds
        definitions, not assignment order, and preferring the definition is the
        conservative half of that disagreement -- it never invents a target in
        another file.)

        The claim stays HIGH, because it is the same evidence step one already
        claims HIGH for: `from .app import Flask` is an exact recorded fact
        about the source text, not an inference over it. Nothing is guessed --
        no MRO approximation, no instance type, no repo-wide name search -- and
        a chain of hops is a conjunction of such facts, so depth does not
        weaken it: three exact facts are not less certain than one. What could
        still be wrong is that the module rebinds the name at runtime, and that
        is exactly as true of the single-hop imported-name step which has been
        HIGH since the start; this adds no new kind of doubt, it reads the same
        kind of statement in a different file. The hop bound therefore never
        produces a weaker answer: exhausting it produces NO answer, and the
        reference falls through to the weak bare-name path it takes today.
        """
        seen = {dotted}
        for _ in range(REEXPORT_HOPS + 1):
            node_id = cls._lookup_defined(dotted, ctx)
            if node_id:
                return node_id
            followed = cls._follow_reexport(dotted, ctx)
            # `followed in seen` is the cycle guard: a re-export cycle
            # (`a/__init__.py` imports X from `a.b`, `a/b.py` imports X from
            # `a`) necessarily reproduces a dotted name already tried, so this
            # terminates it before the hop bound does.
            if followed is None or followed in seen:
                return None
            seen.add(followed)
            dotted = followed
        return None

    @staticmethod
    def _lookup_defined(dotted: str, ctx: ResolveContext) -> str | None:
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

    @staticmethod
    def _follow_reexport(dotted: str, ctx: ResolveContext) -> str | None:
        """`flask.Flask.run` -> `flask.app.Flask.run`, via one import statement.

        Same longest-module-prefix split as `_lookup_defined`, but the first
        segment after the prefix is looked up in that module's *import* map
        instead of its definitions, and any further segments ride along
        untouched (`Flask.run` is `run` on whatever `Flask` turns out to be).

        `from .x import *` is deliberately NOT followed. `build_import_maps`
        records it under the literal name `*`, which no attribute access can
        ever spell, so a star import simply contributes nothing here. Expanding
        it would mean deciding which of the starred module's names are public,
        and that is governed by `__all__` -- which this codebase does not
        record and which real packages build at runtime
        (`__all__ = [...] + other.__all__`). Guessing "every name not starting
        with an underscore" would claim HIGH for names that may not be exported
        at all, so a name reachable only through a star import keeps falling
        through to the weak path, exactly as it does today.

        `__all__` is otherwise irrelevant to this lookup, which is worth saying
        because it looks like it should matter: `__all__` gates `from pkg
        import *` and nothing else. `flask.Flask` reads a module attribute, and
        the attribute is there because the import bound it, whether or not
        `__all__` mentions it.
        """
        parts = dotted.split(".")
        for split in range(len(parts) - 1, 0, -1):
            path = ctx.module_to_path.get(".".join(parts[:split]))
            if path is None:
                continue
            name, *tail = parts[split:]
            target = ctx.import_maps.get(path, {}).get(name)
            if target is None:
                continue
            return ".".join([target, *tail])
        return None

    # -- step 2: a name defined in the same module ------------------------
    def _module_local(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        if ref.dotted:
            return []
        node_id = ctx.qualname_index.get((ctx.path, ref.raw_name))
        return [(node_id, HIGH)] if node_id else []

    # -- step 2b: super().X through the enclosing class's bases -------------
    def _through_super(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        """`super().X()` -- the enclosing class's inherited `X`, at HIGH.

        This is one of the most certain calls Python has: the starting class is
        the one the call is written in, and the lookup skips it. It was LOW
        because `parse.py` used to flatten `super()` to the unknown-receiver
        marker, so it fell to the repo-wide name match -- 26 candidates per site
        on psf/requests, all LOW, exactly one right.

        Unlike `_through_self`, subclass overrides are NOT candidates.
        `super()` walks strictly upwards; a subclass override is what it exists
        to bypass.

        The base walk is the same breadth-first approximation of the MRO used
        everywhere else in this module, and starts at the bases rather than the
        class itself -- `super().__init__()` inside `Child.__init__` must not
        resolve to `Child.__init__`.
        """
        head, _, attribute = ref.raw_name.partition(".")
        if head != SUPER or not attribute or "." in attribute:
            return []
        owner = ctx.qualname_index.get((ctx.path, ref.from_qualname))
        start = ctx.enclosing_class.get(owner) if owner else None
        if start is None:
            return []
        for class_id in breadth_first(start, ctx.bases)[1:]:
            found = self._method_on(class_id, attribute, ctx)
            if found:
                return [(found, HIGH)]
        return []

    # -- step 3: self.X through the class, its bases, and its overrides ----
    def _through_self(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        """`self.X` resolves to the method the enclosing class inherits, PLUS
        every override of it in a subclass.

        Walking only up the MRO and stopping at the first hit is wrong, and
        wrong in the direction that hurts most. `self` is an instance of the
        enclosing class *or of any subclass of it*, so an override is a real
        runtime candidate, and dropping it is the over-approximation bias
        pointing backwards.

        The damage is worst when the base declaration is an abstract stub. In
        `requests`, `SessionRedirectMixin.send` has a `...` body and
        `Session(SessionRedirectMixin)` supplies the real one; first-match-wins
        bound `self.send()` inside `resolve_redirects` to the stub at HIGH
        confidence, so `impact Session.send` -- the single most important edge
        in the library, the one that drives every redirect hop -- reported
        nothing. See issue #14.

        The inherited match keeps HIGH: it is the declaration this class
        actually resolves to by name. Overrides are MEDIUM, because which one
        runs depends on the instance, and that is genuinely less certain than
        a name lookup -- not LOW, which is the tier for a repo-wide guess with
        no hierarchy behind it.
        """
        head, _, attribute = ref.raw_name.partition(".")
        if head != "self" or not attribute or "." in attribute:
            return []
        owner = ctx.qualname_index.get((ctx.path, ref.from_qualname))
        start = ctx.enclosing_class.get(owner) if owner else None
        if start is None:
            return []

        hits: list[tuple[str, str]] = []
        for class_id in self._mro(start, ctx):
            node_id = self._method_on(class_id, attribute, ctx)
            if node_id:
                hits.append((node_id, HIGH))
                break
        if not hits:
            return []

        seen = {hits[0][0]}
        for class_id in self._descendants(start, ctx):
            node_id = self._method_on(class_id, attribute, ctx)
            if node_id and node_id not in seen:
                seen.add(node_id)
                hits.append((node_id, MEDIUM))
        return hits

    @staticmethod
    def _method_on(class_id: str, attribute: str, ctx: ResolveContext) -> str | None:
        class_path, _, class_qualname = class_id.partition("::")
        return ctx.qualname_index.get((class_path, f"{class_qualname}.{attribute}"))

    @staticmethod
    def _mro(start: str, ctx: ResolveContext) -> list[str]:
        """The class and its known bases, nearest first."""
        return breadth_first(start, ctx.bases)

    @staticmethod
    def _descendants(start: str, ctx: ResolveContext) -> list[str]:
        """Every known subclass of `start`, transitively (excluding `start`)."""
        cached = ctx.descendant_cache.get(start)
        if cached is None:
            cached = breadth_first(start, ctx.subclasses)[1:]
            ctx.descendant_cache[start] = cached
        return cached

    # -- steps 4 and 5: a repo-wide match on the last segment -------------
    def _by_last_segment(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        if is_builtin_call(ref):
            return []
        candidates = ctx.name_index.get(ref.raw_name.rpartition(".")[2], ())
        if len(candidates) == 1:
            return [(candidates[0], MEDIUM)]
        return [(node_id, LOW) for node_id in candidates]


@dataclass(frozen=True)
class ResolveStats:
    edges: int = 0
    unresolved: int = 0
    ambiguous: int = 0


def build_import_maps(
    connection: sqlite3.Connection, rev: str, config: Config
) -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    """Per-path local-alias -> absolute-dotted-module map, and the set of
    modules each path imports (feeds `dependents()`).

    The one place a raw `import`/`from ... import` row -- with its
    relative-import `level` -- gets expanded into an absolute dotted module
    name. Both the resolver (`_SymbolTable`, below) and effect detection's
    catalog expansion (`effects/detect.py`) build on this rather than each
    repeating the expansion, so the two cannot silently drift apart.
    """
    paths = [
        row["path"]
        for row in connection.execute("SELECT DISTINCT path FROM tree WHERE rev=?", (rev,))
    ]
    module_for = {path: module_for_path(path, config.source_roots) for path in paths}
    alias_maps: dict[str, dict[str, str]] = {path: {} for path in paths}
    imported_modules: dict[str, set[str]] = {path: set() for path in paths}

    rows = connection.execute(
        "SELECT t.path, i.module, i.level, i.name, i.alias FROM blob_imports i"
        " JOIN tree t ON t.blob_sha = i.blob_sha WHERE t.rev=? ORDER BY t.path, i.ordinal",
        (rev,),
    )
    for row in rows:
        path = row["path"]
        alias_map = alias_maps[path]
        modules = imported_modules[path]
        package = package_for_module(module_for[path], path)
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
    return alias_maps, imported_modules


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
        # Every node sharing a (path, qualname) — live and shadowed alike —
        # keyed by their line span, so a ref originating inside a shadowed
        # definition can be attributed to it rather than to the live one.
        self.owner_index: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
        class_ids: dict[tuple[str, str], str] = {}
        all_rows = connection.execute(
            "SELECT id, path, qualname, kind, line_start, line_end, name_binding FROM nodes"
            " WHERE rev=? ORDER BY id",
            (rev,),
        ).fetchall()
        for row in all_rows:
            key = (row["path"], row["qualname"])
            self.owner_index.setdefault(key, []).append(
                (row["id"], row["line_start"], row["line_end"])
            )

        live_rows = [row for row in all_rows if row["name_binding"] == "live"]
        for row in live_rows:
            key = (row["path"], row["qualname"])
            self.qualname_index[key] = row["id"]
            self.name_index.setdefault(row["qualname"].rpartition(".")[2], []).append(row["id"])
            if row["kind"] == "class":
                class_ids[key] = row["id"]

        #: Live class nodes, by id -- the test `_constructor_target` applies
        #: to a resolved call before treating it as an instantiation.
        self.class_node_ids: frozenset[str] = frozenset(class_ids.values())

        self.enclosing_class: dict[str, str] = {}
        for row in live_rows:
            found = _nearest_class(row["path"], row["qualname"], class_ids)
            if found:
                self.enclosing_class[row["id"]] = found

        self.import_maps, self.imported_modules = build_import_maps(connection, rev, config)

    def context(
        self,
        rev: str,
        path: str,
        bases: dict[str, list[str]],
        subclasses: dict[str, list[str]] | None = None,
        descendant_cache: dict[str, list[str]] | None = None,
    ) -> ResolveContext:
        return ResolveContext(
            rev=rev,
            path=path,
            module=self.module_for[path],
            module_to_path=self.module_to_path,
            qualname_index=self.qualname_index,
            name_index=self.name_index,
            import_map=self.import_maps[path],
            import_maps=self.import_maps,
            bases=bases,
            enclosing_class=self.enclosing_class,
            subclasses=subclasses if subclasses is not None else {},
            descendant_cache=descendant_cache if descendant_cache is not None else {},
        )


def _nearest_class(path: str, qualname: str, class_ids: dict[tuple[str, str], str]) -> str | None:
    """The innermost enclosing class of `qualname`, if any."""
    parts = qualname.split(".")
    for split in range(len(parts) - 1, 0, -1):
        found = class_ids.get((path, ".".join(parts[:split])))
        if found:
            return found
    return None


def _refs_by_path(
    store: Store, rev: str, ref_kind: str, only_paths: list[str] | None = None
) -> dict[str, list[ParsedRef]]:
    """References of one kind, grouped by owning path.

    `only_paths` restricts the scan itself, not just the loop over the result:
    a narrowed resolve that still read every reference in the repository would
    be proportional to repo size in the one place the narrowing exists to fix.
    """
    sql = (
        "SELECT t.path, r.ordinal, r.from_qualname, r.ref_kind, r.raw_name, r.dotted, r.line"
        " FROM blob_refs r JOIN tree t ON t.blob_sha = r.blob_sha"
        " WHERE t.rev=? AND r.ref_kind=?"
    )
    args: tuple = (rev, ref_kind)
    if only_paths is not None:
        sql += f" AND t.path IN ({','.join('?' * len(only_paths))})"
        args += tuple(only_paths)
    rows = store.connection.execute(sql + " ORDER BY t.path, r.ordinal", args)
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
    """The node that owns a reference; module scope gets a stable pseudo-id.

    A qualname can own more than one node when an earlier definition is
    shadowed by a later one of the same name — both still execute, so a
    call made from inside the shadowed body must be attributed to it, not
    to the live definition that happens to share its name. The ref's `line`
    (recorded verbatim as `callsite_line` on the edge) picks out which node's
    span it actually falls inside; a qualname with a single owner keeps the
    direct-lookup fast path.
    """
    if ref.from_qualname == MODULE_SCOPE:
        return f"{path}::{MODULE_SCOPE}"
    candidates = table.owner_index.get((path, ref.from_qualname))
    if not candidates:
        return f"{path}::{ref.from_qualname}"
    if len(candidates) == 1:
        return candidates[0][0]
    for node_id, line_start, line_end in candidates:
        if line_start <= ref.line <= line_end:
            return node_id
    # Should not happen (every ref sits inside the definition it came from);
    # fall back to the live binding rather than dropping the edge.
    return table.qualname_index.get((path, ref.from_qualname), candidates[-1][0])


def is_derivable_fanout(hits: list[tuple[str, str]]) -> bool:
    """Is this candidate set the bare-name fan-out, recomputable from the
    name index alone?

    True exactly when every candidate is LOW, which happens exactly when the
    last-resort step (`_by_last_segment`) matched a bare name against more
    than one live definition. Every other step returns HIGH or MEDIUM: an
    imported name, a module-local name, a `self.X` hit and its overrides, and
    a last-segment match with a single answer all name something the resolver
    actually distinguished, and all get an edge.

    A LOW set does not. It is `name_index[name]` verbatim -- a set the `nodes`
    table already determines -- so materializing it stores nothing the graph
    did not already contain, at a cost quadratic in repository size (2.09M of
    django's 2.16M edges, 96.6%, before #6). It is recorded once in
    `unresolved` with `reason='ambiguous'` instead, and `ambiguity.py`
    reconstructs it on demand. Nothing is dropped and nothing is capped: the
    bound on how many of them a *reader* sees is `--limit`, a property of the
    question, not of the graph. See #25.
    """
    return bool(hits) and all(confidence == LOW for _, confidence in hits)


#: The method a call to a class actually runs. `Cls()` resolves to the class
#: node, and nothing in the source ever writes `Cls.__init__`, so without the
#: edge below a constructor has no incoming call at all -- on psf/requests that
#: left `src/requests/adapters.py::BaseAdapter.__init__` an island of exactly
#: one. #27 records this as a plain bug rather than a limit of static analysis:
#: the caller IS in the source, it just spells the callee's name as the class's.
CONSTRUCTOR = "__init__"


def constructor_target(
    class_id: str, table: _SymbolTable, bases: dict[str, list[str]]
) -> str | None:
    """The `__init__` that `class_id()` runs, or None if it neither defines
    nor inherits one.

    The walk is the same breadth-first approximation of the MRO that
    `AstResolver._through_self` already resolves an inherited method with,
    and is deliberately the same: the class a name is declared on is what a
    reader looking the call up would find. It is not a C3 linearization, so
    under multiple inheritance the branch reached first can differ from the
    one Python picks -- but every candidate it can return is a real
    `__init__` on a real base, and the alternative is the missing edge this
    exists to fix.

    Subclass overrides are deliberately NOT candidates, which is the one
    place this differs from `_through_self`. `self.x()` may run a subclass's
    override because `self` may be an instance of a subclass; `Cls()` names
    the exact class being instantiated, so its `__init__` is looked up on
    `Cls` and its bases and nowhere else.
    """
    for owner in breadth_first(class_id, bases):
        path, _, qualname = owner.partition("::")
        found = table.qualname_index.get((path, f"{qualname}.{CONSTRUCTOR}"))
        if found:
            return found
    return None


def with_constructors(
    hits: list[tuple[str, str]],
    table: _SymbolTable,
    bases: dict[str, list[str]],
    cache: dict[str, str | None],
) -> list[tuple[str, str]]:
    """`hits`, plus the `__init__` each class among them would run.

    Applied only to hits that are actually materialized, so a reference whose
    LOW fan-out was deferred to query time (see `is_derivable_fanout`) cannot
    be re-expanded through the back door -- the constructor edges follow
    exactly the class edges the graph really holds. In practice an all-LOW
    fan-out has no class to construct anyway: `Cls()` resolves through an
    import or a module-local name, never through the bare-name fallback.

    A constructor edge carries the confidence of the class edge implying it.
    It makes the same claim ("this call site may instantiate this class")
    and `Cls()` running `Cls.__init__` adds no uncertainty of its own, so
    weakening it would understate an edge that is certain given the class.
    """
    extra: list[tuple[str, str]] = []
    seen = {node_id for node_id, _ in hits}
    for node_id, confidence in hits:
        if node_id not in table.class_node_ids:
            continue
        if node_id not in cache:
            cache[node_id] = constructor_target(node_id, table, bases)
        target = cache[node_id]
        if target is not None and target not in seen:
            seen.add(target)
            extra.append((target, confidence))
    return hits + extra if extra else hits


def _load_bases(connection: sqlite3.Connection, rev: str) -> dict[str, list[str]]:
    """Rebuild the class hierarchy from already-materialized INHERITS edges.

    Only HIGH links feed the MRO walk, which is exactly the filter the
    inheritance pass applies when it builds this map from scratch, so reading it
    back is equivalent -- provided the base references it was built from have
    not changed. That is a precondition the caller checks before narrowing; see
    `Indexer._narrowable`.
    """
    bases: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT src, dst FROM edges WHERE rev=? AND kind='INHERITS' AND confidence='HIGH'",
        (rev,),
    ):
        bases.setdefault(row["src"], []).append(row["dst"])
    return bases


def resolve_revision(
    store: Store,
    rev: str,
    config: Config,
    resolver: Resolver | None = None,
    only_paths: set[str] | None = None,
) -> ResolveStats:
    """Rewrite `edges`, `imports` and `unresolved` for one revision.

    `only_paths` narrows the rewrite to those paths, leaving every other path's
    rows in place. That is sound only when the revision's symbol table is
    unchanged outside them -- a definition appearing or disappearing anywhere
    changes what bare-name calls in unrelated files can match, and a changed
    base class changes `self.X` resolution in every subclass, wherever it
    lives. The caller owns that check (`Indexer._narrowable`); this function
    trusts it. Passing `None` rewrites the whole revision, which cannot leave a
    stale edge behind under any circumstances.

    The bare-name fan-out is never written to `edges`: a reference whose only
    candidates are LOW is recorded once in `unresolved` as ambiguous, carrying
    the source node, the name, and the count, and is expanded at query time
    instead. See `is_derivable_fanout` and `ambiguity.py`.
    """
    resolver = resolver or AstResolver()
    connection = store.connection
    table = _SymbolTable(store, rev, config)

    if only_paths is None:
        target_paths = table.paths
        bases: dict[str, list[str]] = {}
        connection.execute("DELETE FROM edges WHERE rev=?", (rev,))
        connection.execute("DELETE FROM imports WHERE rev=?", (rev,))
        connection.execute("DELETE FROM unresolved WHERE rev=?", (rev,))
    else:
        target_paths = [path for path in table.paths if path in only_paths]
        # Read the hierarchy back BEFORE deleting the edges it is derived from.
        bases = _load_bases(connection, rev)
        marks = ",".join("?" * len(target_paths))
        args = (rev, *target_paths)
        connection.execute(
            f"DELETE FROM edges WHERE rev=? AND callsite_path IN ({marks})", args
        )
        connection.execute(
            f"DELETE FROM imports WHERE rev=? AND importer_path IN ({marks})", args
        )
        connection.execute(f"DELETE FROM unresolved WHERE rev=? AND path IN ({marks})", args)
    scan_paths = None if only_paths is None else target_paths

    connection.executemany(
        "INSERT INTO imports(rev, importer_path, module) VALUES(?, ?, ?)",
        [
            (rev, path, module)
            for path in target_paths
            for module in sorted(table.imported_modules[path])
        ],
    )

    edge_rows: list[tuple] = []
    unresolved_rows: list[tuple] = []
    ambiguous_rows: list[tuple] = []
    builtin_rows: list[tuple] = []

    # Inheritance first: `self.X` walks the class hierarchy, so the hierarchy
    # has to exist before any call is resolved.
    base_refs = _refs_by_path(store, rev, "base", scan_paths)
    for path in target_paths:
        ctx = table.context(rev, path, bases)
        for ref in base_refs.get(path, ()):
            src = _source_id(ref, table, path)
            # A base named by a bare, repo-wide-ambiguous name fans out exactly
            # like a call does, and on a large repo it is the larger half of the
            # blowup. Deferring it cannot affect the MRO: only HIGH links feed
            # `bases`, and only an all-LOW set is deferred.
            hits = resolver.resolve_call(ref, ctx)
            if is_derivable_fanout(hits):
                ambiguous_rows.append(
                    (rev, src, path, ref.line, ref.raw_name, "base", "ambiguous", len(hits))
                )
                continue
            for node_id, confidence in hits:
                edge_rows.append(
                    (rev, src, node_id, "INHERITS", confidence, PROVENANCE, path, ref.line)
                )
                # Only a certain link feeds the MRO walk, which claims HIGH.
                # A weaker one still gets its edge, and a `self.X` that misses
                # the walk falls through to the repo-wide name match anyway.
                if confidence == HIGH and only_paths is None:
                    bases.setdefault(src, []).append(node_id)

    # The hierarchy is complete now, so it can be inverted once: `self.X`
    # needs to see downwards (overrides in subclasses) as well as upwards.
    subclasses: dict[str, list[str]] = {}
    for subclass, base_ids in bases.items():
        for base in base_ids:
            subclasses.setdefault(base, []).append(subclass)

    # One cache object shared by every file's context, so the descendant walk
    # runs once per class for the whole revision rather than once per reference.
    descendant_cache: dict[str, list[str]] = {}

    # `Cls()` -> `Cls.__init__` is the same lookup for every call site that
    # names the same class, and on django that is tens of thousands of them.
    constructor_cache: dict[str, str | None] = {}

    call_refs = _refs_by_path(store, rev, "call", scan_paths)
    for path in target_paths:
        ctx = table.context(rev, path, bases, subclasses, descendant_cache)
        for ref in call_refs.get(path, ()):
            src = _source_id(ref, table, path)
            hits = resolver.resolve_call(ref, ctx)
            if is_derivable_fanout(hits):
                # Not an edge and not a gap: the answer, held in the one form
                # that does not grow with the square of the repository. See
                # `is_derivable_fanout`.
                #
                # No constructor edge is added here. `with_constructors` below
                # only ever fires on a hit the resolver actually distinguished
                # -- `Cls()` resolves through an import or a module-local name,
                # never through the bare-name fallback -- so an all-LOW fan-out
                # has no class in it to construct.
                ambiguous_rows.append(
                    (rev, src, path, ref.line, ref.raw_name, "call", "ambiguous", len(hits))
                )
                continue
            for node_id, confidence in with_constructors(hits, table, bases, constructor_cache):
                edge_rows.append(
                    (rev, src, node_id, "CALLS", confidence, PROVENANCE, path, ref.line)
                )
            if is_builtin_call(ref):
                # Recorded, but not as a gap. A builtin is a reference the
                # resolver understood and deliberately did not link to a repo
                # symbol -- counting it as "unresolved" buries the real gaps
                # under a large constant. Still written, so the choice is
                # visible rather than silent.
                builtin_rows.append(
                    (rev, src, path, ref.line, ref.raw_name, "call", "builtin", 0)
                )
            elif not hits:
                # Never dropped: the ref stays in `blob_refs` for effect
                # detection, and the gap is counted as a health signal.
                unresolved_rows.append(
                    (rev, src, path, ref.line, ref.raw_name, "call", "unknown", 0)
                )

    connection.executemany(
        "INSERT INTO edges(rev, src, dst, kind, confidence, provenance, callsite_path,"
        " callsite_line) VALUES(?,?,?,?,?,?,?,?)",
        edge_rows,
    )
    connection.executemany(
        "INSERT INTO unresolved(rev, src, path, line, raw_name, ref_kind, reason,"
        " candidates) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        unresolved_rows + ambiguous_rows + builtin_rows,
    )
    # Counted over the whole revision, not over this pass: a narrowed rewrite
    # touches a handful of paths but `status` has to describe the whole graph.
    def total(sql: str) -> int:
        return connection.execute(sql, (rev,)).fetchone()["n"]

    return ResolveStats(
        edges=total("SELECT COUNT(*) AS n FROM edges WHERE rev=?"),
        unresolved=total(
            "SELECT COUNT(*) AS n FROM unresolved WHERE rev=? AND reason='unknown'"
        ),
        ambiguous=total("SELECT COUNT(*) AS n FROM unresolved WHERE rev=? AND reason='ambiguous'"),
    )


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
    """Fuzzy lookup: exact id, then exact qualname, then suffix match.

    All three steps compare case-insensitively (`COLLATE NOCASE` for the
    two exact steps; `LIKE`'s own ASCII case-folding, already the default,
    for the suffix step), so a query's case can never change which set of
    symbols comes back -- `resolve charge` and `resolve CHARGE` return the
    identical result. Before this, steps 1-2 compared with binary `=`
    while step 3 was already case-insensitive, so a query differing only
    in case from the real name could fall straight through the (missed)
    exact steps and land on step 3's dot-anchored suffix pattern -- which
    can never match a top-level, dot-free qualname at all -- producing a
    completely different, disjoint match set instead of the same one.
    """
    columns = "id, path, qualname, kind, line_start, line_end, name_binding"
    for clause, parameters in (
        ("id=? COLLATE NOCASE", (query,)),
        ("qualname=? COLLATE NOCASE", (query,)),
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
