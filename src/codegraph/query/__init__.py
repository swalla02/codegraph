"""Query layer: convert graph state into `Report`s for CLI commands."""

from __future__ import annotations

from codegraph.query.effects import effects_report
from codegraph.query.impact import impact_report

__all__ = ["effects_report", "impact_report"]
