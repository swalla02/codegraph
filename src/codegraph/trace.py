"""An observed run, imported into the graph as `runtime` provenance (#56).

Everything else in this package reads source and deduces. This module is the
one place a fact arrives from outside that process: `tracer.py` watches a
program run and records the calls that actually happened, and this turns that
record into edges nobody could have derived.

## Why it is a separate kind of knowledge, and not just more confidence

`confidence` is a claim about reading: HIGH means the name was imported or
module-local, LOW means it matched every definition in the repository.
Provenance is a claim about evidence: `STATIC` means the text says so,
`RUNTIME` means a run did it. They are orthogonal, and the interesting pairs
are the mixed ones -- a LOW static edge a trace confirms is certain, and a
call with no static edge at all is the strongest evidence in the graph.

So a confirmed pair keeps BOTH rows rather than being merged into one
verdict. Merging would have to throw one of the two facts away, and which
one you throw away is exactly the question a reader is asking.

## What a runtime edge claims, and the tier it claims it at

HIGH, always, whatever the static resolver said about the same pair -- and
for a runtime-only edge, whatever the resolver would have said had it seen
anything at all.

The alternative was a fourth tier above HIGH. It was rejected: a tier is the
answer to "how was the target identified", the whole tier table is about
degrees of inference, and adding an observed rank to it would put two
different axes on one scale -- precisely the conflation this issue exists to
undo. Capping the other way (writing an observed call as MEDIUM because the
text does not name it) would be worse: it would rank a call that demonstrably
happened below one that might. A traced edge's endpoints are not identified
by inference at all; `sys.monitoring` hands over the code objects. There is
no uncertainty left for a tier to express, so it takes the top of the scale
and says the rest with `provenance`.

## What is not imported

A traced pair is dropped, and counted, in two cases:

- *the target is not a function or method.* `PY_START` fires for a module
  body and a class body too, so importing a module traces
  `__init__.py::<module> -> app.py::<module>`. That is an import and a
  definition, not a call, and `CALLS` does not model either. The filter is
  the one `bench/score.py`'s `partition` applies, for the same reason -- and
  it is on the target only, because a module's top level calling
  `create_app()` IS a call, with the synthetic module node as its source.
- *the file it names has changed since the import.* See `project`.

## Revision binding, and how a stale trace stays visible

A trace is evidence about one tree. `trace_runs` is keyed by revision, so a
trace imported against one revision is unreachable from any other -- there is
no leak to prevent, because there is no row to find.

Within a revision, the binding is per file: `trace_files` records the blob
sha of every file the trace named a symbol in, as of the import. `project`
compares those against the revision's current tree and drops every
observation whose caller's or callee's file has moved on, because an
observation is about the code that ran and that code is no longer there.
Per file rather than per tree: a trace of a 2,900-file repository would
otherwise be thrown away in full by one unrelated edit, which is a reason
nobody would keep a trace around.

Dropped observations are counted, not silently discarded, and every report
that reads the graph prints the count (`summary`). "Stale" is a thing the
reader must be able to see, or it is just a wrong answer with extra steps.

## Where it is applied

`Indexer.reconcile` calls `project` inside the same transaction that
materializes edges and effects, so a revision is never visible with the
resolver's edges and a previous run's observations. That makes a trace an
input to materialization, which is why `indexer.RESOLVER_SOURCES` lists this
module and why `Indexer._fingerprint` folds in the trace's identity:
importing one changes every answer for that revision, and the unchanged-tree
fast path must not go on serving the graph from before it.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from codegraph.resolve import CALLS, HIGH, MODULE_SCOPE, RUNTIME, STATIC
from codegraph.store import Store

#: What `summary` says when no run has been observed for a revision. Reports
#: print it rather than omitting the field, because "nothing reaches this" is
#: a far weaker claim without a trace than with one, and a reader cannot
#: discount the claim they were not told about.
NO_TRACE = "none"

#: The node kinds a `CALLS` edge may point AT. `parse.py` gives `module` and
#: `class` to things that execute without being called.
CALLABLE_KINDS = frozenset({"function", "method"})

#: Code objects CPython creates for a scope nobody wrote a `def` for, which
#: therefore has no row in `nodes`. `PY_START` fires for them like anything
#: else, and a trace attributes to `f.<locals>.<genexpr>` the call that
#: `parse.py` attributes to `f` -- a disagreement about naming, not about
#: the call graph, so the names are folded together on the way in.
ANONYMOUS_SCOPES = frozenset({"<genexpr>", "<listcomp>", "<setcomp>", "<dictcomp>", "<lambda>"})


def _qualname(node_id: str) -> str:
    return node_id.partition("::")[2]


def _last_segment(qualname: str) -> str:
    return qualname.rpartition(".")[2]


def is_anonymous(node_id: str) -> bool:
    """Is this an anonymous scope -- a comprehension or a lambda?"""
    return _last_segment(_qualname(node_id)) in ANONYMOUS_SCOPES


def collapse_anonymous(node_id: str) -> str:
    """`m.py::f.<locals>.<genexpr>` -> `m.py::f`, repeatedly.

    A comprehension runs in its own code object, so a call made inside one is
    attributed by the tracer to the comprehension. `parse.py` records
    definitions, and a comprehension is an expression inside `f`'s body, so
    the same call site belongs to `f`. Without this the two describe one call
    under two names.

    A comprehension at module scope collapses to the module node, which is
    where `parse.py` puts it.
    """
    path, separator, qualname = node_id.partition("::")
    parts = qualname.split(".")
    while parts and parts[-1] in ANONYMOUS_SCOPES:
        parts.pop()
        if parts and parts[-1] == "<locals>":
            parts.pop()
    if not parts:
        return f"{path}{separator}{MODULE_SCOPE}"
    return f"{path}{separator}{'.'.join(parts)}"


class TraceMismatch(ValueError):
    """A trace that names nothing this revision contains.

    Raised instead of importing an empty projection, because the likeliest
    cause is a trace of some other repository (or of a revision far from this
    one), and a trace that quietly contributes nothing is indistinguishable
    from no trace at all while still making every report claim one exists.
    """


@dataclass(frozen=True)
class ImportResult:
    """What was taken in, before any of it is matched against the tree."""

    trace_id: str
    observed: int
    executed: int
    #: Observed pairs with at least one endpoint this revision knows as a
    #: node. The import refuses when this is zero; see `TraceMismatch`.
    recognised: int


@dataclass(frozen=True)
class Projection:
    """What `project` made of an imported trace, against one tree."""

    #: Observed pairs the resolver had already found. Both rows are kept.
    confirmed: int = 0
    #: Observed pairs with no static edge: the calls only a run can see.
    added: int = 0
    #: Observed pairs naming a file that has changed since the import.
    stale: int = 0
    #: Observed pairs `CALLS` does not model -- see the module docstring.
    unmodelable: int = 0

    @property
    def edges(self) -> int:
        """Rows written to `edges` with `RUNTIME` provenance."""
        return self.confirmed + self.added


def _paths_of(node_ids) -> set[str]:
    return {node_id.partition("::")[0] for node_id in node_ids}


def import_trace(store: Store, rev: str, payload: dict, source: str) -> ImportResult:
    """Store `payload` as the trace for `rev`, replacing any previous one.

    The revision must already be materialized: the import binds each named
    file to the blob sha the revision currently holds for it, and checks that
    the trace is about this repository at all.

    Nothing is projected here. Writing `edges` is `project`'s job, run by the
    indexer inside the transaction that materializes everything else, so that
    an import and a reconcile can never disagree about the graph.
    """
    connection = store.connection
    edges = {
        (collapse_anonymous(src), dst)
        for src, dst in (tuple(pair) for pair in payload.get("edges", ()))
    }
    # A comprehension inside `f` calling `f` collapses to a self-edge, which
    # codegraph does not model any more than the tracer models recursion.
    edges = {(src, dst) for src, dst in edges if src != dst}
    executed = {collapse_anonymous(node) for node in payload.get("executed", ())}

    known = {row["id"] for row in connection.execute("SELECT id FROM nodes WHERE rev=?", (rev,))}
    recognised = sum(1 for src, dst in edges if src in known or dst in known)
    if (edges or executed) and not recognised and not (executed & known):
        raise TraceMismatch(
            f"{source} names no symbol in {rev}"
            " -- is it a trace of a different repository or revision?"
        )

    tree = {
        row["path"]: row["blob_sha"]
        for row in connection.execute("SELECT path, blob_sha FROM tree WHERE rev=?", (rev,))
    }
    touched = _paths_of(node for pair in edges for node in pair) | _paths_of(executed)

    # Identity by content: two runs producing the same observations are the
    # same evidence, and `Indexer._fingerprint` should not rebuild a revision
    # for a re-import that changes nothing.
    digest = hashlib.blake2b(digest_size=16)
    for src, dst in sorted(edges):
        digest.update(f"{src}\x00{dst}\x00".encode())
    for node in sorted(executed):
        digest.update(f"{node}\x00".encode())
    trace_id = digest.hexdigest()

    with connection:
        forget(store, rev, commit=False)
        connection.execute(
            "INSERT INTO trace_runs(rev, trace_id, source, imported_at, observed, executed)"
            " VALUES(?,?,?,?,?,?)",
            (rev, trace_id, source, int(time.time()), len(edges), len(executed)),
        )
        connection.executemany(
            "INSERT INTO trace_edges(rev, src, dst) VALUES(?,?,?)",
            [(rev, src, dst) for src, dst in sorted(edges)],
        )
        connection.executemany(
            "INSERT INTO trace_executed(rev, node_id) VALUES(?,?)",
            [(rev, node) for node in sorted(executed)],
        )
        connection.executemany(
            "INSERT INTO trace_files(rev, path, blob_sha) VALUES(?,?,?)",
            # A file the trace names that this revision does not have is
            # recorded with an empty sha rather than skipped. Skipping it
            # would leave nothing for `_stale_paths` to compare, so if that
            # path later appeared in the tree -- a file added, a branch
            # switched under a WORKTREE trace -- observations about a
            # different file of the same name would come back to life as
            # though they were fresh. No content can hash to "", so the
            # comparison says "changed" in both directions.
            [(rev, path, tree.get(path, "")) for path in sorted(touched)],
        )
    return ImportResult(
        trace_id=trace_id, observed=len(edges), executed=len(executed), recognised=recognised
    )


def forget(store: Store, rev: str, commit: bool = True) -> int:
    """Drop `rev`'s trace. Returns the number of observations discarded.

    The `edges` rows it produced are not deleted here: they are rewritten by
    the next `project`, which the changed fingerprint forces. Deleting them
    here as well would be a second path to the same state, and the two would
    eventually disagree.
    """
    connection = store.connection
    row = connection.execute("SELECT observed FROM trace_runs WHERE rev=?", (rev,)).fetchone()
    for table in ("trace_runs", "trace_edges", "trace_executed", "trace_files"):
        connection.execute(f"DELETE FROM {table} WHERE rev=?", (rev,))
    if commit:
        connection.commit()
    return row["observed"] if row else 0


def trace_identity(store: Store, rev: str) -> str:
    """The trace's content digest, or `''` when there is none.

    Folded into `Indexer._fingerprint`, which is what makes importing (or
    forgetting) a trace invalidate the materialized revision.
    """
    row = store.connection.execute("SELECT trace_id FROM trace_runs WHERE rev=?", (rev,)).fetchone()
    return row["trace_id"] if row else ""


def _stale_paths(store: Store, rev: str) -> set[str]:
    """Files the trace named whose content has changed since it was taken."""
    connection = store.connection
    current = {
        row["path"]: row["blob_sha"]
        for row in connection.execute("SELECT path, blob_sha FROM tree WHERE rev=?", (rev,))
    }
    return {
        row["path"]
        for row in connection.execute("SELECT path, blob_sha FROM trace_files WHERE rev=?", (rev,))
        if current.get(row["path"]) != row["blob_sha"]
    }


def traced_paths(store: Store, rev: str) -> set[str]:
    """Every file `rev`'s trace named a symbol in, stale or not.

    `Indexer._narrowable` reads this: an edit to one of these files changes
    which observations survive, and the narrowed reconcile that follows
    cannot see that from the static edges alone.
    """
    return {
        row["path"]
        for row in store.connection.execute("SELECT path FROM trace_files WHERE rev=?", (rev,))
    }


def project(store: Store, rev: str) -> Projection:
    """Rewrite `rev`'s `RUNTIME` edge rows from its imported trace.

    Whole-revision every time, never narrowed. The rows are few (a big suite
    on flask observes a few thousand), and which of them survive depends on
    file shas rather than on the paths a reconcile happens to be rewriting --
    editing a callee's file retires an observation whose row is filed under
    the caller's path, which a narrowed rewrite would never have looked at.

    Runs with no trace as well, so that forgetting one is not a special case:
    the delete is the whole of the work, and the graph is the static one
    again.

    A confirmed pair's row copies the static edge's call site instead of
    inventing one. The pair is the same relationship, the text does name it,
    and a query that prefers the observed row (`path` does, to be able to say
    the hop was seen) should not lose the line as the price of saying so.
    A runtime-only pair has no call site anywhere in the text -- that is what
    makes it runtime-only -- so it points at the caller's own declaration,
    the same choice the `IMPLEMENTS` pass makes for a claim with no site.
    """
    connection = store.connection
    connection.execute("DELETE FROM edges WHERE rev=? AND provenance=?", (rev, RUNTIME))
    run = connection.execute("SELECT rev FROM trace_runs WHERE rev=?", (rev,)).fetchone()
    if run is None:
        return Projection()

    kinds: dict[str, str] = {}
    line_start: dict[str, int] = {}
    for row in connection.execute("SELECT id, kind, line_start FROM nodes WHERE rev=?", (rev,)):
        kinds[row["id"]] = row["kind"]
        line_start[row["id"]] = row["line_start"]

    #: (src, dst) -> the earliest call site the resolver recorded for it.
    static_sites: dict[tuple[str, str], tuple[str, int]] = {}
    for row in connection.execute(
        "SELECT src, dst, callsite_path, callsite_line FROM edges"
        " WHERE rev=? AND kind=? AND provenance=?",
        (rev, CALLS, STATIC),
    ):
        key = (row["src"], row["dst"])
        site = (row["callsite_path"], row["callsite_line"])
        if key not in static_sites or site[1] < static_sites[key][1]:
            static_sites[key] = site

    stale_paths = _stale_paths(store, rev)
    confirmed = added = stale = unmodelable = 0
    rows: list[tuple] = []
    for row in connection.execute("SELECT src, dst FROM trace_edges WHERE rev=?", (rev,)):
        src, dst = row["src"], row["dst"]
        if src.partition("::")[0] in stale_paths or dst.partition("::")[0] in stale_paths:
            stale += 1
            continue
        if kinds.get(dst) not in CALLABLE_KINDS or src not in kinds:
            unmodelable += 1
            continue
        site = static_sites.get((src, dst))
        if site is None:
            added += 1
            site = (src.partition("::")[0], line_start[src])
        else:
            confirmed += 1
        rows.append((rev, src, dst, CALLS, HIGH, RUNTIME, site[0], site[1]))

    connection.executemany(
        "INSERT INTO edges(rev, src, dst, kind, confidence, provenance, callsite_path,"
        " callsite_line) VALUES(?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.execute(
        "UPDATE trace_runs SET projected_at=?, confirmed=?, added=?, stale=?, unmodelable=?"
        " WHERE rev=?",
        (int(time.time()), confirmed, added, stale, unmodelable, rev),
    )
    return Projection(confirmed=confirmed, added=added, stale=stale, unmodelable=unmodelable)


def projection(store: Store, rev: str) -> Projection:
    """The counts `project` last recorded for `rev`, without redoing it."""
    row = store.connection.execute(
        "SELECT confirmed, added, stale, unmodelable FROM trace_runs WHERE rev=?", (rev,)
    ).fetchone()
    if row is None:
        return Projection()
    return Projection(
        confirmed=row["confirmed"],
        added=row["added"],
        stale=row["stale"],
        unmodelable=row["unmodelable"],
    )


def observed_nodes(store: Store, rev: str) -> set[str]:
    """Every symbol the run entered, minus the ones whose file has changed.

    A superset of the endpoints of the runtime edges: a function the
    framework dispatched to has no in-repo caller, so it ran without being
    in any edge at all. That is the case `islands` has never had an answer
    for, and the one a trace is most worth having for.
    """
    if not store.connection.execute("SELECT rev FROM trace_runs WHERE rev=?", (rev,)).fetchone():
        return set()
    stale_paths = _stale_paths(store, rev)
    return {
        row["node_id"]
        for row in store.connection.execute(
            "SELECT node_id FROM trace_executed WHERE rev=?", (rev,)
        )
        if row["node_id"].partition("::")[0] not in stale_paths
    }


def summary(store: Store, rev: str) -> str:
    """One line for a report's summary field: what the trace is worth here.

    Printed by `islands` and `orphans` whether or not a trace exists --
    "nothing reaches this" is a far stronger claim with one than without, and
    a reader who is not told which they are reading cannot weigh it.
    """
    row = store.connection.execute(
        "SELECT observed, executed, confirmed, added, stale FROM trace_runs WHERE rev=?",
        (rev,),
    ).fetchone()
    if row is None:
        return NO_TRACE
    live = row["confirmed"] + row["added"]
    if row["observed"] and not live:
        return (
            f"stale -- all {row['observed']} observed calls name code that has changed"
            " since the trace was imported"
        )
    text = (
        f"{row['observed']} calls observed, {row['confirmed']} confirming a static edge,"
        f" {row['added']} runtime-only"
    )
    if row["stale"]:
        text += f", {row['stale']} stale"
    return text


def describe(store: Store, rev: str) -> str:
    """The `codegraph trace` report: several lines, for a human."""
    row = store.connection.execute("SELECT * FROM trace_runs WHERE rev=?", (rev,)).fetchone()
    if row is None:
        return ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["imported_at"]))
    lines = [
        f"trace for {rev}, imported {when} from {row['source']}",
        f"  {row['observed']} calls observed, {row['executed']} functions seen running",
        (
            f"  {row['confirmed']} confirm a static edge, {row['added']} add one"
            " the resolver did not have"
        ),
    ]
    if row["stale"]:
        stale_paths = sorted(_stale_paths(store, rev))
        named = ", ".join(stale_paths[:3])
        more = f" (+{len(stale_paths) - 3} more)" if len(stale_paths) > 3 else ""
        lines.append(
            f"  {row['stale']} stale: {len(stale_paths)} traced file(s) have changed"
            f" since -- {named}{more}"
        )
    if row["unmodelable"]:
        lines.append(
            f"  {row['unmodelable']} not modelled as calls (a module or class body"
            " executing, or a symbol this revision has no node for)"
        )
    return "\n".join(lines)


__all__ = [
    "ANONYMOUS_SCOPES",
    "NO_TRACE",
    "ImportResult",
    "Projection",
    "TraceMismatch",
    "collapse_anonymous",
    "describe",
    "forget",
    "import_trace",
    "is_anonymous",
    "observed_nodes",
    "project",
    "projection",
    "summary",
    "trace_identity",
    "traced_paths",
]
