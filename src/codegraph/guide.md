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
- When asked "when did this start doing X" or "which commit changed this" —
  `codegraph history <symbol>`.

## Workflow

1. Resolve the symbol to a node id:

   ```
   codegraph resolve <name>
   ```

   `<name>` can be a trailing name (`open_workspace`), a qualname
   (`cli.py::open_workspace`), or a full node id.

   All five symbol-taking commands — `resolve`, `impact`, `effects`,
   `path` and `unknowns` — share the same exit-code convention for
   resolving that name/id to a symbol: `0` means a single unambiguous match
   (the node id is printed, e.g. `src/codegraph/cli.py::open_workspace`,
   and for the report-producing commands the report follows); `1` means
   nothing matched (a message on stderr); `2` means more than one symbol
   matched, and every match is printed (`resolve` to stdout, the others to
   stderr) — pick the right one and re-run with the full id. `path` takes
   two symbols and applies the convention to each. The convention is about
   resolving a *name* and nothing else, so a `path` report saying the two
   symbols are not connected is still exit `0`: that is an answer, not a
   failure. `history` applies the same convention to its optional symbol,
   resolved at the head of the range (or, for a symbol the range deleted,
   at its start). The one code outside it is `3`, which only `--strict`
   produces (see below).

2. Ask what depends on it and what it can reach:

   ```
   codegraph impact <id>
   codegraph effects <id>
   ```

   `impact` walks callers and subclasses (and theirs in turn, up to
   `--hops`, default 3) and ranks them. `effects` reports every side-effect kind reachable
   downstream of the symbol, each with a witness chain down to the exact
   `file:line` that causes it.

3. When you already have **two** symbols and the question is how they
   relate, ask that directly instead of running `impact` on one and reading
   the rows for the other:

   ```
   codegraph path <A> <B>
   ```

   Reading `impact` for a second name fails exactly when the answer
   matters — a long chain, a chain past `--hops`, a row `--limit` crowded
   out — and `path` has none of those failure modes, because it looks for
   one chain rather than ranking a frontier.

4. Before you act on any of it, ask what the answer is missing:

   ```
   codegraph unknowns <id>
   ```

   The mirror of `impact`. It reports the references in that symbol's body
   that produced no edge (with the reason, the raw name, the line and the
   candidate count), how many of the body's references resolved, whether
   the symbol sits in an island nothing recognised explains, and whether an
   `impact` walk would stop on its hop budget rather than on the graph —
   which otherwise reads as a complete answer and is not one. Every
   reason comes with one fixed next action; you read a lookup table rather
   than deciding what to do.

5. Read only the top-ranked hits, not the whole list — rows are sorted by
   score (`impact`) or severity (`effects`), most important first, and long
   result sets are truncated with a `truncated` flag rather than dumped in
   full. A `path` report is the exception: its rows are a chain, in walk
   order, and reading them out of order means nothing.

`codegraph path <A> <B>` reports the shortest chain of `CALLS`, `INHERITS`,
`IMPLEMENTS` and `REFERENCES` edges connecting two symbols, in whichever
direction it runs. **Direction is the answer, not a detail**: if A reaches B
then editing B is the risky move, if B reaches A then editing A is, and if
each reaches the other they are in a cycle and both are. `forward` always
means the direction you wrote the arguments in; both directions are always
walked, and the one that was *not* found is reported as `none` so you can
see the tool looked rather than guess.

Each hop names its **kind** (four kinds now mean four different things by
"connected") and its **confidence**, and the path's confidence is its
weakest hop — five HIGH hops and four HIGH plus one LOW are different
answers, so the weak hop is marked as the one to go and read. Every hop also
carries the `file:line` that makes it, the same clickable evidence `effects`
gives.

**"Not connected" is three answers, never one**, and the difference decides
what you do next:

- `reason: no directed path within N hops`, with `show_path: --hops M` — a
  chain exists and your budget was too small. Re-run with the budget it
  names; do not read this as "unrelated".
