"""Run the effectiveness benchmark end to end for one target repository (#35).

    uv run python -m bench.run requests
    uv run python -m bench.run flask --source-root /path/to/clones
    uv run python -m bench.run flask --check-floors      # exits 1 on a regression

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

`--check-floors` is where codegraph's effectiveness floors live (#39). They
are here rather than in `pytest` because a floor needs what this script
needs: a clone of somebody else's repository, a virtualenv, and a few minutes
of their test suite. `tests/` stays offline and fast, and what the default
suite does assert about resolution is a regression guard over a fixture
(`tests/test_resolution_rules.py`), which is a different claim and is named
like one. Each target's floor, and the run it was read off, is recorded in
`TARGETS` below.
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

from bench.score import (
    Floor,
    Report,
    Trace,
    check_floor,
    format_report,
    read_static_graph,
    score,
)
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
    #: What `--check-floors` enforces for this target, or None for a target
    #: nobody has measured yet. A floor belongs to the `tests` above and to
    #: no other scope: recall is a property of the suite you trace as much as
    #: of the repository, and requests reads 0.93 on `test_utils.py` alone
    #: against 0.79 once `test_structures.py` -- a file of dunder tests --
    #: joins it. `--check-floors` refuses a `--tests` override for that
    #: reason.
    floor: Floor | None = None


TARGETS: dict[str, Target] = {
    # The feasibility target from #35, kept at the same scope so the number
    # stays comparable to the 0.93 recorded there. requests' full suite wants
    # a live httpbin; test_utils.py + test_structures.py are pure-CPU.
    "requests": Target(
        name="requests",
        url="https://github.com/psf/requests",
        tests=("tests/test_utils.py", "tests/test_structures.py"),
        floor=Floor(
            recall=0.76,
            recall_high_medium=0.74,
            conditional_precision=0.95,
            measured=(
                "2026-09-19, codegraph ce69dfc, psf/requests dae7ef6: recall 0.79"
                " (91/115 judgeable), at HIGH/MEDIUM 0.77, conditional precision"
                " 0.99 (85/86). All 24 misses are dunders invoked by syntax or"
                " calls reached through an out-of-repo frame."
            ),
        ),
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
        floor=Floor(
            recall=0.27,
            recall_high_medium=0.24,
            conditional_precision=0.70,
            measured=(
                "2026-09-19, codegraph ce69dfc, pallets/flask d73fa1c: recall 0.29"
                " (775/2683 judgeable), at HIGH/MEDIUM 0.26, conditional precision"
                " 0.74 (515/699). 1637 of the 1908 misses are a view defined inside"
                " a test, a decorated target, or a pair only an out-of-repo frame"
                " connects -- dispatch a call-site graph does not model."
            ),
        ),
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
    _warn_if_dirty(repo)
    return repo


def _warn_if_dirty(repo: Path) -> None:
    """Say so, loudly, when the tree about to be measured is not its HEAD.

    The trace executes the files on disk and `index` reads the same worktree,
    so the two always agree -- but a number quoted from a tree with somebody's
    leftover edit in it is not reproducible, and this went unnoticed once
    (a stray `_codegraph_probe` in a flask clone from earlier feasibility
    work). Not fatal: `--source-root` may legitimately point at a repository
    with work in progress.
    """
    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if dirty:
        print(f"WARNING: {repo} is not clean; the score describes THIS tree, not HEAD:")
        print(dirty)


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


def index(repo: Path, rebuild: bool = False) -> Store:
    """Reconcile the target's working tree, optionally from cold.

    `rebuild` is the same two deletes `codegraph index --rebuild` performs
    (cli.py, #30): the parse cache, and this revision's `revisions` row --
    the second is what stops the reconcile that follows from taking the
    unchanged-tree fast path and reporting a build it did not do.

    Since #44 a resolver change invalidates the materialized graph on its
    own, so this is no longer needed to measure two sides of one; what it is
    still for is a cold number. The reported seconds are otherwise a warm
    reconcile of a tree the previous run already indexed, which is a
    different measurement wearing the same label.
    """
    store = Store.open(repo)
    if rebuild:
        store.connection.execute("DELETE FROM blobs")
        store.connection.execute("DELETE FROM revisions WHERE rev=?", (WORKTREE,))
        store.connection.commit()
    started = time.perf_counter()
    stats = Indexer(repo, store, GitTreeSource(repo)).reconcile(WORKTREE)
    print(
        f"indexed {stats.paths_total} paths in {time.perf_counter() - started:.1f}s"
        f" ({stats.edges} edges, {stats.ambiguous} ambiguous refs,"
        f" {stats.blobs_parsed} blobs parsed)"
    )
    return store


def head_sha(repo: Path) -> str:
    """The target's commit, printed beside every score.

    The clone tracks the target's default branch, so two runs a month apart
    measure two different repositories. When a floor fails, the first
    question is whether codegraph changed or the target did, and the answer
    has to be in the output of both runs or it is not available at all.
    """
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() or "(unknown rev)"


def _refuse_unenforceable_floor(target: Target, args: argparse.Namespace) -> None:
    """Fail before spending four minutes producing a number nothing floors.

    A floor is tied to one suite. Narrowing the scope changes the score
    without changing the resolver at all -- requests reads 0.93 on
    `test_utils.py` and 0.79 once `test_structures.py` joins it -- so
    checking a floor against a different scope would compare two unrelated
    measurements and call the difference a regression.
    """
    if target.floor is None:
        raise SystemExit(
            f"{target.name} has no recorded floor. Run the benchmark, then write one"
            " a little below what it prints (bench/run.py, TARGETS)."
        )
    if args.tests:
        raise SystemExit(
            f"--check-floors and --tests are mutually exclusive: {target.name}'s floor was"
            f" measured on {' '.join(target.tests)} and means nothing on another scope."
        )


def report_floor(target: Target, report: Report) -> int:
    """Print every floored metric against its floor; 1 if any is below it."""
    assert target.floor is not None  # _refuse_unenforceable_floor ran first
    checks = check_floor(report, target.floor)
    print()
    print(f"floors for {target.name} ({' '.join(target.tests)}):")
    for check in checks:
        print(f"  {check}")
    print(f"  floor measured at: {target.floor.measured}")
    below = [check for check in checks if not check.ok]
    if not below:
        return 0
    print()
    print(
        f"FAILED: {len(below)} metric(s) below the floor. Either the resolver lost"
        " something, or the target repository moved -- compare the commit above"
        " with the one the floor was measured at before touching the floor."
    )
    return 1


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
        "--rebuild",
        action="store_true",
        help="Discard the parse cache and the revision's graph first, for a cold index time",
    )
    parser.add_argument(
        "--reuse-trace",
        action="store_true",
        help="Score the trace already on disk instead of re-running the suite",
    )
    parser.add_argument(
        "--check-floors",
        action="store_true",
        help="Exit non-zero if this target scores below its recorded floor (#39)",
    )
    parser.add_argument("--json", default=None, help="Also write the report as JSON here")
    args = parser.parse_args(argv)

    target = TARGETS[args.target]
    work = Path(args.work)
    source_root = None if args.source_root is None else Path(args.source_root)
    tests = tuple(args.tests) if args.tests else target.tests

    if args.check_floors:
        _refuse_unenforceable_floor(target, args)

    repo = prepare(target, work, source_root)
    trace_path = work / f"{target.name}-trace.json"
    if args.reuse_trace and trace_path.exists():
        print(f"reusing {trace_path}")
        traced = json.loads(trace_path.read_text())
    else:
        traced = trace(make_venv(target, repo, work), repo, trace_path, tests)

    store = index(repo, rebuild=args.rebuild)
    graph = read_static_graph(store, WORKTREE)
    report = score(Trace.load(traced), graph)
    store.close()

    print()
    print(format_report(f"{target.name} {head_sha(repo)}  ({' '.join(tests)})", report))
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
    if args.check_floors:
        return report_floor(target, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
