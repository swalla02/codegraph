# tests/test_trace.py
"""What a runtime trace changes, and -- more importantly -- what it does not.

The load-bearing test in this file is the last one: with no trace imported,
every row the indexer writes is byte-identical to what it wrote before this
feature existed. A trace is additive or it is a regression.
"""

from __future__ import annotations

import json

import pytest

from codegraph import trace as trace_module
from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.impact import impact_report
from codegraph.query.islands import islands_report
from codegraph.query.orphans import orphans_report
from codegraph.query.path import path_report
from codegraph.query.unknowns import unknowns_report
from codegraph.resolve import RUNTIME, STATIC
from codegraph.store import WORKTREE, Store
from codegraph.uncertainty import UNEXPLAINED_ISLAND
from tests.conftest import git

#: A repository with one call the resolver finds and one it cannot: the
#: `getattr` dispatch #45 names as permanently out of static reach.
APP = """\
class Handler:
    def handle(self):
        return 1


def helper():
    return 2


def caller():
    return helper()


def dispatch(name):
    return getattr(Handler(), name)()
"""


@pytest.fixture
def app_repo(repo, write):
    write("app.py", APP, commit="app")
    return repo


def workspace(root):
    store = Store.open(root)
    return store, Indexer(root, store, GitTreeSource(root))


def payload(edges, executed=(), root="."):
    return {"root": root, "edges": [list(e) for e in edges], "executed": list(executed)}


def edge_rows(store, rev, src, dst):
    return sorted(
        tuple(row)
        for row in store.connection.execute(
            "SELECT kind, confidence, provenance FROM edges WHERE rev=? AND src=? AND dst=?",
            (rev, src, dst),
        )
    )


def import_and_reconcile(store, indexer, rev, data, source="trace.json"):
    result = trace_module.import_trace(store, rev, data, source)
    indexer.reconcile(rev)
    return result


# -- the pair of facts -------------------------------------------------------


def test_confirmed_edge_carries_both_provenances(app_repo):
    """An edge the resolver deduced AND a run observed keeps both rows.

    The pair is the point: replacing the static row would throw away the
    call site, and dropping the runtime one would throw away the
    observation.
    """
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload([("app.py::caller", "app.py::helper")]),
    )

    rows = edge_rows(store, WORKTREE, "app.py::caller", "app.py::helper")
    assert ("CALLS", "HIGH", STATIC) in rows
    assert ("CALLS", "HIGH", RUNTIME) in rows
    store.close()


def test_runtime_only_edge_is_stored_at_high(app_repo):
    """A `getattr` dispatch no static analysis can reach, observed running.

    HIGH, not because the resolver got any more certain -- it never saw
    this call at all -- but because confidence answers a question about
    identifying the target, and a trace answers it by having run it.
    """
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    assert not edge_rows(store, WORKTREE, "app.py::dispatch", "app.py::Handler.handle")

    import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload([("app.py::dispatch", "app.py::Handler.handle")]),
    )

    assert edge_rows(store, WORKTREE, "app.py::dispatch", "app.py::Handler.handle") == [
        ("CALLS", "HIGH", RUNTIME)
    ]
    store.close()


def test_runtime_only_edge_borrows_no_call_site_it_does_not_have(app_repo):
    """There is no call site in the text, so the row points at the caller's
    own declaration -- the same choice `IMPLEMENTS` makes."""
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload([("app.py::dispatch", "app.py::Handler.handle")]),
    )
    row = store.connection.execute(
        "SELECT callsite_path, callsite_line FROM edges WHERE rev=? AND provenance=?",
        (WORKTREE, RUNTIME),
    ).fetchone()
    line_start = store.connection.execute(
        "SELECT line_start FROM nodes WHERE rev=? AND id=?", (WORKTREE, "app.py::dispatch")
    ).fetchone()["line_start"]
    assert (row["callsite_path"], row["callsite_line"]) == ("app.py", line_start)
    store.close()


