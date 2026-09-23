import time

import pytest

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.impact import impact_report
from codegraph.query.rank import fan_in
from codegraph.store import WORKTREE, Store
from tests.conftest import git


def test_fan_in_seeks_by_target_instead_of_reading_the_revision(tmp_path):
    """`impact` calls `fan_in` once per dependent, so its plan is the report's.

    This is offline and instant because the thing that goes wrong is not
    slowness anyone can feel on a fixture -- it is a query plan. Asking for
    `DISTINCT src` makes SQLite prefer the index it can read in `src` order,
    which knows nothing about `dst`, so a question about one node walks every
    edge in the revision. On django that turned a per-dependent 0.06ms into
    87ms and `impact` into minutes, and no test on a repository small enough
    to keep in this suite would have noticed.

    The statement is captured rather than spelled out here, so the test pins
    the plan of whatever `fan_in` actually runs.
    """
    store = Store.open(tmp_path)
    store.connection.execute(
        "INSERT INTO nodes(rev, id, path, qualname, kind, line_start, line_end,"
        " body_hash, name_binding) VALUES(?, 'm.py::f', 'm.py', 'f', 'function',"
        " 1, 2, 'h', 'live')",
        (WORKTREE,),
    )
    store.connection.executemany(
        "INSERT INTO edges(rev, src, dst, kind, confidence, provenance, callsite_path,"
        " callsite_line) VALUES(?,?,?,?,?,?,?,?)",
        [
            (WORKTREE, f"m.py::c{n}", "m.py::f", "CALLS", "HIGH", "static", "m.py", n)
            for n in range(5)
        ],
    )
    store.connection.commit()

    statements: list[str] = []
    store.connection.set_trace_callback(statements.append)
    assert fan_in(store, WORKTREE, "m.py::f") == 5
    store.connection.set_trace_callback(None)

    # `set_trace_callback` hands back the statement with its parameters
    # already substituted, which is exactly what `EXPLAIN QUERY PLAN` wants.
    over_edges = [sql for sql in statements if "FROM edges" in sql]
    assert len(over_edges) == 1, statements
    plan = " ".join(
        str(row[3]) for row in store.connection.execute(f"EXPLAIN QUERY PLAN {over_edges[0]}")
    )
    assert "idx_edges_dst" in plan, plan
    store.close()


@pytest.mark.slow
def test_cold_index_and_warm_query_are_fast(tmp_path):
    repo = tmp_path / "flask"
    git(tmp_path, "clone", "-q", "--depth", "50", "https://github.com/pallets/flask", str(repo))

    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))

    started = time.perf_counter()
    stats = indexer.reconcile("HEAD")
    cold = time.perf_counter() - started
    assert stats.paths_total > 50
    assert cold < 60.0, f"cold index took {cold:.1f}s"

    node = store.connection.execute(
        "SELECT id FROM nodes WHERE rev='HEAD' AND kind='function' LIMIT 1"
    ).fetchone()["id"]

    started = time.perf_counter()
    impact_report(store, "HEAD", node)
    warm = time.perf_counter() - started
    assert warm < 0.3, f"warm query took {warm * 1000:.0f}ms"
    store.close()


@pytest.mark.slow
def test_branch_switch_reparses_nothing(tmp_path):
    repo = tmp_path / "flask"
    git(tmp_path, "clone", "-q", "--depth", "50", "https://github.com/pallets/flask", str(repo))
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "probe")

    started = time.perf_counter()
    stats = indexer.reconcile("HEAD")
    elapsed = time.perf_counter() - started
    assert stats.blobs_parsed == 0  # the cost guarantee: branch creation re-parses nothing
    # Wall-clock is an order-of-magnitude guard against a real regression, not a
    # performance target - it must not be tightened back down to chase a benchmark.
    assert elapsed < 3.0, f"branch switch took {elapsed:.2f}s"
    store.close()
