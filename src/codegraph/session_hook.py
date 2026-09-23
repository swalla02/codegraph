"""The opt-in writer for session pointers: a `prepare-commit-msg` hook.

**This writes into commit messages.** That puts it outside everything else
codegraph does, which reads the repository and never changes it, and it is
why this module is kept apart from the core:

- `install-hooks` never installs it. Those hooks are a pure warming
  optimization (D5): an answer is byte-identical whether they fire or not.
  This hook changes the commit a user makes, so it cannot ride along with
  them, and it is fenced by its own markers so neither installer can strip
  or repair the other's block.
- `init` never installs it either; `init` never touches `.git/` at all.
- The reader, `sessions.py`, does not know it exists. A trailer this hook
  wrote and one typed by hand are the same bytes in the same place, read
  by `git` the same way. Someone who never installs this loses nothing but
  the convenience.

What the hook does, and what it deliberately does not:

- **Nothing unless `CODEGRAPH_SESSION` is set and non-empty.** The agent
  (or the user) exports the pointer; the hook only copies it. The value is
  opaque here exactly as it is to the reader.
- **Appends through `git interpret-trailers`**, so it lands in the
  message's trailer block beside `Co-Authored-By:` and `Signed-off-by:`,
  before git's own `#` comment lines, under whatever trailer conventions
  the repository configures. `--if-exists addIfDifferent` makes it
  idempotent: re-running over a message that already carries this exact
  pointer adds nothing, which is what an `--amend` in the same session
  needs. An amend from a *different* session adds the second pointer --
  both sessions did shape that commit.
- **Writes only into a message that already exists.** The hook's second
  argument says where the draft came from, and only two sources qualify:
  `message` (`-m`, `-F`) and `commit` (`--amend`, `-c`, `-C`). That covers
  how agents commit. Everything else is left alone, each for a reason:

  - no source (the editor opens on an empty draft) and `template`: the
    hook runs *before* the editor, so the user has not written anything
    yet. A trailer there makes the draft non-empty and different from the
    template, and git would no longer abort when the user quits the editor
    without writing a message -- it would commit a message that is only a
    trailer. The escape hatch matters more than the pointer.
  - `merge` and `squash`: git drafts those from commits that already
    exist, and those commits carry their own pointers. The session that
    happens to be exporting one when the merge is made did not write the
    code being merged, and a false pointer is worse than a missing one,
    which the reader already treats as ordinary.
- **Skips a value containing a newline.** A trailer is one line; a second
  line would forge a second trailer, or break the block.
- **Never fails the commit.** The block runs in a subshell ending in
  `|| true`, so a missing `git interpret-trailers`, a read-only message
  file, or a user hook running under `set -eu` cannot abort the commit it
  is decorating. Losing a pointer costs a link; losing a commit costs work.

Installing follows `install-hooks`' rules exactly, through the same
`maintenance` helpers: spliced in after the shebang so a pre-existing
`exit` cannot skip it, a user's existing hook kept verbatim, a non-shell or
binary hook left untouched and reported, a half-formed marker refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codegraph.maintenance import (
    HookBlock,
    HookResult,
    _hooks_dir,
    _plan_one_hook,
    _read_hook,
)
from codegraph.markers import MalformedMarkerError, strip_marker_blocks
from codegraph.sessions import TRAILER_KEY

HOOK_NAME = "prepare-commit-msg"

#: The environment variable the hook reads the pointer from.
ENV_VAR = "CODEGRAPH_SESSION"

_BEGIN_MARKER = "# >>> codegraph session trailer (writes into commit messages) >>>"
_END_MARKER = "# <<< codegraph session trailer (writes into commit messages) <<<"

# `$1` is the message file and `$2` its source (githooks(5)). The newline
# held in `nl` is the one character a pointer may not contain. The module
# docstring gives the reason for each refusal.
_BLOCK_TEXT = f"""{_BEGIN_MARKER}
(
  nl='
'
  case "${{2:-}}" in message|commit) ;; *) exit 0 ;; esac
  case "${{{ENV_VAR}:-}}" in ''|*"$nl"*) exit 0 ;; esac
  git interpret-trailers --in-place --if-exists addIfDifferent --if-missing add \\
    --trailer "{TRAILER_KEY}: ${ENV_VAR}" "$1"
) >/dev/null 2>&1 || true
{_END_MARKER}
"""

SESSION_TRAILER = HookBlock(_BLOCK_TEXT, _BEGIN_MARKER, _END_MARKER, "session trailer")


def install_session_hook(root: Path) -> HookResult:
    """Install (or repair) the session-trailer block in `root`'s
    `prepare-commit-msg` hook, creating the hook if there is none.

    Idempotent, and never clobbers a hook that is already there: see
    `maintenance.plan_hooks` for the rules it shares. Raises
    `FileNotFoundError` for a directory that is not a git repository.
    """
    hooks_dir = _hooks_dir(root)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return _plan_one_hook(HOOK_NAME, hooks_dir / HOOK_NAME, SESSION_TRAILER)


@dataclass(frozen=True)
class RemovalResult:
    """What removing the block did: `removed` is true when a block was
    found and taken out, and `reason` says why a file was left alone."""

    path: Path
    removed: bool
    reason: str | None = None


def uninstall_session_hook(root: Path) -> RemovalResult:
    """Take the session-trailer block back out of `prepare-commit-msg`,
    leaving everything else in the hook byte-for-byte.

    A hook left holding nothing but a shebang is deleted: it did nothing
    before the block went in, so there is nothing to keep, and leaving an
    empty executable behind would be litter from a command that was asked
    to clean up. A warming block is never touched -- it has its own
    markers. Raises `FileNotFoundError` for a directory that is not a git
    repository.
    """
    path = _hooks_dir(root) / HOOK_NAME
    if not path.exists():
        return RemovalResult(path=path, removed=False)
    existing = _read_hook(HOOK_NAME, path, SESSION_TRAILER)
    if isinstance(existing, HookResult):
        # A binary or non-shell hook: install would never have written
        # into it, so there is no block of ours to remove.
        return RemovalResult(path=path, removed=False)
    try:
        stripped = strip_marker_blocks(existing, _BEGIN_MARKER, _END_MARKER)
    except MalformedMarkerError:
        return RemovalResult(
            path=path,
            removed=False,
            reason=(
                "existing hook has a malformed codegraph session-trailer marker; "
                "please repair or remove it by hand -- nothing removed"
            ),
        )
    if stripped == existing:
        return RemovalResult(path=path, removed=False)
    lines = [line for line in stripped.splitlines() if line.strip()]
    if not lines or (len(lines) == 1 and lines[0].startswith("#!")):
        path.unlink()
    else:
        path.write_text(stripped)
    return RemovalResult(path=path, removed=True)
