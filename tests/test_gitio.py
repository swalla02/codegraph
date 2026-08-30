import subprocess

from codegraph import gitio
from tests.conftest import git


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
    past that threshold. Blobs are written directly to the object database
    with `hash-object -w --stdin-paths`, bypassing the index/working tree so
    the fixture stays fast.
    """
    n = 2500
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    paths = [blob_dir / f"f{i}.py" for i in range(n)]
    for i, path in enumerate(paths):
        path.write_text(f"def f{i}():\n    return {i}\n")
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

    got = dict(gitio.cat_file_batch(repo, shas))

    assert len(got) == n
    assert got[shas[0]] == b"def f0():\n    return 0\n"


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
    assert status == {"pkg/c.py": "R"}


def test_hash_object_matches_git(repo):
    sha = gitio.hash_object(repo, b"def alpha():\n    return 1\n")
    assert sha == gitio.ls_tree(repo, "HEAD")["a.py"]


def test_merge_base_and_default_branch(repo, write):
    base = gitio.rev_parse(repo, "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    write("c.py", "def gamma():\n    pass\n", commit="feature work")
    assert gitio.merge_base(repo, "main", "feature") == base
    assert gitio.default_branch(repo) == "main"
