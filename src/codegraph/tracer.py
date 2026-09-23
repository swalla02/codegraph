"""Record the call edges a program ACTUALLY makes (#35), as evidence (#56).

**This file imports nothing from codegraph and never may.** It runs inside
the *target* program's own virtualenv -- which has codegraph installed only
by coincidence, if at all -- so it depends on the standard library and on
pytest, and is invoked by path rather than as a module:

    python .../codegraph/tracer.py --root <repo> --out trace.json -- <pytest args>
    python .../codegraph/tracer.py --root <repo> --out trace.json \
        --script tests/runtests.py -- --parallel=1 basic queries

`tests/test_trace.py` pins that property, because the failure it prevents is
silent in exactly the environment nobody tests in.

`--script` exists because not every suite is a pytest suite. django's is not:
`tests/runtests.py` builds its own settings module, creates test databases and
drives unittest itself, and there is no pytest invocation that reproduces it.
Without a way to trace it, the largest target in the benchmark would have no
runtime evidence at all -- and a trace is the only source of gold answers that
does not come from codegraph's own resolver, so "we could not run it" would
have meant "we cannot measure ourselves there". The script is executed in this
process, as `__main__`, so the monitoring above sees the whole run.

It ships inside the package rather than beside the benchmark because a trace
is no longer only a benchmark input: `codegraph trace <file>` imports what
this writes, and a tool whose evidence can only be produced from a checkout
of the tool is a tool that produces no evidence. `bench/run.py` runs this
same file.

Two things here were expensive to discover and must not be "simplified":

- **`sys.setprofile` does not survive pytest.** It silently stops firing
  after collection -- no error, no warning, just ~16 edges instead of ~170.
  `sys.monitoring` (3.12+) keeps firing for the whole session and is much
  cheaper. Do not swap it back.
- **`PY_START` hands you `(code, offset)`, not a frame.** There is no
  caller in the event, so the caller is found by walking
  `sys._getframe(1).f_back` -- frame 1 being the monitored function's own
  frame -- outward to the nearest ancestor whose code lives in the target
  repository. Skipping the external frames in between is what makes
  `test_x -> helper` come out as an edge even though pytest's own machinery
  sits between them.

Emitted node ids are codegraph's `path::qualname` form, with `path`
repo-relative and POSIX-separated -- which is what lets `trace.import_trace`
match them against a revision's `nodes` without knowing anything about where
the run happened. `code.co_qualname` aligns with what
`parse.py` records, `<locals>` segments for nested functions included.
"""

import argparse
import json
import os
import sys

#: sys.monitoring tool ids 0-5 are reservable; 2 is "profiler" by convention
#: and coverage.py takes it, so use a free one. Nothing else in a bench run
#: monitors, but an id collision raises rather than silently losing events.
TOOL_ID = 3


def run_suite(script, arguments):
    """Run the target's suite in THIS process and return its exit status.

    In this process because `sys.monitoring` is per-interpreter: a suite
    launched as a subprocess would be watched by nobody.

    With no `--script` that is `pytest.main`. With one, the script is run the
    way the shell would run it -- as `__main__`, with its own directory
    leading `sys.path` and its arguments in `sys.argv` -- because a runner
    like django's reads all three. `SystemExit` is caught rather than allowed
    to propagate: a suite with failing tests still produced a real trace, and
    the trace is written by the caller's `finally` either way.
    """
    if script is None:
        import pytest

        return pytest.main(arguments)
    import runpy

    path = os.path.abspath(script)
    sys.path.insert(0, os.path.dirname(path))
    sys.argv = [path, *arguments]
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as stopped:
        return 0 if stopped.code is None else stopped.code
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codegraph/tracer.py")
    parser.add_argument("--root", required=True, help="Target repository root")
    parser.add_argument("--out", required=True, help="Where to write the trace JSON")
    parser.add_argument(
        "--script",
        default=None,
        help="Run this script as __main__ instead of pytest (django's tests/runtests.py)",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root) + os.sep
    edges: set[tuple[str, str]] = set()
    #: Edges where at least one out-of-repo Python frame sat between the
    #: callee and the nearest in-repo ancestor: `test_x` -> werkzeug's
    #: `Client.get` -> `FlaskClient.open`. The relationship is real, but there
    #: is no call site anywhere in the repository's text that names it, so no
    #: static analyser could have it. The scorer reports these apart from
    #: resolution failures instead of blaming the resolver for them.
    indirect: set[tuple[str, str]] = set()
    #: The same pairs seen with no external frame in between. An edge in both
    #: sets is a direct call, whatever else was also observed.
    direct: set[tuple[str, str]] = set()
    #: Every in-repo function that ran at least once, whether or not any
    #: in-repo caller was found for it. A test function invoked by pytest
    #: has no in-repo caller and so appears in no edge, but it did execute
    #: -- and conditional precision is defined over what executed, so the
    #: scorer needs this set rather than the endpoints of `edges`.
    executed: set[str] = set()
    #: code object -> node id (or None for out-of-repo). The callback runs on
    #: every single call in the suite; without this, each one pays a relpath.
    names: dict[object, str | None] = {}

    def node_id(code) -> str | None:
        if code in names:
            return names[code]
        path = code.co_filename
        # Three exclusions, all of them needed:
        #  - outside the repo: not this repository's code at all;
        #  - site-packages: a venv placed inside the repo (run.py keeps it
        #    outside, but a stray one must not become in-repo edges);
        #  - not a .py file: Jinja compiles a template to a code object whose
        #    co_filename is the template. Real execution, but codegraph
        #    indexes Python, so `templates/mail.txt::root` can never be a
        #    node. On flask's suite that is 23 edges.
        if not path.startswith(root) or "site-packages" in path or not path.endswith(".py"):
            names[code] = None
            return None
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        name = f"{relative}::{code.co_qualname}"
        names[code] = name
        return name

    def on_start(code, offset):
        me = node_id(code)
        if me is None:
            return
        executed.add(me)
        back = sys._getframe(1).f_back
        skipped = False
        while back is not None:  # nearest in-repo ancestor is the caller
            caller = node_id(back.f_code)
            if caller is not None:
                if caller != me:  # recursion is not an edge codegraph models
                    edges.add((caller, me))
                    (indirect if skipped else direct).add((caller, me))
                return
            skipped = True
            back = back.f_back

    monitoring = sys.monitoring
    monitoring.use_tool_id(TOOL_ID, "codegraph-bench")
    monitoring.register_callback(TOOL_ID, monitoring.events.PY_START, on_start)
    monitoring.set_events(TOOL_ID, monitoring.events.PY_START)
    try:
        suite_status = run_suite(args.script, [a for a in args.pytest_args if a != "--"])
    finally:
        monitoring.set_events(TOOL_ID, 0)
        monitoring.free_tool_id(TOOL_ID)
        with open(args.out, "w") as handle:
            json.dump(
                {
                    "root": os.path.abspath(args.root),
                    "edges": sorted(edges),
                    "indirect": sorted(indirect - direct),
                    "executed": sorted(executed),
                },
                handle,
            )
    print(
        f"\ntraced {len(edges)} distinct call edges over {len(executed)} executed functions",
        file=sys.stderr,
    )
    # The suite's own exit status is reported, not propagated: a target whose
    # suite has some failing tests still produced a real trace, and the
    # runner decides whether that is acceptable.
    print(f"suite exit status {suite_status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
