"""The `impact` report: everything downstream of a symbol that a change to
it could break, ranked by how urgently each dependent deserves review.

A reverse BFS over `edges(rev, dst)`, starting at the queried symbol and
walking every `resolve.DEPENDENCY_KINDS` edge backward to its callers,
subclasses, implementers and the code that merely names it, theirs in turn,
and so on up to `max_hops`. Each dependent is recorded the first time it is
reached (fewest hops), and among the edges available at that hop the
strongest achievable path confidence wins -- the same widest-path bias
`effects/propagate.py` uses for effect reachability, applied here to
dependents.

Every kind but CALLS is walked for the same reason: this report answers
"what could a change to this break", and a subclass, a structural
implementer of a Protocol and a line that hands the symbol to a library are
all broken by a changed signature exactly as a caller is. INHERITS came in
with #42 -- it used to walk CALLS alone while `rank.fan_in` counted both
kinds, so a base class with three subclasses ranked as having three
dependents and listed none. IMPLEMENTS and REFERENCES came in with #45.

Duplicate edge rows for the same `(src, dst)` pair (the same
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
from codegraph.resolve import (
    CONFIDENCE_RANK,
    DEPENDENCY_KINDS,
    HIGH,
    LOW,
    RUNTIME,
    stronger,
    weaker,
)
from codegraph.store import Store
from codegraph.trace import NO_TRACE
from codegraph.trace import summary as trace_summary
from codegraph.uncertainty import HOP_LIMIT, unknown

_RANK = CONFIDENCE_RANK

#: One edge into a node, as the walk holds it: the strongest confidence any
#: row for the pair carries, and whether a run was seen taking it.
_Link = tuple[str, bool]

#: How many LOW-confidence dependents the default report names before
#: falling back to a count. Small on purpose: the point is to let a reader
#: judge whether the hidden set is noise, not to list it -- `--all` does
#: that. Kept off the `dependents`/`tests` budget entirely, so turning a
#: bare count into an answer can never cost a production caller its row.
_LOW_SAMPLE = 5


def _reverse_edges(store: Store, rev: str) -> dict[str, dict[str, _Link]]:
    """dst -> {src: (confidence, observed)}, one entry per (src, dst) pair at
    its strongest confidence -- duplicate edge rows collapsed before the walk.

    A pair a trace confirmed holds two rows, and they are not competing
    answers: the strongest confidence among them is the pair's, and the
    observation is a separate bit alongside it. It cannot be folded into the
    tier, because "certain the name means this" and "a run did this" are the
    two axes #56 exists to keep apart -- and only the second licenses the
    word the rows print.
    """
    edge_confidence: dict[tuple[str, str], str] = {}
    observed: set[tuple[str, str]] = set()
    marks = ",".join("?" * len(DEPENDENCY_KINDS))
    for row in store.connection.execute(
        f"SELECT src, dst, confidence, provenance FROM edges WHERE rev=? AND kind IN ({marks})",
        (rev, *DEPENDENCY_KINDS),
    ):
        key = (row["src"], row["dst"])
        edge_confidence[key] = stronger(
            edge_confidence.get(key, row["confidence"]), row["confidence"]
        )
        if row["provenance"] == RUNTIME:
            observed.add(key)

    reverse: dict[str, dict[str, _Link]] = {}
    for (src, dst), confidence in edge_confidence.items():
        reverse.setdefault(dst, {})[src] = (confidence, (src, dst) in observed)
    return reverse


def _predecessors(
    reverse: dict[str, dict[str, _Link]], ambiguity: Ambiguity, node_id: str
) -> dict[str, _Link]:
    """Every caller and subclass of `node_id`, materialized and derived
    alike, at the strongest confidence any of them claims.

    The derived half is the bare-name fan-out the graph deliberately does
    not store, expanded here for this one node -- ambiguous calls and
    ambiguous base references both, always LOW, never observed (a trace
    names the callee outright, so an observation is never ambiguous), and
    never strengthening a materialized edge that already reaches the same
    node.
    """
    predecessors = dict(reverse.get(node_id, {}))
    for src in ambiguity.callers(node_id):
        predecessors.setdefault(src, (LOW, False))
    for src in ambiguity.inheritors(node_id):
        predecessors.setdefault(src, (LOW, False))
    return predecessors


def _walk(
    reverse: dict[str, dict[str, _Link]],
    ambiguity: Ambiguity,
    node_id: str,
    max_hops: int,
) -> tuple[dict[str, tuple[int, str, bool]], bool]:
    """Reverse BFS from `node_id`: node -> (hop, path confidence, observed),
    each node recorded once at its shortest hop, with the strongest
    confidence achievable among the edges reaching it at that hop -- and
    whether the budget, rather than the graph, is what stopped it.

    `observed` composes with AND along a chain, the way confidence composes
    with `weaker`: a path was watched running only if every hop of it was
    (#56). Anything looser puts the report's strongest word next to a
    dependent whose connection to the queried symbol is still an inference.

    The second return value is the point of #55. A walk that exhausted the
    graph and a walk that ran out of hops print the identical report, and
    the second one is not the answer it looks like: "these six things depend
    on it" is a different claim from "these six, and I stopped looking". It
    costs one further expansion of the final frontier to tell them apart,
    and the question is only asked once per report, so it is paid here
    rather than approximated by "the frontier was non-empty" -- a frontier
    can be non-empty and have nothing unvisited behind it, which would
    report a complete walk as truncated on every cyclic graph.
    """
    found: dict[str, tuple[int, str, bool]] = {}
    level: dict[str, _Link] = {node_id: (HIGH, True)}
    current_level = {node_id}
    visited = {node_id}
    hop = 0
    while current_level and hop < max_hops:
        next_level: dict[str, _Link] = {}
        for current in current_level:
            path_confidence, path_observed = level[current]
            predecessors = _predecessors(reverse, ambiguity, current)
            for src, (edge_confidence, edge_observed) in predecessors.items():
                if src in visited:
                    continue
                candidate = (
                    weaker(path_confidence, edge_confidence),
                    path_observed and edge_observed,
                )
                best = next_level.get(src)
                # Strongest confidence first, exactly as before; between two
                # equally confident ways to the same node, the watched one.
                if best is None or _stronger_link(candidate, best):
                    next_level[src] = candidate
        hop += 1
        for src, (confidence, observed) in next_level.items():
            visited.add(src)
            found[src] = (hop, confidence, observed)
        level = next_level
        current_level = set(next_level)
    truncated = any(
        src not in visited
        for current in current_level
        for src in _predecessors(reverse, ambiguity, current)
    )
    return found, truncated


def _stronger_link(candidate: _Link, best: _Link) -> bool:
    return (_RANK[candidate[0]], candidate[1]) > (_RANK[best[0]], best[1])


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
    """Everything reachable from `node_id` by walking `DEPENDENCY_KINDS`
    edges backward, ranked by `rank.score` and split into `dependents` and
    `tests` groups."""
    connection = store.connection
    ambiguity = Ambiguity(store, rev)
    reverse = _reverse_edges(store, rev)
    found, hop_limited = _walk(reverse, ambiguity, node_id, max_hops)

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

    for dependent_id, (hop, confidence, observed) in found.items():
        info = node_info.get(dependent_id)
        if info is None:
            # Should not happen: every edge endpoint owns a node row for a
            # revision that resolve.py just resolved.
            continue
        path, qualname, line_start = info

        salience_value = salience(store, rev, dependent_id, ambiguity)
        # Appended, never substituted for the tier: the two answer different
        # questions and the reader wants both. That is #56 in one string.
        detail = f"hop {hop}, {confidence} confidence"
        if observed:
            detail += ", observed"
        row = Row(
            id=dependent_id,
            location=f"{path}:{line_start}",
            detail=detail,
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
    # Only when a trace exists. `islands` and `orphans` say either way,
    # because their finding is an absence and a reader has to know how hard
    # it was looked for; this report's rows are presences, so a revision
    # with no trace prints the summary it printed before #56, field for
    # field.
    traced = trace_summary(store, rev)
    if traced != NO_TRACE:
        summary["trace"] = traced

    return Report(
        summary=summary,
        groups=groups,
        truncated=truncated,
        # The one hole this report can name. `low_confidence_hidden` is
        # deliberately not one: those dependents were walked, ranked and
        # counted, the strongest are on the page and the count says how many
        # are not, with `show_hidden: --all` beside it. A hop budget is the
        # other thing entirely -- the callers past it were never looked at,
        # and nothing in the report says so. See `uncertainty` for the rule.
        unknowns=(
            [
                unknown(
                    HOP_LIMIT,
                    f"the walk still had unvisited dependents at its {max_hops}-hop budget",
                )
            ]
            if hop_limited
            else []
        ),
    )


def hits_hop_limit(store: Store, rev: str, node_id: str, max_hops: int) -> bool:
    """Would an `impact` walk of `max_hops` stop on its budget rather than
    on the graph?

    The same walk `impact_report` runs, so the two can never disagree about
    one symbol -- `query/unknowns.py` reports this about a symbol without
    printing the dependents, and a second implementation of "did it finish"
    would be a claim about a walk nobody ran.
    """
    ambiguity = Ambiguity(store, rev)
    return _walk(_reverse_edges(store, rev), ambiguity, node_id, max_hops)[1]


__all__ = ["hits_hop_limit", "impact_report"]
