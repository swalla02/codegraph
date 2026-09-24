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
(`MECHANISMS` below): a module's top level reaching it, a dunder, a
decorator, a test-runner entry point, an override of an inherited method, a
nested definition its enclosing scope can pass around as a value, or an
import naming it. None of these is a proof that the symbol runs. Each is
counter-evidence to "nothing reaches this", which is the reading a bare
island count invites and the one that gets code deleted.

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

*A run was watched entering it.* Since #56 a revision can carry an
imported trace, and a member of the island having actually executed is the
only entry on this list that is evidence rather than counter-evidence:
everything else here says "here is a way something could reach this", while
this one says "something did". It is reported as its own clause rather than
among the implicit mechanisms, because it is not an invocation mechanism at
all -- it is an observation, and calling it one would be the same
flattening of evidence into inference #56 exists to undo. A revision with no
trace has no such islands and reads exactly as it did before.

*Nothing codegraph recognises reaches it.* The remainder, counted as
`unexplained`. That is the strongest claim available and it is still a
statement about this tool: no resolved call, and no implicit-invocation
mechanism from the list above. On psf/requests the 17 islands left in
this bucket are almost all a library's public surface -- `get_dict`,
`list_domains`, `dict_from_cookiejar` -- called by users of the package
and by stdlib `cookiejar`, neither of which is in the tree. Reading the
bucket as dead code would be wrong in exactly that case.

It is also the bucket that has to earn the report's keep, and #45 is what
happens when it does not: on codegraph's own source it held six singletons
and every one of them was live code -- three Protocols, a class handed to
sqlite, a method reached through a dispatch table, and a `main` under a
`__main__` guard. The answer was to make the graph carry the relationships
that were already there (`REFERENCES` and `IMPLEMENTS` in `resolve.py`,
and the `entry` mechanism below), not to add more ways of excusing an
island. That took this repository from 7 unexplained islands to 1, with no
unexplained singleton left, and psf/requests from 20 to 17.

Measured on psf/requests: 807 symbols, 132 islands, largest 665, 127
singletons; 115 islands carry at least one recognised mechanism, 1 holds a
`NETWORK` boundary, 17 are unexplained. Two earlier states of the same
repository, for scale: before the constructor edge
(`resolve.with_constructors`, the plain bug #27 names) it reported 172
islands with a largest of 628 and 167 singletons, and before #45 it
reported 143 islands with 136 singletons -- linking `Cls()` to the
`__init__` it runs folded 18 islands into the rest of the graph, and
recording mentions and Protocol implementations folded another 11, for 118
further edges (7.6%).

## Three deliberate calls about membership, each of which moves the numbers

*Undirected.* `A -> B` and `B -> A` put A and B on the same island. That
is what the word means here -- a region sharing no call relationship of
any direction with another region. A directed notion (strongly connected
components) would answer a different and much narrower question: almost
every acyclic caller/callee pair would become its own component.

*Every kind `impact` walks.* An island is meant to bound what `impact`
and `effects` can ever say about a symbol, so a symbol's island holds every
node an unlimited-hop walk from it could touch -- a property a reader can
check. That is why the partition is computed from `resolve.DEPENDENCY_KINDS`
rather than from a list of its own: CALLS, INHERITS since #42 (a change to a
base reaches its subclasses), and IMPLEMENTS and REFERENCES since #45. Before
INHERITS, psf/requests reported 172 islands rather than 156: 16 boundaries
that `impact` now crosses. `effects` still walks CALLS only, which keeps the
bound true for it -- an island is never smaller than what either walk
reaches. INHERITS and IMPLEMENTS join classes, not their methods, so an
island of methods is still labelled through the `override` mechanism below.

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
attributed by component root rather than by membership -- and their
presence in a component is itself the `entry` mechanism below, which is
what a function reached only from its own file's `__main__` guard has
instead of a caller.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import chain

