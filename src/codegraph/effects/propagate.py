"""Effect propagation: `effects(n) = direct(n) UNION union(effects(callees))`
over the `CALLS` edge closure for one revision.

Strongly connected components of the call graph are condensed with Tarjan's
algorithm first, so every member of a recursion cycle shares one effect
union computed once. A naive recursive walk without this recurses forever
on mutual recursion (`ping` calls `pong` calls `ping` ...) -- that's what
`test_recursion_cycle_does_not_hang` guards against. Once condensed, the
component graph is a DAG, so a single memoized post-order walk computes
every component's effect set in one pass, carrying confidence as the
minimum along whichever concrete edge/path produced the strongest claim.

`witness_path` is a separate, on-demand BFS over the (uncondensed) `CALLS`
graph, used at query time. BFS never revisits a node, so cycles are handled
for free there and it needs no SCC step of its own. It is confidence-aware:
`propagate` reports the *best* confidence over every path (the aggregate
claim "this effect is reachable, at the strongest confidence any path
supports"), so the witness search is restricted to edges strong enough to
support that same confidence -- otherwise the printed chain could be a
weak, unrelated path to a claim it does not actually justify.

This module never queries the effect `Catalog`: it only reads `effects`
rows Task 10's `detect_direct` already wrote, and `edges`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator

from codegraph.resolve import HIGH, LOW, MEDIUM
from codegraph.store import Store

_RANK = {LOW: 0, MEDIUM: 1, HIGH: 2}


def _weaker(a: str, b: str) -> str:
    return a if _RANK[a] <= _RANK[b] else b


def _stronger(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def propagate(store: Store, rev: str) -> int:
    """Write transitive (`direct=0`) effect rows for `rev`. Returns the
    count of rows written."""
    connection = store.connection
    connection.execute("DELETE FROM effects WHERE rev=? AND direct=0", (rev,))

    node_ids = {row["id"] for row in connection.execute("SELECT id FROM nodes WHERE rev=?", (rev,))}
    calls: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    edge_confidence: dict[tuple[str, str], str] = {}
    for row in connection.execute(
        "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
    ):
        src, dst, confidence = row["src"], row["dst"], row["confidence"]
        if src not in node_ids or dst not in node_ids:
            continue
        calls[src].add(dst)
        key = (src, dst)
        edge_confidence[key] = _stronger(edge_confidence.get(key, confidence), confidence)

    direct_by_node: dict[str, dict[str, str]] = {}
    for row in connection.execute(
        "SELECT node_id, kind, confidence FROM effects WHERE rev=? AND direct=1", (rev,)
    ):
        bucket = direct_by_node.setdefault(row["node_id"], {})
        bucket[row["kind"]] = _stronger(
            bucket.get(row["kind"], row["confidence"]), row["confidence"]
        )

    components, comp_of = _tarjan_scc(node_ids, calls)

    comp_direct: dict[int, dict[str, str]] = {}
    for node_id, node_kinds in direct_by_node.items():
        comp = comp_of.get(node_id)
        if comp is None:
            continue
        bucket = comp_direct.setdefault(comp, {})
        for kind, confidence in node_kinds.items():
            bucket[kind] = _stronger(bucket.get(kind, confidence), confidence)

    comp_edges: dict[int, dict[int, str]] = {}
    for (src, dst), confidence in edge_confidence.items():
        c_src, c_dst = comp_of[src], comp_of[dst]
        if c_src == c_dst:
            continue
        bucket = comp_edges.setdefault(c_src, {})
        bucket[c_dst] = _stronger(bucket.get(c_dst, confidence), confidence)

    # The condensation is a DAG (Tarjan guarantees no cycles between
    # distinct components), so a plain memoized recursion terminates.
    cache: dict[int, dict[str, str]] = {}

    def comp_effects(comp: int) -> dict[str, str]:
        if comp in cache:
            return cache[comp]
        result = dict(comp_direct.get(comp, {}))
        for succ, edge_conf in comp_edges.get(comp, {}).items():
            for kind, confidence in comp_effects(succ).items():
                propagated = _weaker(edge_conf, confidence)
                if kind not in result or _RANK[propagated] > _RANK[result[kind]]:
                    result[kind] = propagated
        cache[comp] = result
        return result

    rows: list[tuple] = []
    for comp_index in range(len(components)):
        effects_here = comp_effects(comp_index)
        if not effects_here:
            continue
        for node_id in components[comp_index]:
            own_direct = direct_by_node.get(node_id, {})
            for kind, confidence in effects_here.items():
                if kind in own_direct:
                    continue
                rows.append((rev, node_id, kind, 0, None, None, confidence))

    connection.executemany(
        "INSERT INTO effects(rev, node_id, kind, direct, evidence_path, evidence_line,"
        " confidence) VALUES(?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _tarjan_scc(
    nodes: set[str], adjacency: dict[str, set[str]]
) -> tuple[list[list[str]], dict[str, int]]:
    """Tarjan's strongly-connected-components algorithm, iterative so a deep
    call graph can't blow the interpreter's recursion limit."""
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    def strongconnect(root: str) -> None:
        nonlocal counter
        work: list[tuple[str, Iterator[str]]] = [(root, iter(adjacency.get(root, ())))]
        index_of[root] = counter
        lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            v, neighbors = work[-1]
            advanced = False
            for w in neighbors:
                if w not in index_of:
                    index_of[w] = counter
                    lowlink[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(adjacency.get(w, ()))))
                    advanced = True
                    break
                if w in on_stack:
                    lowlink[v] = min(lowlink[v], index_of[w])
            if advanced:
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[v])
            if lowlink[v] == index_of[v]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == v:
                        break
                components.append(component)

    for node in nodes:
        if node not in index_of:
            strongconnect(node)

    comp_of = {node_id: i for i, component in enumerate(components) for node_id in component}
    return components, comp_of


def witness_path(store: Store, rev: str, node_id: str, kind: str, confidence: str) -> list[str]:
    """Fewest-hop chain of node ids from `node_id` to a node whose direct
    effect of `kind` causes it, restricted to `CALLS` edges strong enough
    to support `confidence` -- the aggregate value `propagate` already
    computed as the best achievable across every path. Every edge on the
    chain has confidence >= `confidence`, so the chain's own bottleneck
    confidence is never weaker than the number printed next to it. `[]` if
    no such chain exists (should not happen for a `(node_id, kind)` pair
    `propagate` actually produced this confidence for)."""
    connection = store.connection
    direct_nodes = {
        row["node_id"]
        for row in connection.execute(
            "SELECT DISTINCT node_id FROM effects WHERE rev=? AND kind=? AND direct=1",
            (rev, kind),
        )
    }
    if node_id in direct_nodes:
        return [node_id]

    target_rank = _RANK[confidence]
    edge_confidence: dict[tuple[str, str], str] = {}
    for row in connection.execute(
        "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
    ):
        key = (row["src"], row["dst"])
        edge_confidence[key] = _stronger(
            edge_confidence.get(key, row["confidence"]), row["confidence"]
        )

    calls: dict[str, list[str]] = {}
    for (src, dst), conf in edge_confidence.items():
        if _RANK[conf] < target_rank:
            continue
        calls.setdefault(src, []).append(dst)

    visited = {node_id}
    parent: dict[str, str] = {}
    queue: deque[str] = deque([node_id])
    while queue:
        current = queue.popleft()
        for nxt in calls.get(current, ()):
            if nxt in visited:
                continue
            visited.add(nxt)
            parent[nxt] = current
            if nxt in direct_nodes:
                chain = [nxt]
                while chain[-1] != node_id:
                    chain.append(parent[chain[-1]])
                chain.reverse()
                return chain
            queue.append(nxt)
    return []
