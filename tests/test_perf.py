import time

import pytest

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.impact import impact_report
from codegraph.store import Store
from tests.conftest import git


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
def test_branch_switch_is_under_a_second(tmp_path):
    repo = tmp_path / "flask"
    git(tmp_path, "clone", "-q", "--depth", "50", "https://github.com/pallets/flask", str(repo))
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "probe")

    started = time.perf_counter()
    stats = indexer.reconcile("HEAD")
    elapsed = time.perf_counter() - started
    assert stats.blobs_parsed == 0
    assert elapsed < 1.0, f"branch switch took {elapsed:.2f}s"
    store.close()
