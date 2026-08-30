# tests/test_invariants.py
import random

import pytest

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.store import WORKTREE, Store
from tests.conftest import git


def dump_graph(store, rev):
    """Canonical, order-independent snapshot of a materialized revision."""
    connection = store.connection
    return {
        "nodes": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT id, path, qualname, kind, body_hash, name_binding FROM nodes WHERE rev=?",
                (rev,),
            )
        ),
        "edges": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT src, dst, kind, confidence FROM edges WHERE rev=?", (rev,)
            )
        ),
        "unresolved": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT path, raw_name FROM unresolved WHERE rev=?", (rev,)
            )
        ),
    }


def cold_dump(repo, rev):
    """Index the same state from scratch in a throwaway database.

    `Store.open` runs in WAL mode, so committed pages can still live in
    `graph.db-wal` even after `graph.db` itself is unlinked -- deleting only
    the main file can resurrect stale rows into the "cold" database via the
    leftover WAL, silently contaminating this rebuild. All three files that
    make up a WAL-mode database must be removed together.
    """
    directory = repo / ".codegraph"
    for name in ("graph.db", "graph.db-wal", "graph.db-shm"):
        candidate = directory / name
        if candidate.exists():
            candidate.unlink()
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile(rev)
    dump = dump_graph(store, rev)
    store.close()
    return dump


MUTATIONS = ["edit", "add", "delete", "rename", "branch", "switch", "merge"]


def apply_mutation(repo, kind, counter):
    files = sorted(p.name for p in repo.glob("*.py"))
    if kind == "edit" and files:
        target = repo / random.choice(files)
        target.write_text(f"def f{counter}():\n    return {counter}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", f"edit {counter}")
    elif kind == "add":
        (repo / f"m{counter}.py").write_text(f"def g{counter}():\n    return f{counter}()\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", f"add {counter}")
    elif kind == "delete" and len(files) > 1:
        (repo / random.choice(files)).unlink()
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", f"delete {counter}")
    elif kind == "rename" and files:
        git(repo, "mv", random.choice(files), f"r{counter}.py")
        git(repo, "commit", "-qm", f"rename {counter}")
    elif kind == "branch":
        git(repo, "checkout", "-q", "-b", f"b{counter}")
    elif kind == "switch":
        git(repo, "checkout", "-q", "main")
    elif kind == "merge":
        current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current != "main":
            git(repo, "checkout", "-q", "main")
            git(repo, "merge", "-q", "--no-edit", current)


@pytest.mark.parametrize("seed", range(8))
def test_incremental_equals_cold_rebuild(repo, seed):
    random.seed(seed)
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")

    for counter in range(12):
        apply_mutation(repo, random.choice(MUTATIONS), counter)
        indexer.reconcile("HEAD")

    incremental = dump_graph(store, "HEAD")
    store.close()
    assert incremental == cold_dump(repo, "HEAD")


def test_worktree_incremental_equals_cold_rebuild(repo, write):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile(WORKTREE)
    write("a.py", "def alpha():\n    return 5\n")
    write("added.py", "def added():\n    alpha()\n")
    indexer.reconcile(WORKTREE)
    incremental = dump_graph(store, WORKTREE)
    store.close()
    assert incremental == cold_dump(repo, WORKTREE)


# -- cost guarantees --------------------------------------------------------


def test_branch_creation_parses_zero_blobs(repo):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    assert indexer.reconcile("HEAD").blobs_parsed == 0
    store.close()


def test_round_trip_switch_parses_zero_on_return(repo, write):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    write("a.py", "def alpha():\n    return 2\n", commit="edit on feature")
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "main")
    assert indexer.reconcile("HEAD").blobs_parsed == 0
    store.close()


def test_switch_parses_at_most_the_changed_files(repo, write):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    for i in range(12):
        write(f"m{i}.py", f"def f{i}():\n    pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "many")
    indexer.reconcile("HEAD")

    git(repo, "checkout", "-q", "-b", "feature")
    for i in range(3):
        write(f"m{i}.py", f"def f{i}():\n    return {i}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "touch three")
    assert indexer.reconcile("HEAD").blobs_parsed == 3
    store.close()
