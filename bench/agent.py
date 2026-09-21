"""The A/B: one fixed agent, two tool sets, the same questions (#59).

    uv run python -m bench.agent flask --repo ... --trace ... --runs 3

Control gets the file tools -- read, glob, grep. Treatment gets the same three
AND the three codegraph queries, served by `bench/agent_tools.py` as MCP tools.
Everything else is held equal: one model, one turn cap, one per-run spend cap,
one prompt, one repository, one revision. The prompt does not mention codegraph
in either arm, because an arm that is told about a tool and an arm that is not
differ in their prompt, and the prompt is not what this is testing.

It spends money and needs a checkout, so it lives here rather than in `tests/`,
beside the benchmark that set that precedent (#53). `bench/discovery.py` is the
deterministic half of the same question and costs nothing; read that first.

## What is graded, and why it is only set overlap

The question asked has a set of symbol names as its answer, and the answer is
scored by overlap with the set the trace observed. No judge, no rubric, no
prose. That is a narrower question than "did the agent give a better answer",
and it is the one that can be graded the same way twice by two different
people. An LLM judge would have let the question be broader at the cost of
making the grade itself a measurement with an error bar.

## Variance

Two arms and one run each is a coin flip with extra steps. Every question is
asked `--runs` times per arm and the report prints the spread across runs, not
a single number. The runs are independent processes with no shared session, so
there is nothing for one to learn from another.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from bench.callers import Outcome, Question, format_reports, questions, report, spread
from bench.score import Trace

#: The issue number, used as the sampling seed so that "which questions were
#: asked" is a fact about the file rather than about the author's afternoon.
SEED = 59

#: Held equal across arms. The turn cap is a budget, not a limit anybody is
#: expected to reach; the spend cap is a runaway guard, and a run that hits
#: either is reported rather than quietly averaged in.
MODEL = "sonnet"
MAX_TURNS = 15
MAX_BUDGET_USD = 1.0

TOOLS = "Read,Glob,Grep"
CODEGRAPH_TOOLS = (
    "mcp__codegraph__codegraph_impact",
    "mcp__codegraph__codegraph_effects",
    "mcp__codegraph__codegraph_orphans",
)

ASKED = {
    1: "which functions or methods call `{symbol}` directly?",
    2: (
        "which functions or methods can reach `{symbol}` within two calls -- everything"
        " that calls it directly, plus everything that calls those callers?"
    ),
}

PROMPT = """In this repository, {asked}

Answer with one node id per line and nothing else, in the form \
`path/to/file.py::Qualname.method`: the path relative to the repository root, then \
`::`, then the qualified name as Python writes it -- for example \
`src/flask/app.py::Flask.dispatch_request` or `tests/test_basic.py::test_make_response`. \
Include callers in the package and in the tests. No prose, no headings, no counts."""

#: What a parsed answer looks like. Deliberately forgiving about what surrounds
#: it -- a bullet, a backtick, a stray sentence -- because the grade is about
#: which symbols the agent named, not about whether it followed a formatting
#: instruction to the letter.
NODE_ID = re.compile(r"([\w./-]+\.py)::([A-Za-z_][\w.<>]*)")


@dataclass(frozen=True)
class Run:
    """One agent process: what it was asked, and what came back."""

    arm: str
    symbol: str
    repetition: int
    answer: str
    named: list[str]
    turns: int
    cost_usd: float
    seconds: float
    #: tool name -> how many times the run called it. Recorded because the
    #: treatment arm not reaching for the tool it was given is a possible
    #: outcome of this experiment, and an outcome the recall column cannot
    #: tell apart from the tool being useless.
    tools: dict[str, int]
    error: str = ""


def parse_answer(text: str) -> frozenset[str]:
    return frozenset(f"{path}::{qualname}" for path, qualname in NODE_ID.findall(text))


def mcp_config(project: Path, target: Path, python: Path) -> str:
    """The treatment arm's extra tools, as a config the CLI takes inline."""
    return json.dumps(
        {
            "mcpServers": {
                "codegraph": {
                    "command": str(python),
                    "args": ["-m", "bench.agent_tools", "--path", str(target)],
                    "env": {"PYTHONPATH": str(project)},
                }
            }
        }
    )


def command(arm: str, prompt: str, target: Path, config: str) -> list[str]:
    """The CLI invocation for one run.

    Both arms are built from one list, and the treatment's two extra flags are
    the entire manipulation. Written this way so that a reader can check the
    claim "they differ only in tool availability" by reading one function.
    """
    allowed = TOOLS if arm == "control" else ",".join((TOOLS, *CODEGRAPH_TOOLS))
    argv = [
        "claude",
        "-p",
        prompt,
        "--model",
        MODEL,
        "--max-turns",
        str(MAX_TURNS),
        "--max-budget-usd",
        str(MAX_BUDGET_USD),
        # The streamed form, because the single JSON result says what the
        # agent answered and not what it did to get there.
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        TOOLS,
        "--allowedTools",
        allowed,
        "--permission-prompts",
        "none",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--strict-mcp-config",
    ]
    if arm == "treatment":
        argv += ["--mcp-config", config]
    return argv


