"""Score a static call graph against a runtime trace (#35).

`read_static_graph` is the only part that touches the database; everything
else is a pure function over sets, so `tests/test_bench_scorer.py` can check
the arithmetic without a target repository anywhere near it.

What is scored, and what is deliberately not:

*Recall* is meaningful. A call the test suite made is a call that exists, so
a traced edge codegraph does not have is a real gap.

*Unconditional precision is not measurable this way and is not reported.* A
static edge that never appears in a trace is not thereby wrong -- the suite
may simply not cover it, and on a library most of the surface is not covered
by its own tests. What is defensible is conditional: among static HIGH edges
whose two endpoints BOTH executed at least once, how many did the trace
observe? Both endpoints running is what makes "and yet the call never
happened" evidence of anything at all. See `Report.conditional_precision`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codegraph.ambiguity import Ambiguity, last_segment
from codegraph.resolve import MODULE_SCOPE

#: The node kinds `parse.py` gives to things that are called. `module` and
#: `class` are the two it gives to things that are *executed* but not called
#: -- see `JUDGEABLE_KINDS`' use in `partition`.
CALLABLE_KINDS = frozenset({"function", "method"})

CONFIDENCE_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}

#: Code objects CPython creates for a scope that has no `def` and therefore no
#: node in `nodes`: `parse.py` records definitions, and a comprehension or a
#: lambda is not one. PY_START fires for them all the same.
ANONYMOUS_SCOPES = frozenset(
    {"<genexpr>", "<listcomp>", "<setcomp>", "<dictcomp>", "<lambda>"}
)

#: Confidence attributed to an edge that exists only as a query-time
#: expansion of an ambiguous reference. `resolve.py` writes no row for such a
#: reference precisely because every candidate would be LOW (#25), so LOW is
#: what it would have been.
EXPANDED_CONFIDENCE = "LOW"


def _qualname(node_id: str) -> str:
    return node_id.partition("::")[2]


def is_anonymous(node_id: str) -> bool:
    """Is this an anonymous scope -- a comprehension or a lambda?"""
    return last_segment(_qualname(node_id)) in ANONYMOUS_SCOPES


def collapse_anonymous(node_id: str) -> str:
    """`m.py::f.<locals>.<genexpr>` -> `m.py::f`, repeatedly.

    A comprehension runs in its own code object, so the trace attributes a
    call made inside one to `f.<locals>.<genexpr>`. codegraph attributes the
    same call site to `f`, because `parse.py` walks the AST and a
    comprehension is an expression inside `f`'s body, not a definition.
    Without this the two describe the same call under two different names and
    it scores as a miss -- a disagreement about node naming, not about the
    call graph. Measured on requests: 1 of 25 misses was exactly this
    (`_init.<locals>.<genexpr> -> _init.<locals>.doc`).

    A comprehension at module scope collapses to the module node, which is
    what `parse.py` attributes it to.
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


