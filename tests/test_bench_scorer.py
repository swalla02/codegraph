"""Unit tests for the runtime-trace scorer (`bench/score.py`, #35).

The benchmark itself needs a target repository, a virtualenv and a full test
suite run, and lives under `bench/` for that reason. Its arithmetic does not:
`score` is a pure function over sets, and getting it wrong would mean
publishing a wrong number about the resolver. So the arithmetic is checked
here, with synthetic traces, in milliseconds.
"""

from bench.score import (
    Floor,
    Report,
    StaticGraph,
    Trace,
    check_floor,
    classify,
    collapse_anonymous,
    read_static_graph,
    score,
)

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.store import Store


def trace(edges, executed=(), indirect=()):
    return Trace.load(
        {
            "edges": [list(edge) for edge in edges],
            "indirect": [list(edge) for edge in indirect],
            "executed": list(executed),
        }
    )


def test_module_and_class_body_execution_is_not_counted_as_a_missed_call():
    """The filter the whole benchmark rests on. PY_START fires for a module
    body and a class body; neither is a call, and counting them dropped
    requests' measured recall from 0.93 to 0.51 with a miss list full of
    `<module> -> <module>`."""
    graph = StaticGraph(
        edges={("a.py::caller", "b.py::callee"): "HIGH"},
        kinds={
            "a.py::caller": "function",
            "b.py::callee": "function",
            "a.py::<module>": "module",
            "b.py::<module>": "module",
            "b.py::Cls": "class",
        },
    )
    report = score(
        trace(
            [
                ("a.py::caller", "b.py::callee"),
                ("a.py::<module>", "b.py::<module>"),
                ("b.py::<module>", "b.py::Cls"),
            ]
        ),
        graph,
    )
    assert report.judgeable == 1
    assert report.body_execution == 2
    assert report.recall == 1.0


def test_recall_at_high_medium_excludes_a_target_found_only_by_the_low_fan_out():
    graph = StaticGraph(
        edges={
            ("a.py::caller", "b.py::sure"): "HIGH",
            ("a.py::caller", "b.py::guess"): "LOW",
        },
        kinds={"a.py::caller": "function", "b.py::sure": "function", "b.py::guess": "function"},
    )
    report = score(
        trace([("a.py::caller", "b.py::sure"), ("a.py::caller", "b.py::guess")]),
        graph,
    )
    assert report.recall == 1.0
    assert report.found_high_medium == 1
    assert report.recall_high_medium == 0.5


def test_a_target_the_graph_does_not_know_is_reported_rather_than_dropped_quietly():
    """A traced call to a function absent from `nodes` is a hole in
    codegraph's view of the tree. It is excluded from the denominator (nothing
    could match it) but counted and shown, so a parse failure cannot raise the
    score in silence."""
    graph = StaticGraph(edges={}, kinds={"a.py::caller": "function"})
    report = score(trace([("a.py::caller", "b.py::never_parsed")]), graph)
    assert report.judgeable == 0
    assert report.target_unknown == 1
    assert report.unknown_examples == [("a.py::caller", "b.py::never_parsed")]


def test_a_comprehension_is_attributed_to_the_definition_that_contains_it():
    """codegraph records a call made inside a comprehension from the
    enclosing definition, because a comprehension is not a definition. The
    trace names it `f.<locals>.<genexpr>`; without collapsing that, the two
    describe the same call under different names and it scores as a miss."""
    assert collapse_anonymous("a.py::f.<locals>.<genexpr>") == "a.py::f"
    assert collapse_anonymous("a.py::f.<locals>.g.<locals>.<listcomp>") == "a.py::f.<locals>.g"
    assert collapse_anonymous("a.py::<lambda>") == "a.py::<module>"

    graph = StaticGraph(
        edges={("a.py::f", "b.py::callee"): "HIGH"},
        kinds={"a.py::f": "function", "b.py::callee": "function"},
    )
    report = score(trace([("a.py::f.<locals>.<genexpr>", "b.py::callee")]), graph)
    assert report.recall == 1.0


def test_conditional_precision_ignores_static_edges_whose_endpoints_never_ran():
    """The reason unconditional precision is not reported at all: an untraced
    static edge is only evidence of anything if both its endpoints executed,
    and most of a library's surface is not exercised by its own tests."""
    graph = StaticGraph(
        edges={
            ("a.py::ran", "b.py::also_ran"): "HIGH",
            ("a.py::ran", "b.py::never_ran"): "HIGH",
            ("a.py::ran", "b.py::low_guess"): "LOW",
        },
        kinds={
            "a.py::ran": "function",
            "b.py::also_ran": "function",
            "b.py::never_ran": "function",
            "b.py::low_guess": "function",
        },
    )
    report = score(trace([], executed=["a.py::ran", "b.py::also_ran", "b.py::low_guess"]), graph)
    # Only the first edge is testable: the second has an endpoint that never
    # ran, and the third is not HIGH.
    assert report.testable_high == 1
    assert report.observed_high == 0
    assert report.conditional_precision == 0.0