- `reason: no directed path in either direction` — nothing connects them as
  a walk over the edges this run walked. Something may still relate them (a
  common caller, or a file's top level calling both). When `show_path:
  --all` appears beside it, a LOW chain exists that the default did not
  walk.
- `reason: different islands -- no walk in any direction, at any
  confidence, can connect them` — the strongest negative codegraph has, and
  the only one that licenses "these cannot affect each other". It is
  `islands`' own partition, so this report and that one can never disagree.

LOW hops are excluded by default and included with `--all`, matching
`impact` — and that one flag also governs the bare-name fan-out, which is
LOW by construction and is not in the stored graph at all, so under `--all`
a hop may run through `item.save()` and will name the bare name it used.
`--hops` defaults to 6, not `impact`'s 3: this walk follows a single chain
rather than a widening frontier, and on psf/requests a budget of 3 finds 51%
of the connected pairs while 6 finds 99%.

`codegraph unknowns <symbol>` is the one command whose whole subject is
what this tool cannot tell you. Every other report is honest about
uncertainty *in aggregate* — a `low_confidence_hidden` count, an
`unexplained` island tally — which is a property of the report and not of
the symbol you asked about. This one is per symbol, and every number in it
is a count of rows already stored:

```
symbol: django/db/models/base.py::Model.save · references: 14 · resolved: 3
  · unresolved: 11 · island: explained by entry, dunder, decorator, test, override,
  nested, import, NETWORK · mechanisms_not_found: none · basis: ...
ambiguous
  router.db_for_write  django/db/models/base.py:866  call reference, 15 candidates
builtin
  ValueError           django/db/models/base.py:868  call reference
unknowns
  ambiguous  5 references in this body  the bare name matches several definitions; ...
  builtin    6 references in this body  the target is a Python builtin; ...
  hop_limit  an `impact` walk of 3 hops does not exhaust this symbol's dependents  ...
```

`references` is reference *sites* in this body: `(file, line)`, so one
`self.render()` that writes an edge per override counts once, and two calls
on one line collapse into one. **A low ratio is not a defect.** `3 of 14`
here says the resolver identified eleven references exactly and knows none
of them is a symbol in this repository — six are builtins. Read the reason
breakdown, never the ratio alone. The reasons are fixed, and so is the next
action for each:

- `unknown` — the name matches nothing in the repository; expect dynamic
  dispatch, and read the body to see what it is. **This is the one to
  care about.**
- `ambiguous` — the bare name matches several definitions; `codegraph
  resolve <name>` lists them, and `impact --all` walks them.
- `external` — the target is outside the repository; no future index will
  resolve it. Nothing to do.
- `builtin` — the target is a Python builtin; no repository symbol is being
  called. Nothing to do.
- unexplained island — no implicit-invocation mechanism was recognised; a
  runtime trace is the only thing that can confirm this symbol is reached.
  `mechanisms_not_found` lists what was checked, so you can see what
  "recognised" covers rather than take the claim on trust. This is the one
  entry an imported trace closes outright: once a run has been watched
  entering the symbol, its island reads `explained by traced (N seen
  running)` and the entry is no longer raised. The `trace:` field beside it
  says whether there was a run to be seen in at all.
- hop limit — the walk stopped at its budget; re-run with a larger `--hops`.

**The uncertainty envelope, and `--strict`.** Every report that can be
incomplete carries an `unknowns` array beside its results — a real array in
`--json`, a trailing section in text — and `--strict` exits **3** when one
of those entries is blocking. That is the flag to reach for when you are
about to act on the answer without reading it: `codegraph impact X --strict
&& <edit>` refuses rather than letting you act on a walk that stopped early.
`3` is distinct from `1` (no such symbol) and `2` (ambiguous symbol) on
purpose — those call for a different next move.

`impact` raises it for a hop budget the walk did not exhaust; `effects` for
calls in the body it could not follow; `path` for a negative a flag would
turn into a path; `islands` for its own `unexplained` count; `history` for
a `lineage_ambiguous` move (below); `unknowns` for all of the above. `orphans` and `diff` have no `--strict`: `orphans`'
uncertainty is the standing `caveat` on every row, and `diff` compares two
revisions by content hash with no walk and no budget to cut short.

Two things deliberately do *not* count as incomplete. A LOW-confidence row
is an answer given with its tier attached, so `--strict` says nothing about
it; a report is incomplete when it does not show you something, not when
what it shows is uncertain. `truncated` is your own `--limit`, and already
has a field. Likewise `external` and `builtin` entries are printed and never
block — they are answers, not gaps, and each entry carries a `blocking`
field saying which it is.

**An empty `unknowns` means "no hole this tool can name", not "this answer
is complete."** A name assembled at runtime (`getattr`, a registry, a
template) leaves nothing for any of this to find.

`codegraph islands` answers a question the other commands cannot: the
global shape of the graph. It splits the revision's `CALLS`, `INHERITS`,
`IMPLEMENTS` and `REFERENCES` edges — every kind `impact` walks —
read as **undirected**, into connected components — an *island* is a set of symbols
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
psf/requests, `AuthBase.__call__` and `CaseInsensitiveDict.__delitem__` are
each an island of one and neither is unused. Read an island as "this region
shares no call edge with that one", never as "nothing uses this".

Every island is therefore labelled with what codegraph can say about why it
stands apart, and each row carries the label:

- `implicit: entry, dunder, decorator, test, override, nested, import` —
  one or more mechanisms found among the island's members by which something
  could reach it without a call site. Not a proof that the code runs; **it is
  counter-evidence to "nothing reaches this"**, which is the reading that
  gets working code deleted. `entry` means a module's top level calls into
  the island — an import-time statement, or the `main()` under an
  `if __name__ == "__main__"` guard.
- `boundary: NETWORK` — a path inside the island leaves the process. The
  handler is in another repository, so the island boundary *is* the service
  boundary: this is signal, not a defect. `ENV_READ` is printed alongside as
  a legend ("this region is lit up by a variable") but is not a boundary.
- `no implicit-invocation mechanism recognised` — the honest remainder, and
  still a statement about the tool: no resolved call, and no mechanism from
  a list codegraph knows to be incomplete. On psf/requests these 17 islands
  are mostly the library's public surface (`get_dict`, `dict_from_cookiejar`)
  — called by users of the package and by the stdlib, neither of which is in
  the tree. **Do not read this bucket as dead code.**

`codegraph orphans` answers the one question every other command needs the
answer to before you can ask it. `resolve`, `impact` and `effects` all take a
symbol, so they need you to already suspect the bug. `orphans` takes none: it
asks a bug's *shape* — **which functions have callers, all of which are
tests?** That is a helper that was written, tested, and never wired up, so
the feature silently does not exist while its test passes (#37). `islands`
cannot find it: the test's call is a real edge, so the function is not a
one-symbol island.

Reach for it when asked "is there anything wrong in here", "is this dead",
"what is not wired up", or after inheriting an unfamiliar repository. It is
also the check to run after adding a helper and its test — if your new
function appears, you forgot to call it.

Candidates are additionally private by name, undecorated, defined outside
the test tree, and **not mentioned by name anywhere in the source text**.
That last filter has no off switch: a static call graph cannot see a
function passed as a value, so without it a live `signal.signal` handler and
a `subprocess` `preexec_fn` head the list. `--include-public` and
`--include-decorated` relax the other two; the summary counts what each
filter removed (`functions` → `test_callers_only` → `candidates` →
`name_referenced` → `reported`), so you can see the funnel rather than trust
it.

**`orphans` is NOT a dead-code report, and neither its rows nor you should
say it is.** Its claim is about codegraph's knowledge: no caller outside the
test tree was recorded, and the name does not appear in the source text. A
name resolved at runtime — `getattr`, a registry, a `pyproject.toml` entry
point, a template — leaves nothing for either half of it to find, and the
`caveat` field in every summary says so. On the project it was built for, 2
of 5 rows were real defects and the other 3 were deliberate (two back-compat
shims and an import probe that is meant to be test-only). Read the rows,
then read the functions; never delete on the strength of a row. Like
`islands` it takes no symbol, so it exits `0` for a report — including an
empty one — and `1` only for a bad `--rev`.

`codegraph trace [FILE]` imports a recording of a run that actually
happened, and is the only thing that gets past the static ceiling: framework
dispatch, `getattr`, a `visit_*` lookup and a decorator's wrapper are calls
no analysis of the text can find, because the text does not name them.

Run it with no argument first — it says whether the repository already has a
trace and, if not, prints the one command that records one. **Do not record
one unprompted.** Recording means running the project's test suite, which
takes minutes and can touch a database, a network or the filesystem; that is
the user's call, not yours.

With a trace imported:

- an edge a run confirmed keeps *both* facts, and a row that says
  `hop 1, HIGH confidence, observed` is the strongest answer this tool has —
  `observed` on an `impact` row means every hop of the chain was watched
  running, not just the last one.
- calls the resolver never found appear as ordinary edges, so `impact`,
  `effects`, `path` and `islands` all improve without any flag. On
  pallets/flask, `impact` on the wrapper behind `@setupmethod` goes from
  `symbols: 0` — "nothing depends on this", about a function 273 call sites
  invoke — to 412 dependents across 46 modules.
- every `islands` and `orphans` summary carries `trace:`, which reads `none`
  when there is none. Read it before you trust an absence: "nothing reaches
  this" is a much weaker claim with no run behind it.
- editing a file retires the observations about that file, reported as
  `stale` rather than quietly used. A repository with no trace, or a wholly
  stale one, answers exactly as it always has.

`codegraph sessions [<revspec>]` lists the `Session: <uri>` trailers commits
carry: a pointer from a commit to the conversation, PR thread or notes that
produced it. Reach for it when the question is *why* code is shaped the way it
is. The pointer is opaque, so pass it on unchanged and never parse it, and a
pointer you cannot open is "session not available", not an error. Commits
without one are not listed, so empty output is an ordinary answer. **Do not run
`codegraph install-session-hook` unprompted.** It writes into the user's
commit messages, and that is their decision.

`codegraph visualize` is the one command whose output is not for you. It
writes a single self-contained HTML file -- a semantic-zoom treemap of the
directory tree with the graph drawn over it, packages then modules then
symbols then source, four edge kinds and three confidence tiers visually
distinct, islands as fills, and `unexplained` as an absence rather than a
number. Reach for it when a person asks to *see* the shape of a repository,
or asks for something they can send somebody; do not produce one to answer a
question you can answer with a report, and never read the HTML back yourself
-- every fact in it came from the commands above, and they say it in fewer
tokens.

The one part that is worth knowing about: it takes any other command's
`--json` output and lights up the symbols its rows name inside the full
view.

```
codegraph impact <id> --json > impact.json
codegraph visualize --highlight impact.json --out impact.html
```

That is how an answer is handed over with its surroundings intact -- "45
dependents" spread across two packages is a different fact from 45 in one
file. It takes no symbol of its own, so it shares `islands`' exit
convention: `0` for a file written, `1` for a `--rev` or a `--highlight`
file it cannot read.

`codegraph diff [<base>..<head>]` reports what a branch actually changed —
symbols added/removed/changed by content hash (never by line number) plus
any side effect that newly became reachable. With no argument it diffs
`merge-base(default branch, HEAD)` against the worktree, which is what you
want when asked "what did this branch change".

`codegraph history [<symbol>] [<base>..<head>]` walks the commits in a
range, oldest first along the first-parent line, comparing each with its
parent the way `diff` does. With a symbol, it lists only the commits that
changed its **behaviour** — body hash, confident callees, direct
dependents (`dependents +x`), or reachable effects — so a commit that adds a network call to a callee appears in the
caller's history although the caller's text never moved. Without one, each
commit is a group of the symbols added, removed, moved and changed and the
edges and effects gained and lost (`--limit` rows per commit); add
`--islands` to also see islands merging and splitting, counted exactly as
`islands` counts them (slower: it partitions every changed commit). The default
range is `merge-base(default branch, HEAD)..HEAD`: commits only, never the
worktree — use `diff` for uncommitted work.

A node id is `path::qualname`, so a move to another file or class is a
removal plus an addition. `history` pairs them by identical body hash and
says how sure it is: `moved from a.py::f to b.py::f (MEDIUM)` when the
pairing is unique, and it follows the symbol back under the old id. When
several removed definitions share the body, the row names them with `(LOW)`,
the walk stops there, and `unknowns` carries `lineage_ambiguous` — re-run
`history` on the candidate id you mean. A rename changes the body hash (the
name is part of the definition) and reads as a plain `added`. Only the range
you name is materialized, nothing is checked out, and nothing the walk
builds is kept.

All of `resolve`, `impact`, `effects`, `path`, `unknowns`, `islands`,
`orphans`, `diff`, `history` and `visualize` accept `--path <dir>` to run against a
different repository root, and all but `resolve` and `visualize` accept `--json`
for machine-readable output instead of the default text. `impact`, `effects`,
`path`, `unknowns`, `islands` and `history` additionally accept `--strict`.

## What this displaces, and by how much

Grep for a name and you get a superset: the callers, the definition, the
docs, the string literals, and no signal telling you which is which or when
you are done. Measured against a runtime trace of pallets/flask, over the 43
symbols a trace could pose a caller question about: a bare-name grep found
every observed direct caller, in 1328 matching lines to hand over 197 of
them; `codegraph impact` found 72% of the same set in 257 rows. Two hops out
— "what breaks if I change this" — grep costs 15383 lines against 1062.

The same measurement on django/django, over the 542 symbols its ORM suite
could pose: grep finds 94% of the observed direct callers in 118652 matching
lines, `impact` 63% in 5173 rows. The gap in reading is the part that scales
with the repository — 5x on flask, 23x on django, 160x at two hops — and the
gap in recall does not close.

So: `impact` for the ranked, deduplicated answer with a confidence per edge,
and grep when you want the superset and can afford to read it. Neither finds
a call whose two frames are separated by an out-of-repo frame, nor a method
django's ORM attaches to a class under a name assembled at runtime; only
`codegraph trace` does.

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
  reaches nothing, never Python's list repr.
- `LOW`-confidence dependents never enter `dependents` or `tests` — the
  resolver's least certain guesses are real information, but they should
  not read as confirmed impact, and they are left out of `symbols`,
  `modules` and `entry_points` for the same reason. They are not reduced
  to a number either: the strongest few are listed in their own
  `low_confidence` group, and `low_confidence_hidden` counts only the ones
  that did not fit. Pass `--all` to merge the whole set into the main
  groups instead — which the summary now says out loud: a nonzero
  `low_confidence_hidden` is printed with `show_hidden: --all` beside it,
  because a count of what you cannot see is half an answer (#37). The count
  stays an integer in `--json`; the hint is a separate field and appears
  only when something is actually hidden.
- Many of those `LOW` rows are not in the stored graph at all. A call like
  `item.save()` that names nothing importable, nothing module-local and
  nothing reachable through `self` — and whose receiver `item` was neither
  annotated with a class nor assigned one in its scope — matches every
  definition named `save` in the repository — up to 971 of them on django —
  and codegraph records the call once rather than storing that cross
  product, expanding it when a query asks. So `impact` can name callers
  that no edge in the database names, and `--limit` is the only bound on
  how many.
- `impact --limit N` caps the *total* rows kept across `dependents` and
  `tests` combined at `N` (default 40) — not `N` each — so the printed
  report never exceeds its documented budget. The `low_confidence` sample
  is budgeted separately and never eats into it.
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
  evidence than a direct, unambiguous call). A trailing `, observed` is a
  second, independent claim: a recorded run was watched taking every hop of
  that chain. Confidence is about reading the code, `observed` is about
  having seen it happen, and an edge can carry either or both.
- `islands`' summary reads `symbols: 807 · islands: 132 · largest: 665 ·
  singletons: 127 · implicit: 115 · network: 1 · unexplained: 17 · basis:
  undirected CALLS, INHERITS, IMPLEMENTS, REFERENCES edges` (the real figures
  for psf/requests). `symbols`
  excludes the synthetic `path::<module>` node each file gets: those carry
  connectivity — a module-scope call is sometimes the only thing tying a
  helper to the rest of the graph — but they are not symbols anyone wrote,
  so they are never members and never rows (a module node in a component is
  instead reported as the `entry` mechanism). The partition is computed from
  every edge kind `impact` walks, so a symbol's island always holds
  every node an unlimited-hop `impact` or `effects` walk could reach. `implicit` and `network` overlap and are not
  meant to sum — an island can be both — while `unexplained` is exactly the
  complement of their union.
- An `islands` row summarizes a whole component rather than listing it:
  its `id` and `location` are the island's most-called member, and
  `detail` reads `size N across M files; <classification>; also <two more
  members>`. Islands of exactly one are collected into a single
  `singletons` group — one group of 127 rows on psf/requests, not 127
  groups of one — and each such row's `detail` opens `size 1, no resolved
  call in either direction`, which is a statement about the recorded edges
  and not about the symbol (or `size 1, reached only from its module's top
  level`, when the one thing reaching it is an import-time statement or a
  `__main__` guard). `--limit N` (default 20) is a total budget
  across both groups, islands first, exactly as `impact` budgets dependents
  ahead of tests.
- `path`'s summary reads `from: <id> · to: <id> · direction: forward ·
  hops: 2 · confidence: HIGH · reverse: none · basis: shortest directed path
  over CALLS, INHERITS, IMPLEMENTS, REFERENCES edges, LOW excluded, within 6
  hops`. `direction` is `forward`, `reverse`, `both`, `same symbol`, or
  `none`; `hops` and `confidence` describe the path in the group printed
  first, and the *other* direction gets a field named after itself
  (`reverse: none`, or `reverse: 3 hops`) so one number is never quietly
  reporting two chains. When `direction` is `none` there are no rows, and
  `reason` — plus `show_path`, when a flag would turn the answer into a
  path — carries the whole report. A `path` group's rows are a **chain in
  walk order**, not a ranking: the first row is the symbol that does the
  reaching, each later row's `detail` reads `hop N, <KIND>, <CONFIDENCE>
  confidence, call site <file:line>`, and the hop that sets the path's
  confidence is marked `weakest hop`. A hop expanded from the bare-name
  fan-out says so — `hop 2, CALLS via the bare name 'send', LOW
  confidence` — because that hop is not an edge in the stored graph and
  must not read as one.
- `orphans`' summary reads `functions: 812 · test_callers_only: 280 ·
  candidates: 10 · name_referenced: 5 · reported: 5 · basis: ... ·
  caveat: ...` — the funnel, in order, so each filter's work is visible.
  `functions` counts the ones considered at all (outside the test tree,
  under a source root); `name_referenced` is how many candidates the
  source-text scan removed. Every row's `detail` names the tests that call
  it, which is where to start reading: the test says what the function was
  supposed to be for.
- `unknowns`' summary reads `symbol: <id> · references: 14 · resolved: 3 ·
  unresolved: 11 · island: <claim> · mechanisms_not_found: <list or none> ·
  basis: ...`. Its groups are named after the reasons (`unknown`,
  `ambiguous`, `external`, `builtin`), rows are in source order within each,
  and `--limit` is spent on the gaps first — a body with sixty `isinstance`
  calls and one `getattr` never spends its budget on the sixty. The next
  action for each reason is in the `unknowns` envelope, said once per
  reason, not repeated down a column.
- The envelope's entries are `{reason, detail, action, blocking}` in
  `--json`, and `reason  detail  action` under an `unknowns` heading in
  text. `detail` is the per-case data (how many references, which budget);
  `action` is one constant string per reason and never varies. `blocking`
  is what `--strict` refuses on.
- Each `effects` row's `detail` reads `<KIND> <CONFIDENCE> via <chain>` —
  the chain is the call path from the queried symbol down to the concrete
  call site; `location` is that call site's `file:line`, clickable evidence
  rather than a claim you have to trust.