def _rename(traced: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Rename each edge's endpoints the way `parse.py` would name them.

    Only the anonymous-scope collapse; see `collapse_anonymous`, which is
    applied to the SOURCE. A self-edge that results (a comprehension inside
    `f` calling `f`) is dropped for the same reason the tracer drops
    recursion: codegraph does not model it.
    """
    renamed = {(collapse_anonymous(src), dst) for src, dst in traced}
    return {(src, dst) for src, dst in renamed if src != dst}


@dataclass(frozen=True)
class Trace:
    """One run of `bench/tracer.py`, with endpoints named as `parse.py` names
    them (see `collapse_anonymous`)."""

    edges: set[tuple[str, str]]
    #: Every in-repo function that ran, whether or not it has an in-repo
    #: caller. A test function invoked by pytest is in here and in no edge.
    executed: set[str]
    #: The subset of `edges` observed only with an out-of-repo Python frame in
    #: between -- `test_x` -> werkzeug's `Client.get` -> `FlaskClient.open`.
    #: Real, and unreachable by any static analysis of this repository: no
    #: call site in its text names the pair.
    indirect: set[tuple[str, str]]

    @classmethod
    def load(cls, payload: dict) -> Trace:
        """Build from the tracer's JSON, applying the anonymous-scope collapse
        to every endpoint so both sides use one naming scheme."""
        return cls(
            edges=_rename({(src, dst) for src, dst in payload["edges"]}),
            executed={collapse_anonymous(node) for node in payload["executed"]},
            indirect=_rename({(src, dst) for src, dst in payload.get("indirect", ())}),
        )


@dataclass(frozen=True)
class StaticGraph:
    """The graph codegraph's *commands* answer from, for one revision.

    That is `edges` PLUS the query-time expansion of ambiguous references.
    Scoring `edges` alone grades the storage layer rather than the resolver:
    an all-LOW fan-out is stored once in `unresolved` and expanded when a
    query asks, so those targets are answers codegraph gives while being
    rows it never wrote. `tests/test_accuracy.py` performs the same union
    for the same reason.
    """

    #: (src, dst) -> the best confidence any source of that edge carries.
    edges: dict[tuple[str, str], str]
    #: node id -> kind ('function' | 'method' | 'class' | 'module').
    kinds: dict[str, str]
    #: node id -> comma-joined decorator names, for classifying misses.
    decorators: dict[str, str] = field(default_factory=dict)
    #: dst -> every src that reaches it, for the source-attribution cause in
    #: `MISS_CAUSES`. Derived, not an input: rebuilding it per miss would be a
    #: scan of `edges`, and flask's graph has 90k of them.
    by_target: dict[str, set[str]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        for src, dst in self.edges:
            self.by_target.setdefault(dst, set()).add(src)

    def confidence(self, edge: tuple[str, str]) -> str | None:
        return self.edges.get(edge)

    def used_as_decorator(self, src: str, dst: str) -> bool:
        """Does `src`, or anything nested in it, apply `dst` as a decorator?

        `@with_appcontext` calls `with_appcontext` at definition time, and
        `@app.cli.command()` calls `AppGroup.command` -- real calls with no
        call expression in the body of anything. `nodes.decorators` records
        the names, which is how `query/islands.py` already reasons about
        implicit invocation (#27). The nested case is the common one on flask:
        the decorator sits on a view function defined inside the test.
        """
        name = last_segment(_qualname(dst))
        prefix = f"{src}."
        for node_id, decorators in self.decorators.items():
            if not decorators or (node_id != src and not node_id.startswith(prefix)):
                continue
            if any(last_segment(part) == name for part in decorators.split(",")):
                return True
        return False

    def reached_from_elsewhere_in_file(self, src: str, dst: str) -> bool:
        """Does some OTHER definition in `src`'s file statically call `dst`?"""
        path = src.partition("::")[0]
        return any(
            other != src and other.startswith(f"{path}::")
            for other in self.by_target.get(dst, ())
        )


@dataclass(frozen=True)
class Report:
    #: Traced edges after `collapse_anonymous`, which merges a few pairs that
    #: differ only in which comprehension inside the caller made the call.
    traced_total: int
    #: Traced edges dropped because the target is an anonymous scope -- a
    #: comprehension or a lambda. `nodes` holds definitions, so no node for
    #: one can ever exist and no edge to one is judgeable. Kept apart from
    #: `target_unknown` so a parse gap cannot hide among them.
    anonymous_target: int
    #: Traced edges dropped because the target is a module or class body:
    #: import-time and definition-time execution, not a call. See `partition`.
    body_execution: int
    #: Traced edges dropped because the target is not in `nodes` at all.
    #: Reported separately from `body_execution` because this one is NOT a
    #: category error in the trace -- it is a hole in codegraph's own view of
    #: the tree, and folding it into the "not a call" bucket would let a
    #: parse failure quietly improve the score.
    target_unknown: int
    #: The denominator: traced edges whose target is a function or method.
    judgeable: int
    found: int
    found_high_medium: int
    misses: list[tuple[str, str]]
    #: cause label -> the misses it explains, in the order `MISS_CAUSES` tries.
    miss_causes: dict[str, list[tuple[str, str]]]
    #: Static HIGH edges with both endpoints executed, and the observed subset.
    testable_high: int
    observed_high: int
    #: Examples of `target_unknown`, so the number can be acted on.
    unknown_examples: list[tuple[str, str]] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.found / self.judgeable if self.judgeable else 1.0

    @property
    def recall_high_medium(self) -> float:
        return self.found_high_medium / self.judgeable if self.judgeable else 1.0

    @property
    def conditional_precision(self) -> float:
        """Observed / testable among static HIGH edges. NOT precision.

        1.0 here does not mean codegraph invents no edges; it means every
        HIGH edge whose endpoints both ran was in fact taken. The edges whose
        endpoints never both ran are unjudged, and there are usually more of
        them than of these.
        """
        return self.observed_high / self.testable_high if self.testable_high else 1.0


def read_static_graph(store, rev: str) -> StaticGraph:
    """Read `edges` and the ambiguity expansion for `rev` into one graph."""
    connection = store.connection
    kinds: dict[str, str] = {}
    decorators: dict[str, str] = {}
    for row in connection.execute("SELECT id, kind, decorators FROM nodes WHERE rev=?", (rev,)):
        kinds[row["id"]] = row["kind"]
        decorators[row["id"]] = row["decorators"]

    edges: dict[tuple[str, str], str] = {}

    def offer(src: str, dst: str, confidence: str) -> None:
        edge = (src, dst)
        current = edges.get(edge)
        if current is None or CONFIDENCE_RANK[confidence] > CONFIDENCE_RANK[current]:
            edges[edge] = confidence

    for row in connection.execute(
        "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
    ):
        offer(row["src"], row["dst"], row["confidence"])

    ambiguity = Ambiguity(store, rev)
    # One expansion per (src, name), not per row: a function calling
    # `item.save()` twice writes two `unresolved` rows for one relationship.
    seen: set[tuple[str, str]] = set()
    for row in connection.execute(
        "SELECT src, raw_name FROM unresolved WHERE rev=? AND reason='ambiguous'"
        " AND ref_kind='call'",
        (rev,),
    ):
        key = (row["src"], last_segment(row["raw_name"]))
        if key in seen:
            continue
        seen.add(key)
        for candidate in ambiguity.candidates(row["raw_name"]):
            offer(row["src"], candidate, EXPANDED_CONFIDENCE)

    return StaticGraph(edges=edges, kinds=kinds, decorators=decorators)


@dataclass(frozen=True)
class Partition:
    """Traced edges split by whether codegraph could ever have had them."""

    judgeable: set[tuple[str, str]]
    #: Target is a module or class body: import-time / definition-time
    #: execution, not a call.
    body: set[tuple[str, str]]
    #: Target is a comprehension or lambda: no definition, so never a node.
    anonymous: set[tuple[str, str]]
    #: Target is a function codegraph does not know about at all.
    unknown: set[tuple[str, str]]


def partition(traced: set[tuple[str, str]], graph: StaticGraph) -> Partition:
    """Split traced edges by whether codegraph's CALLS edges model them.

    THIS FILTER IS THE BENCHMARK. Do not remove it, and do not widen it to
    "target is any node".

    `sys.monitoring`'s PY_START fires whenever a Python *code object* starts
    executing, and a module body and a class body are code objects. So
    importing `requests` traces `__init__.py::<module> -> api.py::<module>`,
    and defining a class traces `mod.py::<module> -> mod.py::Cls`. Neither is
    a call: the first is an import (codegraph has an `imports` table for it)
    and the second is definition-time execution. codegraph's CALLS edges do
    not model either, by design, so counting them as missed calls measures
    nothing about the resolver.

    Measured on psf/requests: with the filter, recall is 0.93 over 88
    judgeable calls; without it, 0.51 over 172 -- and the miss list is
    dominated by `<module> -> <module>` pairs. The 0.51 is not a worse honest
    number, it is a wrong one, and it would send a reader chasing a
    non-problem.

    The filter is on the TARGET only. A module body is a legitimate *caller*
    -- `app = create_app()` at import time is a call codegraph records, with
    the synthetic `path::<module>` node as its `src`.

    Two further cases are kept apart rather than folded in, so that neither
    can quietly raise the score:

    - the target is an anonymous scope (a comprehension, a lambda). `nodes`
      holds definitions, so no such node exists at any revision.
    - the target is a function `nodes` does not contain. That is a hole in
      codegraph's view of the tree -- a parse failure or a naming
      disagreement -- and it is reported with examples, not hidden.
    """
    result = Partition(judgeable=set(), body=set(), anonymous=set(), unknown=set())
    for edge in traced:
        kind = graph.kinds.get(edge[1])
        if kind in CALLABLE_KINDS:
            result.judgeable.add(edge)
        elif is_anonymous(edge[1]):
            result.anonymous.add(edge)
        elif kind is None:
            result.unknown.add(edge)
        else:
            result.body.add(edge)
    return result


def _is_dunder(node_id: str) -> bool:
    name = last_segment(node_id.partition("::")[2])
    return name.startswith("__") and name.endswith("__")


#: (label, predicate) tried in order; the first match explains the miss.
#: Every label says what KIND of thing is being missed, because "6 misses" is
#: not actionable and "6 dunders invoked by syntax" is: on requests all six
#: were `d[k]`, `d[k] = v`, `for x in jar` and `len(f)` -- one coherent gap
#: (#27's islands cause 1, a real call with no call site in the text), not
#: scattered noise.
MISS_CAUSES: list[tuple[str, object]] = [
    (
        (
            "reached through an out-of-repo frame: no call site in this repository"
            " names the pair, so no static analysis of it could"
        ),
        lambda src, dst, graph, trace: (src, dst) in trace.indirect,
    ),
    (
        (
            "call runs at module scope but codegraph attributes it to a definition"
            " in the same file (a decorator's arguments are the usual case)"
        ),
        lambda src, dst, graph, trace: _qualname(src) == MODULE_SCOPE
        and graph.reached_from_elsewhere_in_file(src, dst),
    ),
    (
        (
            "target is a constructor: `Cls()` DOES have a call site, so this is a"
            " resolution gap rather than implicit invocation"
        ),
        lambda src, dst, graph, trace: last_segment(_qualname(dst)) == "__init__",
    ),
    (
        "target is a dunder: invoked by syntax or protocol, no call site in the text",
        lambda src, dst, graph, trace: _is_dunder(dst),
    ),
    (
        "target is decorated: the decorator may register, wrap or replace it",
        lambda src, dst, graph, trace: bool(graph.decorators.get(dst)),
    ),
    (
        "target is a test function: invoked by the test runner, not by repo code",
        lambda src, dst, graph, trace: last_segment(dst.partition("::")[2]).startswith("test_"),
    ),
    (
        "target is nested in another function: reached through a local variable",
        lambda src, dst, graph, trace: "<locals>" in dst,
    ),
    (
        (
            "target is applied as a decorator by the source or something nested in"
            " it: a call at definition time, with no call expression anywhere"
        ),
        lambda src, dst, graph, trace: graph.used_as_decorator(src, dst),
    ),
    (
        "source node is absent from the graph: nothing could have been recorded",
        lambda src, dst, graph, trace: src not in graph.kinds,
    ),
    (
        "no implicit-invocation mechanism recognised",
        lambda src, dst, graph, trace: True,
    ),
]


def classify(
    misses: set[tuple[str, str]], graph: StaticGraph, trace: Trace
) -> dict[str, list[tuple[str, str]]]:
    grouped: dict[str, list[tuple[str, str]]] = {}
    for src, dst in sorted(misses):
        for label, predicate in MISS_CAUSES:
            if predicate(src, dst, graph, trace):  # type: ignore[operator]
                grouped.setdefault(label, []).append((src, dst))
                break
    return grouped


def score(trace: Trace, graph: StaticGraph) -> Report:
    split = partition(trace.edges, graph)
    judgeable = split.judgeable
    found = {edge for edge in judgeable if graph.confidence(edge) is not None}
    high_medium = {
        edge for edge in found if CONFIDENCE_RANK[graph.edges[edge]] >= CONFIDENCE_RANK["MEDIUM"]
    }
    misses = judgeable - found

    # Conditional precision. Restricted to function/method targets for the
    # same reason `partition` is: a HIGH edge to a class node (`Cls()`
    # resolves to the class, and `resolve.with_constructors` adds the
    # `__init__` edge beside it) can never be traced, so leaving it in the
    # denominator would penalise codegraph for an edge shape the trace
    # cannot express.
    ran = trace.executed
    testable = {
        edge
        for edge, confidence in graph.edges.items()
        if confidence == "HIGH"
        and graph.kinds.get(edge[1]) in CALLABLE_KINDS
        and edge[0] in ran
        and edge[1] in ran
    }
    return Report(
        traced_total=len(trace.edges),
        anonymous_target=len(split.anonymous),
        body_execution=len(split.body),
        target_unknown=len(split.unknown),
        judgeable=len(judgeable),
        found=len(found),
        found_high_medium=len(high_medium),
        misses=sorted(misses),
        miss_causes=classify(misses, graph, trace),
        testable_high=len(testable),
        observed_high=len(testable & trace.edges),
        unknown_examples=sorted(split.unknown)[:5],
    )


def format_report(name: str, report: Report) -> str:
    """The human-readable form. Full miss list, always: the point of the
    benchmark is what is missed, and a truncated list turns a finding back
    into a number."""
    lines = [
        f"=== {name}",
        f"traced                        : {report.traced_total} edges",
        (
            f"  module/class body execution : {report.body_execution}"
            "  (not calls; see partition())"
        ),
        f"  comprehension/lambda target : {report.anonymous_target}  (never a node)",
        f"  target unknown to the graph : {report.target_unknown}",
        f"  judgeable function calls    : {report.judgeable}",
        f"  found                       : {report.found}   RECALL {report.recall:.2f}",
        (
            f"  found at HIGH/MEDIUM        : {report.found_high_medium}"
            f"   ({report.recall_high_medium:.2f})"
        ),
        "",
        "conditional precision (NOT precision: an untraced static edge is not",
        "wrong, the suite may not cover it -- unconditional precision is not",
        "measurable from a trace):",
        f"  static HIGH edges with both endpoints executed : {report.testable_high}",
        (
            f"  of those, observed in the trace                : {report.observed_high}"
            f"   ({report.conditional_precision:.2f})"
        ),
    ]
    if report.unknown_examples:
        lines += ["", "targets unknown to the graph (examples):"]
        lines += [f"  {src} -> {dst}" for src, dst in report.unknown_examples]
    lines += ["", f"misses by cause ({len(report.misses)}):"]
    for label, edges in report.miss_causes.items():
        lines.append(f"  [{len(edges)}] {label}")
        lines += [f"      {src} -> {dst}" for src, dst in edges]
    return "\n".join(lines)


__all__ = [
    "ANONYMOUS_SCOPES",
    "CALLABLE_KINDS",
    "MISS_CAUSES",
    "Partition",
    "Report",
    "StaticGraph",
    "Trace",
    "classify",
    "collapse_anonymous",
    "format_report",
    "is_anonymous",
    "partition",
    "read_static_graph",
    "score",
]
