import subprocess
import threading
from pathlib import Path

import pytest

from codegraph import gitio
from tests.conftest import git


def _hash_many_blobs(repo: Path, tmp_path: Path, n: int, prefix: str) -> list[str]:
    """Write `n` small distinct blobs straight into `repo`'s object database.

    Bypasses the working tree/index entirely (`hash-object -w --stdin-paths`
    from a scratch directory) so building a batch large enough to exceed the
    OS pipe buffer stays fast.
    """
    blob_dir = tmp_path / f"{prefix}-blobs"
    blob_dir.mkdir()
    paths = [blob_dir / f"{prefix}{i}.py" for i in range(n)]
    for i, path in enumerate(paths):
        path.write_text(f"def {prefix}{i}():\n    return {i}\n")
    proc = subprocess.run(
        ["git", "hash-object", "-w", "-t", "blob", "--stdin-paths"],
        cwd=repo,
        input="\n".join(str(p) for p in paths) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    shas = proc.stdout.split()
    assert len(shas) == n
    return shas


def test_is_repo(repo, tmp_path_factory):
    assert gitio.is_repo(repo)
    # Must be a directory outside `repo`'s tree: a subdirectory of `repo`
    # (e.g. `tmp_path / "plain"`, since `repo` is rooted at `tmp_path`)
    # is genuinely inside a git repo and is_repo would correctly say True.
    plain = tmp_path_factory.mktemp("plain")
    assert not gitio.is_repo(plain)


def test_ls_tree_returns_python_blobs(repo, write):
    write("pkg/b.py", "def beta():\n    return 2\n", commit="add b")
    write("notes.md", "# hi\n", commit="add md")
    tree = gitio.ls_tree(repo, "HEAD")
    assert set(tree) == {"a.py", "pkg/b.py"}
    assert all(len(sha) == 40 for sha in tree.values())


def test_cat_file_batch_roundtrips_content(repo):
    tree = gitio.ls_tree(repo, "HEAD")
    sha = tree["a.py"]
    got = dict(gitio.cat_file_batch(repo, [sha]))
    assert got[sha] == b"def alpha():\n    return 1\n"


def test_cat_file_batch_handles_many_blobs(repo, write):
    for i in range(20):
        write(f"m{i}.py", f"def f{i}():\n    return {i}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "many")
    tree = gitio.ls_tree(repo, "HEAD")
    got = dict(gitio.cat_file_batch(repo, tree.values()))
    assert len(got) == len(tree)


def test_cat_file_batch_handles_a_batch_past_the_pipe_buffer(repo, tmp_path):
    """Regression: writing the whole SHA list before reading any output would
    deadlock once the batch is large enough to fill the stdin pipe buffer
    (~64KiB on Linux, roughly 1500+ 41-byte SHA lines). 2500 is comfortably
    past that threshold.
    """
    n = 2500
    shas = _hash_many_blobs(repo, tmp_path, n, "f")

    got = dict(gitio.cat_file_batch(repo, shas))

    assert len(got) == n
    assert got[shas[0]] == b"def f0():\n    return 0\n"


def test_cat_file_batch_survives_early_abandonment(repo, tmp_path):
    """Regression: abandoning the generator before it drains fully must not
    deadlock. With a batch large enough to fill the pipe buffers in both
    directions, closing only 3 items in and then closing the generator can
    catch the writer thread mid-block on a stdin write it will never finish
    (because git itself is blocked writing to a full, unread stdout pipe).
    Cleanup must close stdout *before* joining the writer, or the join waits
    forever for a thread nothing will ever unstick.

    Runs the abandon-early sequence on a background thread and bounds it
    with `join(timeout=...)`, since there is no pytest-timeout plugin here:
    a regression should fail the assertion, not hang the suite.
    """
    n = 2500
    shas = _hash_many_blobs(repo, tmp_path, n, "g")

    def _consume_a_few_then_abandon() -> None:
        gen = gitio.cat_file_batch(repo, shas)
        for _ in range(3):
            next(gen)
        gen.close()

    runner = threading.Thread(target=_consume_a_few_then_abandon, daemon=True)
    runner.start()
    runner.join(timeout=15)

    assert not runner.is_alive(), "cat_file_batch cleanup hung on early abandonment"


def test_ls_tree_sees_non_ascii_paths(repo, write):
    """Regression for F1: `ls-tree --format=...` C-quotes a non-ASCII path
    even under `-z` (`"\\303\\274n\\303\\257code.py"`), which fails
    `.endswith(".py")` and silently drops the file from the tree."""
    write("ünïcode.py", "def u():\n    return 1\n", commit="add unicode path")
    tree = gitio.ls_tree(repo, "HEAD")
    assert "ünïcode.py" in tree


def test_ls_tree_sees_paths_with_a_space(repo, write):
    write("has space.py", "def s():\n    return 1\n", commit="add spaced path")
    tree = gitio.ls_tree(repo, "HEAD")
    assert "has space.py" in tree


def test_ls_tree_sees_paths_with_embedded_quote_or_backslash(repo, write):
    """`"` and `\\` are C-quoted by `--format` unconditionally, independent
    of `core.quotePath` -- only dropping `--format` entirely avoids it."""
    write('weird"quote.py', "def q():\n    return 1\n", commit="add quoted path")
    write("weird\\slash.py", "def s():\n    return 1\n", commit="add backslash path")
    tree = gitio.ls_tree(repo, "HEAD")
    assert 'weird"quote.py' in tree
    assert "weird\\slash.py" in tree


def test_ls_tree_and_status_paths_agree_on_a_dirty_unicode_file(repo, write):
    """The compound failure the review flagged: with `ls_tree` dropping the
    unicode path, an `Indexer` overlay would see it as absent from the base
    tree and `status_paths` would report it `??`/`M` forever, on every
    single diff, even once committed and clean. Once `ls_tree` sees the
    path, a clean checkout must show no dirty status for it at all."""
    write("ünïcode.py", "def u():\n    return 1\n", commit="add unicode path")
    tree = gitio.ls_tree(repo, "HEAD")
    assert "ünïcode.py" in tree
    status = gitio.status_paths(repo)
    assert "ünïcode.py" not in status


def test_status_paths_reports_dirty_files(repo, write):
    write("a.py", "def alpha():\n    return 99\n")
    write("new.py", "def gamma():\n    pass\n")
    status = gitio.status_paths(repo)
    assert status["a.py"] == "M"
    assert status["new.py"] == "??"


def test_status_paths_handles_rename(repo, write):
    write("pkg/b.py", "def beta():\n    return 2\n", commit="add b")
    git(repo, "mv", "pkg/b.py", "pkg/c.py")
    status = gitio.status_paths(repo)
    assert status == {"pkg/c.py": "R", "pkg/b.py": "D"}


def test_status_paths_copy_does_not_delete_source(monkeypatch):
    """`git status` does not expose copy detection in its porcelain output by
    default (unlike `git diff -C`), so a real repo cannot be coaxed into
    emitting a `C` entry reliably. This exercises the parsing branch
    directly instead, by feeding `status_paths` a canned porcelain -z
    payload shaped like a copy entry: `C  pkg/c.py` (new path) followed by
    the NUL-separated old path `pkg/b.py` with no XY prefix.
    """
    porcelain = b"C  pkg/c.py\0pkg/b.py\0"
    monkeypatch.setattr(gitio, "_run", lambda *args, **kwargs: porcelain)
    status = gitio.status_paths(Path("unused"))
    assert status == {"pkg/c.py": "C"}


def test_hash_object_matches_git(repo):
    sha = gitio.hash_object(repo, b"def alpha():\n    return 1\n")
    assert sha == gitio.ls_tree(repo, "HEAD")["a.py"]


def test_merge_base_and_default_branch(repo, write):
    base = gitio.rev_parse(repo, "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    write("c.py", "def gamma():\n    pass\n", commit="feature work")
    assert gitio.merge_base(repo, "main", "feature") == base
    assert gitio.default_branch(repo) == "main"


# -- B9: a `rev` value that happens to start with `-` must be treated as a
# revision (fails as "not found"), never reinterpreted as a git option.
# `rev_parse` is the sharpest case: verified against a scratch repo that
# WITHOUT the `--end-of-options` guard, `git rev-parse --not-a-real-option`
# doesn't error at all -- rev-parse doesn't recognize the flag, falls back
# to its "echo unrecognized arguments back verbatim" behavior, and exits 0
# with the literal string "--not-a-real-option" as if it were a resolved
# sha. That is a silently wrong answer, not just a confusing one.


def test_rev_parse_treats_a_dash_prefixed_value_as_a_revision(repo):
    with pytest.raises(gitio.GitError):
        gitio.rev_parse(repo, "--not-a-real-option")


def test_ls_tree_treats_a_dash_prefixed_rev_as_a_revision(repo):
    with pytest.raises(gitio.GitError):
        gitio.ls_tree(repo, "--not-a-real-option")


def test_merge_base_treats_a_dash_prefixed_rev_as_a_revision(repo):
    with pytest.raises(gitio.GitError):
        gitio.merge_base(repo, "--not-a-real-option", "HEAD")