def test_each_miss_is_explained_by_the_first_cause_that_applies():
    """A miss count is not actionable; a miss count per mechanism is. On
    requests all six misses at recall 0.93 were dunders invoked by syntax."""
    graph = StaticGraph(
        edges={},
        kinds={
            "a.py::caller": "function",
            "b.py::Cls.__getitem__": "method",
            "b.py::handler": "function",
            "b.py::outer.<locals>.inner": "function",
        },
        decorators={"b.py::handler": "app.route"},
    )
    misses = {
        ("a.py::caller", "b.py::Cls.__getitem__"),
        ("a.py::caller", "b.py::handler"),
        ("a.py::caller", "b.py::outer.<locals>.inner"),
        ("a.py::caller", "b.py::reached_via_library"),
    }
    grouped = classify(
        misses, graph, trace([], indirect=[("a.py::caller", "b.py::reached_via_library")])
    )
    labels = {edge: label for label, edges in grouped.items() for edge in edges}
    assert "dunder" in labels[("a.py::caller", "b.py::Cls.__getitem__")]
    assert "decorated" in labels[("a.py::caller", "b.py::handler")]
    assert "nested" in labels[("a.py::caller", "b.py::outer.<locals>.inner")]
    # Out-of-repo frames win over every other cause: no call site in this
    # repository names the pair, so it is not a resolution failure at all.
    assert "out-of-repo" in labels[("a.py::caller", "b.py::reached_via_library")]
    assert sum(len(edges) for edges in grouped.values()) == len(misses)


def test_the_scored_graph_includes_the_query_time_ambiguity_expansion(repo, write):
    """The property that keeps the benchmark honest about the resolver rather
    than about the storage layer: an all-LOW fan-out is never written to
    `edges` (#25), it is expanded when a query asks.
    `tests/test_resolution_rules.py`
    unions the same two sources, and scoring `edges` alone would report a
    target as missed while every real query returns it."""
    write("box.py", "class One:\n    def save(self):\n        pass\n")
    write("crate.py", "class Two:\n    def save(self):\n        pass\n")
    write("use.py", "def go(item):\n    item.save()\n", commit="fan-out")

    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    graph = read_static_graph(store, "HEAD")

    materialized = {
        (row["src"], row["dst"])
        for row in store.connection.execute(
            "SELECT src, dst FROM edges WHERE rev='HEAD' AND kind='CALLS'"
        )
    }
    traced = [("use.py::go", "box.py::One.save"), ("use.py::go", "crate.py::Two.save")]
    assert not any(edge in materialized for edge in traced)  # nothing was stored

    report = score(trace(traced), graph)
    assert report.judgeable == 2
    assert report.found == 2
    assert report.found_high_medium == 0  # the fan-out is LOW, as resolve.py would have written it
    store.close()


def test_a_floor_check_names_every_metric_and_fails_only_below_the_floor():
    """The effectiveness floors live on `bench/` output, per target (#39).

    Three metrics rather than one, because recall alone hides the change that
    matters most: #38 moved 158 flask edges from a LOW bare-name fan-out to a
    resolved HIGH answer and left recall flat at 0.29. A floor on recall
    would not have noticed if that regressed; a floor on recall at
    HIGH/MEDIUM does.
    """
    floor = Floor(recall=0.70, recall_high_medium=0.60, conditional_precision=0.90)
    report = _report(recall=0.70, recall_high_medium=0.60, conditional_precision=0.90)

    checks = check_floor(report, floor)
    assert [check.metric for check in checks] == [
        "recall",
        "recall at HIGH/MEDIUM",
        "conditional precision",
    ]
    assert all(check.ok for check in checks)  # at the floor is not below it

    # Each metric is checked on its own: a confidence regression that leaves
    # recall untouched still fails.
    dropped = _report(recall=0.70, recall_high_medium=0.59, conditional_precision=0.90)
    failed = [check for check in check_floor(dropped, floor) if not check.ok]
    assert [check.metric for check in failed] == ["recall at HIGH/MEDIUM"]


def _report(*, recall: float, recall_high_medium: float, conditional_precision: float) -> Report:
    """A report with the three floored ratios and nothing else that matters."""
    judgeable = 100
    testable_high = 100
    return Report(
        traced_total=judgeable,
        anonymous_target=0,
        body_execution=0,
        target_unknown=0,
        judgeable=judgeable,
        found=round(recall * judgeable),
        found_high_medium=round(recall_high_medium * judgeable),
        misses=[],
        miss_causes={},
        testable_high=testable_high,
        observed_high=round(conditional_precision * testable_high),
    )


# -- #56: the benchmark must not be handed its own answer --------------------


def test_the_scorer_reads_only_what_the_resolver_wrote(tmp_path):
    """A revision carrying an imported trace projects those observations
    into `edges` as `runtime` rows. Scoring them would compare the trace
    against itself: every observed call found, recall 1.00, and a number
    that says nothing about the resolver. `read_static_graph` filters on
    provenance, and this is the test that keeps it filtering.
    """
    from bench.score import read_static_graph

    from codegraph.store import Store

    store = Store.open(tmp_path)
    connection = store.connection
    connection.execute(
        "INSERT INTO nodes(rev, id, path, qualname, kind, line_start, line_end,"
        " body_hash, name_binding) VALUES('r', 'm.py::f', 'm.py', 'f', 'function',"
        " 1, 2, 'h', 'live')"
    )
    connection.executemany(
        "INSERT INTO edges(rev, src, dst, kind, confidence, provenance, callsite_path,"
        " callsite_line) VALUES(?,?,?,?,?,?,?,?)",
        [
            ("r", "m.py::a", "m.py::f", "CALLS", "HIGH", "static", "m.py", 3),
            ("r", "m.py::b", "m.py::f", "CALLS", "HIGH", "runtime", "m.py", 1),
        ],
    )
    connection.commit()

    graph = read_static_graph(store, "r")
    assert ("m.py::a", "m.py::f") in graph.edges
    assert ("m.py::b", "m.py::f") not in graph.edges
    store.close()
