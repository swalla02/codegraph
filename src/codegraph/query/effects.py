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
from codegraph.resolve import CONFIDENCE_RANK, stronger
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


def effects_report(store: Store, rev: str, node_id: str) -> Report:
    """Every effect kind reachable from `node_id`, one group per kind."""
    connection = store.connection
    node_kinds: dict[str, str] = {}
    for row in connection.execute(
        "SELECT kind, confidence FROM effects WHERE rev=? AND node_id=?", (rev, node_id)
    ):
        best = node_kinds.get(row["kind"])
        node_kinds[row["kind"]] = (
            row["confidence"] if best is None else stronger(best, row["confidence"])
        )

    # Worst severity first, then strongest confidence first within a
    # severity -- `CONFIDENCE_RANK` is higher-is-stronger, so this sorts on
    # its negation to put HIGH ahead of LOW.
    ordered = sorted(
        node_kinds,
        key=lambda k: (_SEVERITY_RANK.get(k, len(_SEVERITY)), -CONFIDENCE_RANK[node_kinds[k]]),
    )

    groups: list[Group] = []
    for rank, kind in enumerate(ordered):
        confidence = node_kinds[kind]
        chain = witness_path(store, rev, node_id, kind, confidence)
        cause = chain[-1] if chain else node_id
        location = _evidence_location(store, rev, cause, kind, confidence)
        detail = f"{kind} {confidence} via {' -> '.join(chain)}"
        score = float(len(ordered) - rank)
        row = Row(id=f"{node_id}::{kind}", location=location, detail=detail, score=score)
        groups.append(Group(kind, [row]))

    return Report(
        summary={"symbol": node_id, "effect_kinds": len(ordered)},
        groups=groups,
        truncated=False,
    )


def _evidence_location(
    store: Store, rev: str, direct_node_id: str, kind: str, confidence: str
) -> str:
    """The concrete `path:line` of the direct call site causing `kind` at
    `direct_node_id` -- the tail of the witness chain.

    `direct_node_id` can carry more than one `direct=1` row for the same
    `kind`, at different confidences and different lines (no UNIQUE
    constraint on `effects`, and `detect_direct` writes one row per call
    site -- two `open()` calls in one function, one with a literal mode and
    one with a variable mode, is enough). Picking the earliest line with no
    confidence filter can print evidence that contradicts the reported
    confidence: an ambiguous MEDIUM call that happens to sit on an earlier
    line than the HIGH call that actually earns the tier being printed. So
    this only considers rows whose OWN confidence supports `confidence`
    (rank >= it) -- the same eligibility test `witness_path` applies to a
    chain's endpoint -- and picks the earliest line among those.
    """
    target_rank = CONFIDENCE_RANK[confidence]
    best: tuple[int, str] | None = None
    for row in store.connection.execute(
        "SELECT evidence_path, evidence_line, confidence FROM effects"
        " WHERE rev=? AND node_id=? AND kind=? AND direct=1",
        (rev, direct_node_id, kind),
    ):
        if CONFIDENCE_RANK[row["confidence"]] < target_rank:
            continue
        if best is None or row["evidence_line"] < best[0]:
            best = (row["evidence_line"], row["evidence_path"])
    if best is None:
        return ""
    line, path = best
    return f"{path}:{line}"
