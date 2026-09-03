"""`codegraph init`: make a repository's coding agents aware of codegraph.

A teammate who installs the binary still has an agent that has never heard
of it, and an agent that has never heard of it greps for callers -- the
exact anti-pattern this tool exists to displace. `init` closes that gap
with three files and no daemon, no registry, and no MCP server (a non-goal;
see the README).

Why `AGENTS.md` is the target: it is the one discovery channel that is not
per-vendor. 25+ agent tools read it -- Codex, Cursor, Gemini CLI, Copilot's
coding agent, Devin, Windsurf, Zed, Aider, goose, opencode, Jules, Junie,
Amp, Warp, VS Code, RooCode, Kilo Code, Factory -- across 60k+ repositories.

Why `CLAUDE.md` is only ever *edited*, never created: Claude Code reads
`CLAUDE.md`, not `AGENTS.md`, and the documented bridge is an `@AGENTS.md`
import line inside it (https://code.claude.com/docs/en/memory). But that is
the wrong shape for Claude Code specifically -- `CLAUDE.md` loads into
*every* session, whereas this repo's plugin skill loads only when relevant.
So if a `CLAUDE.md` already exists we wire it up (the user has already
accepted that cost for their own content), and if it does not we say what
to install instead rather than manufacturing a file nobody asked for.

What `init` deliberately does NOT do: touch `.git/hooks/`. Warming hooks
stay behind the explicit `install-hooks` command. A command named `init` is
the one a person runs before they trust the tool, and writing into git's own
directory as an unannounced side effect of "set up some markdown" is how a
tool loses that trust.

Everything here is idempotent and additive. The codegraph section of
`AGENTS.md` is delimited by markers and updated in place; a `codegraph.toml`
that already exists is never rewritten; prose above, below and around any of
it is copied through byte-for-byte. See `markers.py` for the marker
discipline, which `install_hooks` worked out first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codegraph.config import CONFIG_NAME
from codegraph.markers import MalformedMarkerError, replace_marker_blocks

AGENTS_NAME = "AGENTS.md"
CLAUDE_NAME = "CLAUDE.md"

BEGIN_MARKER = "<!-- codegraph:begin -->"
END_MARKER = "<!-- codegraph:end -->"

#: Claude Code's import syntax for pulling another file into memory.
CLAUDE_IMPORT_LINE = "@AGENTS.md"

CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
SKIPPED = "skipped"

_INSTALL_COMMAND = "uv tool install --python 3.12 git+https://github.com/swalla02/codegraph"

# Short on purpose. This block loads into *every* session of every agent
# that reads AGENTS.md, so it is priced per turn, not per repo: it has to
# earn its context by naming the three commands and the trigger, then hand
# off to `codegraph guide` (one cheap tool call, on demand) for exit codes,
# output format and the rest. An inlined full workflow here would be the
# same mistake as an always-loaded MCP tool definition.
AGENTS_BLOCK = f"""{BEGIN_MARKER}
## codegraph

Before editing a Python function or class -- and whenever asked "what breaks if
I change this", "what does this affect", or "what did this branch change" --
query the call graph instead of grepping for callers:

- `codegraph impact <symbol>` -- ranked dependents; what a change could break.
- `codegraph effects <symbol>` -- side effects reachable downstream, each with
  a witness path to the exact `file:line` that causes it.
- `codegraph diff` -- what this branch changed, by content hash.

Grep misses dynamic dispatch and never tells you when you have found the last
caller. Run `codegraph guide` for the full workflow, exit codes, and how to
read the output. If the command is missing, install it:
`{_INSTALL_COMMAND}`
{END_MARKER}
"""

_AGENTS_PREAMBLE = """# AGENTS.md

