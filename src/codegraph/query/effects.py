"""The `effects` report: every side-effect kind reachable from a symbol,
with a witness path a user can verify in one click.

Groups by effect kind (one row per kind), sorted worst-first by severity
then by confidence. `Row.detail` is `f"{kind} {confidence} via {chain}"` --
the effect kind must stay the first whitespace-separated token, since this
module's own tests and Task 11's both split on it.
"""

from __future__ import annotations

from codegraph.effects.propagate import witness_path
from codegraph.query.unknowns import unresolved_references
from codegraph.render import Group, Report, Row, Unknown
from codegraph.resolve import CONFIDENCE_RANK, EXTERNAL, UNKNOWN, stronger
from codegraph.store import Store
from codegraph.uncertainty import unknown

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
        unknowns=_envelope(store, rev, node_id),
    )


def _envelope(store: Store, rev: str, node_id: str) -> list[Unknown]:
    """The holes in this symbol's own body that the propagation walk could
    not cross.

    Two of the four reasons, and the other two are deliberate omissions.
    `ambiguous` is not a hole here: `effects/propagate.py` folds the
    bare-name fan-out in through the same hubs `islands` uses, so those
    calls ARE followed. `builtin` is not one either -- a builtin with an
    effect is in the catalog (`open` is how `FS_WRITE` is usually found),
    and one that is not is a name the catalog has already been asked about.

    `unknown` is a call this walk could not follow at all, so it blocks;
    `external` leaves the repository, where the catalog is the only thing
    that can speak for it, so it is reported and does not (see
    `uncertainty.SETTLED`). Both are counted from this body's own stored
    rows -- this is a statement about the FIRST hop, not a proof that the
    rest of the chain is complete, which no envelope can be.
    """
    counts: dict[str, int] = {}
    for reference in unresolved_references(store, rev, node_id):
        counts[reference.reason] = counts.get(reference.reason, 0) + 1
    return [
        unknown(
            reason,
            f"{counts[reason]} call{'' if counts[reason] == 1 else 's'} in this body that the"
            " witness walk cannot follow",
        )
        for reason in (UNKNOWN, EXTERNAL)
        if reason in counts
    ]


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
