# tests/test_invariants.py
import random
import subprocess

import pytest

from codegraph.config import DEFAULT_AMBIGUITY_LIMIT
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
                "SELECT path, raw_name, ref_kind, reason FROM unresolved WHERE rev=?", (rev,)
            )
        ),
        # Effects are part of the materialized revision, so they belong in the
        # equivalence too. Leaving them out left the incremental-vs-cold net
        # with a hole exactly where detection and propagation are narrowed.
        "effects": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT node_id, kind, direct, evidence_path, evidence_line, confidence"
                " FROM effects WHERE rev=?",
                (rev,),
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


MUTATIONS = ["edit", "add", "delete", "rename", "branch", "switch", "merge", "rebase"]


def _local_branches(repo):
    out = git(repo, "branch", "--format=%(refname:short)")
    return [line.strip() for line in out.splitlines() if line.strip()]


def _current_branch(repo):
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _force_commit(repo, name, counter):
    """Write a uniquely-named marker file and commit it on the checked-out
    branch. `merge` and `rebase` use this to force real divergence on both
    sides of the operation rather than hoping earlier mutations happened to
    land on both branches -- the marker's filename is unique per (mutation
    kind, counter), so it never collides with another branch's marker and
    never itself causes a conflict."""
    (repo / name).write_text(f"MARKER = {counter}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"marker {name}")


def _attempt(repo, command, abort):
    """Run a git command that may stop on an unresolved conflict; abort
    cleanly back to a clean tree on failure rather than letting the walk
    crash. Returns True if the command landed."""
    try:
        git(repo, *command)
        return True
    except subprocess.CalledProcessError:
        git(repo, *abort)
        return False


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
        # Any local branch but the current one -- not always `main`. A repo
        # with only one branch so far leaves this a no-op.
        others = [b for b in _local_branches(repo) if b != _current_branch(repo)]
        if others:
            git(repo, "checkout", "-q", random.choice(others))
    elif kind == "merge":
        source = _current_branch(repo)
        others = [b for b in _local_branches(repo) if b != source]
        if not others:
            return
        target = random.choice(others)
        # Force a commit unique to each side before merging, so this is a
        # genuine two-parent merge rather than a fast-forward: without it,
        # whichever side happens to already be an ancestor of the other
        # (routine once `switch`/`merge` keep returning to the same
        # branches) makes `git merge` a no-op.
        _force_commit(repo, f"_merge_src_{counter}.py", counter)
        git(repo, "checkout", "-q", target)
        _force_commit(repo, f"_merge_dst_{counter}.py", counter)
        # -X ours: a real content conflict (e.g. both sides edited a.py) is
        # resolved deterministically by keeping the checked-out (target)
        # side. A conflict type -X cannot settle (e.g. modify/delete) is
        # aborted back to a clean tree rather than left half-resolved.
        _attempt(repo, ("merge", "-q", "--no-edit", "-X", "ours", source), ("merge", "--abort"))
    elif kind == "rebase":
        current = _current_branch(repo)
        others = [b for b in _local_branches(repo) if b != current]
        if not others:
            return
        onto = random.choice(others)
        # Same forced-divergence reasoning as `merge`: without a commit
        # unique to each side, replaying `current` onto `onto` is either a
        # no-op (already an ancestor) or a plain fast-forward.
        _force_commit(repo, f"_rebase_src_{counter}.py", counter)
        git(repo, "checkout", "-q", onto)
        _force_commit(repo, f"_rebase_onto_{counter}.py", counter)
        git(repo, "checkout", "-q", current)
        _attempt(repo, ("rebase", "-q", "-X", "ours", onto), ("rebase", "--abort"))


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


# -- graph size ------------------------------------------------------------
#
# The last-resort resolution step matches a bare name against every live
# definition in the revision, so before the ambiguity cap the edge table grew
# with the SQUARE of the repo: 120 edges/file on flask (83 files), 740 on django
# (2,930 files) -- 2.09M LOW edges, 96.6% of the graph, up to 971 for a single
# call site. Nothing caught it because every test repo here is a handful of
# files. This is that missing test, at a size a unit test can afford. See #6.


def duck_typed_repo(repo, write, count):
    """`count` files, each carrying one edge of each shape.

    `local{i}()` is a module-local call: exactly one HIGH edge per file however
    big the repo gets -- the linear term, and what keeps the graph non-empty
    once the cap fires. `item.save()` is the quadratic term: `save` is defined
    in every file, so without a cap each of the `count` callers matches all
    `count` definitions.
    """
    for i in range(count):
        write(
            f"m{i}.py",
            f"class C{i}:\n"
            f"    def save(self):\n"
            f"        return {i}\n"
            f"\n\n"
            f"def local{i}():\n"
            f"    return {i}\n"
            f"\n\n"
            f"def persist{i}(item):\n"
            f"    local{i}()\n"
            f"    return item.save()\n",
        )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"{count} files")


def edge_count(repo):
    store = Store.open(repo)
    try:
        return Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD").edges
    finally:
        store.close()