Conventions for coding agents working in this repository.
"""

# Every line is commented out, so the file is inert the moment it lands and
# stays inert until someone means it. The values shown are the defaults
# already in force, which makes this a readable record of current behavior
# rather than a config that silently changes behavior by existing.
_CONFIG_STUB = '''# codegraph configuration -- https://github.com/swalla02/codegraph
#
# Nothing here is active: every setting is commented out, and the values shown
# are the defaults codegraph already applies. Uncomment what you want to change.


# How a file path becomes a Python module name for import resolution. The
# longest matching root is stripped from the front of the path, so with "src"
# in the list, src/pay/service.py resolves as the module `pay.service`. If
# `codegraph status` reports a lot of unresolved references, a missing source
# root is the first thing to check.
# source_roots = ["", "src"]


# There is deliberately no setting here for the bare-name fan-out. A call like
# `item.save()` that names nothing importable, nothing module-local and nothing
# reachable through `self` falls back to matching `save` against every
# definition in the repository -- 971 candidates for a single call site on
# django. That set is not stored: it is exactly "every definition named save",
# which the graph already holds, so codegraph records the call once and expands
# it when a query asks. How many of them you want to see is a property of the
# question, so it is `codegraph impact --limit N` (and `--all`), per query,
# rather than a setting that changes the graph for every future question.
#
# `ambiguity_limit` used to live here. It is still accepted and now does
# nothing; remove it.


# Effect overrides, merged over the built-in catalog. The built-ins only know
# public library calls (stdlib, requests/httpx, SQLAlchemy, psycopg, boto3,
# ...), but most real side effects sit behind a house abstraction instead, and
# for a call into a module THIS repository defines the built-ins are skipped
# entirely -- only the overrides below apply.
#
# `match` is a dotted-name glob, matched against the call target after the
# calling file's imports are expanded: with `from app import db` in scope, a
# call to `db.save()` is matched as `app.db.save`. `kind` is one of DB_READ,
# DB_WRITE, NETWORK, FS_READ, FS_WRITE, PROCESS, ENV_READ, GLOBAL_MUTATE,
# NONDETERMINISM. Where two rules match, the one with the longer literal
# prefix wins, so a specific override always beats a general built-in.

# Every call into the house database module.
# [[effect]]
# match = "app.db.*"
# kind = "DB_WRITE"

# Any `<something>.enqueue(...)`, whatever the receiver turns out to be. A
# wildcard head matches broadly and is scored LOW confidence for exactly that
# reason; a fully literal pattern is scored HIGH.
# [[effect]]
# match = "*.enqueue"
# kind = "NETWORK"
'''


@dataclass(frozen=True)
class FileResult:
    """One file's outcome, with `reason` carrying the detail a user needs
    to act: why a file was left alone, or why it could not be touched."""

    path: Path
    action: str
    reason: str | None = None


def _read_text(path: Path) -> str:
    """Read `path` without newline translation, so a CRLF file is seen as
    the CRLF file it is rather than silently rewritten to LF on the way
    back out. `Path.read_text` only grew a `newline=` parameter in 3.13,
    and this package supports 3.12."""
    return path.read_bytes().decode("utf-8")


def _write_text(path: Path, text: str) -> None:
    """Write `text` verbatim -- `newline=""` disables the translation that
    would otherwise turn every `\\n` into `\\r\\n` on Windows and undo the
    line endings `_block_for` was careful to match."""
    path.write_text(text, encoding="utf-8", newline="")


def _block_for(text: str) -> str:
    """`AGENTS_BLOCK` rendered with the line ending the file already uses.

    A file with any CRLF line is treated as a CRLF file, so the block we
    own matches its surroundings and, crucially, is byte-identical on a
    re-run. Only the block is converted: a mixed-ending file stays exactly
    as mixed as we found it, because nothing outside our own lines is
    rewritten.
    """
    return AGENTS_BLOCK.replace("\n", "\r\n") if "\r\n" in text else AGENTS_BLOCK


def _separator(body: str, newline: str) -> str:
    """The newlines needed between existing content and an appended block:
    enough for one blank line, and none if there is already one.

    Written as "top up what is there" rather than "trim and re-add" so that
    a second run is byte-identical (the blank line it would add is already
    present, so it adds nothing) *without* normalizing away trailing
    whitespace the user chose to leave in their own file.
    """
    if not body or body.endswith(newline * 2):
        return ""
    return newline if body.endswith(newline) else newline * 2


def _plan_agents_md(root: Path) -> FileResult:
    """Create `AGENTS.md`, or splice/refresh only codegraph's own block in
    an existing one."""
    path = root / AGENTS_NAME
    if not path.exists():
        _write_text(path, f"{_AGENTS_PREAMBLE}\n{AGENTS_BLOCK}")
        return FileResult(path, CREATED)

    try:
        existing = _read_text(path)
    except UnicodeDecodeError:
        return FileResult(
            path,
            SKIPPED,
            f"{AGENTS_NAME} is not valid UTF-8; refusing to rewrite it (edit it by hand "
            f"and add a codegraph section, or fix its encoding and re-run)",
        )

    block = _block_for(existing)
    try:
        replaced = replace_marker_blocks(existing, BEGIN_MARKER, END_MARKER, block)
    except MalformedMarkerError:
        return FileResult(
            path,
            SKIPPED,
            f"{AGENTS_NAME} has a malformed codegraph marker (a `{BEGIN_MARKER}` with no "
            f"matching `{END_MARKER}`, or vice versa); repair or remove it by hand and "
            f"re-run -- the file was left untouched",
        )

    if replaced is None:
        newline = "\r\n" if "\r\n" in existing else "\n"
        replaced = existing + _separator(existing, newline) + block

    if replaced == existing:
        return FileResult(path, UNCHANGED, "codegraph section already current")
    _write_text(path, replaced)
    return FileResult(path, UPDATED)


def _plan_claude_md(root: Path) -> FileResult:
    """Add the `@AGENTS.md` import to an existing `CLAUDE.md`. Never
    creates one -- see the module docstring."""
    path = root / CLAUDE_NAME
    if not path.exists():
        return FileResult(
            path,
            UNCHANGED,
            "no CLAUDE.md here, and none created on purpose: for Claude Code, install "
            "the plugin instead (`/plugin marketplace add swalla02/codegraph`) -- its "
            "skill loads on demand rather than into every session",
        )

    try:
        existing = _read_text(path)
    except UnicodeDecodeError:
        return FileResult(
            path,
            SKIPPED,
            f"{CLAUDE_NAME} is not valid UTF-8; refusing to rewrite it (add a "
            f"`{CLAUDE_IMPORT_LINE}` line by hand)",
        )

    # A plain substring test, not a line-anchored one: over-detecting means
    # we decline to add an import the user already has some other way (in a
    # sentence, inside a list, behind a comment), which costs them one
    # manual line. Under-detecting would append a second import of the same
    # file on every run, which is the failure that actually compounds.
    if CLAUDE_IMPORT_LINE in existing:
        return FileResult(path, UNCHANGED, f"already imports {AGENTS_NAME}")

    newline = "\r\n" if "\r\n" in existing else "\n"
    _write_text(path, existing + _separator(existing, newline) + CLAUDE_IMPORT_LINE + newline)
    return FileResult(path, UPDATED, f"added the `{CLAUDE_IMPORT_LINE}` import")


def _plan_config(root: Path) -> FileResult:
    """Write the commented `codegraph.toml` stub, only if there is none.

    An existing config is hand-written, committed, and shared (see the
    README) -- it is the one file here whose content this command could
    not possibly improve on, so it is never rewritten, not even to add
    comments around what is already there.
    """
    path = root / CONFIG_NAME
    if path.exists():
        return FileResult(path, UNCHANGED, "already present; never overwritten")
    _write_text(path, _CONFIG_STUB)
    return FileResult(path, CREATED)


def plan_init(root: Path) -> list[FileResult]:
    """Run every step, one `FileResult` each, in the order printed.

    One step's unexpected failure (a read-only directory, a permission
    error, anything not already handled inside the step) is isolated to
    that step and reported as a skip: an `init` that half-runs and stops
    leaves a repo in a state nobody can reason about, whereas one that
    writes what it can and names what it could not is recoverable by
    re-running after the fix. Same discipline as `plan_hooks`.
    """
    results: list[FileResult] = []
    for step, name in (
        (_plan_agents_md, AGENTS_NAME),
        (_plan_claude_md, CLAUDE_NAME),
        (_plan_config, CONFIG_NAME),
    ):
        try:
            results.append(step(root))
        except OSError as exc:
            results.append(
                FileResult(root / name, SKIPPED, f"could not write {name} ({exc.strerror or exc})")
            )
        except Exception as exc:  # noqa: BLE001 -- per-file isolation net, see docstring
            results.append(FileResult(root / name, SKIPPED, f"unexpected error ({exc})"))
    return results
