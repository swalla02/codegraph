"""The `islands` report: the revision's call graph split into connected
components, treated as undirected.

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
that leaves no call site anywhere in the source: a dunder (`__delitem__`
runs on every `del d[k]`), a decorator, framework dispatch, an ABC
override, a packaging entry point. On psf/requests
`src/requests/auth.py::AuthBase.__call__`,
`src/requests/adapters.py::BaseAdapter.__init__` and
`src/requests/structures.py::CaseInsensitiveDict.__delitem__` are each an
island of exactly one, and not one of them is unused. This report says
where the graph comes apart; *why* a given island exists is issue #27's
stage 2, and it needs edges this graph does not record yet.

Three deliberate calls here, each of which moves the numbers:

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
walk that cannot in fact cross them. Subclasses and their bases are of
course coupled -- `INHERITS` exists so the resolver can do method
resolution, and the coupling reaches `impact` through the CALLS edges that
resolution produces, which is where it belongs.

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
sizes, and from the rows.

Counting them as members instead is exactly what #27's headline figures
do, which is the whole of the difference from them: its largest island of
631 is this report's 628 plus `setup.py::<module>`,
`src/requests/help.py::<module>` and
`src/requests/status_codes.py::<module>`, and its 166 singletons are this
report's 167 minus `src/requests/compat.py::_resolve_char_detection`,
whose only caller is its own module scope and which is therefore a pair
rather than a singleton once the module node counts. The island count,
172, is identical under both rules.
"""

from __future__ import annotations

from collections import Counter

from codegraph.ambiguity import Ambiguity
from codegraph.render import Group, Report, Row, budget
from codegraph.store import Store

#: How many of an island's members a single row names: the row's `id` is
#: the first, `detail` names the rest. An island can hold hundreds of
#: symbols (628 of psf/requests' 807 sit on one), so a row summarizes
#: rather than dumps -- and the ones worth naming are the hubs, the
#: members with the most distinct callers inside the graph.
_HUBS_PER_ROW = 3


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


def islands_report(store: Store, rev: str, limit: int = 20) -> Report:
    """Connected components of `rev`'s CALLS edges, read as undirected.

    Two queries and one pass over the edges, never a query per node: on a
    2,930-file repository this walks ~395k edge rows, and a per-node
    lookup in that loop would be the whole cost of the command.
    """
    connection = store.connection

    members: dict[str, tuple[str, int]] = {}
    for row in connection.execute(
        "SELECT id, path, kind, line_start FROM nodes WHERE rev=?", (rev,)
    ):
        if row["kind"] == "module":
            continue
        members[row["id"]] = (row["path"], row["line_start"])

    # Distinct (src, dst) pairs, never raw edge rows: the same call written
    # twice in a body, or one candidate reached through two import aliases,
    # writes two rows for one relationship and would inflate the fan-in
    # that picks each island's hubs (see rank.fan_in for the same care).
    components = _Components()
    pairs: set[tuple[str, str]] = set()
    for row in connection.execute(
        "SELECT src, dst FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
    ):
        pairs.add((row["src"], row["dst"]))
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

    grouped: dict[str, list[str]] = {}
    for node_id in members:
        grouped.setdefault(components.find(node_id), []).append(node_id)

    island_rows: list[Row] = []
    singleton_rows: list[Row] = []
    largest = 0
    for island in grouped.values():
        size = len(island)
        largest = max(largest, size)
        # Hubs first, then id, so a tie between two never-called members
        # (every member of a singleton or a mutually-recursive pair) still
        # produces the same row on every run.
        island.sort(key=lambda node_id: (-fan_in[node_id], node_id))
        head, *rest = island
        path, line_start = members[head]

        if size == 1:
            # Deliberately phrased as a statement about the recorded edges,
            # not about the symbol: "nothing calls it" is a claim this
            # graph cannot make (see the module docstring).
            detail = "size 1, no resolved call in either direction"
        else:
            files = len({members[node_id][0] for node_id in island})
            detail = f"size {size} across {_plural(files, 'file')}"
            named = rest[: _HUBS_PER_ROW - 1]
            if named:
                detail += f"; also {', '.join(named)}"

        row = Row(
            id=head,
            location=f"{path}:{line_start}",
            detail=detail,
            score=float(size),
        )
        (singleton_rows if size == 1 else island_rows).append(row)

    # `budget` sorts by score (the island size) and is stable, so ordering
    # the rows here is what decides ties -- and every one of the 167
    # singletons on psf/requests is a tie. Without this the printed rows
    # would be in whatever order SQLite handed back the `nodes` rows,
    # which is stable for one database file and not a contract across a
    # rebuild; two runs of the same command should print the same report.
    island_rows.sort(key=lambda row: (-row.score, row.id))
    singleton_rows.sort(key=lambda row: row.id)

    # `limit` is a TOTAL budget across both groups, the same contract
    # `impact` uses for dependents and tests: multi-symbol islands are the
    # structural finding and get first claim, and the long singleton tail
    # (167 of psf/requests' 172 islands) budgets whatever is left rather
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
        # Says what the partition was computed from, so a row is read as
        # "these share no call edge" and never as "nothing reaches this".
        "basis": "undirected CALLS edges",
    }

    return Report(summary=summary, groups=groups, truncated=truncated)


__all__ = ["islands_report"]