def test_graph_size_grows_with_the_repo_not_with_its_square(repo, write, tmp_path_factory):
    small_root, large_root = repo, tmp_path_factory.mktemp("large")
    git(large_root, "init", "-q", "-b", "main")
    git(large_root, "config", "user.email", "test@example.com")
    git(large_root, "config", "user.name", "Test")

    def write_large(rel, text, commit=None):
        (large_root / rel).write_text(text)

    duck_typed_repo(small_root, write, 40)
    duck_typed_repo(large_root, write_large, 160)

    small, large = edge_count(small_root), edge_count(large_root)
    growth = large / small
    # 4x the files. Linear growth is 4x; quadratic is 16x. The bound is loose on
    # purpose -- it is a growth-RATE guard, not a size target, and must not be
    # tightened into a benchmark.
    assert growth < 6.0, f"{small} -> {large} edges for 4x the files ({growth:.1f}x growth)"


def test_no_single_reference_is_expanded_past_the_ambiguity_limit(repo, write):
    duck_typed_repo(repo, write, 60)
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    rows = store.connection.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE rev='HEAD' GROUP BY src, callsite_path,"
        " callsite_line"
    ).fetchall()
    assert rows, "no edges at all — the fixture stopped exercising anything"
    worst = max(row["n"] for row in rows)
    assert worst <= DEFAULT_AMBIGUITY_LIMIT, f"a single call site produced {worst} edges"
    store.close()


# -- a reconcile that has nothing to do costs nothing ------------------------
#
# Every query reconciles the working tree first (the `git status` model), so a
# repeated query used to pay a full rebuild of a revision that had not changed:
# ~86s per query on django, ~2s on flask. See #7.


def resolve_calls(monkeypatch):
    """Count how many times `reconcile` reaches phase 2."""
    import codegraph.indexer as indexer_module

    calls = []
    original = indexer_module.resolve_revision

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(indexer_module, "resolve_revision", spy)
    return calls


def test_reconciling_an_unchanged_tree_does_no_work(repo, write, monkeypatch):
    write(
        "m.py",
        "import requests\n"
        "\n\n"
        "def fetch():\n"
        "    requests.get('u')\n"
        "\n\n"
        "def caller():\n"
        "    fetch()\n",
        commit="m",
    )
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile(WORKTREE)

    calls = resolve_calls(monkeypatch)
    before = dump_graph(store, WORKTREE)
    stats = indexer.reconcile(WORKTREE)

    assert calls == [], "an unchanged tree was re-resolved"
    assert dump_graph(store, WORKTREE) == before
    # The numbers must still describe the graph, not the pass that skipped.
    assert stats.paths_total == 2
    assert stats.edges > 0
    store.close()


def test_a_real_edit_still_rebuilds(repo, write, monkeypatch):
    """Non-vacuity guard for the skip above."""
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile(WORKTREE)

    calls = resolve_calls(monkeypatch)
    write("a.py", "def alpha():\n    return 99\n")
    indexer.reconcile(WORKTREE)
    assert calls == [1]
    store.close()


def test_editing_the_config_invalidates_even_though_no_tracked_file_changed(repo, write):
    """`codegraph.toml` is tracked, so editing it does change the tree -- but
    the point stands for what the fingerprint protects: the graph depends on
    inputs the tree diff alone cannot see."""
    write("app/__init__.py", "")
    write("app/db.py", "def save(row):\n    return row\n")
    write(
        "app/service.py",
        "from app.db import save\n\n\ndef persist(row):\n    return save(row)\n",
        commit="app",
    )
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile(WORKTREE)
    assert not _effect_kinds(store, "app/service.py::persist")

    (repo / "codegraph.toml").write_text('[[effect]]\nmatch = "app.db.*"\nkind = "DB_WRITE"\n')
    # A NEW indexer, so the config is re-read -- the store is the same one.
    Indexer(repo, store, GitTreeSource(repo)).reconcile(WORKTREE)
    assert "DB_WRITE" in _effect_kinds(store, "app/service.py::persist")
    store.close()


def test_an_untracked_config_change_still_invalidates(repo, write):
    """The fingerprint's real job. An untracked `codegraph.toml` is invisible to
    the tree diff (`git status` does not list it into the tree source), so
    without the fingerprint the reconcile would skip and keep serving a graph
    built under the old catalog."""
    write("m.py", "def house_call():\n    pass\n\n\ndef caller():\n    house_call()\n", commit="m")
    (repo / ".gitignore").write_text("codegraph.toml\n")
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile(WORKTREE)
    assert not _effect_kinds(store, "m.py::caller")

    (repo / "codegraph.toml").write_text('[[effect]]\nmatch = "house_call"\nkind = "DB_WRITE"\n')
    Indexer(repo, store, GitTreeSource(repo)).reconcile(WORKTREE)
    assert "DB_WRITE" in _effect_kinds(store, "m.py::caller"), (
        "an ignored config change was skipped by the unchanged-tree fast path"
    )
    store.close()


def _effect_kinds(store, node_id, rev=WORKTREE):
    return {
        row["kind"]
        for row in store.connection.execute(
            "SELECT kind FROM effects WHERE rev=? AND node_id=?", (rev, node_id)
        )
    }
