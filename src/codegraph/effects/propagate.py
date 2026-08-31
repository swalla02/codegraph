"""Effect propagation: for each node, the strongest confidence tier at
which it can reach an effect-bearing node, over the `CALLS` edge closure
for one revision.

This is the classic widest-path (bottleneck shortest-path) problem: a
node's confidence for an effect is `max` over every path to a node that has
that effect directly, of the `min` confidence along that path -- and the
direct effect's OWN recorded confidence is itself one link in that chain,
not just the CALLS edges leading to it. A bare-wildcard-head catalog match
(`*.execute`) is LOW at the node that directly makes it; a caller reaching
that node only over HIGH edges still only earns LOW, because the weakest
link in its path is the direct detection itself. Rather than search
per-path, it is solved level-wise: for each confidence tier, strongest
first (`HIGH`, then `MEDIUM`, then `LOW`), build the subgraph of `CALLS`
edges at that tier or better, and find every node that can reach a node
that carries the effect directly **at that tier or better** within it via
plain reachability -- multi-source BFS on the reversed graph, starting
from only the effect-bearing nodes whose own direct confidence clears the
tier being tried. The first (strongest) tier at which a node reaches an
eligible effect wins; a weaker tier never overwrites it.

Plain reachability handles cycles for free -- a visited set is all it
takes -- so mutual recursion (`ping` calls `pong` calls `ping` ...) needs
no special handling; `test_recursion_cycle_does_not_hang` passes without
any SCC step. An earlier version of this module condensed strongly
connected components with Tarjan and unioned effects across each
component, but that scheme could hand out a confidence no real path
supported: it merged a component member's direct effect into the whole
component's set without accounting for the edge confidence needed to
*reach* that member from elsewhere in the cycle. Concretely: `A --LOW-->
B`, `B --HIGH--> A`, `B` has a direct HIGH effect -- the old code merged
B's HIGH straight into the shared `{A, B}` union, reporting HIGH for A even
though the only way out of A is the LOW edge. Level-wise reachability does
not have that failure mode, because the tier a node's confidence is
assigned at is exactly the subgraph a path was found to exist in -- there
is no condensation step to smuggle a stronger neighbor's confidence past
the weak edge needed to reach it.

`witness_path` reconstructs that path on demand at query time: a forward
BFS from the queried node, restricted to `CALLS` edges at the reported
confidence or better, that only accepts a direct-effect node as the chain's
end if ITS OWN confidence also clears that same tier -- the identical
eligibility test `propagate` used to assign the confidence in the first
place. Because `propagate` only ever assigns a confidence for which such a
path exists (edges AND direct effect both at or above that tier), this BFS
stays total -- it cannot come back empty for a `(node_id, kind,
confidence)` triple `propagate` actually produced. Total-by-construction
depends on this: the graph a confidence was derived from and the graph the
witness BFS traverses must stay identical, tier-eligibility rule included.

This module never queries the effect `Catalog`: it only reads `effects`
rows `detect_direct` already wrote, and `edges`.
"""

from __future__ import annotations

from collections import deque

from codegraph.resolve import CONFIDENCE_RANK, HIGH, LOW, MEDIUM, stronger
from codegraph.store import Store

#: Strongest first: the order `propagate` walks tiers in, since the first
#: (strongest) tier at which a node reaches an effect is the one that wins.
_TIERS = (HIGH, MEDIUM, LOW)
_RANK = CONFIDENCE_RANK