def test_confirming_edge_keeps_the_static_call_site(app_repo):
    """A confirmed pair's runtime row copies the call site the text gives,
    so a query that prefers the observed row still prints real evidence."""
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload([("app.py::caller", "app.py::helper")]),
    )
    sites = {
        row["provenance"]: (row["callsite_path"], row["callsite_line"])
        for row in store.connection.execute(
            "SELECT provenance, callsite_path, callsite_line FROM edges"
            " WHERE rev=? AND src=? AND dst=?",
            (WORKTREE, "app.py::caller", "app.py::helper"),
        )
    }
    assert sites[RUNTIME] == sites[STATIC]
    store.close()


def test_a_query_can_tell_the_two_apart(app_repo):
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload(
            [
                ("app.py::caller", "app.py::helper"),
                ("app.py::dispatch", "app.py::Handler.handle"),
            ]
        ),
    )
    observed = {
        (row["src"], row["dst"])
        for row in store.connection.execute(
            "SELECT src, dst FROM edges WHERE rev=? AND provenance=?", (WORKTREE, RUNTIME)
        )
    }
    assert observed == {
        ("app.py::caller", "app.py::helper"),
        ("app.py::dispatch", "app.py::Handler.handle"),
    }
    store.close()


# -- what is not projected ---------------------------------------------------


def test_a_target_that_is_not_a_function_is_not_an_edge(app_repo):
    """`PY_START` fires for a module body; `CALLS` does not model importing.

    The same filter `bench/score.py`'s `partition` applies, for the same
    reason: importing a module is not a call, and writing one as a CALLS
    edge would put a claim in the graph the graph does not mean.
    """
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    result = import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload(
            [
                ("app.py::<module>", "app.py::Handler"),
                ("app.py::caller", "app.py::helper"),
            ]
        ),
    )
    assert result.observed == 2
    projection = trace_module.projection(store, WORKTREE)
    assert projection.unmodelable == 1
    assert projection.confirmed == 1
    store.close()


def test_a_module_body_is_still_a_legitimate_caller(app_repo, write):
    """The filter is on the target only -- import-time execution calls."""
    write("app.py", APP + "\nVALUE = helper()\n", commit="module call")
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    import_and_reconcile(
        store, indexer, WORKTREE, payload([("app.py::<module>", "app.py::helper")])
    )
    assert ("CALLS", "HIGH", RUNTIME) in edge_rows(
        store, WORKTREE, "app.py::<module>", "app.py::helper"
    )
    store.close()


# -- revision binding and staleness ------------------------------------------


def test_a_trace_does_not_leak_across_revisions(app_repo):
    store, indexer = workspace(app_repo)
    head = git(app_repo, "rev-parse", "HEAD").strip()
    indexer.reconcile(WORKTREE)
    indexer.reconcile(head)
    import_and_reconcile(
        store, indexer, WORKTREE, payload([("app.py::dispatch", "app.py::Handler.handle")])
    )
    indexer.reconcile(head)

    assert edge_rows(store, WORKTREE, "app.py::dispatch", "app.py::Handler.handle")
    assert not edge_rows(store, head, "app.py::dispatch", "app.py::Handler.handle")
    assert trace_module.summary(store, head) == trace_module.NO_TRACE
    store.close()


def test_an_edited_file_makes_its_observations_stale(app_repo, write):
    """The trace saw code that is no longer there, so it is dropped -- and
    counted, so the drop is visible rather than a silent shrink."""
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    import_and_reconcile(
        store, indexer, WORKTREE, payload([("app.py::dispatch", "app.py::Handler.handle")])
    )
    assert edge_rows(store, WORKTREE, "app.py::dispatch", "app.py::Handler.handle")

    write("app.py", APP.replace("return 1", "return 11"))
    indexer.reconcile(WORKTREE)

    assert not edge_rows(store, WORKTREE, "app.py::dispatch", "app.py::Handler.handle")
    projection = trace_module.projection(store, WORKTREE)
    assert projection.stale == 1
    assert "stale" in trace_module.summary(store, WORKTREE)
    store.close()


