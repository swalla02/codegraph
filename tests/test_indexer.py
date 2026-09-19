# tests/test_indexer.py
from codegraph.indexer import (
    RESOLVER_SOURCES,
    FsTreeSource,
    GitTreeSource,
    Indexer,
    digest_sources,
)
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
    assert {row["qualname"] for row in rows} == {"alpha", "added", "<module>"}
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


def test_parse_errors_persist_across_runs(repo, write):
    """Regression for F4: `_ensure_parsed` only counted errors over blobs
    parsed THIS pass, so `status` reported `parse errors: 1` on the first
    run and then silently reported 0 on every later run, even though the
    broken file is unchanged, still unparseable, and still excluded from
    the graph -- the count must reflect the revision's actual broken files,
    not just newly-parsed ones."""
    write("bad.py", "def broken(:\n", commit="bad")
    store, indexer = build(repo)
    first = indexer.reconcile("HEAD")
    assert first.parse_errors == 1
    second = indexer.reconcile("HEAD")
    assert second.parse_errors == 1
    store.close()


def test_parse_errors_reflects_current_tree_not_every_blob_ever_seen(repo, write):
    """A blob that was broken in an earlier revision but isn't part of the
    current tree must not inflate the count."""
    write("bad.py", "def broken(:\n", commit="bad")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.parse_errors == 1
    write("bad.py", "def fixed():\n    return 1\n", commit="fix")
    fixed_stats = indexer.reconcile("HEAD")
    assert fixed_stats.parse_errors == 0
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


# -- what the resolver fingerprint is made of (#44) --------------------------


def _fake_package(root, resolver_body):
    """A directory shaped like `src/codegraph`, with `resolve.py`'s bytes
    under the caller's control."""
    for name in RESOLVER_SOURCES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resolver_body if name == "resolve.py" else f"# {name}\n")
    return root


def test_the_fingerprint_is_derived_from_the_resolver_source_not_declared(tmp_path):
    """The whole point of #44: nobody has to remember to bump anything.

    A declared `RESOLVER_VERSION` describes the resolver only as long as
    every change to it is accompanied by an edit to a constant somewhere
    else, and the bug being fixed is the proof that that does not hold.
    """
    before = _fake_package(tmp_path / "before", "def resolve():\n    return 1\n")
    after = _fake_package(tmp_path / "after", "def resolve():\n    return 2\n")
    same = _fake_package(tmp_path / "same", "def resolve():\n    return 1\n")

    assert digest_sources(before) != digest_sources(after)
    # Same bytes, same digest, every run: the fingerprint is baked into an
    # on-disk cache key, so a process-salted hash would rebuild everything on
    # every query.
    assert digest_sources(before) == digest_sources(same)


def test_the_fingerprint_pins_the_modules_that_write_layer_2_and_no_others(tmp_path):
    """Membership is "does this module's code decide what gets stored".

    `query/` is the interesting exclusion: those modules read the graph and
    never write a row, so a change there cannot make a stored row wrong --
    and they are the ones edited most often. Pinning them would make this
    digest mean "any commit to codegraph rebuilds every revision".

    `parse.py` is excluded for the opposite reason: Layer 1 is keyed
    separately, on blob sha and `PARSER_VERSION`, and re-resolving unchanged
    blob rows under a new parser cannot produce a different graph.
    """
    from pathlib import Path

    import codegraph

    package = Path(codegraph.__file__).resolve().parent
    assert "resolve.py" in RESOLVER_SOURCES
    assert "parse.py" not in RESOLVER_SOURCES
    assert not [name for name in RESOLVER_SOURCES if name.startswith("query/")]
    for name in RESOLVER_SOURCES:
        assert (package / name).is_file(), f"{name} is pinned but no longer exists"
