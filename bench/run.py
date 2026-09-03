"""Run the effectiveness benchmark end to end for one target repository (#35).

    uv run python -m bench.run requests
    uv run python -m bench.run flask --source-root /path/to/clones

Per target: copy the clone, build a virtualenv, install the package
**editable**, run its test suite under `bench/tracer.py`, index the same
working tree with codegraph, and score one against the other.

`-e` is not a preference. A normal install COPIES the source into
site-packages, so `code.co_filename` points there, every in-repo frame is
filtered out as external, and the trace comes back nearly empty -- with no
error. The clone is copied rather than used in place for the same class of
reason: an editable install writes into the target tree, and the benchmark
must leave the source clone untouched.

The revision indexed is WORKTREE, deliberately: the tracer executes the files
on disk, so the graph has to be built from the files on disk. Indexing HEAD
would score a graph of slightly different code whenever the clone is dirty.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from bench.score import Trace, format_report, read_static_graph, score
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.store import WORKTREE, Store

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    #: What to hand pytest. A subset, where the full suite needs network or
    #: services -- the benchmark measures the resolver, and a flaky suite
    #: measures the weather.
    tests: tuple[str, ...]
    #: Test-only dependencies the editable install does not pull in.
    extra_deps: tuple[str, ...] = ()
    note: str = ""


TARGETS: dict[str, Target] = {
    # The feasibility target from #35, kept at the same scope so the number
    # stays comparable to the 0.93 recorded there. requests' full suite wants
    # a live httpbin; test_utils.py + test_structures.py are pure-CPU.
    "requests": Target(
        name="requests",
        url="https://github.com/psf/requests",
        tests=("tests/test_utils.py", "tests/test_structures.py"),
    ),
    # The interesting one: decorators, framework dispatch, a context-local
    # proxy object. A static resolver should do measurably worse here, and
    # the point of the benchmark is to find out how much worse.
    "flask": Target(
        name="flask",
        url="https://github.com/pallets/flask",
        tests=("tests/",),
        extra_deps=("pytest-asyncio", "python-dotenv", "asgiref", "greenlet"),
        note="tests/ minus the ones needing extras; see --tests to narrow",
    ),
}


def _run(command: list[str], cwd: Path | None = None, check: bool = True) -> int:
    print(f"$ {' '.join(str(part) for part in command)}", flush=True)
    completed = subprocess.run(command, cwd=cwd, check=False)
    if check and completed.returncode != 0:
        raise SystemExit(f"failed ({completed.returncode}): {' '.join(map(str, command))}")
    return completed.returncode


def prepare(target: Target, work: Path, source_root: Path | None) -> Path:
    """Put a private, editable-installable copy of the target under `work`."""
    repo = work / target.name
    if repo.exists():
        print(f"reusing {repo}")
        return repo
    work.mkdir(parents=True, exist_ok=True)
    source = None if source_root is None else source_root / target.name
    if source is not None and source.exists():
        print(f"copying {source} -> {repo}")
        shutil.copytree(source, repo, symlinks=True)
    else:
        _run(["git", "clone", "-q", "--depth", "50", target.url, str(repo)])
    return repo


def make_venv(target: Target, repo: Path, work: Path) -> Path:
    """A venv OUTSIDE the target tree, so it is not mistaken for repo source."""
    venv = work / f"{target.name}-venv"
    python = venv / "bin" / "python"
    if not python.exists():
        _run(["uv", "venv", "--quiet", "--python", "3.12", str(venv)])
        _run(
            [
                "uv",
                "pip",
                "install",
                "--quiet",
                "--python",
                str(python),
                "-e",
                str(repo),
                "pytest",
                *target.extra_deps,
            ]
        )
    return python


def trace(python: Path, repo: Path, out: Path, tests: tuple[str, ...]) -> dict:
    started = time.perf_counter()
    _run(
        [
            str(python),
            str(HERE / "tracer.py"),
            "--root",
            str(repo),
            "--out",
            str(out),
            "--",
            "-q",
            "-p",
            "no:cacheprovider",
            *tests,
        ],
        cwd=repo,
        check=False,  # a suite with failures still produced a real trace
    )
    print(f"traced in {time.perf_counter() - started:.1f}s")
    return json.loads(out.read_text())


def index(repo: Path) -> Store:
    store = Store.open(repo)
    started = time.perf_counter()
    stats = Indexer(repo, store, GitTreeSource(repo)).reconcile(WORKTREE)
    print(
        f"indexed {stats.paths_total} paths in {time.perf_counter() - started:.1f}s"
        f" ({stats.edges} edges, {stats.ambiguous} ambiguous refs)"
    )
    return store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.run")
    parser.add_argument("target", choices=sorted(TARGETS), help="Which repository to score")
    parser.add_argument(
        "--work",
        default=str(Path(tempfile.gettempdir()) / "codegraph-bench"),
        help="Where copies, venvs and traces live (reused across runs)",
    )
    parser.add_argument(
        "--source-root",
        default=None,
        help="Directory holding existing clones named after the target; copied, never modified",
    )
    parser.add_argument(
        "--tests", nargs="+", default=None, help="Override the target's pytest arguments"
    )
    parser.add_argument(
        "--reuse-trace",
        action="store_true",
        help="Score the trace already on disk instead of re-running the suite",
    )
    parser.add_argument("--json", default=None, help="Also write the report as JSON here")
    args = parser.parse_args(argv)

    target = TARGETS[args.target]
    work = Path(args.work)
    source_root = None if args.source_root is None else Path(args.source_root)
    tests = tuple(args.tests) if args.tests else target.tests

    repo = prepare(target, work, source_root)
    trace_path = work / f"{target.name}-trace.json"
    if args.reuse_trace and trace_path.exists():
        print(f"reusing {trace_path}")
        traced = json.loads(trace_path.read_text())
    else:
        traced = trace(make_venv(target, repo, work), repo, trace_path, tests)

    store = index(repo)
    graph = read_static_graph(store, WORKTREE)
    report = score(Trace.load(traced), graph)
    store.close()

    print()
    print(format_report(f"{target.name}  ({' '.join(tests)})", report))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "target": target.name,
                    "tests": list(tests),
                    "traced_total": report.traced_total,
                    "anonymous_target": report.anonymous_target,
                    "body_execution": report.body_execution,
                    "target_unknown": report.target_unknown,
                    "judgeable": report.judgeable,
                    "found": report.found,
                    "recall": report.recall,
                    "found_high_medium": report.found_high_medium,
                    "recall_high_medium": report.recall_high_medium,
                    "testable_high": report.testable_high,
                    "observed_high": report.observed_high,
                    "conditional_precision": report.conditional_precision,
                    "misses": {label: edges for label, edges in report.miss_causes.items()},
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
