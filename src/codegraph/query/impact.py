"""The `impact` report: everything downstream of a symbol that a change to
it could break, ranked by how urgently each dependent deserves review.

A reverse BFS over `edges(rev, dst)`, starting at the queried symbol and
walking CALLS edges backward to its callers, its callers' callers, and so
on up to `max_hops`. Each dependent is recorded the first time it is
reached (fewest hops), and among the edges available at that hop the
strongest achievable path confidence wins -- the same widest-path bias
`effects/propagate.py` uses for effect reachability, applied here to
dependents. Duplicate edge rows for the same `(src, dst)` pair (the same
call written twice in a body, or the same candidate reached through two
import aliases) collapse to a single edge of their strongest confidence
before the walk starts, so they can never count as two distinct hops or
inflate `rank.salience`'s fan-in term.

Rows whose path starts with `tests/` or whose qualname's last segment
starts with `test_` are split into their own `tests` group -- a change
breaking a test is worth knowing, but it should never crowd out production
callers in the ranked list. `LOW`-confidence dependents are counted in the
summary but held back from both groups unless `include_low` is set: the
resolver's least certain guesses are real information, but they should not
read as confirmed impact by default.
"""

from __future__ import annotations

from codegraph.query.rank import fan_in, salience, score
from codegraph.render import Group, Report, Row, budget
from codegraph.resolve import HIGH, LOW, MEDIUM
from codegraph.store import Store

_RANK = {LOW: 0, MEDIUM: 1, HIGH: 2}


def _stronger(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def _weaker(a: str, b: str) -> str:
    return a if _RANK[a] <= _RANK[b] else b


def _reverse_edges(store: Store, rev: str) -> dict[str, dict[str, str]]:
    """dst -> {src: confidence}, one entry per (src, dst) pair at its
    strongest confidence -- duplicate edge rows collapsed before the walk."""
    edge_confidence: dict[tuple[str, str], str] = {}
    for row in store.connection.execute(
        "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
    ):
        key = (row["src"], row["dst"])
        edge_confidence[key] = _stronger(
            edge_confidence.get(key, row["confidence"]), row["confidence"]
        )

    reverse: dict[str, dict[str, str]] = {}
    for (src, dst), confidence in edge_confidence.items():
        reverse.setdefault(dst, {})[src] = confidence
    return reverse


def _walk(
    reverse: dict[str, dict[str, str]], node_id: str, max_hops: int
) -> dict[str, tuple[int, str]]:
    """Reverse BFS from `node_id`: node -> (hop, path confidence), each node
    recorded once at its shortest hop, with the strongest confidence
    achievable among the edges reaching it at that hop."""
    found: dict[str, tuple[int, str]] = {}
    level_confidence: dict[str, str] = {node_id: HIGH}
    current_level = {node_id}
    visited = {node_id}
    hop = 0
    while current_level and hop < max_hops:
        next_level: dict[str, str] = {}
        for current in current_level:
            path_confidence = level_confidence[current]
            for src, edge_confidence in reverse.get(current, {}).items():
                if src in visited:
                    continue
                candidate = _weaker(path_confidence, edge_confidence)
                best = next_level.get(src)
                if best is None or _RANK[candidate] > _RANK[best]:
                    next_level[src] = candidate
        hop += 1
        for src, confidence in next_level.items():
            visited.add(src)
            found[src] = (hop, confidence)
        level_confidence = next_level
        current_level = set(next_level)
    return found


def _is_test(path: str, qualname: str) -> bool:
    return path.startswith("tests/") or qualname.rpartition(".")[2].startswith("test_")


def impact_report(
    store: Store,
    rev: str,
    node_id: str,
    max_hops: int = 3,
    limit: int = 40,
    include_low: bool = False,
) -> Report:
    """Everything reachable from `node_id` by walking CALLS edges backward,
    ranked by `rank.score` and split into `dependents` and `tests` groups."""
    connection = store.connection
    reverse = _reverse_edges(store, rev)
    found = _walk(reverse, node_id, max_hops)

    node_info: dict[str, tuple[str, str, int]] = {}
    if found:
        placeholders = ",".join("?" * len(found))
        for row in connection.execute(
            f"SELECT id, path, qualname, line_start FROM nodes WHERE rev=? AND id IN ({placeholders})",
            (rev, *found),
        ):
            node_info[row["id"]] = (row["path"], row["qualname"], row["line_start"])

    dependent_rows: list[Row] = []
    test_rows: list[Row] = []
    low_confidence_hidden = 0
    entry_points = 0
    modules: set[str] = set()

    for dependent_id, (hop, confidence) in found.items():
        info = node_info.get(dependent_id)
        if info is None:
            # Should not happen: every edge endpoint owns a node row for a
            # revision that resolve.py just resolved.
            continue
        path, qualname, line_start = info

        if confidence == LOW and not include_low:
            low_confidence_hidden += 1
            continue

        salience_value = salience(store, rev, dependent_id)
        if fan_in(store, rev, dependent_id) == 0:
            entry_points += 1
        modules.add(path)

        row = Row(
            id=dependent_id,
            location=f"{path}:{line_start}",
            detail=f"hop {hop}, {confidence} confidence",
            score=score(hop, confidence, salience_value),
        )
        if _is_test(path, qualname):
            test_rows.append(row)
        else:
            dependent_rows.append(row)

    kept, truncated = budget(dependent_rows, limit)
    groups = [Group("dependents", kept)] if kept else []
    if test_rows:
        kept_tests, tests_truncated = budget(test_rows, limit)
        groups.append(Group("tests", kept_tests))
        truncated = truncated or tests_truncated

    effects_reachable = sorted(
        {
            row["kind"]
            for row in connection.execute(
                "SELECT DISTINCT kind FROM effects WHERE rev=? AND node_id=?", (rev, node_id)
            )
        }
    )

    summary = {
        "symbols": len(dependent_rows) + len(test_rows),
        "modules": len(modules),
        "entry_points": entry_points,
        "low_confidence_hidden": low_confidence_hidden,
        "effects_reachable": effects_reachable,
    }

    return Report(summary=summary, groups=groups, truncated=truncated)


__all__ = ["impact_report"]
