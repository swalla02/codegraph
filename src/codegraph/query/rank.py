"""Scoring for the `impact` report: how urgently a dependent deserves a
human's attention, given how far it sits from the changed symbol, how
confident the resolver was about the path, and how salient the dependent
itself is.

`salience` counts fan-in over DISTINCT source nodes, never edge rows: the
`edges` table can hold more than one row for the same `(src, dst)` pair --
the same call written twice in a body, or the same candidate reached
through two import aliases -- and none of that should inflate how many
distinct callers a node has.
"""

from __future__ import annotations

from codegraph.ambiguity import Ambiguity
from codegraph.resolve import HIGH, LOW, MEDIUM
from codegraph.store import Store

_CONFIDENCE_WEIGHT = {HIGH: 1.0, MEDIUM: 0.6, LOW: 0.25}

#: Fan-in is capped before it contributes to salience, so one extremely
#: popular utility cannot dwarf every other salience term.
_FAN_IN_CAP = 10


def score(hop: int, confidence: str, salience_value: float) -> float:
    """Rank a dependent: closer hops and higher resolver confidence score
    higher, boosted by how salient the dependent itself is."""
    return (1.0 / hop) * _CONFIDENCE_WEIGHT[confidence] * (1.0 + salience_value)


def fan_in(store: Store, rev: str, node_id: str, ambiguity: Ambiguity | None = None) -> int:
    """Count of DISTINCT callers of `node_id` -- never raw edge rows, since
    the `edges` table can hold more than one row for the same (src, dst)
    pair. `salience` folds this into its composite score; callers that need
    the raw fan-in itself (e.g. to test whether a node is a true entry
    point, `fan_in == 0`) should call this directly rather than
    reverse-engineering it out of `salience`'s combined value, which a
    public, well-called node can also cross via its other two terms alone.

    With an `ambiguity`, the bare-name callers the graph does not store are
    counted too -- unioned by source id rather than added, since a caller
    that both imports a symbol and calls it by bare name elsewhere is still
    one caller. Without one this counts materialized edges only, which is
    what a caller holding no expansion for the revision can honestly say.
    """
    sources = {
        row["src"]
        for row in store.connection.execute(
            "SELECT DISTINCT src FROM edges WHERE rev=? AND dst=?", (rev, node_id)
        )
    }
    if ambiguity is None:
        return len(sources)
    return ambiguity.caller_count(node_id, sources)


def salience(
    store: Store, rev: str, node_id: str, ambiguity: Ambiguity | None = None
) -> float:
    """How much a node deserves attention on its own merits: 0.5 if it has
    no callers of its own (an entry point), 0.3 if its qualname's last
    segment is not private (does not start with `_`), plus a fan-in term
    capped at `_FAN_IN_CAP` distinct callers."""
    connection = store.connection

    callers = fan_in(store, rev, node_id, ambiguity)

    value = 0.0
    if callers == 0:
        value += 0.5

    # `Ambiguity` already holds every live node's last qualname segment --
    # it is the key of the very name index this expands through -- so with
    # one in hand this is a dict lookup rather than a query per dependent.
    last_segment = ambiguity.name_of.get(node_id) if ambiguity is not None else None
    if last_segment is None:
        row = connection.execute(
            "SELECT qualname FROM nodes WHERE rev=? AND id=?", (rev, node_id)
        ).fetchone()
        last_segment = row["qualname"].rpartition(".")[2] if row else node_id.rpartition(".")[2]
    if not last_segment.startswith("_"):
        value += 0.3

    value += min(callers, _FAN_IN_CAP) / 20

    return value


__all__ = ["fan_in", "salience", "score"]
