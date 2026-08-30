"""The `effects` report: every side-effect kind reachable from a symbol,
with a witness path a user can verify in one click.

Groups by effect kind (one row per kind), sorted worst-first by severity
then by confidence. `Row.detail` is `f"{kind} {confidence} via {chain}"` --
the effect kind must stay the first whitespace-separated token, since this
module's own tests and Task 11's both split on it.
"""

from __future__ import annotations

from codegraph.effects.propagate import witness_path
from codegraph.render import Group, Report, Row
from codegraph.store import Store

#: Worst-first. DB writes and network calls are the effects a reviewer
#: should see before anything else; nondeterminism is the mildest of the nine.
_SEVERITY: tuple[str, ...] = (
    "DB_WRITE",
    "NETWORK",
    "PROCESS",
    "FS_WRITE",
    "GLOBAL_MUTATE",
    "DB_READ",
    "FS_READ",
    "ENV_READ",
    "NONDETERMINISM",
)
_SEVERITY_RANK = {kind: rank for rank, kind in enumerate(_SEVERITY)}
_CONFIDENCE_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def effects_report(store: Store, rev: str, node_id: str) -> Report:
    """Every effect kind reachable from `node_id`, one group per kind."""
    connection = store.connection
    node_kinds: dict[str, str] = {}
    for row in connection.execute(
        "SELECT kind, confidence FROM effects WHERE rev=? AND node_id=?", (rev, node_id)
    ):
        best = node_kinds.get(row["kind"])
        if best is None or _CONFIDENCE_RANK[row["confidence"]] < _CONFIDENCE_RANK[best]:
            node_kinds[row["kind"]] = row["confidence"]

    ordered = sorted(
        node_kinds,
        key=lambda k: (_SEVERITY_RANK.get(k, len(_SEVERITY)), _CONFIDENCE_RANK[node_kinds[k]]),
    )

    groups: list[Group] = []
    for rank, kind in enumerate(ordered):
        confidence = node_kinds[kind]
        chain = witness_path(store, rev, node_id, kind)
        cause = chain[-1] if chain else node_id
        location = _evidence_location(store, rev, cause, kind)
        detail = f"{kind} {confidence} via {' -> '.join(chain)}"
        score = float(len(ordered) - rank)
        row = Row(id=f"{node_id}::{kind}", location=location, detail=detail, score=score)
        groups.append(Group(kind, [row]))

    return Report(
        summary={"symbol": node_id, "effect_kinds": len(ordered)},
        groups=groups,
        truncated=False,
    )


def _evidence_location(store: Store, rev: str, direct_node_id: str, kind: str) -> str:
    """The concrete `path:line` of the direct call site causing `kind` at
    `direct_node_id` -- the tail of the witness chain."""
    row = store.connection.execute(
        "SELECT evidence_path, evidence_line FROM effects"
        " WHERE rev=? AND node_id=? AND kind=? AND direct=1"
        " ORDER BY evidence_line LIMIT 1",
        (rev, direct_node_id, kind),
    ).fetchone()
    if row is None:
        return ""
    return f"{row['evidence_path']}:{row['evidence_line']}"
