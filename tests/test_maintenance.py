import os
import stat
import time

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.maintenance import gc, install_hooks
from codegraph.query.impact import impact_report
from codegraph.resolve import find_symbol
from codegraph.store import Store
from tests.conftest import git


def test_gc_removes_unreferenced_blobs(repo, write):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    write("a.py", "def alpha():\n    return 2\n", commit="edit")
    indexer.reconcile("HEAD")
    before = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    removed = gc(store, {"HEAD"})
    after = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert removed >= 1
    assert after < before
    store.close()


def test_gc_keeps_blobs_of_retained_revisions(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    gc(store, {"HEAD"})
    rows = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert rows == 1
    store.close()


def test_install_hooks_writes_executable_hooks(repo):
    written = install_hooks(repo)
    assert {p.name for p in written} == {"post-commit", "post-checkout", "post-merge"}
    for path in written:
        assert os.stat(path).st_mode & stat.S_IXUSR
        assert "codegraph index" in path.read_text()


def test_hooks_do_not_block_the_git_operation(repo, write):
    install_hooks(repo)
    write("c.py", "def gamma():\n    pass\n", commit="with hooks installed")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


# --- Extra tests per the brief -------------------------------------------


def test_install_hooks_preserves_pre_existing_hook(repo):
    """A repo may already have a post-commit hook (e.g. a linter). Installing
    codegraph's hooks must not clobber it -- the existing script's own
    behavior (here, appending to a marker file) must still run, and its
    original source text must still be present in the file verbatim."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "post-commit"
    original = "#!/bin/sh\necho ran >> marker.txt\n"
    existing_hook.write_text(original)
    existing_hook.chmod(existing_hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    content = existing_hook.read_text()
    assert "echo ran >> marker.txt" in content
    assert "codegraph index" in content
    assert existing_hook in written

    # The pre-existing behavior must still actually fire.
    git(repo, "commit", "--allow-empty", "-qm", "trigger hooks")
    assert (repo / "marker.txt").read_text() == "ran\n"


def test_install_hooks_fires_even_after_a_pre_existing_exit(repo, monkeypatch, tmp_path):
    """A pre-existing hook that ends in `exit 0` -- a very common idiom -- is
    exactly the case that was silently broken: code appended after that
    `exit` is unreachable, so the warming block never ran even though
    install_hooks reported success. It must fire regardless, by running
    before the pre-existing script's own logic rather than after it."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "post-commit"
    existing_hook.write_text("#!/bin/sh\necho hi >> marker.txt\nexit 0\n")
    existing_hook.chmod(existing_hook.stat().st_mode | stat.S_IXUSR)

    # A `codegraph` shim on PATH that just logs that it was invoked.
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log = tmp_path / "invoked.log"
    shim = shim_dir / "codegraph"
    shim.write_text(f'#!/bin/sh\necho invoked >> "{log}"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")

    install_hooks(repo)
    content = existing_hook.read_text()
    # The codegraph block must precede the pre-existing `exit 0`, not follow it.
    assert content.index("codegraph index") < content.index("exit 0")

    git(repo, "commit", "--allow-empty", "-qm", "trigger hooks despite exit 0")

    # The backgrounded warm-up may still be running when `git commit`
    # returns; give it a short window to land before asserting it fired.
    deadline = time.monotonic() + 2.0
    while not log.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert log.exists(), "codegraph shim was never invoked -- warming block was skipped"
    # The pre-existing hook's own behavior still ran too.
    assert (repo / "marker.txt").read_text() == "hi\n"


def test_install_hooks_is_idempotent(repo):
    """Running install_hooks twice must not double-append the codegraph
    block or produce two invocations of `codegraph index`."""
    install_hooks(repo)
    first = (repo / ".git" / "hooks" / "post-commit").read_text()
    written_again = install_hooks(repo)
    second = (repo / ".git" / "hooks" / "post-commit").read_text()

    assert first == second
    assert second.count("codegraph index") == 1
    assert {p.name for p in written_again} == {"post-commit", "post-checkout", "post-merge"}


def test_gc_does_not_corrupt_a_live_graph(repo, write):
    """After gc(store, {"HEAD"}), a query at HEAD must still return the same
    answer it did before the gc: Layer 1 rows for retained revisions, and
    everything Layer 2 needs, must survive."""
    write(
        "b.py",
        "from a import alpha\n\ndef beta():\n    return alpha()\n",
        commit="add beta",
    )
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")

    node_id = find_symbol(store, "HEAD", "alpha")[0]["id"]
    before = impact_report(store, "HEAD", node_id)

    gc(store, {"HEAD"})

    after = impact_report(store, "HEAD", node_id)
    assert after == before
    assert len(before.groups) > 0
    store.close()


def test_gc_with_empty_keep_revs_removes_everything(repo):
    """An empty keep_revs retains nothing: every Layer 1 row is unreferenced
    by definition, so gc removes it all. This is the defined behavior for
    the empty case, not an accident of the query."""
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    before = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert before > 0

    removed = gc(store, set())

    after = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert removed == before
    assert after == 0
    store.close()