from codegraph.ambiguity import Ambiguity
from codegraph.config import Config
from codegraph.render import Group, Report, Row, budget
from codegraph.resolve import DEPENDENCY_KINDS, IMPLEMENTS, INHERITS, module_for_path
from codegraph.store import Store
from codegraph.trace import observed_nodes
from codegraph.trace import summary as trace_summary
from codegraph.uncertainty import UNEXPLAINED_ISLAND, unknown

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
ENTRY = "entry"  # a module's top level calls into it: import-time, or `__main__`
DUNDER = "dunder"  # `del d[k]` runs `__delitem__`; `Cls()` runs `__init__`
DECORATOR = "decorator"  # ran at definition time; may register/wrap/replace
TEST = "test"  # matches pytest's default collection convention
OVERRIDE = "override"  # same name on both ends of an INHERITS/IMPLEMENTS link
NESTED = "nested"  # defined inside a function that can pass it as a value
IMPORT = "import"  # its dotted name is imported somewhere in this revision
MECHANISMS = (ENTRY, DUNDER, DECORATOR, TEST, OVERRIDE, NESTED, IMPORT)

#: Members of an island a trace was seen executing. Deliberately NOT in
#: `_MECHANISMS`: those are ways a symbol could be reached without a call
#: site, and this is not a way at all -- it is the fact that it was. It is
#: still enough to explain an island, and it is the strongest explanation
#: the report has.
TRACED = "traced"

#: The effect kinds a row reports, in display order. `NETWORK` is the only
#: one that marks a boundary or makes an island explained; `ENV_READ` is
#: printed as a legend entry only. See the module docstring and #27's scope
#: decision for why the list stops there.
NETWORK = "NETWORK"
ENV_READ = "ENV_READ"
_BOUNDARY_KINDS = (NETWORK, ENV_READ)


