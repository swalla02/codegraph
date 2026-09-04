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
callers in the ranked list.

`LOW`-confidence dependents get a third group of their own. They must not
read as confirmed impact, so they never enter `dependents` or `tests`
unless `include_low` is set -- but a bare `low_confidence_hidden: 235`
was worse than useless: it told the reader something was there and gave
them no way to find out whether it was 235 pieces of noise or the one
caller that mattered, when this module had already ranked them and could
simply have said. So the strongest few are printed, in a group labelled
for what they are, on a budget of their own that cannot eat into the
production callers'. `low_confidence_hidden` now counts only what is
genuinely not on the page, and `--all` (`include_low`) still merges the
whole set into the main groups. See #25. A nonzero count carries
`show_hidden: --all` beside it, because a count with no way to see what it
counts is the same footgun one level down (#37).

The LOW set itself is not read from `edges`. The bare-name fan-out is
never materialized (see `ambiguity.py`); it is expanded here, at
each hop of the walk, through the same live name index the resolver used.
That makes this report strictly more complete than the stored graph: a
call site matching 971 definitions contributed *nothing* to `impact`
before, because the graph declined to enumerate it.
"""

from __future__ import annotations

from codegraph.ambiguity import Ambiguity
from codegraph.query.rank import fan_in, salience, score
from codegraph.render import Group, Report, Row, budget
from codegraph.resolve import CONFIDENCE_RANK, HIGH, LOW, stronger, weaker
from codegraph.store import Store

_RANK = CONFIDENCE_RANK

#: How many LOW-confidence dependents the default report names before
#: falling back to a count. Small on purpose: the point is to let a reader
#: judge whether the hidden set is noise, not to list it -- `--all` does
#: that. Kept off the `dependents`/`tests` budget entirely, so turning a
#: bare count into an answer can never cost a production caller its row.
_LOW_SAMPLE = 5


def _reverse_edges(store: Store, rev: str) -> dict[str, dict[str, str]]:
    """dst -> {src: confidence}, one entry per (src, dst) pair at its
    strongest confidence -- duplicate edge rows collapsed before the walk."""
    edge_confidence: dict[tuple[str, str], str] = {}
    for row in store.connection.execute(
        "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
    ):
        key = (row["src"], row["dst"])
        edge_confidence[key] = stronger(
            edge_confidence.get(key, row["confidence"]), row["confidence"]
        )

    reverse: dict[str, dict[str, str]] = {}
    for (src, dst), confidence in edge_confidence.items():
        reverse.setdefault(dst, {})[src] = confidence
    return reverse


def _predecessors(
    reverse: dict[str, dict[str, str]], ambiguity: Ambiguity, node_id: str
) -> dict[str, str]:
    """Every caller of `node_id`, materialized and derived alike, at the
    strongest confidence any of them claims.

    The derived half is the bare-name fan-out the graph deliberately does
    not store, expanded here for this one node -- always LOW, and never
    strengthening a materialized edge that already reaches the same caller.
    """
    callers = dict(reverse.get(node_id, {}))
    for src in ambiguity.callers(node_id):
        callers.setdefault(src, LOW)
    return callers


def _walk(
    reverse: dict[str, dict[str, str]],
    ambiguity: Ambiguity,
    node_id: str,
    max_hops: int,
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
            for src, edge_confidence in _predecessors(reverse, ambiguity, current).items():
                if src in visited:
                    continue
                candidate = weaker(path_confidence, edge_confidence)
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
    ambiguity = Ambiguity(store, rev)
    reverse = _reverse_edges(store, rev)
    found = _walk(reverse, ambiguity, node_id, max_hops)

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
    low_rows: list[Row] = []
    entry_points = 0
    modules: set[str] = set()

    for dependent_id, (hop, confidence) in found.items():
        info = node_info.get(dependent_id)
        if info is None:
            # Should not happen: every edge endpoint owns a node row for a
            # revision that resolve.py just resolved.
            continue
        path, qualname, line_start = info

        salience_value = salience(store, rev, dependent_id, ambiguity)
        row = Row(
            id=dependent_id,
            location=f"{path}:{line_start}",
            detail=f"hop {hop}, {confidence} confidence",
            score=score(hop, confidence, salience_value),
        )

        if confidence == LOW and not include_low:
            # Ranked, but kept out of the counted `symbols`/`modules`
            # totals and out of `entry_points`: those describe impact the
            # report is willing to stand behind, and the whole reason this
            # group exists separately is that a LOW row is not that.
            low_rows.append(row)
            continue

        if fan_in(store, rev, dependent_id, ambiguity) == 0:
            entry_points += 1
        modules.add(path)

        if _is_test(path, qualname):
            test_rows.append(row)
        else:
            dependent_rows.append(row)

    # `limit` is a TOTAL budget across both groups, not `limit` rows each --
    # `dependents` gets first claim on it (production callers should never
    # be crowded out by tests), and whatever's left over budgets `tests`.
    kept, truncated = budget(dependent_rows, limit)
    groups = [Group("dependents", kept)] if kept else []
    remaining = limit - len(kept)
    if test_rows:
        kept_tests, tests_truncated = budget(test_rows, remaining)
        if kept_tests:
            groups.append(Group("tests", kept_tests))
        truncated = truncated or tests_truncated

    # The LOW group is budgeted LAST and separately, on `_LOW_SAMPLE` rather
    # than on whatever is left of `limit`: it exists to make the count
    # actionable, not to compete with the callers the report is confident
    # about. `--limit 5` still means five production dependents.
    low_confidence_hidden = len(low_rows)
    if low_rows:
        kept_low, _ = budget(low_rows, min(_LOW_SAMPLE, limit))
        if kept_low:
            groups.append(Group("low_confidence", kept_low))
            low_confidence_hidden -= len(kept_low)

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
        # Only what is NOT on the page. `low_confidence` above holds the
        # strongest of them, so 0 here now means "all of them are listed",
        # not "there were none" -- the group's presence says which.
        "low_confidence_hidden": low_confidence_hidden,
        # ...and, when there IS something hidden, how to see it. A count of
        # what is missing with no way to look at it is the footgun #37 names:
        # answering "is anything still depending on this?" requires already
        # knowing that `--all` exists, and the likeliest reader of a nonzero
        # count here is the one who does not.
        #
        # Conditional, and spliced in right beside the count rather than
        # appended: the summary line is dense enough that a permanent field
        # for a number that is usually 0 would cost every other reader, and a
        # hint that renders three fields away from the count it explains is
        # not next to it in any sense the reader cares about. A separate
        # string field rather than folding the flag into the value
        # ("235 (--all)") because `low_confidence_hidden` is an int in
        # `--json`, and machine-readable output is entitled to stay so.
        **({"show_hidden": "--all"} if low_confidence_hidden else {}),
        "effects_reachable": effects_reachable,
    }

    return Report(summary=summary, groups=groups, truncated=truncated)


__all__ = ["impact_report"]
