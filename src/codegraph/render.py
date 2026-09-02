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
class Report:
    """A full report with summary, groups, and truncation flag."""

    summary: dict
    groups: list[Group]
    truncated: bool


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


def render_text(report: Report) -> str:
    """Render report as human-readable text.

    Format:
    - First line: summary items joined with ' · '
    - Then each group as a heading followed by indented rows

    Args:
        report: Report to render.

    Returns:
        Formatted text string.
    """
    lines = []

    # Summary line: join all values with ' · '
    summary_items = [str(v) for v in report.summary.values()]
    summary_line = " · ".join(summary_items)
    lines.append(summary_line)

    # Groups
    for group in report.groups:
        lines.append(group.title)
        for row in group.rows:
            lines.append(f"  {row.id}  {row.location}  {row.detail}")

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
    "budget",
    "render_json",
    "render_text",
]
