"""Blob-cache garbage collection and warming-only git hooks.

Both operations are pure housekeeping: `gc` never touches Layer 2 (the
materialized per-revision graph), and the hooks `install_hooks` writes only
ever warm Layer 1 in the background. Per D5 in the design doc, every
`codegraph` query reconciles the working tree itself before answering, so
neither of these is ever required for correctness -- only for speed. If a
hook never fires, or `gc` is never run, results are byte-identical.
"""

from __future__ import annotations

import stat
from pathlib import Path

from codegraph.store import Store

_CHUNK = 400  # stays well under SQLite's default 999-variable limit per statement

_HOOK_NAMES = ("post-commit", "post-checkout", "post-merge")

_BEGIN_MARKER = "# >>> codegraph (warming only; safe to remove) >>>"
_END_MARKER = "# <<< codegraph (warming only; safe to remove) <<<"

# Preserves `$?` from whatever ran before this block (0 at a fresh script's
# start) so appending to a pre-existing hook can never flip its exit status:
# our warming command is backgrounded and its own status discarded, then the
# script exits with the status it already had.
_HOOK_BLOCK = f"""{_BEGIN_MARKER}
_codegraph_status=$?
( cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && codegraph index --quiet >/dev/null 2>&1 & ) >/dev/null 2>&1 || true
exit $_codegraph_status
{_END_MARKER}
"""


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def gc(store: Store, keep_revs: set[str]) -> int:
    """Prune Layer 1 (the content-addressed parse cache) down to only the
    blobs referenced by `keep_revs`'s materialized trees.

    Layer 2 (`revisions`, `tree`, `nodes`, `edges`, `effects`, `imports`,
    `unresolved`) is never touched -- it is already fully materialized for
    every revision in the store, retained or not, and does not read back
    through Layer 1. Losing a Layer 1 entry only costs a re-parse the next
    time that blob's content is seen; it never invalidates an existing
    answer.

    `keep_revs` empty is well-defined, not accidental: nothing is retained,
    so every Layer 1 row is unreferenced and gets removed.

    Returns the number of Layer 1 blob rows removed.
    """
    connection = store.connection

    keep_shas: set[str] = set()
    if keep_revs:
        for batch in _chunks(sorted(keep_revs), _CHUNK):
            placeholders = ",".join("?" * len(batch))
            rows = connection.execute(
                f"SELECT DISTINCT blob_sha FROM tree WHERE rev IN ({placeholders})",
                tuple(batch),
            )
            keep_shas.update(row["blob_sha"] for row in rows)

    all_shas = {row["blob_sha"] for row in connection.execute("SELECT blob_sha FROM blobs")}
    remove_shas = sorted(all_shas - keep_shas)
    if not remove_shas:
        return 0

    with connection:
        for batch in _chunks(remove_shas, _CHUNK):
            placeholders = ",".join("?" * len(batch))
            params = tuple(batch)
            connection.execute(f"DELETE FROM blob_nodes WHERE blob_sha IN ({placeholders})", params)
            connection.execute(f"DELETE FROM blob_refs WHERE blob_sha IN ({placeholders})", params)
            connection.execute(
                f"DELETE FROM blob_imports WHERE blob_sha IN ({placeholders})", params
            )
            connection.execute(f"DELETE FROM blobs WHERE blob_sha IN ({placeholders})", params)

    return len(remove_shas)


def _hooks_dir(root: Path) -> Path:
    """Resolve `root`'s git hooks directory, honoring `.git`-as-a-file
    (worktrees, submodules) as well as the ordinary `.git`-as-a-directory
    case that every test in this repo uses."""
    git_path = root / ".git"
    if git_path.is_dir():
        return git_path / "hooks"
    if git_path.is_file():
        text = git_path.read_text().strip()
        prefix = "gitdir:"
        if text.startswith(prefix):
            gitdir = Path(text[len(prefix) :].strip())
            if not gitdir.is_absolute():
                gitdir = (root / gitdir).resolve()
            return gitdir / "hooks"
    raise FileNotFoundError(f"not a git repository: {root}")


def install_hooks(root: Path) -> list[Path]:
    """Write (or extend) `post-commit`, `post-checkout`, and `post-merge`
    hooks that warm the codegraph cache in the background.

    Idempotent: re-running never double-appends the codegraph block. A
    pre-existing hook is never overwritten -- the codegraph block is
    appended after whatever was already there, guarded so it can only ever
    add a background warm-up, never change the hook's own exit status.

    Returns the paths of all three hooks (written or already present).
    """
    hooks_dir = _hooks_dir(root)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name in _HOOK_NAMES:
        path = hooks_dir / name
        existing = path.read_text() if path.exists() else ""

        if _BEGIN_MARKER not in existing:
            if not existing:
                existing = "#!/bin/sh\n"
            elif not existing.endswith("\n"):
                existing += "\n"
            path.write_text(existing + _HOOK_BLOCK)

        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        written.append(path)

    return written
