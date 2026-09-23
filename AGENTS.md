# AGENTS.md

Conventions for coding agents working in this repository.

## Writing about the code

This repository explains its design in prose, and the prose navigates by
citation. Those citations are checked -- `tests/test_doc_references.py` reads
every comment, docstring and Markdown line and resolves the ones that name
something we own.

- Write a code reference inside backticks: `rank.fan_in`,
  `Ambiguity.caller_count`, `effects/propagate.py`. That is what marks it as
  a reference rather than a word, and it is all the scanner looks at.
- Name the symbol, never the line. `file:line` citations are rejected: they
  are correct when written and silently wrong after the next edit above them.
- If the check fires on an invented example -- a `Thing`, a `pkg/` -- the
  example has collided with one of our own names. Rename the example.

<!-- codegraph:begin -->
## codegraph

Before editing a Python function or class -- and whenever asked "what breaks if
I change this", "what does this affect", or "what did this branch change" --
query the call graph instead of grepping for callers:

- `codegraph impact <symbol>` -- ranked dependents; what a change could break.
- `codegraph effects <symbol>` -- side effects reachable downstream, each with
  a witness path to the exact `file:line` that causes it.
- `codegraph diff` -- what this branch changed, by content hash.

Grep returns a superset you then have to read: on flask five times the output
for the same callers, on django twenty-three (README, "Does querying beat
grepping?"). Run
`codegraph guide` for the full workflow, exit codes, and how to read the
output. If the command is missing, install it:
`uv tool install --python 3.12 git+https://github.com/swalla02/codegraph`
<!-- codegraph:end -->