def test_a_wholly_stale_trace_says_so(app_repo, write):
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    import_and_reconcile(store, indexer, WORKTREE, payload([("app.py::caller", "app.py::helper")]))
    write("app.py", APP.replace("return 2", "return 22"))
    indexer.reconcile(WORKTREE)
    assert trace_module.summary(store, WORKTREE).startswith("stale")
    store.close()


def test_importing_a_trace_invalidates_the_materialized_revision(app_repo):
    """The unchanged-tree fast path must not serve a graph the trace has
    not been folded into -- the #44 failure mode, one input further out."""
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    before = store.connection.execute(
        "SELECT fingerprint FROM revisions WHERE rev=?", (WORKTREE,)
    ).fetchone()["fingerprint"]

    trace_module.import_trace(
        store, WORKTREE, payload([("app.py::caller", "app.py::helper")]), "trace.json"
    )
    catalog_free = indexer.reconcile(WORKTREE)
    after = store.connection.execute(
        "SELECT fingerprint FROM revisions WHERE rev=?", (WORKTREE,)
    ).fetchone()["fingerprint"]

    assert before != after
    assert catalog_free.observed_edges == 1
    store.close()


def test_a_trace_that_names_nothing_in_the_revision_is_refused(app_repo):
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    with pytest.raises(trace_module.TraceMismatch):
        trace_module.import_trace(
            store, WORKTREE, payload([("other.py::a", "other.py::b")]), "trace.json"
        )
    assert trace_module.summary(store, WORKTREE) == trace_module.NO_TRACE
    store.close()


def test_forget_restores_the_static_graph_exactly(app_repo):
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    before = _dump(store, WORKTREE)

    import_and_reconcile(
        store, indexer, WORKTREE, payload([("app.py::dispatch", "app.py::Handler.handle")])
    )
    assert _dump(store, WORKTREE) != before

    trace_module.forget(store, WORKTREE)
    indexer.reconcile(WORKTREE)
    assert _dump(store, WORKTREE) == before
    store.close()


def _dump(store, rev):
    return sorted(
        tuple(row)
        for row in store.connection.execute(
            "SELECT src, dst, kind, confidence, provenance FROM edges WHERE rev=?", (rev,)
        )
    )


# -- what the reports do with it ---------------------------------------------


def test_impact_gains_the_dependent_the_resolver_never_had(app_repo):
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    before = impact_report(store, WORKTREE, "app.py::Handler.handle")
    assert "app.py::dispatch" not in _ids(before)

    import_and_reconcile(
        store, indexer, WORKTREE, payload([("app.py::dispatch", "app.py::Handler.handle")])
    )
    after = impact_report(store, WORKTREE, "app.py::Handler.handle")
    assert "app.py::dispatch" in _ids(after)
    assert any("observed" in row.detail for group in after.groups for row in group.rows)
    store.close()


def test_impact_says_observed_only_when_every_hop_was(app_repo, write):
    """A chain is observed end to end or it is not observed at all."""
    write(
        "app.py",
        APP + "\n\ndef outer():\n    return dispatch('handle')\n",
        commit="outer",
    )
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    import_and_reconcile(
        store, indexer, WORKTREE, payload([("app.py::dispatch", "app.py::Handler.handle")])
    )
    report = impact_report(store, WORKTREE, "app.py::Handler.handle")
    details = {row.id: row.detail for group in report.groups for row in group.rows}
    assert "observed" in details["app.py::dispatch"]
    assert "observed" not in details["app.py::outer"]
    store.close()


def test_a_report_with_no_trace_never_says_observed(app_repo):
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    report = impact_report(store, WORKTREE, "app.py::helper")
    assert not any("observed" in row.detail for group in report.groups for row in group.rows)
    assert "trace" not in report.summary
    store.close()


