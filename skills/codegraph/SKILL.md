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

Reach for codegraph:

- Before modifying any function or class.
- When asked "what breaks if I change this", "what does this affect", "is
  this safe to change", or "what did this branch change".

## Workflow

1. Resolve the symbol to a node id:

   ```
   codegraph resolve <name>
   ```

   `<name>` can be a trailing name (`open_workspace`), a qualname
   (`cli.py::open_workspace`), or a full node id.

   All three symbol-taking commands — `resolve`, `impact`, and `effects` —
   share the same exit-code convention for resolving that name/id to a
   symbol: `0` means a single unambiguous match (the node id is printed,
   e.g. `src/codegraph/cli.py::open_workspace`, and for `impact`/`effects`
   the report follows); `1` means nothing matched (a message on stderr);
   `2` means more than one symbol matched, and every match is printed
   (`resolve` to stdout, `impact`/`effects` to stderr) — pick the right one
   and re-run with the full id.

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

`codegraph islands` answers a question the other commands cannot: the
global shape of the graph. It splits the revision's `CALLS` edges, read as
**undirected**, into connected components — an *island* is a set of symbols
that share some call relationship, in either direction and however
indirect, with each other and with nothing outside it. It takes no symbol,
so the exit-code convention above does not apply to it: `0` for a report
(including on a repository with no Python in it), `1` only for a bad
`--rev`.

**An island is not a reachability result, and a one-symbol island is not
dead code.** It is computed from the call edges the resolver recorded, and
plenty of code is invoked by a mechanism that leaves no call site in the
source at all: dunders (`__delitem__` runs on every `del d[k]`),
decorators, framework dispatch, ABC overrides, packaging entry points. On
psf/requests, `AuthBase.__call__`, `BaseAdapter.__init__` and
`CaseInsensitiveDict.__delitem__` are each an island of one and none of
them is unused. Read an island as "this region shares no call edge with
that one", never as "nothing uses this".

`codegraph diff [<base>..<head>]` reports what a branch actually changed —
symbols added/removed/changed by content hash (never by line number) plus
any side effect that newly became reachable. With no argument it diffs
`merge-base(default branch, HEAD)` against the worktree, which is what you
want when asked "what did this branch change".

All of `resolve`, `impact`, `effects`, `islands`, and `diff` accept
`--path <dir>` to run against a different repository root, and
`impact`/`effects`/`islands`/`diff` accept `--json` for machine-readable
output instead of the default text.

## The anti-pattern this displaces

Do not grep for callers. Grep misses dynamically dispatched calls (anything
reached through a method resolution order, an attribute, or an alias) and
gives you no way to know when you are done — there is no signal that you
have found the last caller versus just the last one grep's pattern happened
to match. `codegraph impact` walks the actual call graph and tells you both
the full set and how confident it is in each edge.

## Reading the output

- Every report's default text output leads with a summary line of
  `key: value` pairs joined by ` · `, one per summary field, in the order
  the producer defines them — e.g. `impact`'s reads `symbols: 6 ·
  modules: 1 · entry_points: 6 · low_confidence_hidden: 0 ·
  effects_reachable: DB_WRITE, PROCESS`. `symbols`, `modules`,
  `entry_points`, and `low_confidence_hidden` are integer counts;
  `effects_reachable` is a **list of effect-kind strings**, not a count —
  in `--json` output it is a real JSON array; in text it is rendered as a
  comma-separated list (`DB_WRITE, PROCESS`), or `none` when the symbol
  reaches nothing, never Python's list repr. `low_confidence_hidden`
  counts `LOW`-confidence dependents that are held back from the
  `dependents` and `tests` groups by default — the resolver's least
  certain guesses are real information, but they should not read as
  confirmed impact. Pass `--all` to include them in the listed rows.
- `impact --limit N` caps the *total* rows kept across `dependents` and
  `tests` combined at `N` (default 40) — not `N` each — so the printed
  report never exceeds its documented budget.
- `diff`'s summary reads `new_effects: <kind list or none> ·
  added: N · removed: N · changed: N · base: <sha> · head: <rev>`.
  `new_effects` covers every symbol newly reachable at `head`, whether it
  is itself new (`added`) or pre-existing and edited (`changed`) — an
  effect reachable only through a brand-new function is not invisible
  just because the function has no `base` counterpart to diff against.
- Rows under a `tests` group are dependents whose path is under `tests/` or
  whose name starts with `test_`. They are bucketed separately from
  `dependents` on purpose: a change breaking a test is worth knowing, but
  tests should never crowd production callers out of the ranked list.
- Each `impact` row's `detail` column reads `hop N, <CONFIDENCE> confidence`
  — `HIGH`/`MEDIUM`/`LOW` reflects how certain the resolver is that the call
  really targets this symbol (e.g. a dynamic dispatch site is weaker
  evidence than a direct, unambiguous call).
- `islands`' summary reads `symbols: 807 · islands: 172 · largest: 628 ·
  singletons: 167 · basis: undirected CALLS edges` (the real figures for
  psf/requests). `symbols` excludes the synthetic `path::<module>` node
  each file gets: those carry connectivity — a module-scope call is
  sometimes the only thing tying a helper to the rest of the graph — but
  they are not symbols anyone wrote, so they are never members and never
  rows. `INHERITS` edges deliberately do not join an island, which keeps a
  symbol's island exactly equal to the set of nodes an unlimited-hop
  `impact` or `effects` walk could reach.
- An `islands` row summarizes a whole component rather than listing it:
  its `id` and `location` are the island's most-called member, and
  `detail` reads `size N across M files; also <two more members>`. Islands
  of exactly one are collected into a single `singletons` group — one
  group of 167 rows on psf/requests, not 167 groups of one — and each such
  row's `detail` reads `size 1, no resolved call in either direction`,
  which is a statement about the recorded edges and not about the symbol.
  `--limit N` (default 20) is a total budget across both groups, islands
  first, exactly as `impact` budgets dependents ahead of tests.
- Each `effects` row's `detail` reads `<KIND> <CONFIDENCE> via <chain>` —
  the chain is the call path from the queried symbol down to the concrete
  call site; `location` is that call site's `file:line`, clickable evidence
  rather than a claim you have to trust.
