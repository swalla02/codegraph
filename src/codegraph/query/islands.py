"""The `islands` report: the revision's call graph split into connected
components, treated as undirected, and each component labelled with what
codegraph can say about *why* it stands apart.

`impact` and `effects` are node-local -- "who calls X", "what can X reach".
Neither says anything about the graph's global shape, and that shape is
real information: the call graph is not connected, and the disconnection
is structure rather than a defect. A service boundary, a config-gated
region and genuinely unreferenced code all show up as separate components.
An **island** is one such component: a set of symbols that share some call
relationship, however indirect, with each other and none at all with
anything outside it.

**An island is not a reachability claim, and a one-symbol island is not a
dead-code finding.** Membership is computed from the call edges the
resolver actually recorded, and a symbol can be invoked by a mechanism
that leaves no call site anywhere in the source. Issue #27 names three
different reasons an island exists and observes that they render
identically; two of the three are things this report can now say.

## Why an island exists

*It is invoked by a mechanism that is not a call site.* Each island is
tagged with every such mechanism codegraph recognises among its members
(`_MECHANISMS` below): a dunder, a decorator, a test-runner entry point,
an override of an inherited method, a nested definition its enclosing
scope can pass around as a value, or an import naming it. None of these
is a proof that the symbol runs. Each is counter-evidence to "nothing
reaches this", which is the reading a bare island count invites and the
one that gets code deleted.

*The path leaves the process.* An island holding a `NETWORK` effect is a
boundary: the call graph provably ends there because the next hop is a
socket, and the handler is in another repo. This is the report's most
useful positive finding, and it is deliberately the ONLY effect kind
counted as a boundary -- a socket call leaving the process is a
structural fact needing no schema knowledge, whereas establishing that
two functions are coupled through a database means reading SQL and
tracking a schema, which is a different tool (see #27's scope decision,
and #26 for the annotation treadmill that parks). `ENV_READ` rides along
in the row as a legend entry -- "this region is lit up by a variable" --
but is not itself a boundary and never makes an island `explained`.

*Nothing codegraph recognises reaches it.* The remainder, counted as
`unexplained`. That is the strongest claim available and it is still a
statement about this tool: no resolved call, and no implicit-invocation
mechanism from the list above. On psf/requests the 29 islands left in
this bucket are almost all a library's public surface -- `get_dict`,
`list_domains`, `dict_from_cookiejar` -- called by users of the package
and by stdlib `cookiejar`, neither of which is in the tree. Reading the
bucket as dead code would be wrong in exactly that case.

Measured on psf/requests: 807 symbols, 154 islands, largest 646, 149
singletons; 125 islands carry at least one recognised mechanism, 1 holds a
`NETWORK` boundary, 29 are unexplained. Before the constructor edge
(`resolve.with_constructors`, the plain bug #27 names) the same repository
reported 172 islands with a largest of 628 and 167 singletons: linking
`Cls()` to the `__init__` it runs folded 18 islands into the rest of the
graph.

## Three deliberate calls about membership, each of which moves the numbers

*Undirected.* `A -> B` and `B -> A` put A and B on the same island. That
is what the word means here -- a region sharing no call relationship of
any direction with another region. A directed notion (strongly connected
components) would answer a different and much narrower question: almost
every acyclic caller/callee pair would become its own component.

*CALLS only -- `INHERITS` edges do not join an island.* An island is
meant to bound what `impact` and `effects` can ever say about a symbol,
and both of those walk CALLS and only CALLS. So a symbol's island is
exactly the set of nodes an unlimited-hop `impact` or `effects` walk could
ever touch, which is a property a reader can check. Folding INHERITS in
would break that correspondence: measured on psf/requests it merges 172
islands down to 156, so 16 boundaries would be reported as crossed by a
walk that cannot in fact cross them. Inheritance still gets read here --
it is what the `override` mechanism is computed from -- but it labels an
island rather than merging two.

*The bare-name fan-out counts, and it is not in `edges`.* Since #25 the
resolver does not materialize a call whose name matches more than one
definition; `ambiguity.py` expands it on demand instead. This report
has to include it, or its central claim -- that an island bounds what an
unlimited-hop `impact` or `effects` walk could ever touch -- stops being
true, since both of those expand it too. It is folded in through the same
per-name hub nodes `effects/propagate.py` uses: for connectivity, unioning
`src` with `HUB(save)` and `HUB(save)` with every definition named `save`
puts exactly the same set of symbols in one component as the N x M direct
edges would, for O(N + M) rather than O(N x M). Hubs are treated exactly
like the module nodes below -- connectivity, never membership.

*Synthetic `path::<module>` nodes connect islands but are never members.*
Their edges are real: a module-scope call (`_init()` at the foot of
`status_codes.py`) is the only thing tying that file's helper to the rest
of the graph, and ignoring those 8 edges on psf/requests splits it into
174 islands instead of 172. But `path::<module>` is not a symbol anyone
wrote, and counting one per file would invent 32 further islands on
requests out of files whose top level simply calls nothing. So module
nodes carry connectivity and are excluded from `symbols`, from island
sizes, and from the rows. They can still carry an effect, so a boundary is
attributed by component root rather than by membership.
"""