class Components:
    """Union-find over node ids, growing its node set on demand.

    Ids are added as they are seen rather than pre-seeded, so an edge
    endpoint with no `nodes` row (which `impact.py` also guards against)
    still joins the two sides it connects instead of being dropped and
    silently splitting an island in two. It follows that `find` answers for
    an id this revision never saw as well -- a component of one, which is
    the right answer for a symbol with no edge in either direction and the
    reason `query/path.py` can ask about any pair of ids without checking
    them first.
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


def _partition(
    store: Store, rev: str, ambiguity: Ambiguity
) -> tuple[Components, set[tuple[str, str]], set[tuple[str, str]]]:
    """The revision's connected components, plus the two edge sets the
    report reads afterwards: distinct `(src, dst)` pairs, and the subset of
    them that links one class to another.

    Distinct pairs, never raw edge rows: the same call written twice in a
    body, or one candidate reached through two import aliases, writes two
    rows for one relationship and would inflate the fan-in that picks each
    island's hubs (see `rank.fan_in` for the same care).

    One pass over `edges` for all three results, because on a 2,930-file
    repository that is ~395k rows and this is the whole cost of the command.
    """
    components = Components()
    pairs: set[tuple[str, str]] = set()
    class_links: set[tuple[str, str]] = set()
    marks = ",".join("?" * len(DEPENDENCY_KINDS))
    for row in store.connection.execute(
        f"SELECT src, dst, kind FROM edges WHERE rev=? AND kind IN ({marks})",
        (rev, *DEPENDENCY_KINDS),
    ):
        pairs.add((row["src"], row["dst"]))
        if row["kind"] in (INHERITS, IMPLEMENTS):
            class_links.add((row["src"], row["dst"]))
    for src, dst in pairs:
        components.union(src, dst)

    # The unmaterialized bare-name fan-out, through per-name hubs. Hub pairs
    # join components but are deliberately kept out of `pairs`, and so out of
    # the fan-in the caller computes from it: a hub is not a caller, and
    # letting one stand in for its whole reference set would rank a name's
    # definitions by how ambiguous the name is rather than by how much of the
    # graph actually reaches them.
    for src, dst in chain(ambiguity.hub_edges(), ambiguity.base_hub_edges()):
        components.union(src, dst)
    return components, pairs, class_links


def connected_components(store: Store, rev: str, ambiguity: Ambiguity | None = None) -> Components:
    """This revision's islands, as a structure to ask membership of.

    The partition `islands_report` prints, computed by the same code rather
    than by a second implementation that could come to disagree with it --
    `query/path.py` uses this to make the strongest negative answer it has
    ("no walk can ever connect these two"), and that answer is only worth
    printing while it means exactly what an `islands` row means.

    Pass an `Ambiguity` a caller has already built; the fan-out has to be
    folded in either way (see the module docstring) and building it twice
    for one command is ~0.8s of pure waste on django.
    """
    return _partition(store, rev, ambiguity or Ambiguity(store, rev))[0]


def island_roots(store: Store, rev: str, ambiguity: Ambiguity | None = None) -> dict[str, str]:
    """Every non-module symbol of `rev`, mapped to the root of the island
    it belongs to -- the grouping `islands_report` counts, from the same
    partition, so `history` can compare two revisions' islands without a
    second definition of what an island is. Two symbols share an island
    exactly when they map to the same root."""
    components = connected_components(store, rev, ambiguity)
    return {
        row["id"]: components.find(row["id"])
        for row in store.connection.execute(
            "SELECT id FROM nodes WHERE rev=? AND kind != 'module'", (rev,)
        )
    }


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
    traced: int = 0,
) -> str:
    """The `detail` column for one island's row.

    The size clause comes first because it is what orders the report; then
    the classification, which is the answer to "why is this apart"; the
    hub names the caller appends last, because they are the longest and
    least structured part.
    """
    if size == 1 and ENTRY in mechanisms:
        # One member, but not nothing: a module's top level reaches it. The
        # row has to say so, because the sentence below would be false.
        detail = "size 1, reached only from its module's top level"
    elif size == 1:
        # Deliberately phrased as a statement about the recorded edges, not
        # about the symbol: "nothing calls it" is a claim this graph cannot
        # make (see the module docstring).
        detail = "size 1, no resolved call in either direction"
    else:
        detail = f"size {size} across {_plural(files, 'file')}"

    named = [name for name in MECHANISMS if name in mechanisms]
    if named:
        detail += f"; implicit: {', '.join(named)}"
    else:
        # The strongest negative claim available, and still a statement
        # about this tool rather than about the code.
        detail += "; no implicit-invocation mechanism recognised"
    kinds = [kind for kind in _BOUNDARY_KINDS if kind in boundary]
    if kinds:
        detail += f"; boundary: {', '.join(kinds)}"
    if traced:
        # Last, and phrased as the observation it is: not "something could
        # reach this" but "a run went in here".
        detail += f"; {TRACED}: {_plural(traced, 'member')} seen running"
    return detail


@dataclass(frozen=True)
class _Labelled:
    """The whole partition, labelled: what `islands_report` prints rows
    from, and what one symbol's label is read out of.

    Held as one value because the five parts are computed in one pass and
    only mean anything together -- `mechanisms` is keyed by component root,
    so it is unreadable without `components`.
    """

    components: Components
    #: node id -> (path, first line). Members only: a `path::<module>` node
    #: carries connectivity and is never one.
    members: dict[str, tuple[str, int]]
    #: component root -> its members.
    grouped: dict[str, list[str]]
    #: component root -> the implicit-invocation mechanisms found in it.
    mechanisms: dict[str, set[str]]
    #: component root -> the process-boundary effect kinds found in it.
    boundaries: dict[str, set[str]]
    #: node id -> how many distinct nodes reach it, for picking hubs.
    fan_in: Counter
    #: component root -> how many of its members an imported run was seen
    #: entering. Empty on every revision with no trace (#56).
    traced: dict[str, int]


@dataclass(frozen=True)
class IslandLabel:
    """What this report can say about one symbol's island.

    The per-symbol form of an `islands` row, for #55: `unexplained: 17` is
    a property of the report, and an agent asking about one function needs
    to know whether *that* function is one of the seventeen -- and, when it
    is, what was looked for and not found, since an unexplained island and
    an unexamined one read identically otherwise.
    """

    size: int
    #: The mechanisms found, in `MECHANISMS` order.
    found: tuple[str, ...]
    #: The ones checked and not found, in the same order. The complement of
    #: `found`, spelled out rather than left to the reader, because the
    #: claim "nothing recognised reaches this" is only readable beside the
    #: list of what "recognised" covers.
    missing: tuple[str, ...]
    #: Process-boundary effect kinds on the island, in `_BOUNDARY_KINDS` order.
    boundary: tuple[str, ...]
    #: How many of the island's members an imported run was seen entering;
    #: 0 when there is no trace (#56).
    traced: int = 0

    @property
    def explained(self) -> bool:
        """Exactly the condition `islands`' `unexplained` counts, negated:
        a recognised mechanism, a `NETWORK` boundary, or a run watched
        entering the island. Computed here so the two cannot come to
        disagree about one island.

        The trace term is what closes #55's own loop. `NEXT_ACTION` for
        `unexplained_island` says a runtime trace is the only thing that
        can confirm the symbol is reached; once somebody has taken that
        action, the entry has to stop being raised, or the report would go
        on asking for evidence it has already been given.
        """
        return bool(self.found) or NETWORK in self.boundary or bool(self.traced)


def island_label(store: Store, rev: str, node_id: str, config: Config | None = None) -> IslandLabel:
    """One symbol's island, labelled exactly as the `islands` report labels
    it -- same partition, same mechanism passes, same code.

    It costs what `islands` costs (one pass over the revision's edges and
    one over its nodes) because the labels are properties of a component,
    and a component is not knowable from one node. That is the honest price
    and it is stated rather than approximated: a cheaper per-symbol
    re-derivation would be a second implementation, and this project has
    already learned what two implementations of one graph produce.
    """
    labelled = _labelled(store, rev, config)
    root = labelled.components.find(node_id)
    found = tuple(name for name in MECHANISMS if name in labelled.mechanisms.get(root, set()))
    boundary = labelled.boundaries.get(root, set())
    return IslandLabel(
        size=len(labelled.grouped.get(root, [node_id])),
        found=found,
        missing=tuple(name for name in MECHANISMS if name not in found),
        boundary=tuple(kind for kind in _BOUNDARY_KINDS if kind in boundary),
        traced=labelled.traced.get(root, 0),
    )


def _labelled(store: Store, rev: str, config: Config | None = None) -> _Labelled:
    """The partition plus every label the report puts on it.

    Four queries and one pass over the edges, never a query per node: on a
    2,930-file repository this walks ~395k edge rows, and a per-node
    lookup in that loop would be the whole cost of the command.
    """
    connection = store.connection
    source_roots = (config or Config()).source_roots

    # `class_links` is the class-to-class half of the edges: a base and its
    # subclass, and a Protocol and the class that satisfies it. Both mean the
    # same thing for a method declared on each end -- one declaration is what
    # the caller names and the other is what runs -- and the `override` pass
    # below is computed from them alone.
    components, pairs, class_links = _partition(store, rev, Ambiguity(store, rev))
    fan_in = Counter(dst for _, dst in pairs)

    imported = _imported_dotted_names(store, rev)

    members: dict[str, tuple[str, int]] = {}
    grouped: dict[str, list[str]] = {}
    mechanisms: dict[str, set[str]] = {}
    # (class node id) -> {method leaf name: node id}, for the override pass.
    # Built here rather than by a second query because the node scan is
    # already reading every qualname it needs.
    methods: dict[str, dict[str, str]] = {}
    module_names: dict[str, str] = {}
    module_nodes: list[str] = []
    for row in connection.execute(
        "SELECT id, path, qualname, kind, line_start, decorators FROM nodes WHERE rev=?", (rev,)
    ):
        if row["kind"] == "module":
            module_nodes.append(row["id"])
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

    # A module node in a component is a statement at a file's top level
    # reaching into it -- `app = create_app()`, a registration call, or the
    # `main()` inside a `__main__` guard. It is not a member (see the module
    # docstring), which is exactly why the island it connects to needed a
    # label: the `main` of `codegraph.tracer` in this repository was
    # reported as a
    # singleton with nothing recognised reaching it, when the edge from its
    # own module node was sitting in the graph the whole time. The claim is
    # the strongest in the list -- it is a resolved call, not an inference
    # from a name -- and it is still not proof the code runs: whether anything
    # executes that file is a question about how the program is started.
    for node_id in module_nodes:
        root = components.find(node_id)
        if root in grouped:
            mechanisms.setdefault(root, set()).add(ENTRY)

    # A method declared on both ends of an INHERITS or IMPLEMENTS edge is
    # reached by dispatch through the other declaration -- the ABC/subclass
    # shape #27 names, and the Protocol/implementer shape #45 adds, which is
    # the same shape with the link established structurally rather than
    # nominally. Driven from the edges rather than from the nodes so it costs
    # one dict intersection per link, not a hierarchy walk per method.
    for subclass, base in class_links:
        shared = methods.get(subclass, {}).keys() & methods.get(base, {}).keys()
        for leaf in shared:
            for class_id in (subclass, base):
                node_id = methods[class_id][leaf]
                mechanisms.setdefault(components.find(node_id), set()).add(OVERRIDE)

    # The imported run, by component. A symbol the trace saw executing may
    # have no edge at all -- a framework dispatches to it from outside this
    # tree -- which is exactly the island this report could never say
    # anything about. Attributed by root like the boundaries above, so an
    # observation on a module node still lands on the island it connects.
    traced: dict[str, int] = {}
    for node_id in observed_nodes(store, rev):
        root = components.find(node_id)
        if root in grouped:
            traced[root] = traced.get(root, 0) + 1

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

    return _Labelled(components, members, grouped, mechanisms, boundaries, fan_in, traced)


def islands_report(store: Store, rev: str, config: Config | None = None, limit: int = 20) -> Report:
    """Connected components of `rev`'s CALLS and INHERITS edges, read as
    undirected, each labelled with the implicit-invocation mechanisms and
    process boundaries found inside it."""
    labelled = _labelled(store, rev, config)
    members, grouped = labelled.members, labelled.grouped
    mechanisms, boundaries, fan_in = labelled.mechanisms, labelled.boundaries, labelled.fan_in
    traced = labelled.traced

    island_rows: list[Row] = []
    singleton_rows: list[Row] = []
    largest = 0
    implicit_count = network_count = unexplained_count = traced_count = 0
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
        seen_running = traced.get(root, 0)
        implicit_count += bool(marks)
        network_count += NETWORK in boundary
        traced_count += bool(seen_running)
        unexplained_count += not marks and NETWORK not in boundary and not seen_running

        files = len({members[node_id][0] for node_id in island})
        detail = _describe(size, files, marks, boundary, seen_running)
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
        # Islands with a member an imported run was seen entering; 0 on
        # every revision with no trace, which is every revision until
        # somebody imports one.
        "traced": traced_count,
        "unexplained": unexplained_count,
        # Says what the partition was computed from, so a row is read as
        # "these share no call edge" and never as "nothing reaches this".
        "basis": f"undirected {', '.join(DEPENDENCY_KINDS)} edges",
        # ...and whether anything beyond the text was available to say it
        # with. "Nothing reaches this" is a far stronger claim about a
        # revision whose test suite has been watched running than about one
        # where the only witness is the source code, and a reader cannot
        # weigh an `unexplained` count without knowing which they hold. So
        # this field is printed either way, `none` included.
        "trace": trace_summary(store, rev),
    }

    return Report(
        summary=summary,
        groups=groups,
        truncated=truncated,
        # `unexplained` has always been this report's own admission of
        # ignorance; the envelope is that count in the form a machine can
        # act on, with the next move attached. It is the whole envelope
        # here: an island is computed from the complete edge set with no
        # budget and no walk to cut short, so nothing else about this
        # report can be incomplete.
        unknowns=(
            [
                unknown(
                    UNEXPLAINED_ISLAND,
                    f"{unexplained_count} of {len(grouped)} islands carry no recognised"
                    f" mechanism; the ones checked are {', '.join(MECHANISMS)}",
                )
            ]
            if unexplained_count
            else []
        ),
    )


__all__ = [
    "MECHANISMS",
    "Components",
    "IslandLabel",
    "connected_components",
    "is_test_path",
    "island_label",
    "island_roots",
    "islands_report",
]