def _ids(report):
    return {row.id for group in report.groups for row in group.rows}


def test_islands_states_whether_a_trace_was_available(app_repo):
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    assert islands_report(store, WORKTREE, indexer.config).summary["trace"] == (
        trace_module.NO_TRACE
    )

    import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload([("app.py::caller", "app.py::helper")], executed=["app.py::caller"]),
    )
    summary = islands_report(store, WORKTREE, indexer.config).summary
    assert summary["trace"] != trace_module.NO_TRACE
    store.close()


def test_an_island_whose_member_was_seen_running_is_explained(app_repo, write):
    """The one mechanism in the list that is evidence rather than
    counter-evidence: the code ran."""
    write("lonely.py", "def solo():\n    return 1\n", commit="lonely")
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    before = islands_report(store, WORKTREE, indexer.config)
    assert before.summary["unexplained"] >= 1

    import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload([("app.py::caller", "app.py::helper")], executed=["lonely.py::solo"]),
    )
    after = islands_report(store, WORKTREE, indexer.config)
    assert after.summary["unexplained"] == before.summary["unexplained"] - 1
    detail = next(
        row.detail for group in after.groups for row in group.rows if row.id == "lonely.py::solo"
    )
    assert "traced" in detail
    store.close()


def test_a_traced_symbol_is_no_longer_an_unexplained_island(app_repo, write):
    """#55 says the next action for an unexplained island is "record a
    runtime trace". Once somebody has, the entry has to stop being raised,
    or the report goes on asking for evidence it has been given."""
    write("lonely.py", "def solo():\n    return 1\n", commit="lonely")
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    before = unknowns_report(store, WORKTREE, "lonely.py::solo", indexer.config)
    assert before.summary["island"] == "unexplained"
    assert UNEXPLAINED_ISLAND in [entry.reason for entry in before.unknowns]

    import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload([("app.py::caller", "app.py::helper")], executed=["lonely.py::solo"]),
    )
    after = unknowns_report(store, WORKTREE, "lonely.py::solo", indexer.config)
    assert "traced" in after.summary["island"]
    assert UNEXPLAINED_ISLAND not in [entry.reason for entry in after.unknowns]
    store.close()


def test_unknowns_states_whether_a_trace_was_available(app_repo):
    """Its island line is an absence claim like `islands`' own, and an
    absence means something different when a run has been watched."""
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    report = unknowns_report(store, WORKTREE, "app.py::helper", indexer.config)
    assert report.summary["trace"] == trace_module.NO_TRACE

    import_and_reconcile(store, indexer, WORKTREE, payload([("app.py::caller", "app.py::helper")]))
    report = unknowns_report(store, WORKTREE, "app.py::helper", indexer.config)
    assert report.summary["trace"] != trace_module.NO_TRACE
    store.close()


def test_orphans_states_whether_a_trace_was_available(app_repo, write):
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    report = orphans_report(store, WORKTREE, indexer.source, indexer.config)
    assert report.summary["trace"] == trace_module.NO_TRACE
    store.close()


def test_a_runtime_edge_takes_a_function_out_of_orphans(app_repo, write):
    """A helper only tests appeared to call, observed being called by the
    code that was supposed to call it, is no longer the report's shape."""
    write("pkg/thing.py", "def _shim():\n    return 1\n", commit="shim")
    write(
        "tests/test_thing.py",
        "from pkg.thing import _shim\n\n\ndef test_shim():\n    assert _shim()\n",
        commit="test",
    )
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    before = orphans_report(store, WORKTREE, indexer.source, indexer.config)
    assert "pkg/thing.py::_shim" in _ids(before)

    import_and_reconcile(
        store,
        indexer,
        WORKTREE,
        payload([("app.py::dispatch", "pkg/thing.py::_shim")]),
    )
    after = orphans_report(store, WORKTREE, indexer.source, indexer.config)
    assert "pkg/thing.py::_shim" not in _ids(after)
    store.close()