def read_stream(stdout: str) -> tuple[dict, dict[str, int]]:
    """The result event, and a tally of the tools the run actually called."""
    result: dict = {}
    tools: dict[str, int] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event
        elif event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", ()):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name"))
                    tools[name] = tools.get(name, 0) + 1
    return result, tools


def ask(arm: str, question: Question, repetition: int, target: Path, config: str) -> Run:
    prompt = PROMPT.format(asked=ASKED[question.hops].format(symbol=question.symbol))
    started = time.perf_counter()
    completed = subprocess.run(
        command(arm, prompt, target, config),
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    seconds = time.perf_counter() - started
    payload, tools = read_stream(completed.stdout)
    if not payload:
        return Run(
            arm,
            question.symbol,
            repetition,
            "",
            [],
            0,
            0.0,
            seconds,
            tools,
            error=(completed.stderr or completed.stdout)[-400:] or "no output",
        )
    answer = payload.get("result") or ""
    return Run(
        arm=arm,
        symbol=question.symbol,
        repetition=repetition,
        answer=answer,
        named=sorted(parse_answer(answer)),
        turns=int(payload.get("num_turns") or 0),
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        seconds=seconds,
        tools=tools,
        error="" if not payload.get("is_error") else str(payload.get("subtype")),
    )


def outcomes(runs: list[Run], asked: dict[str, Question]) -> dict[str, list[Outcome]]:
    """Group answers by arm, costed in turns rather than in rows of output.

    A row of grep output and a turn of an agent are not the same unit, so
    this report's `cost` column is not comparable to `bench/discovery.py`'s.
    It is comparable between the two arms, which is the only comparison
    being made here.
    """
    grouped: dict[str, list[Outcome]] = {}
    for run in runs:
        if run.error:
            continue
        grouped.setdefault(run.arm, []).append(
            Outcome(asked[run.symbol], run.arm, frozenset(run.named), run.turns)
        )
    return grouped


def per_run_recall(runs: list[Run], asked: dict[str, Question]) -> dict[str, list[float]]:
    """Every individual run's recall, per arm -- the spread the table hides."""
    values: dict[str, list[float]] = {}
    for run in runs:
        if run.error:
            continue
        gold = asked[run.symbol].callers
        found = frozenset(run.named) & gold
        values.setdefault(run.arm, []).append(len(found) / len(gold) if gold else 1.0)
    return values


def paired(runs: list[Run], asked: dict[str, Question], metric: str) -> list[float]:
    """Treatment minus control, per question, averaged over the repeats.

    Paired on the question, because a question is the unit two arms share and
    the unit they vary across: `setupmethod` is hard for both arms and
    `Flask.run` easy for both, so an unpaired comparison spends most of its
    variance on which questions happened to be asked.
    """
    scores: dict[str, dict[str, list[float]]] = {}
    for run in runs:
        if run.error:
            continue
        gold = asked[run.symbol].callers
        found = frozenset(run.named) & gold
        value = {
            "recall": len(found) / len(gold) if gold else 1.0,
            "turns": float(run.turns),
            "cost": run.cost_usd,
        }[metric]
        scores.setdefault(run.symbol, {}).setdefault(run.arm, []).append(value)
    differences = []
    for symbol in sorted(scores):
        arms = scores[symbol]
        if "control" not in arms or "treatment" not in arms:
            continue
        differences.append(statistics.fmean(arms["treatment"]) - statistics.fmean(arms["control"]))
    return differences


def bootstrap(
    differences: list[float], seed: int = SEED, resamples: int = 20000
) -> tuple[float, float, float]:
    """The observed mean difference and a 95% percentile interval around it.

    A bootstrap rather than a t-test because twelve paired differences, most
    of them exactly zero, are not a distribution any table has a row for.
    Seeded, so the published interval is the interval anyone re-running this
    gets back.
    """
    if not differences:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean([rng.choice(differences) for _ in differences]) for _ in range(resamples)
    )
    return (
        statistics.fmean(differences),
        means[int(0.025 * resamples)],
        means[int(0.975 * resamples)],
    )


