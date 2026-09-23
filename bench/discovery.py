"""Score caller discovery -- grep against `codegraph impact` -- on one target (#59).

    uv run python -m bench.discovery flask --work /tmp/codegraph-bench
    uv run python -m bench.discovery flask --json /tmp/discovery.json
    uv run python -m bench.discovery django --work ... --package-root django/

This is the deterministic half of #59. It compares the two ways of answering
"who calls this" against the same runtime trace `bench/run.py` already
produces, with no model and no judge anywhere in it: every number here can be
recomputed by anyone with the clone, the trace and this file.

It measures the TOOL, not the agent. Whether an agent given the tool writes a
better answer is a separate claim needing a separate experiment, and this one
is its floor: a tool that does not beat grep against observed ground truth
cannot be rescued by wrapping an agent around it.

## What it needs, and what it refuses

It needs a checkout of the target, a trace of that same tree from
`codegraph.tracer` (`bench/run.py` leaves one in its work directory), and an
index of it. It refuses to run against a revision that has a trace IMPORTED
(`codegraph trace`), because the gold answers are read out of a trace: a graph
that had already been told those edges would be scored against its own input.
That refusal is the whole integrity of the measurement, so it is a hard error
rather than a warning.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from bench.callers import (
    PATTERNS,
    Corpus,
    GrepResult,
    Outcome,
    Question,
    format_reports,
    grep_rounds,
    impact_callers,
    questions,
    report,
)
from bench.run import TARGETS, index
from bench.score import Trace
from codegraph.query.impact import impact_report
from codegraph.render import render_json
from codegraph.store import WORKTREE, Store
from codegraph.trace import NO_TRACE, summary

#: The two depths both sides are asked at. One hop is "who calls this", which
#: is the narrowest honest question a trace can pose. Two is the question
#: `AGENTS.md` is written about -- "what breaks if I change this" -- and it is
#: where the two workflows stop resembling each other: the second grep round
#: has to be aimed by hand at every name the first one turned up.
DEPTHS = (1, 2)

#: Row cap for the query. The CLI defaults to 40, which is a reading budget
#: for a human; a recall measurement wants the whole answer, and the report
#: says how many questions the default would have truncated.
LIMIT = 500
CLI_LIMIT = 40


class NotStaticOnly(SystemExit):
    """The indexed revision already knows the trace this is scored against."""


def read_sources(repo: Path) -> dict[str, str]:
    """Every tracked `.py` file, as grep would see them.

    Tracked rather than walked: a stray build artefact or a virtualenv inside
    the clone would give grep hits in files no reader would ever search.
    """
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    sources: dict[str, str] = {}
    for name in listed:
        try:
            sources[name] = (repo / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return sources


def require_static_only(store: Store, rev: str) -> None:
    if summary(store, rev) != NO_TRACE:
        raise NotStaticOnly(
            "this revision has an imported trace, and the gold answers come from a"
            " trace: scoring one against the other would grade codegraph on its own"
            " input. Index a clean copy of the target (no `codegraph trace`) and"
            " point --work at that."
        )


#: Both settings of the query, because grep is scored at both of its. `--all`
#: merges the LOW-confidence bare-name fan-out into the answer, which is the
#: query's own version of "match the name and let the reader sort it out".
#: Quoting only the flattering one of the two would be the same dishonesty as
#: quoting only `grep-call`.
CODEGRAPH_MODES = {"codegraph": False, "codegraph-all": True}


def ask_codegraph(
    store: Store, rev: str, question: Question, include_low: bool
) -> tuple[frozenset[str], int, bool]:
    """Run the query `AGENTS.md` tells an agent to run, for one symbol.

    Through `impact_report`, which is the function the CLI calls, so this is
    the command's answer and not a privileged view of the database.
    """
    built = impact_report(
        store, rev, question.symbol, max_hops=question.hops, limit=LIMIT, include_low=include_low
    )
    payload = json.loads(render_json(built))
    found = impact_callers(payload)
    return found, len(found), len(found) > CLI_LIMIT


def run(repo: Path, trace: Trace, package_root: str) -> tuple[dict[int, list], dict[int, list]]:
    """Ask every question at every depth, of every tool."""
    sources = Corpus(read_sources(repo))
    store = index(repo)
    by_depth: dict[int, list] = {}
    asked_by_depth: dict[int, list[Question]] = {}
    try:
        require_static_only(store, WORKTREE)
        for hops in DEPTHS:
            asked = questions(trace, package_root=package_root, hops=hops)
            if not asked:
                raise SystemExit(f"no question passed the filter for {repo}")
            asked_by_depth[hops] = asked
            outcomes: dict[str, list[Outcome]] = {
                tool: [] for tool in (*PATTERNS, *CODEGRAPH_MODES)
            }
            for number, question in enumerate(asked, start=1):
                # Progress, because django asks 542 questions twice over and
                # a silent process running that long is indistinguishable
                # from a hung one. stderr, so `--json` and the tables stay
                # clean.
                print(f"{hops} hop(s) {number}/{len(asked)} {question.symbol}", file=sys.stderr)
                for tool, pattern in PATTERNS.items():
                    result: GrepResult = grep_rounds(sources, question.name, pattern, hops)
                    outcomes[tool].append(Outcome(question, tool, result.callers, result.hits))
                for tool, include_low in CODEGRAPH_MODES.items():
                    found, cost, _ = ask_codegraph(store, WORKTREE, question, include_low)
                    outcomes[tool].append(Outcome(question, tool, found, cost))
            by_depth[hops] = [
                report(tool, found, trace.executed) for tool, found in outcomes.items()
            ]
    finally:
        store.close()
    return by_depth, asked_by_depth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.discovery")
    parser.add_argument("target", choices=sorted(TARGETS), help="Which repository to score")
    parser.add_argument(
        "--work",
        default="/tmp/codegraph-bench",
        help="Where `bench.run` left the clone and its trace (default: /tmp/codegraph-bench)",
    )
    parser.add_argument("--repo", default=None, help="Override the checkout to score")
    parser.add_argument("--trace", default=None, help="Override the trace JSON to score against")
    parser.add_argument(
        "--package-root",
        default="src/",
        help="Only ask about symbols under this prefix (default: src/)",
    )
    parser.add_argument("--json", default=None, help="Also write the outcomes as JSON here")
    args = parser.parse_args(argv)

    work = Path(args.work)
    repo = Path(args.repo) if args.repo else work / args.target
    trace_path = Path(args.trace) if args.trace else work / f"{args.target}-trace.json"
    if not repo.exists() or not trace_path.exists():
        raise SystemExit(
            f"need a checkout at {repo} and a trace at {trace_path}."
            f" Run `python -m bench.run {args.target}` first."
        )
    trace = Trace.load(json.loads(trace_path.read_text()))
    by_depth, asked_by_depth = run(repo, trace, args.package_root)

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    for hops, reports in by_depth.items():
        asked = asked_by_depth[hops]
        print()
        print(
            format_reports(
                f"{args.target} {head}: {len(asked)} symbols, dependents within {hops} hop(s)",
                reports,
            )
        )
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "target": args.target,
                    "revision": head,
                    "depths": {
                        str(hops): {
                            "questions": [
                                {"symbol": question.symbol, "gold": sorted(question.callers)}
                                for question in asked_by_depth[hops]
                            ],
                            "tools": {
                                item.tool: {
                                    "recall": item.recall,
                                    "hits": item.hits,
                                    "gold": item.gold,
                                    "perfect": item.perfect,
                                    "conditional_precision": item.conditional_precision,
                                    "cost": item.cost,
                                    "per_question": item.per_question,
                                    "found": {
                                        outcome.question.symbol: sorted(outcome.found)
                                        for outcome in item.outcomes
                                    },
                                }
                                for item in reports
                            },
                        }
                        for hops, reports in by_depth.items()
                    },
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
