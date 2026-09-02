# AGENTS.md

Conventions for coding agents working in this repository.

<!-- codegraph:begin -->
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
`uv tool install --python 3.12 git+https://github.com/swalla02/codegraph`
<!-- codegraph:end -->
