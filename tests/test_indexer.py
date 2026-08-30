# tests/test_indexer.py
from codegraph.indexer import FsTreeSource, GitTreeSource, Indexer
from codegraph.store import WORKTREE, Store
from tests.conftest import git


def build(repo):
    store = Store.open(repo)
    return store, Indexer(repo, store, GitTreeSource(repo))


def test_first_index_parses_every_blob(repo, write):
    write("b.py", "def beta():\n    pass\n", commit="add b")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.paths_total == 2
    assert stats.blobs_parsed == 2
    assert stats.blobs_cached == 0
    store.close()


def test_second_index_parses_nothing(repo):
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    stats = indexer.reconcile("HEAD")
    assert stats.blobs_parsed == 0
    assert stats.blobs_cached == 1
    store.close()


def test_creating_a_branch_costs_zero_parses(repo):
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    stats = indexer.reconcile("HEAD")
    assert stats.blobs_parsed == 0
    store.close()


def test_switching_back_reparses_nothing(repo, write):
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    write("a.py", "def alpha():\n    return 42\n", commit="change alpha")
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "main")
    stats = indexer.reconcile("HEAD")
    assert stats.blobs_parsed == 0, "blobs seen on main were already cached"
    store.close()


def test_worktree_revision_sees_uncommitted_edits(repo, write):
    store, indexer = build(repo)
    indexer.reconcile(WORKTREE)
    write("a.py", "def alpha():\n    return 7\n\n\ndef added():\n    pass\n")
    indexer.reconcile(WORKTREE)
    rows = store.connection.execute(
        "SELECT qualname FROM nodes WHERE rev=?", (WORKTREE,)
    ).fetchall()
    assert {row["qualname"] for row in rows} == {"alpha", "added"}
    store.close()


def test_node_ids_combine_path_and_qualname(repo, write):
    write("pkg/service.py", "class Svc:\n    def charge(self):\n        pass\n", commit="svc")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    ids = {row["id"] for row in store.connection.execute("SELECT id FROM nodes WHERE rev='HEAD'")}
    assert "pkg/service.py::Svc.charge" in ids
    store.close()


def test_deleted_file_drops_its_nodes(repo, write):
    write("gone.py", "def temp():\n    pass\n", commit="add gone")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    (repo / "gone.py").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "remove gone")
    indexer.reconcile("HEAD")
    rows = store.connection.execute(
        "SELECT id FROM nodes WHERE rev='HEAD' AND path='gone.py'"
    ).fetchall()
    assert rows == []
    store.close()


def test_rename_costs_no_parsing(repo, write):
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    git(repo, "mv", "a.py", "renamed.py")
    git(repo, "commit", "-qm", "rename")
    stats = indexer.reconcile("HEAD")
    assert stats.blobs_parsed == 0
    store.close()


def test_syntax_error_is_recorded_not_raised(repo, write):
    write("bad.py", "def broken(:\n", commit="bad")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.parse_errors == 1
    store.close()


def test_works_without_git(tmp_path):
    (tmp_path / "solo.py").write_text("def solo():\n    pass\n")
    store = Store.open(tmp_path)
    indexer = Indexer(tmp_path, store, FsTreeSource(tmp_path))
    stats = indexer.reconcile(WORKTREE)
    assert stats.paths_total == 1
    assert stats.blobs_parsed == 1
    store.close()


def test_staged_rename_drops_old_path_in_worktree(repo, write):
    """Regression: `gitio.status_paths` used to discard a rename's old path
    entirely, so a `git mv` staged but not committed left a stale node at
    the old path in the WORKTREE revision alongside the correct one at the
    new path.
    """
    store, indexer = build(repo)
    indexer.reconcile(WORKTREE)
    git(repo, "mv", "a.py", "renamed.py")
    git(repo, "add", "-A")
    indexer.reconcile(WORKTREE)
    rows = store.connection.execute("SELECT path FROM nodes WHERE rev=?", (WORKTREE,)).fetchall()
    paths = {row["path"] for row in rows}
    assert "a.py" not in paths
    assert "renamed.py" in paths
    store.close()
