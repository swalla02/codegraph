"""Presentation layer: convert reports to text and JSON."""

import dataclasses
import json


@dataclasses.dataclass(frozen=True)
class Row:
    """A single result row."""

    id: str
    location: str
    detail: str
    score: float


@dataclasses.dataclass(frozen=True)
class Group:
    """A group of rows with a title."""

    title: str
    rows: list[Row]


@dataclasses.dataclass(frozen=True)
class Unknown:
    """One thing a report could not answer, and the one action that would
    settle it.

    The uncertainty envelope (#55). Every other field of a report is an
    answer; this is the list of holes in it, so an agent reading `--json`
    can act on "there is more here that I did not see" without parsing
    prose. `reason` is a key from `uncertainty.REASONS` and `action` is that
    key's one constant string, looked up rather than written here -- a
    reason described two different ways by two producers is exactly the
    aggregate-honesty problem this replaced.

    `detail` is the per-case data (how many references, which budget was
    hit); `action` never varies with it. `blocking` says whether this hole
    means the report may be missing something -- `--strict` refuses on a
    blocking entry and prints a settled one, since `external` and `builtin`
    are answers rather than gaps. See `uncertainty.SETTLED`.
    """

    reason: str
    detail: str
    action: str
    blocking: bool


@dataclasses.dataclass(frozen=True)
class Report:
    """A full report with summary, groups, truncation flag, and the
    uncertainty envelope.

    `unknowns` defaults to empty, which is the honest default for the
    reports that cannot be incomplete (`diff` compares two revisions by
    content hash: no walk, no budget, nothing hidden) and for a report that
    happens to have no hole this run. An empty envelope means "no hole this
    tool can name", never "this answer is complete" -- the difference is
    the one `orphans`' `caveat` field has always been about.
    """

    summary: dict
    groups: list[Group]
    truncated: bool
    unknowns: list[Unknown] = dataclasses.field(default_factory=list)


def budget(rows: list[Row], limit: int) -> tuple[list[Row], bool]:
    """Sort rows by score descending, keep top limit, return (kept, truncated).

    Args:
        rows: List of Row objects to budget.
        limit: Maximum number of rows to keep.

    Returns:
        Tuple of (kept rows sorted by score descending, was_truncated).
        was_truncated is True if len(rows) > limit.
    """
    was_truncated = len(rows) > limit
    sorted_rows = sorted(rows, key=lambda r: r.score, reverse=True)
    kept = sorted_rows[:limit]
    return kept, was_truncated


def _format_summary_value(value: object) -> str:
    """Render one summary value for the text format. A list (e.g.
    `effects_reachable: ["DB_WRITE", "PROCESS"]`) is joined with ', ' rather
    than shown as Python's `repr` (`['DB_WRITE', 'PROCESS']`) -- readable
    output, not a debugger dump. An empty list reads as 'none' rather than
    a blank field, so `key: ` never appears with nothing after the colon."""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    return str(value)


def render_text(report: Report) -> str:
    """Render report as human-readable text.

    Format:
    - First line: summary items as 'key: value', joined with ' · '
    - Then each group as a heading followed by indented rows

    Args:
        report: Report to render.

    Returns:
        Formatted text string.
    """
    lines = []

    # Summary line: 'key: value' pairs joined with ' · ', so every field is
    # labeled -- unlabeled positional values (`2 · 1 · 1 · 0 · ['DB_WRITE']`)
    # are meaningless without cross-referencing the producer's source.
    summary_items = [
        f"{key}: {_format_summary_value(value)}" for key, value in report.summary.items()
    ]
    summary_line = " · ".join(summary_items)
    lines.append(summary_line)

    # Groups
    for group in report.groups:
        lines.append(group.title)
        for row in group.rows:
            lines.append(f"  {row.id}  {row.location}  {row.detail}")

    # The envelope last, under a heading of its own, because it is not an
    # answer: a reader scanning the rows should reach it after the results
    # rather than have the results pushed down by a caveat. One line per
    # hole, and the action is the end of the line -- the part that says
    # what to do next is where a reader stops reading.
    if report.unknowns:
        lines.append("unknowns")
        for item in report.unknowns:
            lines.append(f"  {item.reason}  {item.detail}  {item.action}")

    return "\n".join(lines)


def render_json(report: Report) -> str:
    """Render report as JSON.

    Args:
        report: Report to render.

    Returns:
        JSON string with indent=2.
    """
    report_dict = dataclasses.asdict(report)
    return json.dumps(report_dict, indent=2)


__all__ = [
    "Group",
    "Report",
    "Row",
    "Unknown",
    "budget",
    "render_json",
    "render_text",
]
