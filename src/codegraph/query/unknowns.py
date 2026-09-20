"""The `unknowns` report: what codegraph does not know about one symbol,
and what would settle each part of it.

The mirror of `impact`. That report answers "what depends on this"; this
one answers the question an agent has to ask before trusting the answer --
*what did you fail to see while working it out?* Until #55 the only
available answer was aggregate: a `low_confidence_hidden` count, an
`unexplained` island tally, an `unresolved` total in `status`. All true, and
none of it addressable. An agent about to edit one function could not find
out that *that* function's body is half `getattr`.

Nothing here is inferred. Every number is a count of rows the indexer
already wrote, and every sentence of advice is a lookup in
`uncertainty.NEXT_ACTION`. That is the whole design: a deterministic
contract an agent can act on without exercising judgement, because the
moment it has to judge, the tool has handed back the problem it exists to
solve.

## Four questions, and where each answer comes from

*Which references in this body produced no edge.* The `unresolved` rows for
this node -- `src` is the node the reference was made from, so this is a
direct read, not a reconstruction. Each row already carries the raw name,
the line, the reason and the candidate count as of indexing.

*How much of the body resolved.* Below, in `reference_counts`.

*Whether the symbol sits in an unexplained island, and what was checked.*
`islands.island_label`, which is the `islands` report's own partition and
its own mechanism passes. Not a cheaper re-derivation: two implementations
of one graph is the mistake this project has made before and it produces
exactly the kind of quiet disagreement that makes a tool untrustworthy.

*Whether an `impact` walk would stop on its budget.* `impact.hits_hop_limit`,
which is `impact`'s own walk. A report that says "six dependents" after
stopping at three hops reads precisely like one that finished, and that is
the confidently-wrong answer the design spec says is worse than no tool.

## The unit is a reference SITE, and two of them can share a line

A ratio needs a denominator, and "references in this body" has to be
counted from Layer 2 alone -- `blob_refs` is keyed by blob and qualname,
and mapping those back onto node ids would mean reimplementing the
resolver's own attribution (`resolve` does it with the reference's line
against each candidate node's span) at query time, for a second answer that
could differ from the one the edges were written with.

So a site is `(path, line)`, taken from `edges.callsite_path` and
`callsite_line` for the resolved half and from `unresolved` for the rest.
Collapsing the resolved half by site is not an approximation, it is the
correction: one `self.render()` writes one edge per override and one
`Cls()` writes two, and counting edge rows would report a body as having
more references than it has lines. Two resolved calls written on one line
do collapse into one site, and `edges` carries no column that would
separate them -- a known, stated undercount of both halves of the ratio
rather than a guess dressed as a count.

## A low ratio is not a defect, and the report does not imply it is

`9 of 20` on a body full of `len()` and `json.dumps()` says the resolver
identified eleven references exactly and knows that none of them is a
symbol in this repository. That is why every reason is reported with its
own count and its own next action, and why `builtin` and `external` never
make the report incomplete: they are answers. `unknown` and `ambiguous` are
the gaps, and they are the ones `--strict` refuses on. See
`uncertainty.SETTLED`.
"""

from __future__ import annotations

from dataclasses import dataclass

from codegraph.config import Config
from codegraph.query.impact import hits_hop_limit
from codegraph.query.islands import MECHANISMS, NETWORK, IslandLabel, island_label
from codegraph.render import Group, Report, Row, Unknown
from codegraph.resolve import UNRESOLVED_REASONS
from codegraph.store import Store
from codegraph.uncertainty import HOP_LIMIT, UNEXPLAINED_ISLAND, unknown

#: `impact`'s own default, repeated deliberately: this report's answer about
#: the hop budget is only useful if it is the budget the reader's next
#: `impact` run will use.
DEFAULT_HOPS = 3


@dataclass(frozen=True)
class Reference:
    """One reference in a body that produced no edge, as `unresolved`
    stored it.

    `candidates` is the count as of indexing and is meaningful for
    `ambiguous` alone -- every other reason writes 0, because there was
    nothing to count. See the `unresolved` table's own comment in `store`.
    """

    raw_name: str
    ref_kind: str
    reason: str
    path: str
    line: int
    candidates: int


def unresolved_references(store: Store, rev: str, node_id: str) -> list[Reference]:
    """Every reference this symbol makes that the resolver recorded and did
    not turn into an edge, in source order.

    One query, filtered on `src` -- the node the reference was made from,
    and the one thing about a reference that is not derivable from the name
    index (see the `unresolved` table's comment). Sorted here rather than in
    SQL because the sort is part of the report's contract: two runs of one
    command print one report, and SQLite's row order is stable for a
    database file and not a promise across a rebuild.
    """
    rows = [
        Reference(
            raw_name=row["raw_name"],
            ref_kind=row["ref_kind"],
            reason=row["reason"],
            path=row["path"],
            line=row["line"],
            candidates=row["candidates"],
        )
        for row in store.connection.execute(
            "SELECT raw_name, ref_kind, reason, path, line, candidates FROM unresolved"
            " WHERE rev=? AND src=?",
            (rev, node_id),
        )
    ]
    return sorted(rows, key=lambda ref: (ref.path, ref.line, ref.raw_name))


def _resolved_sites(store: Store, rev: str, node_id: str) -> int:
    """How many distinct call sites in this body did become edges.

    Distinct `(path, line)` rather than edge rows: see the module docstring
    on why one reference legitimately writes several edges. Served by
    `idx_edges_src`, so the cost is the symbol's own out-degree and not the
    revision's edge count.
    """
    return len(
        {
            (row["callsite_path"], row["callsite_line"])
            for row in store.connection.execute(
                "SELECT callsite_path, callsite_line FROM edges WHERE rev=? AND src=?",
                (rev, node_id),
            )
        }
    )