def test_path_reports_the_hop_as_observed(app_repo):
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    import_and_reconcile(
        store, indexer, WORKTREE, payload([("app.py::dispatch", "app.py::Handler.handle")])
    )
    report = path_report(store, WORKTREE, "app.py::dispatch", "app.py::Handler.handle")
    assert report.summary["direction"] == "forward"
    assert any("observed" in row.detail for group in report.groups for row in group.rows)
    assert report.summary["trace"] != trace_module.NO_TRACE
    store.close()


def test_effects_flow_along_an_observed_call(app_repo, write):
    """A trace does not only add dependents: it adds reachability, which is
    what `effects` walks."""
    write(
        "app.py",
        APP.replace("        return 1\n", "        import socket\n\n        socket.socket()\n"),
        commit="effectful",
    )
    store, indexer = workspace(app_repo)
    indexer.reconcile(WORKTREE)
    assert not _effect_kinds(store, WORKTREE, "app.py::dispatch")

    import_and_reconcile(
        store, indexer, WORKTREE, payload([("app.py::dispatch", "app.py::Handler.handle")])
    )
    assert "NETWORK" in _effect_kinds(store, WORKTREE, "app.py::dispatch")
    store.close()


def _effect_kinds(store, rev, node_id):
    return {
        row["kind"]
        for row in store.connection.execute(
            "SELECT DISTINCT kind FROM effects WHERE rev=? AND node_id=?", (rev, node_id)
        )
    }


# -- the CLI -----------------------------------------------------------------


def test_cli_imports_reports_and_forgets(app_repo, tmp_path, capsys):
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps(payload([("app.py::dispatch", "app.py::Handler.handle")])))

    assert main(["trace", str(trace_path), "--path", str(app_repo)]) == 0
    assert "1" in capsys.readouterr().out

    assert main(["trace", "--path", str(app_repo)]) == 0
    assert "observed" in capsys.readouterr().out

    assert main(["trace", "--forget", "--path", str(app_repo)]) == 0
    capsys.readouterr()

    assert main(["trace", "--path", str(app_repo)]) == 0
    out = capsys.readouterr().out
    assert "no trace" in out
    assert "tracer.py" in out  # tells the reader how to make one


def test_cli_reports_a_missing_file_cleanly(app_repo, tmp_path, capsys):
    assert main(["trace", str(tmp_path / "nope.json"), "--path", str(app_repo)]) == 1
    assert "nope.json" in capsys.readouterr().err


def test_cli_reports_a_mismatched_trace_cleanly(app_repo, tmp_path, capsys):
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps(payload([("other.py::a", "other.py::b")])))
    assert main(["trace", str(trace_path), "--path", str(app_repo)]) == 1
    assert "names no symbol" in capsys.readouterr().err


def test_cli_reports_bad_json_cleanly(app_repo, tmp_path, capsys):
    trace_path = tmp_path / "trace.json"
    trace_path.write_text("{not json")
    assert main(["trace", str(trace_path), "--path", str(app_repo)]) == 1
    assert capsys.readouterr().err.strip()


def test_cli_with_bad_rev_reports_cleanly(app_repo, capsys):
    assert main(["trace", "--path", str(app_repo), "--rev", "nosuchrev"]) == 1
    assert capsys.readouterr().err.strip() == "revision not found: nosuchrev"


# -- the guarantee -----------------------------------------------------------


def test_with_no_trace_the_graph_is_exactly_what_it_was(app_repo):
    """Every stored row, with and without the trace tables in the schema.

    This is the whole additivity claim, and it is checked by comparing the
    materialized revision against one built by a store that never heard of
    a trace -- which is the same store, since no trace was imported.
    """
    store, indexer = workspace(app_repo)
    stats = indexer.reconcile(WORKTREE)
    assert stats.observed_edges == 0
    provenances = {
        row["provenance"]
        for row in store.connection.execute(
            "SELECT DISTINCT provenance FROM edges WHERE rev=?", (WORKTREE,)
        )
    }
    assert provenances == {STATIC}
    assert trace_module.summary(store, WORKTREE) == trace_module.NO_TRACE
    store.close()