def read_runs(path: Path) -> list[Run]:
    """Re-read a finished experiment, so a number can be checked for free."""
    return [Run(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.agent")
    parser.add_argument("target", help="Name for the report (e.g. flask)")
    parser.add_argument("--repo", required=True, help="Checkout to run the agent in")
    parser.add_argument("--trace", required=True, help="Trace the gold answers come from")
    parser.add_argument("--package-root", default="src/", help="Symbol prefix to ask about")
    # One hop by default. Two is the question `AGENTS.md` is written about,
    # and a pilot showed why it cannot be the question here: the two-hop gold
    # set on flask runs to dozens of symbols, no agent enumerates dozens of
    # symbols inside any budget worth paying for, and both arms score near
    # zero for the same reason. That measures the answer's length, not the
    # tool. `bench/discovery.py` asks at both depths, where enumerating is
    # free.
    parser.add_argument("--hops", type=int, default=1, help="Depth of the question (default: 1)")
    parser.add_argument("--questions", type=int, default=10, help="How many symbols to ask about")
    parser.add_argument("--runs", type=int, default=3, help="Repetitions per question per arm")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent agent processes")
    parser.add_argument("--out", required=True, help="Where to write every run as JSONL")
    parser.add_argument(
        "--replay",
        action="store_true",
        help="Re-score the runs already in --out instead of spending tokens on new ones",
    )
    parser.add_argument(
        "--project",
        default=str(Path(__file__).resolve().parent.parent),
        help="This checkout, put on the MCP server's PYTHONPATH",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    project = Path(args.project).resolve()
    trace = Trace.load(json.loads(Path(args.trace).read_text()))
    pool = questions(trace, package_root=args.package_root, hops=args.hops)
    chosen = random.Random(SEED).sample(pool, min(args.questions, len(pool)))
    asked = {question.symbol: question for question in chosen}
    config = mcp_config(project, repo, project / ".venv" / "bin" / "python")

    out = Path(args.out)
    if args.replay:
        runs = read_runs(out)
        print(f"re-scoring {len(runs)} runs from {out}", flush=True)
    else:
        jobs = [
            (arm, question, repetition)
            for question in chosen
            for arm in ("control", "treatment")
            for repetition in range(args.runs)
        ]
        print(f"{len(jobs)} runs: {len(chosen)} questions x 2 arms x {args.runs}", flush=True)
        runs = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool_executor:
            futures = [
                pool_executor.submit(ask, arm, question, repetition, repo, config)
                for arm, question, repetition in jobs
            ]
            with out.open("w") as handle:
                for future in futures:
                    run = future.result()
                    runs.append(run)
                    handle.write(json.dumps(asdict(run)) + "\n")
                    handle.flush()
                    tools = " ".join(
                        f"{name.rpartition('__')[2]}={count}" for name, count in run.tools.items()
                    )
                    print(
                        f"  {run.arm:<9} {run.symbol:<52} named={len(run.named):>3}"
                        f" turns={run.turns:>2} ${run.cost_usd:.2f} {run.seconds:>3.0f}s"
                        f"  {tools} {run.error}",
                        flush=True,
                    )

    failed = [run for run in runs if run.error]
    grouped = outcomes(runs, asked)
    reports = [report(arm, found, trace.executed) for arm, found in sorted(grouped.items())]
    print()
    print(
        format_reports(
            f"{args.target}: {len(chosen)} symbols x {args.runs} runs,"
            f" dependents within {args.hops} hop(s)",
            reports,
        )
    )
    print()
    print("recall per RUN (min, q1, median, q3, max), which is the variance across repeats:")
    for arm, values in sorted(per_run_recall(runs, asked).items()):
        low, median, high = spread(sorted(values))
        print(
            f"  {arm:<10}{min(values):>6.2f}{low:>7.2f}{median:>7.2f}{high:>7.2f}"
            f"{max(values):>7.2f}   n={len(values)}"
        )
    print()
    print("treatment minus control, paired by question, with a 95% bootstrap interval:")
    for metric in ("recall", "turns", "cost"):
        observed, low, high = bootstrap(paired(runs, asked, metric))
        print(f"  {metric:<10}{observed:>+8.3f}   [{low:+.3f}, {high:+.3f}]")
    print()
    for arm in sorted(grouped):
        arm_runs = [run for run in runs if run.arm == arm and not run.error]
        tally: dict[str, int] = {}
        for run in arm_runs:
            for name, count in run.tools.items():
                tally[name] = tally.get(name, 0) + count
        print(
            f"  {arm:<10} ${sum(run.cost_usd for run in arm_runs):.2f}"
            f"  {sum(run.turns for run in arm_runs)} turns"
            f"  {sum(run.seconds for run in arm_runs) / max(len(arm_runs), 1):.0f}s per run"
        )
        for name, count in sorted(tally.items(), key=lambda item: -item[1]):
            print(f"    {name.rpartition('__')[2]:<20}{count}")
    if failed:
        print(f"\n{len(failed)} run(s) failed and are excluded: see {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