from __future__ import annotations

from collections import Counter

from codegraph.ambiguity import Ambiguity
from codegraph.config import Config
from codegraph.render import Group, Report, Row, budget
from codegraph.resolve import module_for_path
from codegraph.store import Store

#: How many of an island's members a single row names: the row's `id` is
#: the first, `detail` names the rest. An island can hold hundreds of
#: symbols (646 of psf/requests' 807 sit on one), so a row summarizes
#: rather than dumps -- and the ones worth naming are the hubs, the
#: members with the most distinct callers inside the graph.
_HUBS_PER_ROW = 3

#: Every implicit-invocation mechanism this report recognises, in the order
#: a row lists them: strongest claim first.
#:
#: None of these is proof that a symbol runs, and that asymmetry is the
#: whole design. A false "nothing reaches this" gets working code deleted;
#: a mechanism named on an island that turns out to be genuinely
#: unreferenced costs the reader one line of output. So each test below is
#: deliberately permissive, and a mechanism is claimed on the island as a
#: whole as soon as ONE member matches.
DUNDER = "dunder"  # `del d[k]` runs `__delitem__`; `Cls()` runs `__init__`
DECORATOR = "decorator"  # ran at definition time; may register/wrap/replace
TEST = "test"  # matches pytest's default collection convention
OVERRIDE = "override"  # same name declared on a class linked by INHERITS
NESTED = "nested"  # defined inside a function that can pass it as a value
IMPORT = "import"  # its dotted name is imported somewhere in this revision
_MECHANISMS = (DUNDER, DECORATOR, TEST, OVERRIDE, NESTED, IMPORT)

#: The effect kinds a row reports, in display order. `NETWORK` is the only
#: one that marks a boundary or makes an island explained; `ENV_READ` is
#: printed as a legend entry only. See the module docstring and #27's scope
#: decision for why the list stops there.
NETWORK = "NETWORK"
ENV_READ = "ENV_READ"
_BOUNDARY_KINDS = (NETWORK, ENV_READ)