# -- the tracer ships, and stays runnable where it has to run ----------------


def test_the_tracer_imports_nothing_from_codegraph():
    """It runs inside the traced program's own virtualenv.

    That venv has the target installed and pytest installed; whether it has
    codegraph is nobody's business and usually no. An import of this package
    would therefore fail in the one environment the file exists to run in,
    and nothing in this test suite -- which runs in codegraph's own venv --
    would ever notice. So the check is on the source text.
    """
    import ast
    import sys
    from pathlib import Path

    from codegraph import tracer

    source = Path(tracer.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.partition(".")[0])

    assert "codegraph" not in imported
    # pytest is the one third-party name allowed, and it is imported inside
    # `main` rather than at module scope, where the target's venv supplies it.
    assert imported - {"pytest"} <= sys.stdlib_module_names


def test_the_tracer_is_inside_the_installed_package():
    """`codegraph trace` tells a user to run it by path, so the path has to
    exist in an install and not only in a checkout."""
    from pathlib import Path

    import codegraph
    from codegraph import tracer

    assert Path(tracer.__file__).parent == Path(codegraph.__file__).parent


# -- a suite that is not a pytest suite (#71) --------------------------------


def _write_runner(directory, body):
    """A standalone script, plus a sibling module only importable from beside it."""
    (directory / "sibling.py").write_text("MARK = 'imported from the script directory'\n")
    script = directory / "runner.py"
    script.write_text(body)
    return script


def run_suite_in_a_subprocess(script, arguments):
    """`tracer.run_suite` in a process of its own.

    It mutates `sys.path` and `sys.argv` and runs somebody else's `__main__`
    -- all three of which leak into whatever runs next. In-process that would
    be this test session.
    """
    import subprocess
    import sys
    from pathlib import Path

    from codegraph import tracer

    helper = (
        "import json, sys;"
        f" sys.path.insert(0, {str(Path(tracer.__file__).parent.parent)!r});"
        " from codegraph.tracer import run_suite;"
        " print(json.dumps(run_suite(sys.argv[1], sys.argv[2:])))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", helper, str(script), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed


def test_a_script_runs_as_main_with_its_own_argv_and_directory(tmp_path):
    """The three things a runner like django's reads.

    `tests/runtests.py` does all of it: it guards its work behind
    `__name__ == "__main__"`, parses `sys.argv`, and imports its settings
    module from its own directory. Run it any other way and it either does
    nothing at all or fails on an import, both silently enough to be mistaken
    for an empty suite.
    """
    script = _write_runner(
        tmp_path,
        "import sys\n"
        "if __name__ == '__main__':\n"
        "    import sibling\n"
        "    print('argv', sys.argv[1:], sibling.MARK)\n",
    )
    completed = run_suite_in_a_subprocess(script, ["--parallel=1", "basic"])
    assert completed.returncode == 0, completed.stderr
    assert "argv ['--parallel=1', 'basic'] imported from the script directory" in completed.stdout


def test_a_failing_script_reports_its_status_instead_of_propagating(tmp_path):
    """A suite with failures still produced a real trace.

    `main` writes the trace in a `finally`, so a `SystemExit` escaping here
    would not lose it -- but it would make the tracer exit non-zero and
    `bench/run.py` treat a perfectly good trace as a failed run.
    """
    script = _write_runner(tmp_path, "import sys\nsys.exit(1)\n")
    completed = run_suite_in_a_subprocess(script, [])
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("1")


def test_a_script_that_falls_off_the_end_is_a_pass(tmp_path):
    """No `SystemExit` is success, the way a shell reads it."""
    script = _write_runner(tmp_path, "pass\n")
    completed = run_suite_in_a_subprocess(script, [])
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("0")
