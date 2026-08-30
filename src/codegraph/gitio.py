"""Thin wrapper over the git CLI. Never touches SQLite."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO


class GitError(Exception):
    """A git subprocess failed."""


def _run(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True, check=False)
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode(errors="replace").strip())
    return proc.stdout


def is_repo(root: Path) -> bool:
    try:
        _run(root, "rev-parse", "--git-dir")
    except (GitError, FileNotFoundError):
        return False
    return True


def ls_tree(root: Path, rev: str) -> dict[str, str]:
    out = _run(root, "ls-tree", "-r", "-z", "--format=%(objectname) %(path)", rev)
    tree: dict[str, str] = {}
    for entry in out.split(b"\0"):
        if not entry:
            continue
        sha, _, path = entry.decode().partition(" ")
        if path.endswith(".py"):
            tree[path] = sha
    return tree


def cat_file_batch(root: Path, shas: Iterable[str]) -> Iterator[tuple[str, bytes]]:
    """Stream blob contents through a single `git cat-file --batch` process.

    The requested SHAs are written to the child's stdin from a background
    thread while this generator reads responses from stdout on the calling
    thread. Writing the whole batch up front (before reading anything) would
    deadlock once the batch is large enough to fill the stdin pipe buffer
    (~64KiB on Linux, roughly 1500+ 41-byte SHA lines): git would block
    writing accumulated blob output that nobody is reading yet, while this
    process blocked writing the remaining SHAs that git isn't yet reading.
    """
    wanted = list(shas)
    if not wanted:
        return
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout

    def _feed(stdin: IO[bytes]) -> None:
        try:
            for sha in wanted:
                stdin.write((sha + "\n").encode())
            stdin.flush()
        except BrokenPipeError:
            pass  # child exited early; reader loop below observes EOF
        finally:
            stdin.close()

    writer = threading.Thread(target=_feed, args=(proc.stdin,), daemon=True)
    writer.start()
    try:
        for _ in wanted:
            header = proc.stdout.readline().decode().strip()
            if not header or header.endswith("missing"):
                continue
            sha, _kind, size_text = header.split(" ")
            payload = proc.stdout.read(int(size_text))
            proc.stdout.read(1)  # trailing newline
            yield sha, payload
    finally:
        writer.join()
        proc.stdout.close()
        proc.stdin.close()
        proc.stderr.close()
        proc.wait()


def status_paths(root: Path) -> dict[str, str]:
    """Map repo-relative path to porcelain status code (M, A, D, ??, R, ...).

    Uses `--porcelain=v1 -z`: entries are NUL-separated as `XY<space>PATH`.
    A rename/copy entry (X == 'R' or 'C') is followed by a second,
    NUL-separated field holding the old path with no XY prefix — that field
    must be consumed and discarded, not treated as its own entry.
    """
    out = _run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    status: dict[str, str] = {}
    fields = [f for f in out.decode().split("\0") if f]
    index = 0
    while index < len(fields):
        field = fields[index]
        code, path = field[:2].strip() or field[:2], field[3:]
        if code.startswith(("R", "C")):
            index += 1  # rename/copy source follows; skip it
        if path.endswith(".py"):
            status[path] = code
        index += 1
    return status


def hash_object(root: Path, data: bytes) -> str:
    return _run(root, "hash-object", "-t", "blob", "--stdin", stdin=data).decode().strip()


def rev_parse(root: Path, rev: str) -> str:
    return _run(root, "rev-parse", rev).decode().strip()


def merge_base(root: Path, a: str, b: str) -> str:
    return _run(root, "merge-base", a, b).decode().strip()


def default_branch(root: Path) -> str:
    for candidate in ("origin/HEAD", "main", "master"):
        try:
            resolved = _run(root, "rev-parse", "--abbrev-ref", candidate).decode().strip()
        except GitError:
            continue
        return resolved.removeprefix("origin/")
    raise GitError("no default branch found")