def reference_counts(store: Store, rev: str, node_id: str) -> tuple[int, int]:
    """`(references, resolved)` for one body, from stored rows only.

    The pair the ratio is printed from. Both halves are counts of rows this
    revision already holds -- no scan of the source, no re-resolution, and
    nothing whose cost a reader could not predict from the size of the
    symbol they asked about.
    """
    resolved = _resolved_sites(store, rev, node_id)
    return resolved + len(unresolved_references(store, rev, node_id)), resolved


def _detail(reference: Reference) -> str:
    """One row's `detail`: what kind of reference it is, and -- for the one
    reason that has a number worth printing -- how many definitions answered
    to the name. The next action is deliberately NOT here: it is one
    constant per reason and belongs in the envelope, said once, rather than
    repeated down a column."""
    detail = f"{reference.ref_kind} reference"
    if reference.candidates:
        detail += f", {reference.candidates} candidates"
    return detail


def _island_summary(label: IslandLabel) -> str:
    """The island line of the summary: the claim first, then what carries
    it. `explained by decorator, import` reads as an answer; `unexplained`
    reads as the admission it is, with `mechanisms_not_found` beside it
    listing what "recognised" covers."""
    if not label.explained:
        return "unexplained"
    # `NETWORK` and nothing else from the boundary kinds: `ENV_READ` rides
    # in an `islands` row as a legend entry and is explicitly not a
    # boundary there, so naming it as something that explains an island
    # would make this report say what that one refuses to.
    carried = [*label.found, *(kind for kind in label.boundary if kind == NETWORK)]
    return f"explained by {', '.join(carried)}"


def unknowns_report(
    store: Store,
    rev: str,
    node_id: str,
    config: Config | None = None,
    max_hops: int = DEFAULT_HOPS,
    limit: int = 40,
) -> Report:
    """What this graph does not know about `node_id`, and what would settle
    each part of it."""
    references = unresolved_references(store, rev, node_id)
    # The two halves of `reference_counts`, spelled out rather than called,
    # because the rows are wanted anyway and asking for them twice would be
    # one query per report spent re-fetching what is already in hand.
    resolved = _resolved_sites(store, rev, node_id)
    total = resolved + len(references)
    label = island_label(store, rev, node_id, config)
    hop_limited = hits_hop_limit(store, rev, node_id, max_hops)

    # Not `render.budget`: within a reason these rows are a walk through a
    # body in source order, not a ranking, and sorting them by a score
    # would destroy the only order they have (`query/path.py` declines it
    # for the same reason). `--limit` is still one total across the groups,
    # and it is spent in `UNRESOLVED_REASONS` order -- the gaps first, the
    # settled reasons last. A body with sixty `isinstance` calls and one
    # `getattr` must not spend its budget on the sixty, which is `impact`'s
    # rule that production callers get first claim on the rows, applied to
    # the claim each reason makes rather than to a score.
    ordered = sorted(references, key=lambda ref: UNRESOLVED_REASONS.index(ref.reason))
    truncated = len(ordered) > limit
    by_reason: dict[str, list[Row]] = {}
    for reference in ordered[:limit]:
        by_reason.setdefault(reference.reason, []).append(
            Row(
                id=reference.raw_name,
                location=f"{reference.path}:{reference.line}",
                detail=_detail(reference),
                score=float(-reference.line),
            )
        )

    groups = [
        Group(reason, by_reason[reason]) for reason in UNRESOLVED_REASONS if reason in by_reason
    ]

    summary = {
        "symbol": node_id,
        # The ratio, as two counts rather than a percentage: 9 of 20 and
        # 45% are the same number, and only one of them lets a reader see
        # that the body has twenty references in it.
        "references": total,
        "resolved": resolved,
        "unresolved": len(references),
        "island": _island_summary(label),
        # Always printed, including when the island IS explained: "no
        # mechanism recognised" is only readable beside the list of what
        # was looked for, and a reader who has to go and find that list
        # elsewhere is being asked to take the claim on trust.
        "mechanisms_not_found": list(label.missing),
        "basis": (
            f"stored rows for this symbol only; island label from the {len(MECHANISMS)}"
            f" mechanisms `islands` recognises; hop budget {max_hops}"
        ),
    }

    return Report(
        summary=summary,
        groups=groups,
        truncated=truncated,
        unknowns=_envelope(references, label, hop_limited, max_hops),
    )


def _envelope(
    references: list[Reference], label: IslandLabel, hop_limited: bool, max_hops: int
) -> list[Unknown]:
    """One entry per kind of hole, never one per instance.

    A body with forty unknown references has one thing wrong with it, not
    forty; the rows say where they are and this says what to do, once. That
    is also what keeps the action a constant -- a per-instance entry would
    invite a per-instance sentence, which is exactly the composition
    `uncertainty` exists to prevent.
    """
    counts: dict[str, int] = {}
    for reference in references:
        counts[reference.reason] = counts.get(reference.reason, 0) + 1
    entries = [
        unknown(
            reason,
            f"{counts[reason]} reference{'' if counts[reason] == 1 else 's'} in this body",
        )
        for reason in UNRESOLVED_REASONS
        if reason in counts
    ]
    if not label.explained:
        entries.append(
            unknown(
                UNEXPLAINED_ISLAND,
                f"island of {label.size}; checked and not found: {', '.join(label.missing)}",
            )
        )
    if hop_limited:
        entries.append(
            unknown(
                HOP_LIMIT,
                f"an `impact` walk of {max_hops} hops does not exhaust this symbol's dependents",
            )
        )
    return entries


__all__ = [
    "DEFAULT_HOPS",
    "Reference",
    "reference_counts",
    "unknowns_report",
    "unresolved_references",
]
