"""The `path` report: how two named symbols are connected, or which of the
three ways they are not.

Every other query starts at one symbol and fans out. `impact` walks
dependents backward, `effects` walks downstream, `islands` takes no symbol
at all. None of them answers the question an agent asks once it already has
two names in hand -- *how are these two related?* -- and the workaround,
running `impact` on one and reading the other out of the rows, fails
exactly where the question gets interesting: a long chain, a chain past
`--hops`, or a row that `--limit` crowded out. A path query has none of
those failure modes, because it is looking for one chain rather than
ranking a frontier.

## Direction is the answer, not a detail

"A and B are connected" is three different facts, and which one holds
changes what a reader should do. If A reaches B, editing B is what is
risky. If B reaches A, editing A is. If each reaches the other they are in
a cycle and both are. So both directions are always walked, the report
names which one it found, and the direction it did *not* find is stated
too -- `reverse: none` is what tells a reader the tool looked, rather than
leaving them to wonder.

`forward` throughout means the direction the arguments were written in --
`from` reaches `to`, along the edges, the way a caller reaches a callee.
The rows of either group always read along the edges, so the first row is
whichever symbol does the reaching.

## Shortest first, then strongest, then an arbitrary but total order

The default answer is a shortest path. Among paths of equal length the
strongest wins, where a path's confidence is its weakest hop -- the same
widest-path bias `effects/propagate.py` and `impact.py` already apply to
reachability, for the same reason: of two equally direct explanations the
better-evidenced one is the better answer. Length comes first, though. A
stronger five-hop path is not a better answer to "how are these connected"
than a weaker two-hop one; it is an answer to a different question.

That leaves ties, and ties have to break the same way on every run --
`islands.py` records what happens when they do not, and the answer is the
same here: the order SQLite hands back rows is stable for one database file
and is not a contract across a rebuild. So equal-length, equal-confidence
paths are ordered by their node-id sequence, lexicographically. That rule
is arbitrary in the sense that no path is *better* for satisfying it, and
that is the point -- it is total, it is derived from the graph rather than
from the storage, and two runs of one command print one report.

## Three negative answers, in order of strength

A single "not found" would be the confidently-wrong answer this project
keeps refusing to print, because the three reasons call for three different
next moves:

1. *No path within `--hops`.* A chain exists and is longer than the budget.
   The report says how long, so the reader can raise `--hops` instead of
   concluding there is nothing there. A count of what you cannot see with
   no way to see it is half an answer -- the same reasoning behind
   `impact`'s `show_hidden` (#37).
2. *No directed path in either direction.* Nothing connects them as a
   walk, though something may still relate them: a common caller, or a
   file's top level reaching both. Where LOW hops were excluded, a LOW
   chain may still exist, and `show_path: --all` says so.
3. *Different islands.* No walk in any direction at any confidence can
   ever connect them. This is the strongest negative available and it is
   not recomputed here: `islands.connected_components` is the same
   partition the `islands` command prints, so this answer and that report
   cannot come to disagree.

The three are tried in the opposite order to their strength, which is
deliberate. The island partition folds in the bare-name fan-out through
per-name hubs, while this walk expands it pointwise through
`Ambiguity.candidates`, and the two differ in one corner (a hub does not
link a class's deferred fan-out to the `__init__` that instantiating it
runs, while `candidates` does). Consulting the partition last means the
strongest claim is only ever printed when this command's own walk has
already come back empty -- so `path` can never tell a reader that no walk
exists while a walk it could itself perform does.

## Three calls this module makes about what counts as a hop

*The bare-name fan-out participates, on the same switch as every other LOW
hop.* `impact` expands it because a call site matching 971 definitions
contributed nothing to the stored graph; a path query has the same claim on
it. The fan-out is LOW by construction, so `--all` is already the flag that
governs it, and no second switch is needed: by default a path is
MEDIUM-or-better and made of edges that are really in the database, and
with `--all` it may run through a name the resolver could not pin down.
Such a hop names the bare name it went through, because "these are
connected" through `item.save()` where two classes define `save` is a claim
a reader has to be able to check for themselves.

*Hops carry `effects`-style witness detail, because it is already paid
for.* `edges` stores `callsite_path` and `callsite_line`, so the exact line
that makes each hop costs nothing to report, and a hop that names it is
evidence rather than an assertion -- the distinction `effects`' witness
chains exist for. A derived hop's site is not in `edges` at all; it is read
back from `unresolved`, one small query per hop, and only for the hops of
the path actually printed. `location` stays what it means in every other
report (where the row's own symbol is defined) and the call site rides in
`detail`, exactly as `effects` puts its chain there.

*A `path::<module>` node can only ever be an origin.* Nothing in the graph
points at one -- 0 of psf/requests' 1,671 edges have a module node as
`dst`, against 20 that have one as `src` -- because a module's top level
calls but is never called. So the question of a path *through* one does not
arise: it can appear as the first row, when the user names one, and there
it is labelled for what it is rather than printed as a symbol somebody
wrote. Where module nodes do matter is the negative answer, and they are
the reason answer 2 exists separately from answer 3: `islands` unions them
for connectivity while never counting them as members, so two functions
their file's top level both calls are one island with no path between them.
Reporting "different islands" there would be false, and reporting "no path"
without the distinction would hide a real coupling.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from codegraph.ambiguity import Ambiguity, last_segment
from codegraph.query.islands import connected_components
from codegraph.render import Group, Report, Row
from codegraph.resolve import (
    AMBIGUOUS,
    CALLS,
    CONFIDENCE_RANK,
    DEPENDENCY_KINDS,
    HIGH,
    INHERITS,
    LOW,
    weaker,
)
from codegraph.store import Store
from codegraph.uncertainty import HOP_LIMIT, LOW_CONFIDENCE, unknown

_RANK = CONFIDENCE_RANK

#: Where a hop budget stops being a safety rail and starts being the
#: answer. `impact` defaults to 3 because its walk is a frontier that
#: widens at every hop; this one follows a single chain, so its cost is the
#: graph rather than the branching factor and it can afford to look
#: further. The number matters because the wrong default turns the useful
#: answer ("they connect, at five hops") into the useless one ("no path
#: within 3 hops"). Measured on psf/requests over 3,000 random symbol
#: pairs, of which 99 are connected at all: median 3 hops, mean 3.26, max
#: 8. `--hops 3` would find 51% of them and 6 finds 99%, which is where the
#: curve flattens -- 7 and 8 buy one pair between them. Raising it costs
#: nothing but time, and the report says when raising it would help.
DEFAULT_HOPS = 6

#: The two directions, and the summary/group vocabulary for them. `forward`
#: is always the direction the arguments were written in.
FORWARD, REVERSE = "forward", "reverse"

#: Tie-break order for two edge rows describing the same (src, dst) pair --
#: the same call written twice, or one relationship recorded under two
#: kinds. Strongest confidence first, then this order, so a pair that is
#: both a CALLS and a REFERENCES prints as the call it is.
_KIND_ORDER = {kind: rank for rank, kind in enumerate(DEPENDENCY_KINDS)}

#: One edge of the walk, as the adjacency holds it: kind, confidence, and
#: the `file:line` that makes it. A tuple rather than a dataclass because
#: there is one per distinct (src, dst) pair in the revision -- ~330k of
#: them on django -- and this is the report's whole resident footprint.
_Edge = tuple[str, str, str]


@dataclass(frozen=True)
class _Hop:
    """One step of a path: what it arrived at, and on what evidence.

    `via` is the bare name a derived hop went through and is empty for a
    materialized edge -- which is also how the two are told apart, since a
    derived hop is a relationship the graph deliberately does not store
    (see `ambiguity.py`) and a row must not present it as one it does.
    """

    dst: str
    kind: str
    confidence: str
    callsite: str
    via: str = ""


def _forward_edges(store: Store, rev: str) -> dict[str, dict[str, _Edge]]:
    """src -> {dst: (kind, confidence, call site)}, one entry per (src, dst)
    pair -- duplicate edge rows collapsed before the walk, exactly as
    `impact._reverse_edges` collapses them for the reverse direction, so
    the same relationship written twice can never read as two hops.

    One pass over `edges`, never a query per node: the walk can visit the
    whole graph, and a lookup per node inside a BFS is the cost of the
    command.
    """
    best: dict[tuple[str, str], _Edge] = {}
    marks = ",".join("?" * len(DEPENDENCY_KINDS))
    for row in store.connection.execute(
        "SELECT src, dst, kind, confidence, callsite_path, callsite_line FROM edges"
        f" WHERE rev=? AND kind IN ({marks})",
        (rev, *DEPENDENCY_KINDS),
    ):
        key = (row["src"], row["dst"])
        edge: _Edge = (
            row["kind"],
            row["confidence"],
            f"{row['callsite_path']}:{row['callsite_line']}",
        )
        current = best.get(key)
        if current is None or _edge_order(edge) < _edge_order(current):
            best[key] = edge

    forward: dict[str, dict[str, _Edge]] = {}
    for (src, dst), edge in best.items():
        forward.setdefault(src, {})[dst] = edge
    return forward


def _edge_order(edge: _Edge) -> tuple[int, int, str]:
    kind, confidence, callsite = edge
    return (-_RANK[confidence], _KIND_ORDER[kind], callsite)


def _derived_names(ambiguity: Ambiguity) -> dict[str, list[tuple[str, str]]]:
    """src -> the (bare name, kind) pairs whose fan-out that node makes.

    The forward direction of `Ambiguity.callers`/`inheritors`, which
    `impact` walks backward. The two have to describe one graph -- every
    confidence this project has got wrong has been a claim derived from one
    graph and traversed on another, which is why `Ambiguity.reaching_names`
    exists at all -- so the targets come from `Ambiguity.candidates`, the
    same set the reverse direction is built from.
    """
    derived: dict[str, set[tuple[str, str]]] = {}
    for refs, kind in ((ambiguity.call_refs, CALLS), (ambiguity.base_refs, INHERITS)):
        for name, sources in refs.items():
            for src in sources:
                derived.setdefault(src, set()).add((name, kind))
    # Sorted, so the walk visits a node's deferred names in one order on
    # every run even before the tie-break below has anything to say.
    return {src: sorted(names) for src, names in derived.items()}


def _successors(
    forward: dict[str, dict[str, _Edge]],
    derived: dict[str, list[tuple[str, str]]],
    ambiguity: Ambiguity,
    node_id: str,
    include_low: bool,
) -> Iterator[_Hop]:
    """Every node one hop along from `node_id`, materialized then derived.

    A derived hop never shadows a materialized edge to the same node, which
    is `impact._predecessors`' `setdefault` in the forward direction: the
    graph's own answer about a pair is always the better evidence.
    """
    seen: set[str] = set()
    for dst, (kind, confidence, callsite) in forward.get(node_id, {}).items():
        if confidence == LOW and not include_low:
            continue
        seen.add(dst)
        yield _Hop(dst, kind, confidence, callsite)
    if not include_low:
        # The fan-out is LOW by construction, so excluding LOW excludes it.
        # That is the whole of the decision described in the module
        # docstring: one switch, not two.
        return
    for name, kind in derived.get(node_id, ()):
        for dst in ambiguity.candidates(name):
            if dst in seen:
                continue
            seen.add(dst)
            yield _Hop(dst, kind, LOW, "", via=name)


def _confidence(path: tuple[_Hop, ...]) -> str:
    """A path is only as strong as its weakest hop."""
    strength = HIGH
    for hop in path:
        strength = weaker(strength, hop.confidence)
    return strength


def _path_order(path: tuple[_Hop, ...]) -> tuple[int, tuple[str, ...]]:
    """The comparison two candidate paths of equal length are settled by:
    strongest first, then the node-id sequence. See the module docstring on
    why the second half is arbitrary and has to exist anyway."""
    return (-_RANK[_confidence(path)], tuple(hop.dst for hop in path))


def _walk(
    forward: dict[str, dict[str, _Edge]],
    derived: dict[str, list[tuple[str, str]]],
    ambiguity: Ambiguity,
    start: str,
    target: str,
    max_hops: int | None,
    include_low: bool,
) -> tuple[_Hop, ...] | None:
    """The best path from `start` to `target`, or None within `max_hops`
    (`None` meaning no bound at all).

    Level-synchronous BFS, so every candidate for a node at hop N has been
    considered before hop N+1 begins -- which is what makes it safe to stop
    the moment `target` appears, and what makes each node's kept path final
    when it does. Keeping only the strongest path to a node can never lose
    the strongest path through it: a path's confidence is the minimum over
    its hops, and a minimum cannot be raised by extending a weaker prefix.
    """
    if start == target:
        return ()
    current: dict[str, tuple[_Hop, ...]] = {start: ()}
    visited = {start}
    hops = 0
    while current and (max_hops is None or hops < max_hops):
        level: dict[str, tuple[_Hop, ...]] = {}
        for node_id, trail in current.items():
            for hop in _successors(forward, derived, ambiguity, node_id, include_low):
                if hop.dst in visited:
                    continue
                candidate = (*trail, hop)
                incumbent = level.get(hop.dst)
                if incumbent is None or _path_order(candidate) < _path_order(incumbent):
                    level[hop.dst] = candidate
        hops += 1
        visited.update(level)
        if target in level:
            return level[target]
        current = level
    return None


def _shortest_distance(
    forward: dict[str, dict[str, _Edge]],
    derived: dict[str, list[tuple[str, str]]],
    ambiguity: Ambiguity,
    from_id: str,
    to_id: str,
    include_low: bool,
) -> int | None:
    """How many hops apart the two are with no hop budget at all, in
    whichever direction is shorter -- the probe that tells "there is a path
    and you did not ask for enough hops" apart from "there is no path".
    """
    lengths = [
        len(path)
        for path in (
            _walk(forward, derived, ambiguity, from_id, to_id, None, include_low),
            _walk(forward, derived, ambiguity, to_id, from_id, None, include_low),
        )
        if path is not None
    ]
    return min(lengths) if lengths else None


def _node_info(store: Store, rev: str, node_ids: list[str]) -> dict[str, tuple[str, str]]:
    """id -> (`path:line`, node kind) for the handful of ids a path names."""
    if not node_ids:
        return {}
    marks = ",".join("?" * len(node_ids))
    return {
        row["id"]: (f"{row['path']}:{row['line_start']}", row["kind"])
        for row in store.connection.execute(
            f"SELECT id, path, kind, line_start FROM nodes WHERE rev=? AND id IN ({marks})",
            (rev, *node_ids),
        )
    }


def _derived_callsite(store: Store, rev: str, src: str, name: str) -> str:
    """The first line at which `src` makes its ambiguous reference to
    `name`.

    `Ambiguity` collapses these rows to distinct source nodes, because a
    report ranking thousands of them cannot afford to carry a line for each
    (see its `call_refs`). A printed hop needs exactly one, so it is read
    back here -- at most one query per hop of one path, and only for the
    path that is actually printed.
    """
    best: tuple[str, int] | None = None
    for row in store.connection.execute(
        "SELECT path, line, raw_name FROM unresolved WHERE rev=? AND reason=? AND src=?",
        (rev, AMBIGUOUS, src),
    ):
        if last_segment(row["raw_name"]) != name:
            continue
        if best is None or row["line"] < best[1]:
            best = (row["path"], row["line"])
    return f"{best[0]}:{best[1]}" if best else ""


def _weakest_hop(path: tuple[_Hop, ...]) -> int:
    """Which hop (1-based) sets the path's confidence, or 0 when pointing
    at one would say nothing.

    A path whose hops are all the same tier has no weak link to name, and
    labelling the first hop "weakest" there would be noise. A mixed path
    does, and it is the actionable part of the report: four HIGH hops and
    one LOW is a chain with one place to go and look.
    """
    if len(path) < 2 or len({hop.confidence for hop in path}) == 1:
        return 0
    weakest = _confidence(path)
    return next(index for index, hop in enumerate(path, start=1) if hop.confidence == weakest)


def _rows(store: Store, rev: str, start: str, path: tuple[_Hop, ...]) -> list[Row]:
    """One row per node along the path, origin first.

    Deliberately not `render.budget`ed: a path is its own budget (it can
    never exceed `--hops` rows), and these rows are a sequence rather than
    a ranking -- reordering them by score would destroy the only thing they
    mean. `score` descends along the chain anyway, so a consumer that sorts
    by it out of habit gets the walk order back.
    """
    info = _node_info(store, rev, [start, *(hop.dst for hop in path)])
    location, kind = info.get(start, ("", ""))
    # A module node is the one origin that is not a symbol anybody wrote.
    # It can never be an interior hop (nothing in the graph points at one),
    # so this is the only place the distinction can arise.
    origin = "start, this file's module top level" if kind == "module" else "start"
    rows = [Row(id=start, location=location, detail=origin, score=float(len(path)))]

    weakest = _weakest_hop(path)
    source = start
    for index, hop in enumerate(path, start=1):
        callsite = hop.callsite or (
            _derived_callsite(store, rev, source, hop.via) if hop.via else ""
        )
        detail = f"hop {index}, {hop.kind}"
        if hop.via:
            detail += f" via the bare name {hop.via!r}"
        detail += f", {hop.confidence} confidence"
        if callsite:
            detail += f", call site {callsite}"
        if index == weakest:
            detail += f" -- weakest hop, the path is {hop.confidence}"
        rows.append(
            Row(
                id=hop.dst,
                location=info.get(hop.dst, ("", ""))[0],
                detail=detail,
                score=float(len(path) - index),
            )
        )
        source = hop.dst
    return rows


def _plural_hops(count: int) -> str:
    return "1 hop" if count == 1 else f"{count} hops"


def _basis(max_hops: int, include_low: bool) -> str:
    """What the walk was, spelled out beside its answer -- so a negative is
    read against the edges it was looked for over, the way `islands`'
    `basis` field keeps an island from reading as "nothing reaches this"."""
    tiers = "LOW included" if include_low else "LOW excluded"
    return (
        f"shortest directed path over {', '.join(DEPENDENCY_KINDS)} edges,"
        f" {tiers}, within {_plural_hops(max_hops)}"
    )


def path_report(
    store: Store,
    rev: str,
    from_id: str,
    to_id: str,
    max_hops: int = DEFAULT_HOPS,
    include_low: bool = False,
) -> Report:
    """How `from_id` and `to_id` are connected, in whichever direction they
    are, or which of the three ways they are not."""
    if from_id == to_id:
        # Not an error and not nothing: the honest answer to "how are these
        # two connected" when they are one symbol, which is a thing two
        # different spellings of a name can turn out to be.
        return Report(
            summary={
                "from": from_id,
                "to": to_id,
                "direction": "same symbol",
                "hops": 0,
                "basis": _basis(max_hops, include_low),
            },
            groups=[],
            truncated=False,
        )

    ambiguity = Ambiguity(store, rev)
    forward_edges = _forward_edges(store, rev)
    derived = _derived_names(ambiguity)

    forward = _walk(forward_edges, derived, ambiguity, from_id, to_id, max_hops, include_low)
    reverse = _walk(forward_edges, derived, ambiguity, to_id, from_id, max_hops, include_low)

    summary: dict = {"from": from_id, "to": to_id}
    groups: list[Group] = []

    if forward is not None or reverse is not None:
        if forward is not None and reverse is not None:
            direction = "both"
        else:
            direction = FORWARD if forward is not None else REVERSE
        # The reported path is the forward one whenever there is one: it is
        # the direction the question was asked in. `hops` and `confidence`
        # describe it, and the OTHER direction gets a field of its own --
        # never a second set of numbers under the same names, which would
        # leave a reader of `hops: 2` guessing which chain it counted.
        if forward is not None:
            primary, other_title, other = forward, REVERSE, reverse
        else:
            primary, other_title, other = reverse, FORWARD, None
        summary["direction"] = direction
        summary["hops"] = len(primary)
        summary["confidence"] = _confidence(primary)
        summary[other_title] = "none" if other is None else _plural_hops(len(other))
        if forward is not None:
            groups.append(Group(FORWARD, _rows(store, rev, from_id, forward)))
        if reverse is not None:
            groups.append(Group(REVERSE, _rows(store, rev, to_id, reverse)))
        summary["basis"] = _basis(max_hops, include_low)
        return Report(summary=summary, groups=groups, truncated=False)

    summary["direction"] = "none"
    reason, show_path, hole = _negative(
        store, rev, forward_edges, derived, ambiguity, from_id, to_id, max_hops, include_low
    )
    summary["reason"] = reason
    if show_path:
        # Beside the reason rather than appended to it, and a separate
        # field rather than folded into the sentence, because `--json` is
        # entitled to a flag it can act on -- `impact`'s `show_hidden`
        # makes the same split for the same reason (#37).
        summary["show_path"] = show_path
    summary["basis"] = _basis(max_hops, include_low)
    # Exactly the negatives a flag would turn into a path, and no others.
    # "Different islands" is the strongest claim this tool has and it is
    # complete: no budget and no tier excluded it, so an envelope entry
    # there would tell a reader to go looking for something that is not
    # there. A path that WAS found needs no entry either -- it is an
    # answer, and the basis line already says what it was found over.
    return Report(
        summary=summary,
        groups=groups,
        truncated=False,
        unknowns=[unknown(hole, reason)] if hole else [],
    )


def _negative(
    store: Store,
    rev: str,
    forward_edges: dict[str, dict[str, _Edge]],
    derived: dict[str, list[tuple[str, str]]],
    ambiguity: Ambiguity,
    from_id: str,
    to_id: str,
    max_hops: int,
    include_low: bool,
) -> tuple[str, str, str]:
    """Which of the three negatives holds, the flag that would turn it into
    a path, and the envelope reason that flag corresponds to -- empty when
    the negative is complete and no flag would change it.

    Tried weakest-claim first. The island partition is consulted last and
    only when every walk this command can perform has come back empty, so
    the strongest sentence in the report -- "no walk can ever connect
    them" -- is never printed over evidence to the contrary that this same
    module could have produced. See the module docstring for the one corner
    where the partition and the pointwise fan-out differ.
    """
    distance = _shortest_distance(forward_edges, derived, ambiguity, from_id, to_id, include_low)
    if distance is not None:
        return (
            f"no directed path within {_plural_hops(max_hops)}",
            f"--hops {distance}",
            HOP_LIMIT,
        )

    if not include_low:
        with_low = _shortest_distance(forward_edges, derived, ambiguity, from_id, to_id, True)
        if with_low is not None:
            flag = "--all" if with_low <= max_hops else f"--all --hops {with_low}"
            return "no directed path in either direction", flag, LOW_CONFIDENCE

    components = connected_components(store, rev, ambiguity)
    if components.find(from_id) != components.find(to_id):
        unconnectable = (
            "different islands -- no walk in any direction, at any confidence, can connect them"
        )
        return unconnectable, "", ""
    # No flag, and no entry: every walk this command can perform has run,
    # over every tier, and come back empty. The pair share an island, so
    # something relates them -- a common caller, or a file's top level
    # reaching both -- and `reason` says exactly that. There is nothing
    # further for a reader to try, which is what an envelope entry would
    # otherwise be promising.
    return "no directed path in either direction", "", ""


__all__ = ["DEFAULT_HOPS", "path_report"]