def propagate(store: Store, rev: str) -> int:
    """Write transitive (`direct=0`) effect rows for `rev`. Returns the
    count of rows written."""
    connection = store.connection
    connection.execute("DELETE FROM effects WHERE rev=? AND direct=0", (rev,))

    node_ids = {row["id"] for row in connection.execute("SELECT id FROM nodes WHERE rev=?", (rev,))}

    edge_confidence: dict[tuple[str, str], str] = {}
    for row in connection.execute(
        "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
    ):
        src, dst, confidence = row["src"], row["dst"], row["confidence"]
        if src not in node_ids or dst not in node_ids:
            continue
        key = (src, dst)
        edge_confidence[key] = stronger(edge_confidence.get(key, confidence), confidence)

    # reverse_by_tier[tier][dst] = every src with an edge src->dst whose
    # confidence is at least `tier`. These are nested supersets as the tier
    # weakens: the HIGH subgraph is a subset of MEDIUM-or-better, which is a
    # subset of LOW-or-better (everything).
    reverse_by_tier: dict[str, dict[str, set[str]]] = {tier: {} for tier in _TIERS}
    for (src, dst), confidence in edge_confidence.items():
        rank = _RANK[confidence]
        for tier in _TIERS:
            if _RANK[tier] <= rank:
                reverse_by_tier[tier].setdefault(dst, set()).add(src)

    direct_by_node: dict[str, dict[str, str]] = {}
    direct_nodes_by_kind: dict[str, set[str]] = {}
    for row in connection.execute(
        "SELECT node_id, kind, confidence FROM effects WHERE rev=? AND direct=1", (rev,)
    ):
        direct_by_node.setdefault(row["node_id"], {})[row["kind"]] = row["confidence"]
        direct_nodes_by_kind.setdefault(row["kind"], set()).add(row["node_id"])

    # node_confidence[node][kind] = the strongest tier at which `node` can
    # reach some node that carries `kind` directly, bounded by BOTH the
    # edges on the path AND that direct effect's own recorded confidence --
    # the weakest link in the whole chain, not just the CALLS-edge portion
    # of it. At tier T, a direct-effect node only counts as a valid
    # endpoint if its own confidence for `kind` is >= T: a HIGH chain of
    # edges into a LOW-confidence direct effect (e.g. a bare-wildcard-head
    # catalog match) must not report HIGH just because the edges were
    # strong -- the direct detection itself is the weak link there. At the
    # weakest tier (LOW), every direct node is eligible regardless of its
    # own confidence, so this is a strict narrowing of the old
    # edge-only behavior, never a loss of reachability.
    node_confidence: dict[str, dict[str, str]] = {}
    for tier in _TIERS:
        adjacency = reverse_by_tier[tier]
        tier_rank = _RANK[tier]
        for kind, sources in direct_nodes_by_kind.items():
            eligible = {s for s in sources if _RANK[direct_by_node[s][kind]] >= tier_rank}
            if not eligible:
                continue
            for node_id in _reverse_reachable(eligible, adjacency):
                bucket = node_confidence.setdefault(node_id, {})
                bucket.setdefault(kind, tier)

    rows: list[tuple] = []
    for node_id, kinds in node_confidence.items():
        own_direct = direct_by_node.get(node_id, {})
        for kind, confidence in kinds.items():
            if kind in own_direct:
                continue
            rows.append((rev, node_id, kind, 0, None, None, confidence))

    connection.executemany(
        "INSERT INTO effects(rev, node_id, kind, direct, evidence_path, evidence_line,"
        " confidence) VALUES(?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _reverse_reachable(sources: set[str], adjacency: dict[str, set[str]]) -> set[str]:
    """Every node that can reach some node in `sources` via the forward
    edges `adjacency` encodes in reverse (`adjacency[dst]` is every direct
    predecessor of `dst`). Multi-source BFS on the reverse graph; a plain
    visited set makes this correct in the presence of cycles without any
    extra bookkeeping."""
    visited = set(sources)
    queue: deque[str] = deque(sources)
    while queue:
        current = queue.popleft()
        for predecessor in adjacency.get(current, ()):
            if predecessor not in visited:
                visited.add(predecessor)
                queue.append(predecessor)
    return visited


def witness_path(store: Store, rev: str, node_id: str, kind: str, confidence: str) -> list[str]:
    """Fewest-hop chain of node ids from `node_id` to a node whose direct
    effect of `kind` causes it, restricted to `CALLS` edges strong enough
    to support `confidence` -- the value `propagate` already computed as
    the strongest tier at which `node_id` can reach an effect of this
    kind. Every edge on the chain has confidence >= `confidence`, AND the
    direct effect at the chain's end has its own confidence >= `confidence`
    too, so the chain's own bottleneck -- including the direct detection at
    the end of it, not just the edges leading there -- is never weaker than
    the number printed next to it. `[]` if no such chain exists (should not
    happen for a `(node_id, kind, confidence)` triple `propagate`
    produced)."""
    connection = store.connection
    direct_confidence: dict[str, str] = {}
    for row in connection.execute(
        "SELECT node_id, confidence FROM effects WHERE rev=? AND kind=? AND direct=1",
        (rev, kind),
    ):
        direct_confidence[row["node_id"]] = stronger(
            direct_confidence.get(row["node_id"], row["confidence"]), row["confidence"]
        )
    if node_id in direct_confidence:
        return [node_id]

    target_rank = _RANK[confidence]
    # A direct-effect node only counts as a valid witness endpoint if its
    # OWN confidence for `kind` is >= `confidence` -- the same eligibility
    # test `propagate` applied when it assigned this confidence, so this
    # BFS stays total for every triple `propagate` can actually produce.
    direct_nodes = {n for n, c in direct_confidence.items() if _RANK[c] >= target_rank}
    edge_confidence: dict[tuple[str, str], str] = {}
    for row in connection.execute(
        "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
    ):
        key = (row["src"], row["dst"])
        edge_confidence[key] = stronger(
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
