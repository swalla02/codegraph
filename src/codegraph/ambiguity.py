"""Query-time expansion of the bare-name fan-out.

The resolver's last-resort step matches a call's final dotted segment
against every live definition in the revision. When more than one answers,
`resolve.py` writes no edges at all: it records the reference once in
`unresolved` with `reason='ambiguous'`, carrying the source node, the raw
name and the candidate count.

This module is the other half of that decision. The candidate set is
`every live node whose qualname's last segment is this name` -- a set the
`nodes` table already determines -- so it is recomputed here, exactly,
whenever a query needs it. Nothing was compressed away at write time and
nothing has to be decompressed: the graph never held these edges, and it
never needed to.

Why that is the right seam (#25). Whether 971 candidates for one
`item.save()` is too many is a property of the **question**, not of the
graph: `impact` wants them ranked and bounded by `--limit`, `effects` wants
pure reachability through them, `diff` wants none of them (they are a
statement about the whole repository, not about the symbol being compared).
Materializing them fixed one answer for every future question, at a cost
quadratic in repository size -- 2.09M of django's 2.16M edges.

Two shapes are offered, because the two kinds of consumer want different
things:

*Pointwise.* `callers(node_id)` answers "which ambiguous references could
mean this node" -- the reverse direction `impact` walks. It touches only
the names it is asked about, so an `impact` query never pays for the
fan-out of names it never visits.

*Hubs.* `hub_edges()` answers "the whole fan-out, as a graph" for the
consumers that need a global picture -- `effects/propagate.py`'s
reachability closure and `query/islands.py`'s connected components. It
routes every reference through one synthetic node per name rather than
enumerating the cross product: `src -> HUB(save)` and `HUB(save) -> dst`
for every definition named `save`. For *reachability* -- which is all
either consumer computes over these edges -- that is exactly equivalent to
the N x M direct edges, and it is O(refs + definitions) instead of
O(refs x definitions). On django that is ~120k edges rather than ~2.07M.

Hub ids start with a NUL byte, which no file path and therefore no node id
(`path::qualname`) can contain, so they can never collide with a real node
and are trivially recognised for filtering back out of a result.
"""

from __future__ import annotations

from collections.abc import Iterator

from codegraph.resolve import CONSTRUCTOR, breadth_first
from codegraph.store import Store

#: Prefix for the synthetic per-name node ids `hub_edges` routes through.
#: A NUL byte cannot appear in a POSIX path, so this cannot collide with a
#: real `path::qualname` node id.
HUB_PREFIX = "\x00"


def hub_id(name: str) -> str:
    """The synthetic node every ambiguous reference to `name` routes through."""
    return f"{HUB_PREFIX}{name}"


def is_hub(node_id: str) -> bool:
    """Is this one of `hub_edges`' synthetic per-name nodes rather than a symbol?"""
    return node_id.startswith(HUB_PREFIX)


def last_segment(dotted: str) -> str:
    """`a.b.save` -> `save`; the key both halves of the name index share."""
    return dotted.rpartition(".")[2]


