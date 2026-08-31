---
name: codegraph
description: Use when about to modify a Python function or class, or when asked "what breaks if I change this", "what does this affect", "is this safe to change", or "what did this branch change" — answers with a ranked, git-native call graph instead of a grep.
---

# codegraph

`codegraph` is a sidecar index over a Python codebase (content-addressed by
git blob SHA, so it never touches how the code is written). It answers two
questions grep structurally cannot: who transitively depends on a symbol,
ranked, and which of those paths reach a side effect (database write,
network call, filesystem write, global mutation, ...).

## Trigger

Reach for this skill:

- Before modifying any function or class.
- When asked "what breaks if I change this", "what does this affect", "is
  this safe to change", or "what did this branch change".

## Workflow

1. Resolve the symbol to a node id:

   ```
   codegraph resolve <name>
   ```

   `<name>` can be a trailing name (`open_workspace`), a qualname
   (`cli.py::open_workspace`), or a full node id. Exit code tells you what
   happened: `0` and one line of output means a single unambiguous match
   (the node id, e.g. `src/codegraph/cli.py::open_workspace`); `2` means
   more than one symbol matched and every match is printed — pick the right
   one and re-run with the full id; `1` means nothing matched.

2. Ask what depends on it and what it can reach:

   ```
   codegraph impact <id>
   codegraph effects <id>
   ```

   `impact` walks callers (and callers of callers, up to `--hops`, default
   3) and ranks them. `effects` reports every side-effect kind reachable
   downstream of the symbol, each with a witness chain down to the exact
   `file:line` that causes it.

3. Read only the top-ranked hits, not the whole list — rows are sorted by
   score (`impact`) or severity (`effects`), most important first, and long
   result sets are truncated with a `truncated` flag rather than dumped in
   full.

`codegraph diff [<base>..<head>]` reports what a branch actually changed —
symbols added/removed/changed by content hash (never by line number) plus
any side effect that newly became reachable. With no argument it diffs
`merge-base(default branch, HEAD)` against the worktree, which is what you
want when asked "what did this branch change".

All of `resolve`, `impact`, `effects`, and `diff` accept `--path <dir>` to
run against a different repository root, and `impact`/`effects`/`diff` accept
`--json` for machine-readable output instead of the default text.

## The anti-pattern this displaces

Do not grep for callers. Grep misses dynamically dispatched calls (anything
reached through a method resolution order, an attribute, or an alias) and
gives you no way to know when you are done — there is no signal that you
have found the last caller versus just the last one grep's pattern happened
to match. `codegraph impact` walks the actual call graph and tells you both
the full set and how confident it is in each edge.

## Reading the output

- `impact`'s summary line (`symbols · modules · entry_points ·
  low_confidence_hidden · effects_reachable`) counts `LOW`-confidence
  dependents in `low_confidence_hidden` but hides them from the `dependents`
  and `tests` groups by default — the resolver's least certain guesses are
  real information, but they should not read as confirmed impact. Pass
  `--all` to include them in the listed rows.
- Rows under a `tests` group are dependents whose path is under `tests/` or
  whose name starts with `test_`. They are bucketed separately from
  `dependents` on purpose: a change breaking a test is worth knowing, but
  tests should never crowd production callers out of the ranked list.
- Each `impact` row's `detail` column reads `hop N, <CONFIDENCE> confidence`
  — `HIGH`/`MEDIUM`/`LOW` reflects how certain the resolver is that the call
  really targets this symbol (e.g. a dynamic dispatch site is weaker
  evidence than a direct, unambiguous call).
- Each `effects` row's `detail` reads `<KIND> <CONFIDENCE> via <chain>` —
  the chain is the call path from the queried symbol down to the concrete
  call site; `location` is that call site's `file:line`, clickable evidence
  rather than a claim you have to trust.
