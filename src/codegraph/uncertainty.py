"""What codegraph cannot answer, and the one thing that would settle each
case: the reason -> next-action table, and the envelope reports carry it in.

Before #55 this project was honest about *that* it was uncertain and
roughly *what kind* -- confidence tiers on every edge, a named `reason` on
every unresolved reference, `low_confidence_hidden`, `islands`'
`unexplained`. All of it aggregate: `low_confidence_hidden: 235` is a
property of a report, not of any symbol, and none of it ever said what a
reader should do next. A count of what you cannot see, with no way to see
it, is the footgun #37 named one level up; a reason with no next move is
the same footgun one level down.

## One constant string per reason, looked up and never composed

`NEXT_ACTION` is that table. Each entry is written once, here, and handed
out verbatim: `unknown` reads the same sentence in an `unknowns` report as
in an `effects` envelope, because it is the same string object and there is
nowhere else to write a different one. `unknown()` below is the only
constructor, and it subscripts the table, so a reason nobody wrote an
action for raises rather than printing a hole with no handle on it --
which is what makes the totality test in `tests/test_unknowns.py` a
contract rather than a wish.

Composition happens in `Unknown.detail` instead, which is per-case data:
how many references, which budget was reached. The split matters because
the action is the part an agent is meant to act on unread, and an action
assembled per case is one that can come out subtly different -- or wrong --
for the case nobody tested.

## Gaps and settled answers

Not every entry means the report is incomplete, and conflating the two
would make `--strict` useless. `external` and `builtin` are decisions: the
resolver identified the reference exactly and knows that no node in this
graph is its target. Nothing about a future index changes them, so they are
reported (an agent is entitled to know that `pytest.main` leaves the
repository) and they never make a report incomplete -- a `--strict` that
refused on every body calling `len()` is a flag nobody can pass. `SETTLED`
is that distinction, and it rides in each entry as `blocking` so a
machine reading `--json` does not need this module to interpret it.

## What counts as incomplete, and what does not

The rule applied across every report: **an entry names something the run
did not examine.** Not something it examined and summarized.

- An `impact` walk stopped at `--hops` did not examine the callers past
  it, so it is incomplete -- and today it reads exactly like a walk that
  finished, which is the confidently-wrong answer this project keeps
  refusing to print.
- A `path` walk that excluded LOW edges did not traverse them.
- `low_confidence_hidden` is NOT an entry. Those rows were walked, ranked
  and counted; the report shows the strongest of them and says how many
  more there are, with `show_hidden: --all` beside the count (#37). A LOW
  row is an answer given with its tier attached, and a report full of them
  is uncertain rather than incomplete. Were `--strict` to refuse on LOW, it
  would refuse on every real repository and stop carrying information.
- `truncated` is NOT an entry either. `--limit` is the caller's own budget,
  it already has a top-level field, and a report that honoured the budget
  it was given did not fail to see anything.

An empty envelope therefore means "no hole this tool can name", never
"this answer is complete". A name assembled at runtime leaves nothing for
any of this to find, which is why `orphans` states its blind spot as a
standing `caveat` on every row rather than as an entry here: a caveat that
appears on every run is not news, and it cannot be cleared by re-running.
"""

from __future__ import annotations

from codegraph.render import Report, Unknown
from codegraph.resolve import AMBIGUOUS, BUILTIN, EXTERNAL, UNKNOWN, UNRESOLVED_REASONS

#: The symbol's island carries no implicit-invocation mechanism that
#: `islands` recognises. Not a resolver reason -- no single reference
#: produced it -- but the same kind of claim, and an agent reading the
#: envelope should not have to consult two tables.
UNEXPLAINED_ISLAND = "unexplained_island"

#: A walk stopped because it reached its `--hops` budget with more graph in
#: front of it.
HOP_LIMIT = "hop_limit"

#: An answer exists over edges this run excluded for being LOW. Only
#: reports that exclude LOW from the WALK raise this -- `path` does;
#: `impact` walks LOW and excludes it from the page instead, which is a
#: different thing and deliberately not an entry (see the module docstring).
LOW_CONFIDENCE = "low_confidence"

#: A symbol's history reached a commit where its body matches more than one
#: removed definition, so which one it came from is a guess `history` will
#: not make. The commits before that point, under the earlier id, were not
#: examined for this symbol.
LINEAGE_AMBIGUOUS = "lineage_ambiguous"

#: Every reason an entry can carry: the resolver's four, plus the four a
#: query makes for itself. `NEXT_ACTION` is keyed by exactly this tuple, and
#: the test that says so is what stops a fifth resolver reason from shipping
#: without an action.
REASONS: tuple[str, ...] = (
    *UNRESOLVED_REASONS,
    UNEXPLAINED_ISLAND,
    HOP_LIMIT,
    LOW_CONFIDENCE,
    LINEAGE_AMBIGUOUS,
)

#: The reasons that are answers rather than gaps. See the module docstring.
SETTLED: tuple[str, ...] = (EXTERNAL, BUILTIN)

#: Reason -> what to do about it. One sentence each, phrased as what is
#: true and then what would settle it, because an agent reads this without
#: the surrounding paragraph.
NEXT_ACTION: dict[str, str] = {
    UNKNOWN: (
        "the name matches nothing in the repository; expect dynamic dispatch,"
        " and read the body to see what it is"
    ),
    AMBIGUOUS: (
        "the bare name matches several definitions; `codegraph resolve <name>`"
        " lists them, and `impact --all` walks them"
    ),
    EXTERNAL: "the target is outside the repository; no future index will resolve it",
    BUILTIN: "the target is a Python builtin; no repository symbol is being called",
    UNEXPLAINED_ISLAND: (
        "no implicit-invocation mechanism was recognised; a runtime trace is the"
        " only thing that can confirm this symbol is reached"
    ),
    HOP_LIMIT: "the walk stopped at its budget; re-run with a larger --hops",
    LOW_CONFIDENCE: "an answer exists over LOW-confidence edges; re-run with --all",
    LINEAGE_AMBIGUOUS: (
        "the symbol's body matches several removed definitions, so its earlier id is"
        " a guess; re-run `history` on each candidate id to follow the one you mean"
    ),
}


def unknown(reason: str, detail: str) -> Unknown:
    """One envelope entry: a reason, the per-case detail, and the table's
    action for that reason.

    The only way an `Unknown` is built. Subscripting `NEXT_ACTION` here
    rather than defaulting to a vague sentence is deliberate: a reason
    added without an action should fail loudly at the point it is raised,
    not print an entry that tells the reader nothing they did not know.
    """
    return Unknown(
        reason=reason,
        detail=detail,
        action=NEXT_ACTION[reason],
        blocking=reason not in SETTLED,
    )


def is_incomplete(report: Report) -> bool:
    """Does this report carry a hole that could make acting on it wrong?

    What `--strict` exits nonzero for. Settled entries do not count: see
    `SETTLED` and the module docstring.
    """
    return any(item.blocking for item in report.unknowns)


__all__ = [
    "HOP_LIMIT",
    "LINEAGE_AMBIGUOUS",
    "LOW_CONFIDENCE",
    "NEXT_ACTION",
    "REASONS",
    "SETTLED",
    "UNEXPLAINED_ISLAND",
    "is_incomplete",
    "unknown",
]