class Ambiguity:
    """One revision's ambiguous references, expandable through its name index.

    Built in two queries and held in memory for the life of a report. On
    django that is ~46k live names and ~90k ambiguous references -- a
    fraction of the ~330k edge rows this replaces, and it is read once per
    report rather than once per node.
    """

    def __init__(self, store: Store, rev: str) -> None:
        connection = store.connection

        #: name -> every live node id whose qualname ends in that name. This
        #: is `resolve._SymbolTable.name_index`, rebuilt from `nodes`: the
        #: resolver's LOW candidate set is this list verbatim.
        self.by_name: dict[str, list[str]] = {}
        #: The inverse, for the pointwise direction. A node id present here
        #: is by construction a member of `by_name[name_of[node_id]]`.
        self.name_of: dict[str, str] = {}
        classes: list[str] = []
        node_ids: set[str] = set()
        for row in connection.execute(
            "SELECT id, qualname, kind FROM nodes WHERE rev=? AND name_binding='live'", (rev,)
        ):
            name = last_segment(row["qualname"])
            self.by_name.setdefault(name, []).append(row["id"])
            self.name_of[row["id"]] = name
            node_ids.add(row["id"])
            if row["kind"] == "class":
                classes.append(row["id"])

        #: class node id -> the `__init__` that instantiating it runs.
        #:
        #: `resolve.with_constructors` adds this edge for every class the
        #: resolver actually distinguished, but an all-LOW fan-out is not
        #: materialized at all now, and a fan-out CAN be entirely classes --
        #: `box.Widget()` with two `Widget` classes in the repo. Without this,
        #: merging query-time expansion with the constructor edge lost that
        #: `__init__` link on both paths at once: deferred at index time, and
        #: absent from the expansion. Query-time expansion has to produce
        #: exactly what index time would have, or the two disagree about the
        #: same graph.
        self.constructor_of: dict[str, str] = {}
        self._constructor_owner: dict[str, str] = {}
        if classes:
            inherits: dict[str, list[str]] = {}
            for row in connection.execute(
                "SELECT src, dst FROM edges WHERE rev=? AND kind='INHERITS'"
                " AND confidence='HIGH'",
                (rev,),
            ):
                inherits.setdefault(row["src"], []).append(row["dst"])
            for class_id in classes:
                # Same breadth-first walk, and the same HIGH-only base filter,
                # that `resolve.constructor_target` uses. Subclass overrides are
                # deliberately not candidates there and are not here: `Cls()`
                # names the exact class being instantiated.
                for owner in breadth_first(class_id, inherits):
                    candidate = f"{owner}.{CONSTRUCTOR}"
                    if candidate in node_ids:
                        self.constructor_of[class_id] = candidate
                        #: The inverse, so the REVERSE walk can find the class
                        #: name that reaches this `__init__`. See
                        #: `reaching_names`.
                        self._constructor_owner[candidate] = class_id
                        break

        # Distinct source nodes per name, not raw rows: one function calling
        # `item.save()` twice writes two ambiguous rows for one relationship,
        # and counting it twice would inflate `rank.fan_in` exactly the way
        # duplicate edge rows already must not.
        calls: dict[str, set[str]] = {}
        bases: dict[str, set[str]] = {}
        for row in connection.execute(
            "SELECT src, raw_name, ref_kind FROM unresolved WHERE rev=? AND reason='ambiguous'",
            (rev,),
        ):
            bucket = calls if row["ref_kind"] == "call" else bases
            bucket.setdefault(last_segment(row["raw_name"]), set()).add(row["src"])
        #: name -> the source nodes of every ambiguous CALL to it, sorted so
        #: two runs of one query produce the same report.
        self.call_refs: dict[str, list[str]] = {n: sorted(s) for n, s in calls.items()}
        #: The same for `class X(Base)` references, kept apart because
        #: `impact`, `effects` and `islands` all walk CALLS and only CALLS.
        self.base_refs: dict[str, list[str]] = {n: sorted(s) for n, s in bases.items()}
        # The same sets kept as sets, for membership rather than iteration.
        # `rank.fan_in` has to union these with a node's materialized callers
        # once per dependent, and a report on a crowded name walks thousands
        # of dependents: iterating a caller list of thousands inside that loop
        # is quadratic, and measurably so -- `impact Model.save` on django took
        # 4m45s before this existed. Testing the (small) materialized side for
        # membership here instead makes the union proportional to it.
        self._call_sets: dict[str, frozenset[str]] = {n: frozenset(s) for n, s in calls.items()}
        self._base_sets: dict[str, frozenset[str]] = {n: frozenset(s) for n, s in bases.items()}

    # -- pointwise -------------------------------------------------------
    def reaching_names(self, node_id: str) -> tuple[str, ...]:
        """Every name an ambiguous reference could use to reach `node_id`.

        Usually just the node's own last segment. A constructor is the
        exception and the reason this exists: `box.Widget()` reaches
        `Widget.__init__`, but the reference's name is `Widget`, so looking
        `__init__` up finds nothing.

        Forward (`candidates`) and reverse (`callers`) MUST agree about the
        same graph. They did not when constructor expansion was added to one
        and not the other: the expansion offered `Widget.__init__` as a
        candidate while the reverse walk reported it as having no callers, so
        `impact` on it came back empty on a graph that could reach it. Every
        confidence this project has got wrong has been some version of that --
        a claim derived from one graph and traversed on another.
        """
        own = self.name_of.get(node_id)
        via_class = self._constructor_owner.get(node_id)
        names = [] if own is None else [own]
        if via_class is not None:
            owner_name = self.name_of.get(via_class)
            if owner_name is not None and owner_name not in names:
                names.append(owner_name)
        return tuple(names)

    def callers(self, node_id: str) -> list[str]:
        """Every node with an ambiguous call that could mean `node_id`.

        Empty for a node the name lookup can never reach: a shadowed
        binding (`name_of` holds live nodes only, exactly as the resolver's
        own index does), or a name nothing calls ambiguously.
        """
        names = self.reaching_names(node_id)
        if len(names) == 1:
            return self.call_refs.get(names[0], [])
        found: set[str] = set()
        for name in names:
            found.update(self.call_refs.get(name, ()))
        return sorted(found)

    def inheritors(self, node_id: str) -> list[str]:
        """The same, for ambiguous base-class references."""
        names = self.reaching_names(node_id)
        if len(names) == 1:
            return self.base_refs.get(names[0], [])
        found: set[str] = set()
        for name in names:
            found.update(self.base_refs.get(name, ()))
        return sorted(found)

    def caller_count(self, node_id: str, also: set[str]) -> int:
        """How many DISTINCT nodes call `node_id`, counting `also` -- the
        node's materialized callers -- and the ambiguous ones together.

        Proportional to `also`, never to the ambiguous set, which is why
        this lives here rather than as a set union at the call site: on a
        crowded name the ambiguous set has thousands of members and the
        materialized one has a handful.
        """
        names = self.reaching_names(node_id)
        if len(names) == 1:
            calls = self._call_sets.get(names[0], frozenset())
            bases = self._base_sets.get(names[0], frozenset())
        else:
            # Rare (constructors only), so the extra allocation is fine here
            # and the fast path above stays allocation-free.
            calls = frozenset().union(*(self._call_sets.get(n, frozenset()) for n in names or [""]))
            bases = frozenset().union(*(self._base_sets.get(n, frozenset()) for n in names or [""]))
        derived = len(calls) + sum(1 for src in bases if src not in calls)
        return derived + sum(1 for src in also if src not in calls and src not in bases)

    def candidates(self, raw_name: str) -> list[str]:
        """Everything an ambiguous reference to `raw_name` could mean --
        the resolver's LOW candidate set, recomputed, plus the `__init__` any
        class among them would run.

        The constructor half mirrors `resolve.with_constructors`, which does
        the same for references the resolver could distinguish. Both paths have
        to describe the same graph.
        """
        found = self.by_name.get(last_segment(raw_name), [])
        if not found:
            return found
        extra = [
            self.constructor_of[node_id]
            for node_id in found
            if node_id in self.constructor_of and self.constructor_of[node_id] not in found
        ]
        return found + extra if extra else found

    # -- whole-graph ------------------------------------------------------
    def hub_edges(self) -> Iterator[tuple[str, str]]:
        """The ambiguous CALL fan-out as `(src, dst)` pairs routed through
        one synthetic node per name.

        Reachability-equivalent to the full cross product and linear rather
        than quadratic; see the module docstring. A name whose definitions
        have all disappeared yields nothing rather than a dangling hub.
        """
        for name, sources in self.call_refs.items():
            targets = self.by_name.get(name)
            if not targets:
                continue
            hub = hub_id(name)
            for source in sources:
                yield (source, hub)
            for target in targets:
                yield (hub, target)

    def call_sites(self) -> int:
        """How many distinct (source, name) call relationships are deferred."""
        return sum(len(sources) for sources in self.call_refs.values())


__all__ = ["HUB_PREFIX", "Ambiguity", "hub_id", "is_hub", "last_segment"]
