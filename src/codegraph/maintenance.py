"""Blob-cache garbage collection and warming-only git hooks.

Both operations are pure housekeeping: `gc` never touches Layer 2 (the
materialized per-revision graph), and the hooks `install_hooks` writes only
ever warm Layer 1 in the background. Per D5 in the design doc, every
`codegraph` query reconciles the working tree itself before answering, so
neither of these is ever required for correctness -- only for speed. If a
hook never fires, or `gc` is never run, results are byte-identical.

Installing hooks is deliberately conservative: anything about a pre-existing
hook that this module can't confidently reason about (a binary file, an
interpreter it doesn't recognize as shell-compatible, a half-formed marker
from some prior mishap, any other unexpected error) is a loud skip, never a
guess. D5 is what licenses that: warming is optional, so skipping a hook
only ever costs speed, and a corrupted user hook is a strictly worse outcome
than one that stays un-warmed.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from codegraph.store import Store

_CHUNK = 400  # stays well under SQLite's default 999-variable limit per statement

_HOOK_NAMES = ("post-commit", "post-checkout", "post-merge")

_BEGIN_MARKER = "# >>> codegraph (warming only; safe to remove) >>>"
_END_MARKER = "# <<< codegraph (warming only; safe to remove) <<<"

# Fires a backgrounded job and falls straight through -- no `exit`, no
# capturing `$?`. It has to sit as the *first* statement after the shebang
# (see `_insert_after_shebang`) rather than at the end: a pre-existing hook
# commonly ends in its own `exit 0` (or `exit 1`, or hits one inside an
# `if`), and code appended after that is simply never reached. A block that
# runs first can't be skipped that way, and since all it does is background
# a job, it can't change -- or need to preserve -- whatever exit status the
# rest of the script goes on to produce.
_HOOK_BLOCK = f"""{_BEGIN_MARKER}
( cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && codegraph index --quiet >/dev/null 2>&1 & ) >/dev/null 2>&1 || true
{_END_MARKER}
"""

# Splicing shell syntax into a script written for a different interpreter
# corrupts it outright (a Perl or Python hook fails to even parse). This is
# the allowlist of interpreters `_HOOK_BLOCK`'s POSIX-shell syntax is safe
# to land inside; anything else is left completely untouched.
_SHELL_INTERPRETERS = {"sh", "bash", "dash", "ash", "ksh", "zsh"}


class _MalformedMarker(Exception):
    """A codegraph marker line was found without its matching partner --
    a lone BEGIN with no END, a lone END with no BEGIN, or two BEGINs
    with no END between them. This is damage from a previous run gone
    wrong, or a hand-edited file, and guessing how much surrounding
    content to delete risks eating real statements. The caller must
    refuse to touch the file, not repair around it."""


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


def _shebang_interpreter(shebang_line: str) -> str:
    """The interpreter name a `#!...` line claims, resolving
    `#!/usr/bin/env [flags] <interp> [flags]` to `<interp>` the same as a
    direct `#!/path/to/<interp> [flags]`. `env`'s own flags (`-S`, `-i`, ...)
    are `-`-prefixed and skipped when looking for the interpreter token, so
    `#!/usr/bin/env -S bash -e` resolves to `bash`, not `-S`. Empty string
    if the line names nothing usable (a bare `#!`, or `env` followed by
    nothing but its own flags)."""
    tokens = shebang_line[2:].split()
    if not tokens:
        return ""
    name = Path(tokens[0]).name
    if name == "env":
        name = ""
        for token in tokens[1:]:
            if not token.startswith("-"):
                name = Path(token).name
                break
    return name


def _is_shell_shebang(shebang_line: str) -> bool:
    return _shebang_interpreter(shebang_line) in _SHELL_INTERPRETERS


def _is_marker_line(line: str, marker: str) -> bool:
    """True only if `line` (ignoring surrounding whitespace) is *exactly*
    the marker, not merely a line that happens to contain it somewhere --
    a hook whose own comment or quoted string mentions this marker text
    must never be mistaken for a real block boundary."""
    return line.strip() == marker


def _strip_existing_blocks(text: str) -> str:
    """Remove every complete codegraph block (a BEGIN marker line, its
    contents, and the following END marker line -- matched as whole lines
    only, never a substring inside a longer line). Loops until every
    marker pair is gone, so a file carrying more than one block (an old
    one left at the end plus a newer one spliced at the top, say) converges
    to zero, not one-fewer.

    Raises `_MalformedMarker` if the marker lines found don't pair up
    cleanly -- a BEGIN with no following END, an END with no preceding
    BEGIN, or two BEGINs with no END between them. The caller must treat
    that as damage to leave alone, not a shape to repair by guessing.
    """
    lines = text.splitlines(keepends=True)
    marker_positions = [
        (index, "begin")
        for index, line in enumerate(lines)
        if _is_marker_line(line, _BEGIN_MARKER)
    ] + [
        (index, "end") for index, line in enumerate(lines) if _is_marker_line(line, _END_MARKER)
    ]
    marker_positions.sort()

    blocks: list[tuple[int, int]] = []
    pending_begin: int | None = None
    for index, kind in marker_positions:
        if kind == "begin":
            if pending_begin is not None:
                raise _MalformedMarker
            pending_begin = index
        else:
            if pending_begin is None:
                raise _MalformedMarker
            blocks.append((pending_begin, index))
            pending_begin = None
    if pending_begin is not None:
        raise _MalformedMarker

    if not blocks:
        return text

    removed = {index for start, end in blocks for index in range(start, end + 1)}
    return "".join(line for index, line in enumerate(lines) if index not in removed)


def _insert_after_shebang(existing: str) -> str:
    """Splice `_HOOK_BLOCK` in as the first statement of the script, right
    after its shebang line so it always runs -- before any pre-existing
    `exit`, `exec`, or `if`-guarded early return could skip it.

    A file with no shebang gets one (`#!/bin/sh`) prepended; git hooks with
    no shebang already run under the shell's default anyway, so this changes
    nothing about how the rest of the script executes.
    """
    lines = existing.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        return lines[0] + _HOOK_BLOCK + "".join(lines[1:])
    return "#!/bin/sh\n" + _HOOK_BLOCK + existing


@dataclass(frozen=True)
class HookResult:
    """One hook's outcome: installed (written or repaired), or skipped with
    a human-readable reason."""

    name: str
    path: Path
    installed: bool
    reason: str | None = None


def _plan_one_hook(name: str, path: Path) -> HookResult:
    """Decide and apply the outcome for a single hook. Never raises for an
    ordinary "can't safely touch this file" case -- those come back as a
    skipped `HookResult` -- so any exception that does escape is a genuinely
    unexpected failure, which `plan_hooks` isolates per-hook."""
    if path.exists():
        try:
            existing = path.read_text()
        except UnicodeDecodeError:
            # A compiled/binary hook (pre-commit frameworks and compiled
            # shims ship these). It IS a non-shell hook -- same refusal as
            # Perl, just detected a layer earlier, before there's even a
            # shebang line to read.
            return HookResult(
                name=name,
                path=path,
                installed=False,
                reason=(
                    "existing hook is not valid UTF-8 (binary or non-text hook); "
                    "warming not installed"
                ),
            )
    else:
        existing = ""

    lines = existing.splitlines()
    if lines and lines[0].startswith("#!") and not _is_shell_shebang(lines[0]):
        return HookResult(
            name=name,
            path=path,
            installed=False,
            reason=(
                f"existing hook uses a non-shell interpreter ({lines[0].strip()}); "
                "warming not installed"
            ),
        )

    try:
        stripped = _strip_existing_blocks(existing)
    except _MalformedMarker:
        return HookResult(
            name=name,
            path=path,
            installed=False,
            reason=(
                "existing hook has a malformed codegraph marker (a begin marker with "
                "no matching end, or vice versa); please repair or remove it by hand -- "
                "warming not installed"
            ),
        )

    content = _insert_after_shebang(stripped)
    path.write_text(content)
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return HookResult(name=name, path=path, installed=True)


def plan_hooks(root: Path) -> list[HookResult]:
    """Install (or repair) `post-commit`, `post-checkout`, and
    `post-merge`, one `HookResult` per hook.

    A pre-existing hook whose shebang names an interpreter outside
    `_SHELL_INTERPRETERS` (Perl, Python, Ruby, Node, ...), or that isn't
    valid UTF-8 at all (a binary hook), is **left completely untouched**
    and reported as skipped -- splicing POSIX-shell syntax into it would
    corrupt the script outright, not just fail to warm it. A file with no
    shebang at all makes no interpreter claim to contradict, so it's
    treated the same as a shell script (a `#!/bin/sh` is added).

    A hook already carrying one or more codegraph blocks -- from this run
    or an older version of `install_hooks` -- has all of them stripped and
    a single fresh one spliced in. This is what makes repeated calls
    idempotent (always converges to exactly one block, however many were
    there before) *and* self-repairing (a block left dead by a since-fixed
    bug, e.g. one appended after the pre-existing hook's own `exit 0`, is
    replaced with a live one instead of staying broken forever). A hook
    whose marker lines don't pair up cleanly is left untouched and skipped
    instead -- that shape is damage, not something to guess a repair for.

    One hook's unexpected failure (a permission error, anything not
    already handled above) is isolated to that hook: it comes back as a
    skipped result with the error as its reason, and the other hooks are
    still processed. A maintenance command that half-runs because of one
    bad hook, or dumps a raw traceback, is worse than one that skips
    loudly and keeps going.
    """
    hooks_dir = _hooks_dir(root)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    results: list[HookResult] = []
    for name in _HOOK_NAMES:
        path = hooks_dir / name
        try:
            results.append(_plan_one_hook(name, path))
        except Exception as exc:  # noqa: BLE001 -- per-hook isolation net, see docstring
            results.append(
                HookResult(
                    name=name,
                    path=path,
                    installed=False,
                    reason=f"unexpected error inspecting hook ({exc}); warming not installed",
                )
            )

    return results


def install_hooks(root: Path) -> list[Path]:
    """Paths of the hooks actually installed or repaired.

    Omits any hook skipped for using a non-shell interpreter, being
    unreadable as text, carrying a malformed marker, or failing
    unexpectedly -- see `plan_hooks` for the reason behind each skip,
    which is what the CLI surfaces to the user (a skip must be loud, never
    silent: D5 licenses skipping a hook, not hiding that it happened).
    """
    return [result.path for result in plan_hooks(root) if result.installed]