class _Components:
    """Union-find over node ids, growing its node set on demand.

    Ids are added as they are seen rather than pre-seeded, so an edge
    endpoint with no `nodes` row (which `impact.py` also guards against)
    still joins the two sides it connects instead of being dropped and
    silently splitting an island in two.
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, node_id: str) -> str:
        parent = self._parent
        root = parent.setdefault(node_id, node_id)
        while root != parent[root]:
            parent[root] = parent[parent[root]]
            root = parent[root]
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[left_root] = right_root


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _is_dunder(leaf: str) -> bool:
    """`__delitem__` yes, `__init__` yes, `_private` no, `__` no.

    `__init__` counts even though `resolve.with_constructors` now gives it
    a real edge from every `Cls()` in the tree: a class instantiated only
    by a caller outside the repository -- which is every library base class
    -- still has none.
    """
    return len(leaf) > 4 and leaf.startswith("__") and leaf.endswith("__")


#: Directory names that mean "everything below here is the test tree".
#: `tests/` is the common one, `test/` the other spelling, and both are
#: matched at ANY depth so a package-internal `src/pkg/tests/` subpackage
#: counts too.
_TEST_DIRS = ("tests", "test")


def _is_test_module(stem: str) -> bool:
    """pytest's default `python_files = test_*.py *_test.py`, on a file stem.

    The one place that rule is spelled out. `_is_test_entry_point` and
    `is_test_path` are both built on it so they cannot come to disagree
    about what a test file is -- they differ only in how much MORE than the
    stem each accepts, which is the part that is actually a judgement call.
    """
    return stem.startswith("test_") or stem.endswith("_test")


def is_test_path(path: str) -> bool:
    """Is this file part of the test tree?

    Broader than `_is_test_entry_point` on purpose, and the two answer
    different questions. That one asks "would pytest COLLECT this symbol",
    which has to be strict because over-matching there explains away every
    unreferenced helper in the test tree. This one asks "is this FILE test
    code", where a fixture in `conftest.py` and a plain helper in
    `tests/support.py` are as much test code as a collected `test_foo`, and
    none of them is a production caller.

    Four shapes, because four are in the wild: pytest's own file stems
    (shared with `_is_test_entry_point` through `_is_test_module`),
    `conftest.py`, and a `tests/` or `test/` directory at any depth --
    which covers both a top-level test tree and a package-internal `tests`
    subpackage. `query/orphans.py` reads this to decide whether a caller is
    a test; it is deliberately NOT what splits `impact`'s report groups
    (`impact._is_test`), which is presentation rather than a claim.
    """
    directories, _, filename = path.rpartition("/")
    stem = filename.removesuffix(".py")
    if stem == "conftest" or _is_test_module(stem):
        return True
    return any(part in _TEST_DIRS for part in directories.split("/"))


def _is_test_entry_point(path: str, qualname: str) -> bool:
    """Would pytest collect this under its default configuration?

    `python_files = test_*.py *_test.py`, `python_classes = Test*`,
    `python_functions = test*`, and collection only reaches module-level
    functions and the methods of a collected class -- so the qualname has
    to be one or two segments and a `<locals>` definition never qualifies.
    Being stricter than `is_test_path` above is deliberate: over-matching
    here would silently explain away every unreferenced helper in the test
    tree.
    """
    stem = path.rpartition("/")[2].removesuffix(".py")
    if not _is_test_module(stem):
        return False
    parts = qualname.split(".")
    if len(parts) == 1:
        return parts[0].startswith(("test", "Test"))
    return len(parts) == 2 and parts[0].startswith("Test") and parts[1].startswith("test")


def _imported_dotted_names(store: Store, rev: str) -> set[str]:
    """Every dotted name this revision's `from a.b import c` lines name.

    `resolve.build_import_maps` already resolved relative imports into
    absolute dotted names before these rows were written, so an entry is
    directly comparable with a node's own `module.qualname`. A hit means
    something in the tree refers to the symbol by name -- not that it calls
    it, which is exactly why the call graph does not have the edge.
    """
    return {
        row["module"]
        for row in store.connection.execute(
            "SELECT DISTINCT module FROM imports WHERE rev=?", (rev,)
        )
    }


def _describe(
    size: int,
    files: int,
    mechanisms: set[str],
    boundary: set[str],
) -> str:
    """The `detail` column for one island's row.

    The size clause comes first because it is what orders the report; then
    the classification, which is the answer to "why is this apart"; the
    hub names the caller appends last, because they are the longest and
    least structured part.
    """
    if size == 1:
        # Deliberately phrased as a statement about the recorded edges, not
        # about the symbol: "nothing calls it" is a claim this graph cannot
        # make (see the module docstring).
        detail = "size 1, no resolved call in either direction"
    else:
        detail = f"size {size} across {_plural(files, 'file')}"

    named = [name for name in _MECHANISMS if name in mechanisms]
    if named:
        detail += f"; implicit: {', '.join(named)}"
    else:
        # The strongest negative claim available, and still a statement
        # about this tool rather than about the code.
        detail += "; no implicit-invocation mechanism recognised"
    kinds = [kind for kind in _BOUNDARY_KINDS if kind in boundary]
    if kinds:
        detail += f"; boundary: {', '.join(kinds)}"
    return detail


def islands_report(
    store: Store, rev: str, config: Config | None = None, limit: int = 20
) -> Report:
    """Connected components of `rev`'s CALLS edges, read as undirected, each
    labelled with the implicit-invocation mechanisms and process boundaries
    found inside it.

    Four queries and one pass over the edges, never a query per node: on a
    2,930-file repository this walks ~395k edge rows, and a per-node
    lookup in that loop would be the whole cost of the command.
    """
    connection = store.connection
    source_roots = (config or Config()).source_roots

    # Distinct (src, dst) pairs, never raw edge rows: the same call written
    # twice in a body, or one candidate reached through two import aliases,
    # writes two rows for one relationship and would inflate the fan-in
    # that picks each island's hubs (see rank.fan_in for the same care).
    # INHERITS is read in the SAME pass -- it never joins an island, but it
    # is what `override` is computed from, and a second scan of 395k rows
    # to fetch a few thousand of them would be the more expensive half.
    components = _Components()
    pairs: set[tuple[str, str]] = set()
    inherits: set[tuple[str, str]] = set()
    for row in connection.execute(
        "SELECT src, dst, kind FROM edges WHERE rev=? AND kind IN ('CALLS', 'INHERITS')", (rev,)
    ):
        if row["kind"] == "CALLS":
            pairs.add((row["src"], row["dst"]))
        else:
            inherits.add((row["src"], row["dst"]))
    for src, dst in pairs:
        components.union(src, dst)
    fan_in = Counter(dst for _, dst in pairs)

    # The unmaterialized bare-name fan-out, through per-name hubs. Hub pairs
    # join components but are deliberately kept out of `fan_in`: a hub is not
    # a caller, and letting one stand in for its whole reference set would
    # rank a name's definitions by how ambiguous the name is rather than by
    # how much of the graph actually reaches them.
    for src, dst in Ambiguity(store, rev).hub_edges():
        components.union(src, dst)

    imported = _imported_dotted_names(store, rev)

    members: dict[str, tuple[str, int]] = {}
    grouped: dict[str, list[str]] = {}
    mechanisms: dict[str, set[str]] = {}
    # (class node id) -> {method leaf name: node id}, for the override pass.
    # Built here rather than by a second query because the node scan is
    # already reading every qualname it needs.
    methods: dict[str, dict[str, str]] = {}
    module_names: dict[str, str] = {}
    for row in connection.execute(
        "SELECT id, path, qualname, kind, line_start, decorators FROM nodes WHERE rev=?", (rev,)
    ):
        if row["kind"] == "module":
            continue
        node_id, path, qualname = row["id"], row["path"], row["qualname"]
        members[node_id] = (path, row["line_start"])
        root = components.find(node_id)
        grouped.setdefault(root, []).append(node_id)
        marks = mechanisms.setdefault(root, set())

        owner, dot, leaf = qualname.rpartition(".")
        if _is_dunder(leaf):
            marks.add(DUNDER)
        if row["decorators"]:
            marks.add(DECORATOR)
        if ".<locals>." in qualname:
            marks.add(NESTED)
        if _is_test_entry_point(path, qualname):
            marks.add(TEST)
        if path not in module_names:
            module_names[path] = module_for_path(path, source_roots)
        if f"{module_names[path]}.{qualname}" in imported:
            marks.add(IMPORT)
        if dot:
            methods.setdefault(f"{path}::{owner}", {})[leaf] = node_id

    # A method declared on both ends of an INHERITS edge is reached by
    # dispatch through the other declaration -- the ABC/subclass shape #27
    # names. Driven from the edges rather than from the nodes so it costs
    # one dict intersection per inheritance link, not a hierarchy walk per
    # method.
    for subclass, base in inherits:
        shared = methods.get(subclass, {}).keys() & methods.get(base, {}).keys()
        for leaf in shared:
            for class_id in (subclass, base):
                node_id = methods[class_id][leaf]
                mechanisms.setdefault(components.find(node_id), set()).add(OVERRIDE)

    # Attributed by component root, not by membership: a direct effect can
    # sit on a `path::<module>` node, which carries connectivity but is
    # never a member. `direct=1` only -- a propagated effect is reached over
    # CALLS edges, which never leave the island, so the island holding the
    # direct one is the same island either way.
    boundaries: dict[str, set[str]] = {}
    for row in connection.execute(
        "SELECT DISTINCT node_id, kind FROM effects WHERE rev=? AND direct=1 AND kind IN (?, ?)",
        (rev, NETWORK, ENV_READ),
    ):
        boundaries.setdefault(components.find(row["node_id"]), set()).add(row["kind"])

    island_rows: list[Row] = []
    singleton_rows: list[Row] = []
    largest = 0
    implicit_count = network_count = unexplained_count = 0
    for root, island in grouped.items():
        size = len(island)
        largest = max(largest, size)
        # Hubs first, then id, so a tie between two never-called members
        # (every member of a singleton or a mutually-recursive pair) still
        # produces the same row on every run.
        island.sort(key=lambda node_id: (-fan_in[node_id], node_id))
        head, *rest = island
        path, line_start = members[head]

        marks = mechanisms.get(root, set())
        boundary = boundaries.get(root, set())
        implicit_count += bool(marks)
        network_count += NETWORK in boundary
        unexplained_count += not marks and NETWORK not in boundary

        files = len({members[node_id][0] for node_id in island})
        detail = _describe(size, files, marks, boundary)
        named = rest[: _HUBS_PER_ROW - 1]
        if size > 1 and named:
            detail += f"; also {', '.join(named)}"

        row = Row(
            id=head,
            location=f"{path}:{line_start}",
            detail=detail,
            score=float(size),
        )
        (singleton_rows if size == 1 else island_rows).append(row)

    # `budget` sorts by score (the island size) and is stable, so ordering
    # the rows here is what decides ties -- and every one of the 149
    # singletons on psf/requests is a tie. Without this the printed rows
    # would be in whatever order SQLite handed back the `nodes` rows,
    # which is stable for one database file and not a contract across a
    # rebuild; two runs of the same command should print the same report.
    island_rows.sort(key=lambda row: (-row.score, row.id))
    singleton_rows.sort(key=lambda row: row.id)

    # `limit` is a TOTAL budget across both groups, the same contract
    # `impact` uses for dependents and tests: multi-symbol islands are the
    # structural finding and get first claim, and the long singleton tail
    # (149 of psf/requests' 154 islands) budgets whatever is left rather
    # than crowding them out.
    kept, truncated = budget(island_rows, limit)
    groups = [Group("islands", kept)] if kept else []
    if singleton_rows:
        kept_singletons, singletons_truncated = budget(singleton_rows, limit - len(kept))
        if kept_singletons:
            groups.append(Group("singletons", kept_singletons))
        truncated = truncated or singletons_truncated

    summary = {
        "symbols": len(members),
        "islands": len(grouped),
        "largest": largest,
        "singletons": len(singleton_rows),
        # `implicit` and `network` overlap and are not meant to sum: an
        # island can be both, and `ENV_READ` alone is neither. `unexplained`
        # is the exact complement of their union, so the three answer "for
        # how many islands can this tool say nothing at all".
        "implicit": implicit_count,
        "network": network_count,
        "unexplained": unexplained_count,
        # Says what the partition was computed from, so a row is read as
        # "these share no call edge" and never as "nothing reaches this".
        "basis": "undirected CALLS edges",
    }

    return Report(summary=summary, groups=groups, truncated=truncated)


__all__ = ["is_test_path", "islands_report"]
