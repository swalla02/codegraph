"""Marker-delimited blocks that codegraph owns inside files it does not.

Two commands splice a codegraph-managed block into a file whose *rest* is
the user's: `install-hooks` into `.git/hooks/*`, and `init` into
`AGENTS.md`. Both need the same three properties, and `install-hooks`
learned all three the hard way, so they live here once rather than being
re-derived per caller:

- **Whole-line anchoring.** A marker is recognized only when it is the
  entire line (modulo surrounding whitespace). Substring matching deletes
  everything between two lines that merely *mention* the marker text --
  a hook echoing a reminder string, or an `AGENTS.md` documenting this very
  convention.
- **Converge on exactly one block.** Every complete block is accounted for,
  however many there are, so a re-run lands on one -- never one-fewer, never
  one-more -- and a stale block left by an older version of the writer is
  replaced rather than left to rot beside a live one.
- **Refusal over repair.** Markers that don't pair up cleanly are damage
  from a previous mishap or a hand-edit. Guessing how much surrounding
  content the missing partner would have covered risks eating the user's
  own text, so the caller is told to leave the file alone instead.
"""

from __future__ import annotations


class MalformedMarkerError(Exception):
    """A codegraph marker line was found without its matching partner --
    a lone BEGIN with no END, a lone END with no BEGIN, or two BEGINs
    with no END between them. This is damage from a previous run gone
    wrong, or a hand-edited file, and guessing how much surrounding
    content to delete risks eating real statements. The caller must
    refuse to touch the file, not repair around it."""


def is_marker_line(line: str, marker: str) -> bool:
    """True only if `line` (ignoring surrounding whitespace) is *exactly*
    the marker, not merely a line that happens to contain it somewhere --
    a file whose own comment or quoted string mentions this marker text
    must never be mistaken for a real block boundary."""
    return line.strip() == marker


def _find_marker_blocks(
    lines: list[str], begin_marker: str, end_marker: str
) -> list[tuple[int, int]]:
    """Line-index spans (begin line, end line, both inclusive) of every
    complete marker block, in file order.

    Raises `MalformedMarkerError` if the marker lines found don't pair up
    cleanly -- a BEGIN with no following END, an END with no preceding
    BEGIN, or two BEGINs with no END between them. The caller must treat
    that as damage to leave alone, not a shape to repair by guessing.
    """
    marker_positions = [
        (index, "begin")
        for index, line in enumerate(lines)
        if is_marker_line(line, begin_marker)
    ] + [
        (index, "end") for index, line in enumerate(lines) if is_marker_line(line, end_marker)
    ]
    marker_positions.sort()

    blocks: list[tuple[int, int]] = []
    pending_begin: int | None = None
    for index, kind in marker_positions:
        if kind == "begin":
            if pending_begin is not None:
                raise MalformedMarkerError
            pending_begin = index
        else:
            if pending_begin is None:
                raise MalformedMarkerError
            blocks.append((pending_begin, index))
            pending_begin = None
    if pending_begin is not None:
        raise MalformedMarkerError
    return blocks


def strip_marker_blocks(text: str, begin_marker: str, end_marker: str) -> str:
    """Remove every complete marker block (a BEGIN marker line, its
    contents, and the following END marker line -- matched as whole lines
    only, never a substring inside a longer line). Removes *all* of them,
    so a file carrying more than one block (an old one left at the end plus
    a newer one spliced at the top, say) converges to zero, not one-fewer.

    Raises `MalformedMarkerError`; see `_find_marker_blocks`.
    """
    lines = text.splitlines(keepends=True)
    blocks = _find_marker_blocks(lines, begin_marker, end_marker)
    if not blocks:
        return text
    removed = {index for start, end in blocks for index in range(start, end + 1)}
    return "".join(line for index, line in enumerate(lines) if index not in removed)


def replace_marker_blocks(
    text: str, begin_marker: str, end_marker: str, block: str
) -> str | None:
    """Swap `block` in for the marker block already in `text`, *where it
    already sits*, and return the result -- or `None` if `text` carries no
    block at all, leaving the caller to decide where a first one goes.

    Updating in place rather than strip-then-append is the difference
    between a file the user can arrange and one the tool keeps
    rearranging: a block a reader deliberately moved below their own
    preamble stays there across re-runs. If there is more than one block
    (an older writer's, plus this one's), the first keeps its position and
    the rest are removed -- converging on exactly one, the same property
    `install_hooks` needs from `strip_marker_blocks`.

    `block` must already end in a newline; everything outside the block's
    own line span is copied through byte-for-byte.

    Raises `MalformedMarkerError`; see `_find_marker_blocks`.
    """
    lines = text.splitlines(keepends=True)
    blocks = _find_marker_blocks(lines, begin_marker, end_marker)
    if not blocks:
        return None

    keep_start, keep_end = blocks[0]
    dropped = {index for start, end in blocks[1:] for index in range(start, end + 1)}
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in dropped:
            continue
        if index == keep_start:
            out.append(block)
            continue
        if keep_start < index <= keep_end:
            continue
        out.append(line)
    return "".join(out)
