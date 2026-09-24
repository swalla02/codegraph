# codegraph

A standalone navigation layer over a Python codebase — a sidecar index, in the
spirit of `.git/`, that never changes how you write code and never asks the
code to import it.

It answers two questions grep answers badly:

- **`codegraph impact <symbol>`** — what transitively depends on this, ranked,
  with the blast radius summarized rather than dumped.
- **`codegraph effects <symbol>`** — does anything downstream write the
  database, hit the network, touch the filesystem, or mutate global state —
  each claim backed by a witness path to the exact `file:line` that causes it.

Composed, they give the answer you actually want before a change: not "47 things
call this," which is anxiety rather than information, but *"47 things call this,
and 3 of the paths end in a database write."*

"Badly" is measured rather than asserted, and the measurement is less
flattering than the sentence above implies: on pallets/flask, a bare-name grep
finds **every** caller a run observed, and buries them in five times as much
output; on django it finds 94% of them and buries those in twenty-three times
as much. The graph's measured advantage over grep is density and confidence,
not recall, and it widens with the size of the repository — see
[Does querying beat grepping?](#does-querying-beat-grepping-59).

It follows git. The parse cache is content-addressed by git blob SHA, so a
file's content is analysed once for the life of the repository and shared
across every branch: creating a branch changes no blobs, so it costs zero
parsing, and switching to a branch whose blobs have already been seen also
costs zero parsing — this half of the cost guarantee is measured, not just
claimed (see "The cost guarantee, honestly" below).

That also makes `codegraph diff` possible — the semantic delta of a branch:
which symbols and edges changed, and which side effects newly became
reachable. Walked over a range of commits, the same comparison becomes
`codegraph history`: `git log -L` at the level of symbols and edges.

Built agent-first: a CLI, driven through `AGENTS.md` (`codegraph init` writes
the section, for any of the 25+ agents that read it) and through `SKILL.md`
for Claude Code (`/plugin marketplace add swalla02/codegraph`), rather than an
MCP server (see "Why no MCP server" below), with a schema a visual navigator
can project from later.

**Status:** implemented and in use. See
[the design spec](docs/superpowers/specs/2026-08-29-codegraph-design.md) for
the full rationale behind every decision below.

## Quickstart

Two commands, from a fresh machine to an agent that knows the tool exists:

```sh
uv tool install --python 3.12 git+https://github.com/swalla02/codegraph
cd /path/to/your/repo && codegraph init
```

**`--python 3.12` is not optional.** This tool needs Python 3.12+, and on a machine
whose default interpreter is 3.11 — still the distro default nearly everywhere —
the install fails outright rather than degrading. Passing the version explicitly
makes `uv` fetch a suitable interpreter instead of failing on yours.

`uv tool install` puts the `codegraph` console script (`pyproject.toml`'s
`[project.scripts]` entry point) on `PATH` in its own isolated environment. Not
yet published to PyPI, hence the git URL. From a local checkout, `uv pip install
-e .` works the same way.

### `codegraph init`

`codegraph init` makes a repository's coding agents aware of codegraph, and is
safe to re-run — it is idempotent, additive, and never overwrites content it
did not write. It:

- writes a short codegraph-owned section into `AGENTS.md` (creating the file if
  needed), delimited by `<!-- codegraph:begin -->` / `<!-- codegraph:end -->`
  so a later run updates that block in place instead of duplicating it. The
  section is deliberately ~15 lines: it loads into *every* session of every
  agent that reads `AGENTS.md`, so it names the commands and the trigger and
  defers the rest to `codegraph guide`;
- adds the documented `@AGENTS.md` import to `CLAUDE.md` **if that file already
  exists**. It never creates one — for Claude Code the plugin below is strictly
  better, because a skill loads on demand rather than into every session;
- writes a fully commented-out `codegraph.toml` stub if there isn't one, and
  never touches one there is.

It does not install git hooks. That stays behind `install-hooks`, which you ask
for by name — and the one hook that writes into commits stays behind
`install-session-hook`, which `install-hooks` does not install either.

`AGENTS.md` is the cross-agent convention — Codex, Cursor, Gemini CLI, Copilot's
coding agent, Aider, goose, opencode, Zed, Windsurf, Amp, Warp, Junie, Jules,
Devin, RooCode, Kilo Code, Factory and others read it. Claude Code reads
`CLAUDE.md` instead, which is what the `@AGENTS.md` import bridges
([docs](https://code.claude.com/docs/en/memory)).

### As a Claude Code plugin

```
/plugin marketplace add swalla02/codegraph
```

The repository doubles as its own marketplace (`.claude-plugin/
marketplace.json` and `plugin.json`), so this one command both registers and
installs it. It adds `skills/codegraph/SKILL.md`, which teaches an agent when
and how to invoke the CLI — no server process, no MCP tool definitions
occupying context on every turn. You still need the `codegraph` console
script on `PATH` (install it as above); the plugin does not bundle a Python
runtime.

## Commands

Every command reconciles the requested revision before answering (see "Lazy
refresh on query" below), so results are never stale, only occasionally
slower on a cold cache.

| Command | What it does |
|---|---|
| `codegraph status [--rev REV]` | Reconcile a revision and print summary counts: paths, blobs parsed/cached, edges, unresolved refs, parse errors. |
| `codegraph index [--rev REV] [--rebuild] [--quiet]` | Reconcile a revision into the graph explicitly, paying the cold-build cost up front. `--rebuild` discards the Layer 1 parse cache *and* the revision's materialized graph first, so it really does rebuild; `--quiet` is what the warming hooks invoke in the background. |
| `codegraph resolve <name>` | Fuzzy-match a name (trailing name, qualname, or full node id) to node ids. |
| `codegraph effects <symbol> [--json] [--strict]` | Report every side-effect kind reachable from a symbol, each with a witness chain down to the causing `file:line`. |
| `codegraph impact <symbol> [--hops N] [--limit N] [--all] [--json] [--strict]` | Report the ranked dependents of a symbol — everything a change to it could break. |
| `codegraph unknowns <symbol> [--hops N] [--limit N] [--json] [--strict]` | The mirror of `impact`: what codegraph *cannot* tell you about this symbol. Every reference in its body that produced no edge, with the reason, the raw name, the line and the candidate count; how many of the body's references resolved; whether it sits in an island no recognised mechanism explains, and which mechanisms were checked; and whether an `impact` walk would stop on its hop budget rather than on the graph. Every number is a count of rows already stored, and every reason carries one fixed next action (see below). |
| `codegraph path <A> <B> [--hops N] [--all] [--json] [--strict]` | Report how two symbols are connected: the shortest chain of edges between them, in whichever direction it runs, with each hop's kind, confidence and call site, and the path's own confidence — its weakest hop. Both directions are always checked and the one found is named. When there is none, the report distinguishes three answers that are not the same: no chain within `--hops` (and how many it would take), no directed chain in either direction, and the two being on different islands, which means no walk can ever connect them. |
| `codegraph islands [--rev REV] [--limit N] [--json] [--strict]` | Report the connected components of the revision's `CALLS`, `INHERITS`, `IMPLEMENTS` and `REFERENCES` edges, read as undirected: how many separate regions the codebase is in, how big each is, and which symbols anchor them, plus what the tool can say about why each one stands apart (implicit invocation, a `NETWORK` boundary, or nothing it recognises). An island of one is *not* a dead-code finding (see below). |
| `codegraph orphans [--rev REV] [--limit N] [--include-public] [--include-decorated] [--json]` | Find functions whose every recorded caller is a test — defined, tested, and never invoked by the code that was supposed to invoke it. Such a function is *not* a one-symbol island, precisely because its test calls it, so `islands` structurally cannot surface it. Candidates are private by name, undecorated, defined outside the test tree, and never mentioned by name anywhere in the source text — that last filter has no off switch, because a static call graph cannot see a callback handed to a library. Not a dead-code report (see below). |
| `codegraph diff [<base>..<head>] [--json]` | Report what changed between two revisions by content hash, never by line number: symbols added/removed/changed, plus any side effect newly reachable. Defaults to `merge-base(default branch, HEAD)..WORKTREE` — "what has this branch changed so far." |
| `codegraph history [<symbol>] [<base>..<head>] [--limit N] [--json] [--strict]` | The graph over a range of commits, oldest first, each compared with its first parent the way `diff` compares two revisions. With a symbol: every commit that changed its body hash, its confident callees or the side effects reachable from it, followed across a move to another file or class — a pairing reported with a confidence tier, never as the same id. Without one: per commit, the symbols added, removed, moved and changed, and the edges and effects gained and lost. Defaults to `merge-base(default branch, HEAD)..HEAD`; only the named range is materialized, and nothing it materializes is kept (see below). |
| `codegraph trace [FILE] [--rev REV] [--forget]` | Import a recorded run (see "What a trace buys" below) and bind it to a revision, so that calls the resolver cannot see — framework dispatch, `getattr`, a decorator's wrapper — become edges marked `runtime` alongside the ones it deduced. With no argument it describes the trace the revision holds, or tells you how to record one. Additive: a repository with no trace answers exactly as it did before. |
| `codegraph gc [--keep REV]` | Prune the Layer 1 parse cache down to what `HEAD`, the worktree, and any `--keep`-named revisions still reference. Never touches the graph itself, so it can only make a future answer slower to rebuild, never wrong. |
| `codegraph init` | Make this repository's coding agents aware of codegraph: an `AGENTS.md` section, the `@AGENTS.md` bridge into an existing `CLAUDE.md`, and a commented `codegraph.toml` stub. Idempotent; never overwrites content it did not write; never touches `.git/`. |
| `codegraph guide` | Print the agent-facing workflow to stdout — the same text the plugin ships as `SKILL.md`, so the short `AGENTS.md` section can defer to it rather than inline it. |
| `codegraph install-hooks` | Install `post-commit`/`post-checkout`/`post-merge` git hooks that warm the cache in the background. Purely an optimization — every query reconciles the working tree itself regardless (see below), so results are identical whether or not a hook ever fires. |
| `codegraph sessions [<revspec>] [--json]` | List the `Session:` trailers commits carry — the pointer from a commit to the session that produced it (see "Sessions: which conversation wrote a commit" below) — as `<commit>  <pointer>`, newest first. `<revspec>` is a revision or range as `git log` takes it, default `HEAD`. Commits without a pointer are not listed, so a repository with none prints nothing. Reads git only. |
| `codegraph install-session-hook [--uninstall]` | **Opt-in, and it writes into your commit messages.** Installs a `prepare-commit-msg` hook that appends `Session: $CODEGRAPH_SESSION` when that variable is set. Nothing else installs it, `install-hooks` included; `--uninstall` takes it back out. |

### What an island is, and is not

`codegraph islands` treats every edge a change travels along — a call, a
base class, a structural implementation of a `typing.Protocol`, and a name
used as a value — as undirected, and splits the graph into connected
components. That the call graph is *not* connected is real
structure, not a defect: a service boundary, a config-gated region, and
code nothing references all show up as separate islands. On psf/requests
it reports 807 symbols in 132 islands, the biggest holding 665 of them and
127 being islands of exactly one.

**An island is not a reachability result, and a one-symbol island is not
dead code.** Membership comes from the call edges the resolver recorded,
and a great deal of Python is invoked by mechanisms that leave no call site
in the source: dunders (`__delitem__` runs on every `del d[k]`),
decorators, framework dispatch, ABC overrides, packaging entry points.
`AuthBase.__call__` and `CaseInsensitiveDict.__delitem__` are each an
island of one on requests, and neither is unused.

So each island is labelled with what can be said about why it stands
apart, and no label is ever a claim that code is dead:

- **`implicit: entry, dunder, decorator, test, override, nested, import`**
  — a mechanism found among the island's members by which something could
  reach it without a call site. Not proof that it runs; counter-evidence to
  "nothing reaches this". 115 of requests' 132 islands carry at least one.
  `entry` is the one that is not an inference: a statement at some file's
  top level calls into the island, which is what a `main()` under an
  `if __name__ == "__main__"` guard has instead of a caller.
- **`boundary: NETWORK`** — a path inside the island leaves the process.
  The handler lives in another repository, so the island boundary *is* the
  service boundary. That is signal, not a false positive. `NETWORK` is
  deliberately the only effect kind that marks one: a socket call leaving
  the process is a structural fact, whereas coupling two functions through
  a database means reading SQL and tracking a schema, which is a different
  tool.
- **`traced: N members seen running`** — a run was watched entering this
  island. It is listed apart from the mechanisms above because it is not a
  mechanism: every one of those says "here is a way something *could* reach
  this", and this one says a run *did*. It only appears on a revision with
  an imported trace, and it is the only label in this list that is evidence
  rather than counter-evidence.
- **`no implicit-invocation mechanism recognised`** — the remainder, 17
  islands on requests, and still a statement about the tool rather than
  about the code. Most of them are the library's own public surface
  (`get_dict`, `dict_from_cookiejar`), called by users of the package and
  by the stdlib — neither of which is in the tree.

Every `islands` and `orphans` summary also carries a `trace:` field, which
reads `none` until a run has been imported. That is deliberate: "nothing
reaches this" is a far stronger claim about a repository whose test suite has
been watched running than about one where the only witness is the source
text, and a reader cannot discount a claim they were never told the basis of.

Two things that moved the numbers are worth naming separately, because both
were relationships the source really states and the graph simply did not
hold. `Cls()` resolves to the class and nothing in the source ever spells
`Cls.__init__`, so constructors had no incoming edge at all; implying that
edge (following the MRO for an inherited one) folded 18 of requests' 172
islands into the rest of the graph. And a name used as a *value* —
`connection.row_factory = _Row`, a method listed in a dispatch table, a
callback passed to a library — was recorded nowhere, which is why on
codegraph's own source all six symbols in the `unexplained` bucket were
live code. Recording it (`REFERENCES`, plus `IMPLEMENTS` for a class that
satisfies a `typing.Protocol` without naming it) took that bucket from 7
islands to 1 here, and from 20 to 17 on requests, for 7.6% more edges.

### What `orphans` finds that nothing else can

Every other query needs the symbol's name as input, which means already
suspecting the bug. `orphans` asks a bug's *shape* instead: **which
functions have callers, all of which are tests?** That is the signature of a
helper that was written, tested, and then never wired up — `_get_mlx_info()`
on a 948-file project, docstring'd "Surface MLX info in the doctor report",
covered by `test_doctor_has_mlx_info`, and never reached by the command, so
the feature silently did not exist and the test passed anyway (#37).

`islands` cannot find that, structurally: the test's call is a real edge, so
the function is not a one-symbol island. The thing that lets the bug survive
review is the thing that hides it from the only global view.

The filters are the command. On that project, "every caller is a test"
alone answers **280** functions — a list nobody reads. Private by name and
undecorated cuts it to **10**; "not mentioned by name anywhere in the source
text" cuts it to **5**, of which **2 were real defects** (one is now merged
upstream). 40% on a hand-reviewable list, in a repo with 19,960 tests.

That last filter is not optional and has no flag, because a static call
graph cannot see a function passed as a **value**. Without it the same
project's list is headed by `_run_bash_sandbox.<locals>.preexec` (handed to
`subprocess`'s `preexec_fn`) and `_handle_sigint` (registered through
`signal.signal`), neither of which is dead. `blob_refs` cannot answer it —
it records calls, not names used as values — so the filter is a documented
grep over the revision's non-test source text, which over-matches on
purpose: it can only remove a candidate, never invent one.

**This is not a dead-code report, and no row says otherwise.** The claim is
about codegraph's knowledge — no caller outside the test tree was recorded,
and the name does not appear in the source text — and the blind spot is
printed in the summary, with the rows, not left here. A name resolved at
runtime (`getattr`, a registry, a `pyproject.toml` entry point, a template)
leaves nothing for either half of this report to find. Three of those five
survivors were deliberate: two documented back-compat shims and an import
probe whose docstring says test-only is the point.

### What `path` answers that `impact` cannot

Every other query starts at one symbol and fans out. `path` starts at two,
which is the position you are actually in when you suspect a coupling and
cannot name the chain. The workaround — run `impact` on one and look for
the other in the rows — fails in exactly the cases worth asking about: a
long chain, a chain past `--hops`, or a row `--limit` crowded out.

```
$ codegraph path HTTPAdapter.send get_encoding_from_headers
from: src/requests/adapters.py::HTTPAdapter.send · to: src/requests/utils.py::get_encoding_from_headers
  · direction: forward · hops: 2 · confidence: HIGH · reverse: none
  · basis: shortest directed path over CALLS, INHERITS, IMPLEMENTS, REFERENCES edges, LOW excluded, within 6 hops
forward
  src/requests/adapters.py::HTTPAdapter.send  src/requests/adapters.py:634  start
  src/requests/adapters.py::HTTPAdapter.build_response  src/requests/adapters.py:365  hop 1, CALLS, HIGH confidence, call site src/requests/adapters.py:748
  src/requests/utils.py::get_encoding_from_headers  src/requests/utils.py:569  hop 2, CALLS, HIGH confidence, call site src/requests/adapters.py:385
```

Direction is the answer rather than a detail: if A reaches B then editing B
is the risky move, if B reaches A then editing A is, and `forward` always
means the direction you wrote the arguments in. The direction that was *not*
found is printed too (`reverse: none`), because that is what tells you the
tool looked rather than leaving you to wonder.

Each hop names its kind, since after `REFERENCES` and `IMPLEMENTS` landed
"connected" means four different things, and its confidence, since a path is
only as strong as its weakest hop — five HIGH hops and four HIGH plus one LOW
are different answers, and the report marks which hop is the weak one so you
know where to go and look. Every hop carries the `file:line` that makes it,
the same clickable evidence `effects` gives, because `edges` already stores
it.

**"Not connected" is three different answers, and they are never collapsed
into one:**

```
$ codegraph path HTTPAdapter.send get_encoding_from_headers --hops 1
... direction: none · reason: no directed path within 1 hop · show_path: --hops 2 · basis: ...

$ codegraph path Session.request get_encoding_from_headers
... direction: none · reason: no directed path in either direction · show_path: --all · basis: ...

$ codegraph path FlaskyStyle get_encoding_from_headers
... direction: none · reason: different islands -- no walk in any direction, at any confidence, can connect them · basis: ...
```

The first is a budget you set, and the report says what budget would answer
it. The second is a real negative over the edges it walked, with
`show_path: --all` when a LOW chain exists that the default did not walk —
the same "a count you cannot see is half an answer" rule as `impact`'s
`show_hidden`. The third is the strongest statement this tool can make about
two symbols, and it is not computed here: it is `islands`' own partition, so
`path` and `islands` cannot come to disagree about the same pair. It is also
consulted *last*, only once every walk `path` can perform has come back
empty, so the strong claim is never printed over evidence against it.

LOW hops are excluded by default and included with `--all`, matching
`impact`. That one flag also governs the bare-name fan-out, which is LOW by
construction and is not in the stored graph at all: with `--all` a path may
run through `item.save()`, and the hop says so, naming the bare name it went
through.

`--hops` defaults to 6 rather than `impact`'s 3, because this walk follows
one chain instead of a widening frontier and can afford to look further. On
psf/requests, of 3,000 randomly sampled symbol pairs the 99 that are
connected at all sit a median of 3 hops apart (mean 3.26, max 8); a budget of
3 would find 51% of them and 6 finds 99%.

### What `history` answers, and what it costs

`diff` says what changed between two revisions; `history` says which commit
did it. For a symbol, it lists the commits in the range that changed its
*behaviour* — its body hash, the callees it reaches with confidence, or the
side effects reachable from it — rather than every commit that touched its
file. That last part is the one `git log -L` cannot answer: a commit that
puts a `NETWORK` call inside `charge` shows up in the history of `checkout`,
whose text never changed.

```
$ codegraph history checkout main~3..main
symbol: m.py::checkout · commits: 3 · changed_in: 2 · base: 48aa84e… · head: a34af83…
commits
  b9e6ed2…  m.py:8  effects +NETWORK · "charge hits the network"
  a34af83…  m.py:4  calls +pay.py::charge; calls -m.py::charge · "move charge to pay.py"
```

**A move is an inference, and says so.** A node id is `path::qualname`, so
moving `charge` from `m.py` to `pay.py` is, in the graph, a removal and an
addition. `history` pairs the two when their bodies hash the same, and
grades the pairing with the resolver's own rule for a name matched by
guesswork: MEDIUM if it is the only candidate, LOW if it is one of several.
Never HIGH — no text states that two ids are one symbol. A MEDIUM move is
followed (`moved from m.py::charge to pay.py::charge (MEDIUM)`); a LOW one
names the candidates and stops, with a `lineage_ambiguous` entry in
`unknowns`, because continuing down one of them would be a guess presented
as history. A rename changes the definition's own name, which is part of its
body hash, so it reads as exactly what the source shows: a removal and an
unrelated addition.

**What it compares** is what `diff` compares — body hash, and edges and
effects without the LOW tier, which is a guess about the whole repository
and moves whenever anyone anywhere adds a same-named symbol. Merges are one
step each, along the first-parent line, as `git log --first-parent` reads a
branch.

**What it costs.** The walk materializes only the range it is given: the
first commit's parent (which is `base` itself whenever `base` sits on
`head`'s first-parent line), then each commit in turn. There is no
backfill, nothing is checked out, and every revision the walk created is
discarded when it ends; one it found already materialized is copied from,
never consumed. Each commit's graph starts as a copy of its parent's and is then
reconciled like an edit to the working tree, so a commit pays for the files
it touched — narrowed when it did not change the symbol table — and parsing
is proportional to the blobs the range introduces, since the parse cache is
shared with every revision ever seen. The one cold build is the starting
revision. The default range is `merge-base(default branch, HEAD)..HEAD`,
matching `diff`'s base; the head is `HEAD` rather than the worktree because
history is a list of commits, and `diff` is the command for what is not
committed yet.

### What `unknowns` answers, and what `--strict` does with it

The point of this tool is to give an agent a truthful representation of a
codebase, which means being explicit about what it cannot answer. It always was —
confidence tiers on every edge, a named reason on every unresolved
reference, `low_confidence_hidden`, `islands`' `unexplained` — but all of
that is *aggregate*. `low_confidence_hidden: 235` is a property of a report,
not of any symbol, so an agent asking about one function could not find out
that *that* function's body is half dynamic dispatch, and nothing ever said
what would settle it.

```
$ codegraph unknowns Model.save --path django
symbol: django/db/models/base.py::Model.save · references: 14 · resolved: 3 · unresolved: 11
  · island: explained by entry, dunder, decorator, test, override, nested, import, NETWORK
  · mechanisms_not_found: none · basis: stored rows for this symbol only; ...
ambiguous
  router.db_for_write  django/db/models/base.py:866  call reference, 15 candidates
  field_names.add      django/db/models/base.py:905  call reference, 20 candidates
  ...
builtin
  ValueError           django/db/models/base.py:868  call reference
  ...
unknowns
  ambiguous  5 references in this body  the bare name matches several definitions; `codegraph resolve <name>` lists them, and `impact --all` walks them
  builtin    6 references in this body  the target is a Python builtin; no repository symbol is being called
  hop_limit  an `impact` walk of 3 hops does not exhaust this symbol's dependents  the walk stopped at its budget; re-run with a larger --hops
```

Three parts, all of them queries over rows the indexer already wrote —
nothing is inferred, and nothing asks the reader to exercise judgement:

- **The references that produced no edge**, each with its reason, raw name,
  line and candidate count, grouped by reason. Beside them, **the resolved
  ratio**: `3 of 14` of this body's reference sites became edges. A site is
  `(path, line)`, so one `self.render()` that writes an edge per override
  counts once, and two calls written on one line collapse into one — a
  stated undercount rather than a guess.
- **The island**, labelled by `islands`' own partition and its own mechanism
  passes, never a second implementation that could come to disagree. When it
  is unexplained, `mechanisms_not_found` lists what was checked, because
  "nothing recognised reaches this" is only readable beside the list of what
  *recognised* covers.
- **Whether `impact` would stop on its budget.** An `impact --hops 3` that
  ran out of hops prints exactly like one that exhausted the graph, and the
  difference is the whole of "is this the answer or just what fit".

A **low ratio is not a defect**. `3 of 14` on a body full of `ValueError`
and `frozenset` says the resolver identified eleven references exactly and
knows that none of them is a symbol in this repository. That is why every
reason has its own count and its own next action:

| reason | what the tool says |
|---|---|
| `unknown` | the name matches nothing in the repository; expect dynamic dispatch, and read the body to see what it is |
| `ambiguous` | the bare name matches several definitions; `codegraph resolve <name>` lists them, and `impact --all` walks them |
| `external` | the target is outside the repository; no future index will resolve it |
| `builtin` | the target is a Python builtin; no repository symbol is being called |
| unexplained island | no implicit-invocation mechanism was recognised; a runtime trace is the only thing that can confirm this symbol is reached |
| hop limit | the walk stopped at its budget; re-run with a larger `--hops` |

Each string is written once, in `uncertainty.NEXT_ACTION`, and looked up —
never composed around a case, which is how one reason comes to be described
two different ways. The per-case numbers ride in a separate `detail` field.
A test asserts the mapping is total against the resolver's own
`resolve.UNRESOLVED_REASONS`, so a fifth reason cannot be added without one.

### The uncertainty envelope

Every report that can be incomplete now carries an `unknowns` array beside
its results — in `--json` as a real array, in text under a heading of its
own — and `--strict` exits **3** when one of those entries is blocking. "Do
not act on this answer" becomes an exit code rather than a judgement. 3, not
1 or 2: those already mean "no symbol matched" and "more than one matched",
and `codegraph impact X --strict && edit` has to be able to tell a missing
symbol from an answer with a hole in it.

What counts as a hole is one rule, applied everywhere: **an entry names
something the run did not examine.**

| report | entries it can carry | why |
|---|---|---|
| `unknowns` | one per reason present, plus an unexplained island and a hop limit | the whole report |
| `impact` | the hop limit | the callers past `--hops` were never looked at |
| `effects` | `unknown` and `external` calls in the body | the witness walk cannot follow them; `ambiguous` is followed through the same hubs `islands` uses, and a `builtin` with an effect is in the catalog |
| `path` | the hop limit, or a LOW chain the default walk excluded | exactly the negatives a flag would turn into a path |
| `islands` | its own `unexplained` count | the number it already prints, in the form a machine can act on |
| `orphans` | none | its uncertainty is a standing `caveat` on every row — a name resolved at runtime leaves nothing for either half of the report to find — and a caveat that fires on every run is not news. A `--strict` there would refuse on every non-empty report |
| `diff` | none | a content-hash comparison of two revisions: no walk, no budget, nothing hidden |
| `history` | `lineage_ambiguous` | a symbol whose body matches several removed definitions in one commit: the commits before it, under whichever id it had, were not examined |

Two things deliberately do **not** make a report incomplete:

- **A LOW-confidence row.** It is an answer, given with its tier attached.
  `low_confidence_hidden` counts rows the report chose to summarize rather
  than print, and it already carries `show_hidden: --all` (#37). Were
  `--strict` to refuse on LOW, it would refuse on every real repository and
  stop carrying information.
- **`truncated`.** `--limit` is the caller's own budget and already has a
  field; a report that honoured the budget it was given did not fail to see
  anything.

`external` and `builtin` entries are printed and never block: they are
answers, not gaps — the resolver identified the reference exactly and knows
no node in this graph is its target. Each entry says which it is in a
`blocking` field, so a `--json` consumer does not need this table.

**An empty `unknowns` means "no hole this tool can name", never "this answer
is complete."** A name assembled at runtime leaves nothing for any of this
to find. What shrinks the gap is observing a run, which is what
`codegraph.tracer` records and `codegraph trace` imports — see "What a trace
buys, and what it costs" below. An `unexplained_island` entry names the one
hole a trace closes outright: once a run has been watched entering the
symbol, the entry is no longer raised.

`resolve`, `impact`, `effects`, `path` and `unknowns` share one exit-code
convention for resolving `<symbol>` to a node id: `0` = a single unambiguous match, `1` =
nothing matched, `2` = more than one match (every candidate is printed; pick
the right one and re-run with the full node id). `path` applies it to each of
its two arguments. The convention is about resolving a *name*, and nothing
else — so a report saying the two symbols are not connected at all is still
exit `0`, because that is an answer, and so is a full list of what codegraph
does not know about a symbol. `3` is the one code outside the convention,
and only under `--strict`: the name resolved, the report was produced, and
it has a hole in it.

`islands` and `orphans` take no symbol, so that convention does not apply
to either: they exit `0` for a report — an empty one included, since "nothing
matched" is a real answer — and `1` only for a revision they cannot
resolve.

All commands accept `--path <dir>` to run against a different repository
root (default: the current directory).

## Confidence, and what earns it

Every edge carries HIGH, MEDIUM or LOW. The tier is not a score, it is a claim
about *how the target was identified*, and the resolver tries the steps below in
order and stops at the first that matches.

| step | tier | example |
|---|---|---|
| the name is imported | HIGH | `from pay import charge` then `charge()` |
| ...through a package re-export | HIGH | `pkg.Thing` where `pkg/__init__.py` says `from .app import Thing` |
| the name is defined in this module | HIGH | a module-local `def charge` |
| `super().X()` | HIGH | resolved through the enclosing class's bases, skipping the class itself |
| `self.X()` | HIGH + MEDIUM | the inherited method at HIGH, every subclass override at MEDIUM |
| `Cls()` | inherits the class edge's tier | plus an edge to the `__init__` it would run, found up the MRO |
| a bare name matched against the whole repository | MEDIUM if unique, else LOW | `item.save()` where nothing says what `item` is |

A re-export is followed because it is the *same* evidence as the row above it,
read in another file: `from .app import Thing` is a recorded fact about the
source text, not an inference over it, so a chain of them is a conjunction of
facts and depth does not weaken the claim. Two things are deliberately not
followed, because they would be inferences: `from .app import *` (which names
it binds depends on `__all__`, which real packages compute at runtime), and a
chain longer than `REEXPORT_HOPS`. Neither produces a weaker edge — both
produce no edge, and the reference falls to the last step below.

The last step is the one to be suspicious of. It is a guess by construction, and
on a large repository one name can match hundreds of definitions, so those
candidates are not stored at all: the reference is recorded once and the
candidates are recomputed when a query asks (see `--all`).

Two deliberate refusals, both of which used to produce confident nonsense:

- **A Python builtin is never matched against a repository symbol.** `set(x)`
  used to become an edge to a class that happened to define a method called
  `set`, and that phantom edge then carried an effect into a witness path.
- **The built-in effect catalog only describes third-party code.** Analysing
  `requests` itself, every internal call expanded into the `requests.*`
  namespace and matched the catalog's own network rule, so the library's helpers
  were reported as network calls and the real one was missed. A name belonging
  to a module this repository defines skips the catalog; your own `[[effect]]`
  rules still apply, since naming house abstractions is what they are for.

### Provenance: the axis confidence is not

Every edge also carries a **provenance**, and it answers a question the tier
above cannot. Confidence is about reading: "how sure is the resolver that
this reference means that symbol". Provenance is about evidence: `static`
means the text says so, `runtime` means a run was watched doing it. They are
orthogonal, and the mixed cases are the point — a LOW static edge a trace
confirms is, in fact, certain.

So an edge a run confirms keeps **both** rows. Merging them would have to
throw one of the two facts away, and which one gets thrown away is exactly
what a reader is asking about. A traced edge the resolver never found is
stored at HIGH, whatever the resolver would have guessed: a tier is a
statement about degrees of inference, and `sys.monitoring` hands over the
code objects, so there is no inference left for a tier to express. Adding a
fourth tier above HIGH was the alternative and was rejected for putting two
different axes on one scale.

Everything below this line is `none` until you import a trace, and nothing
about an untraced repository changes.

## What a trace buys, and what it costs

The static resolver has a ceiling this project has been explicit about:
framework dispatch, `visit_*` name lookup, `getattr`, a decorator's wrapper.
Those calls are not in the text to be read, so no amount of resolver work
reaches them — on pallets/flask they are most of why recall is 0.29. A run
is the only thing that sees them.

```sh
# 1. record, with the interpreter that can actually run the program
python "$(python -c 'import codegraph.tracer as t; print(t.__file__)')" \
    --root . --out trace.json -- -q

# 2. import, bound to the revision it was taken from
codegraph trace trace.json
```

Measured on pallets/flask `d73fa1c` (83 files, 1,622 static `CALLS` pairs),
tracing its own `tests/` suite:

| | |
|---|---|
| calls observed | 2,886 |
| confirming an edge the resolver already had | 694 |
| **adding one it did not** | **1,989** |
| functions seen running | 1,465 |
| islands | 237 → **85** (singletons 228 → 81, unexplained 17 → **6**) |

A concrete answer that changes. `setupmethod.<locals>.wrapper_func` in
`src/flask/sansio/scaffold.py` is the wrapper every `@setupmethod`-decorated
method is replaced by. No call site in flask names it, so
`codegraph impact` on it reported `symbols: 0 · effects_reachable: none` —
"nothing depends on this", about a function 273 call sites invoke. With the
trace imported it reports **412 dependents across 46 modules**, the first
rows reading `hop 1, HIGH confidence, observed`, and five reachable effect
kinds behind it.

What it costs:

- **A run.** Flask's suite is 494 tests and ~40s; a suite that needs a
  database or a network needs them here too. Recall of the trace is recall
  of whatever you ran, and a path your suite never takes is a path the trace
  does not have.
- **One re-index of that revision.** Importing changes what the graph
  contains, so it invalidates the materialized revision exactly as a
  `codegraph.toml` edit or an upgrade does — the "adds or removes a
  definition" row in the cost table above. `codegraph trace` pays it up
  front rather than leaving it for the next query.
- **Nothing ongoing.** A trace is bound to the revision it was imported for
  and to the *content* of each file it named. Edit a file and every
  observation about it is dropped, reported as `stale` in the summary and by
  `codegraph trace`, and the rest of the trace keeps working. It never
  silently describes code that is no longer there, and `codegraph trace
  --forget` returns the graph to exactly what it was.

What it does **not** buy: a better benchmark number. `bench/` scores the
resolver, so it reads `static` rows only — importing a trace into a target
repository leaves every figure in the table below unchanged, which is
checked rather than asserted (`tests/test_bench_scorer.py`).

## Sessions: which conversation wrote a commit

A trace says *that* an edge is real. A session says *why* the code is shaped
the way it is. Most code is now written in a conversation with an agent, and
that conversation — the alternatives rejected, the constraint that forced the
odd shape — is thrown away, leaving a sentence of it in the commit message.
A commit can keep a pointer to it instead, as a git trailer:

```
Fix the retry loop

Co-Authored-By: ...
Session: <uri>
```

`codegraph sessions` lists them:

```sh
codegraph sessions                  # every commit reachable from HEAD that has one
codegraph sessions main..HEAD       # just this branch
codegraph sessions --json
```

The contract is small on purpose:

- **The pointer is opaque.** Whatever follows `Session:` is returned as a
  string — a Claude Code session id, a Codex rollout path, a PR thread, a
  design doc, your own notes. It is never parsed, and there is no field
  for which agent wrote it. It works the same for a human.
- **The link lives in git, not in the index.** It travels with push and
  clone, and git reads it: `git log`'s own trailer parsing decides what
  counts (the last paragraph, `key: value` lines, the key matched
  case-insensitively), so a `Session:` line in the middle of a message body
  is not one.
- **Absence is not an error.** A commit without a pointer, and a directory
  that is not a git repository, answer with nothing. A repository with no
  pointers answers every other command exactly as it did before; like a
  trace, the link is additive.

### Opening a session is an adapter's job

Turning a pointer into "open this session", or better, "fork it and ask the
agent that wrote this code why", depends entirely on the agent, so none of
it is in codegraph. An adapter lives beside the core, takes the opaque
pointer `codegraph sessions --json` hands it, and either opens the session or
reports **session not available**. That is an answer, not a failure:
transcripts can hold secrets and are often local-only, so a pointer that
resolves on one machine will not on another. Where an agent cannot fork a
session, the fallback is to hand the transcript to a new one as context.

### Writing the pointer (opt-in)

Writing the pointer is the agent's or your job, and a hand-written trailer
is read exactly like any other. For convenience, and only if you ask for it:

```sh
codegraph install-session-hook               # add the prepare-commit-msg hook
export CODEGRAPH_SESSION="<uri>"             # the agent exports its session
codegraph install-session-hook --uninstall   # take it out again
```

**This hook writes into your commit messages**, which is why it is its own
command and why neither `install-hooks` nor `init` ever installs it:
`install-hooks` stays a pure warming optimization, and `init` never touches
`.git/`. The hook:

- does nothing unless `CODEGRAPH_SESSION` is set and non-empty;
- appends through `git interpret-trailers`, so the pointer lands in the
  trailer block beside `Co-Authored-By:` and is never duplicated — an
  `--amend` in the same session adds nothing, one from another session adds
  a second pointer;
- writes only into a message that exists before the editor opens (`-m`,
  `-F`, `--amend`, `-c`/`-C`). A draft you have yet to write in the editor
  is left alone, because a trailer in it would stop git from aborting when
  you quit without a message. Merge and squash messages are left alone too:
  the commits they are drafted from carry their own pointers;
- can never fail a commit, and keeps an existing `prepare-commit-msg` hook
  intact, under the same rules as `install-hooks`.

## `codegraph.toml`

The built-in effect catalog (stdlib, `requests`/`httpx`, SQLAlchemy, psycopg,
boto3, ...) only knows public library calls. Most side effects in a real
codebase sit behind house abstractions instead — a `Repo.save()`, an internal
`db` module — so a project-level `codegraph.toml` at the repository root lets
you extend the catalog to reach them:

```toml
source_roots = ["", "src"]

[[effect]]
match = "app.db.*"
kind = "DB_WRITE"
```

`source_roots` controls how a file path is turned into a Python module name
for import resolution (`src/pay/service.py` -> `pay.service` when `"src"` is
a root). `[[effect]]` entries merge over the built-in catalog by pattern;
`match` is a dotted-name glob (`*` wildcards allowed) and `kind` is one of
the nine effect kinds (`DB_WRITE`, `DB_READ`, `NETWORK`, `PROCESS`,
`FS_WRITE`, `FS_READ`, `ENV_READ`, `GLOBAL_MUTATE`, `NONDETERMINISM`).

There is no setting for the bare-name fan-out, and that is deliberate.
When a call like `item.save()` names nothing importable, nothing
module-local, and nothing reachable through `self`, the resolver falls back
to matching `save` against every definition in the repository — a handful
on a small codebase, 971 for a single call site on django. None of that is
stored. The candidate set *is* "every live definition named `save`", which
the graph already holds, so the call is recorded once and the set is
recomputed whenever a query asks for it. Materializing it stored nothing new
at a cost that grew with the *square* of the repository: 2.09M of django's
2.16M edges were that one kind of guess.

Which also settles where the bound belongs. Whether 971 candidates is too
many is a property of the question, not of the graph: `impact` wants them
ranked and cut off at `--limit`, `effects` wants pure reachability through
them, `diff` wants none of them. So it is `--limit`, per query, and no
future question is bound by a number you picked once at index time.

`ambiguity_limit` used to be that number. It is still read, warns when set,
and no longer affects anything; remove it. See issue #25.

`codegraph.toml` lives at the repository root, not inside `.codegraph/`,
because it is hand-written configuration meant to be committed and shared, whereas
`.codegraph/` self-ignores and holds only derived cache: config is tracked,
everything derived is disposable.

`codegraph init` drops a fully commented-out version of this file, documenting
every setting above with its default, if there isn't one already. It is inert
until you uncomment something, and an existing `codegraph.toml` is never
rewritten.

## Lazy refresh on query

Every query reconciles the working tree first, then answers — the `git
status` model, not an explicit-index-only model where a stale index silently
lies. No daemon, no required hooks; `codegraph index` exists only to pay the
cold-build cost up front rather than on the first real query, and
`install-hooks` exists only to move that cost into the background after a
commit/checkout/merge. If a hook never fires and `index` is never run by
hand, every answer is still correct — merely slower on that one query.

## The cost guarantee, honestly

The design's core promise is "after the first index, work is proportional to
the diff, never to repository size." That promise has two halves, and they
are not in the same place:

- **Parse (Layer 1) — real, and measured.** The parse cache is keyed on git
  blob SHA, not `(path, mtime)`, so identical content is parsed once for the
  life of the repository regardless of how many branches or paths reference
  it. Verified directly against a scratch repo: creating a branch parses 0
  blobs, switching `A -> B -> A` re-parses 0 blobs on the return trip, and
  results do not depend on file mtimes across checkouts (which `git
  checkout` rewrites indiscriminately and an mtime-keyed cache would have
  re-parsed on every switch).
- **Resolve (Layer 2) — proportional for most edits, not all.** Resolution is
  global by nature: a bare-name call matches every definition in the revision,
  and `self.X` walks a class hierarchy that spans files. So a reconcile narrows
  to the edited files only when it can prove the revision's *symbol table* is
  unchanged — same qualnames, same kinds, same live/shadowed bindings, same
  base classes. A body-only edit qualifies; adding, removing or renaming a
  definition does not, and falls back to a whole-revision rewrite, which cannot
  leave a stale edge behind.

  Measured on django (2,930 files, 93k edges). Reported as a fraction of that
  repository's own cold index, because absolute seconds are not reproducible:
  the same commit measured 24s, 49s and 112s for a cold index on the same
  machine on the same day, a 4.6x spread from background load alone. Ratios
  hold across that; wall-clock does not, and an earlier version of this table
  quoted a "before" column measured on a different day, which made a 2x load
  difference look like a 2x regression.

  | | cost, relative to a cold index |
  |---|---|
  | reconcile with no changes (every query pays this) | **~1/160** |
  | body-only edit | **~1/13** |
  | edit that adds or removes a definition | ~0.4 |
  | cold index | 1 |

  For scale, one full run of that session: cold 112s, no-change reconcile
  0.45s, body-only edit 8.7s, definition added 45s.

  So the honest version: the common cases are proportional now, and the
  symbol-table-changing case is not. What remains non-proportional is effect
  *propagation* — an effect flows along edges, so a change anywhere can reach
  anywhere and there is no cheap frontier to start from. It is skipped
  entirely when a narrowed edit provably did not change any of its three
  inputs, which is why a body-only edit is 3.7s rather than 9s, but a
  structural edit still pays it in full.

- **An upgrade re-resolves every materialized revision, once.** The
  fingerprint that lets an unchanged tree skip its work pins a digest of
  codegraph's own resolution source, not just the parser version and your
  `codegraph.toml` — so the first reconcile after `pip install -U` (or after
  pulling a new commit into a checkout) rebuilds Layer 2 for each revision
  you query, at roughly the "adds or removes a definition" row above rather
  than the `~1/160` one. Measured on django (2,932 files, 109k edges,
  one session): **12s** to re-resolve after a resolver change, against **34s**
  to index the same tree cold and 0.1s for the no-change reconcile every query
  otherwise pays.

  The parse cache is deliberately *not* invalidated: what a parse produces is
  a function of the blob's bytes and the parser version alone, never of
  resolver code, so this is a re-resolve and not a re-index — zero blobs
  parsed. The price is the right one to pay, because the alternative is what
  happened before #44: the upgraded resolver simply never ran. Same tree,
  same fingerprint, so the reconcile decided there was nothing to do and kept
  answering with the previous version's edges — indefinitely, with no error
  and no warning.

## How good is the graph, honestly

`tests/test_resolution_rules.py` reports recall 1.00 at precision 0.91, and
that is **not** a statement about real code: it measures 15 hand-written call
sites in a synthetic 17-file repository, each authored to illustrate a rule the
resolver already implements, with no dunder invoked by syntax, no decorated
target and no closure among them. It is a regression guard, it is named like
one, and its assertions say so when they fail. The effectiveness floors are on
`bench/` output instead (#39), per target repository — see
[Effectiveness floors](#effectiveness-floors).

`bench/` is the real measurement (#35). It runs a target repository's **own
test suite** under `sys.monitoring` (`codegraph.tracer`, the same recorder
`codegraph trace` imports from), records every `(caller, callee)` pair that
actually executed, and scores the static graph against it. A call the tests
made is a call that exists, so a traced edge missing from the static graph is a
real gap — no labelling judgement involved.

```sh
uv run python -m bench.run requests          # clones, or --source-root DIR to copy a clone
uv run python -m bench.run flask --json /tmp/flask.json
uv run python -m bench.run requests --tests tests/test_utils.py   # narrow the suite
uv run python -m bench.run flask --reuse-trace --rebuild          # cold index time, not a warm reconcile
uv run python -m bench.run requests --check-floors                # exits 1 below the floor
```

Each run copies the clone (an editable install writes into the tree, and the
source clone must stay untouched), builds a venv, installs the target
**editable** — a normal install copies the source into `site-packages`, where
every in-repo frame is then filtered out as external and the trace comes back
nearly empty, silently — traces the suite, indexes the same working tree, and
scores. Nothing about it runs during `pytest -q`; only the scorer's arithmetic
is unit-tested there (`tests/test_bench_scorer.py`).

### What it measures

| | psf/requests | pallets/flask | django/django |
|---|---|---|---|
| suite traced | `test_utils.py`, `test_structures.py` (240 tests) | `tests/` (494 tests) | the ORM test apps (5120 tests) |
| traced call edges (judgeable) | 115 | 2683 | 52774 |
| **recall** | **0.79** (91/115) | **0.29** (775/2683) | **0.40** (20959/52774) |
| recall at HIGH/MEDIUM | 0.77 | 0.26 | 0.25 |
| conditional precision | 0.99 (85/86) | 0.98 (515/524) | 0.91 (9662/10665) |

requests and flask measured 2026-09-20 at codegraph `0351e8a`, against psf/requests
`dae7ef6` and pallets/flask `d73fa1c`; django measured 2026-09-23 at codegraph
`5c246d5`, against django/django `951d13c`. Every run prints the target's commit
beside the score, because a clone tracks its default branch and two runs a month
apart measure two different repositories.

django's suite is not a pytest suite — `tests/runtests.py` writes its own
settings module, creates test databases and drives unittest itself — so
`tracer.run_suite` runs it as `__main__` in the traced process rather than
calling `pytest.main`. The scope is the ORM test apps, listed one by one in
`bench/run.py`'s `TARGETS`, because the full suite wants memcached, redis, a
browser and a dozen optional packages; `--parallel=1` is not a speed setting
but a correctness one, since `sys.monitoring` is per-interpreter and a forked
worker's edges would be recorded by nobody. It carries no floor: nothing has
watched this number long enough for one to mean anything.

Worth reading those rows together rather than reading the headline alone,
because on flask the headline has now sat at 0.29 through three changes that
moved the graph substantially.

#38 (following a package re-export, so `flask.Flask` resolves instead of
degrading to an ambiguous bare name) left **recall flat**: 765 -> 776 judgeable
edges found, 0.29 before and after. It could not move much. Recall counts an
edge the LOW bare-name fan-out would produce, and the fan-out already contained
the right answer — buried among the wrong ones. What changed is which tier the
answer is claimed at: recall at HIGH/MEDIUM 0.19 -> 0.25, conditional precision
0.86 -> 0.93.

#50 (resolving a call on a receiver whose type is annotated or assigned in the
same scope) moved the same two rows again, and one of them **down**. Scoring
one flask trace with the graph from before and after: recall 0.29 -> 0.29,
recall at HIGH/MEDIUM 673 -> 694 edges, and static HIGH edges with both
endpoints executed 490 -> 699 — of which the trace observed 455 -> 515. So
conditional precision fell **0.93 -> 0.74**: 209 more HIGH edges were put up
for judgement on a framework and only 60 of them were taken.

#54 asked what those 149 wrong claims were, by instrumenting the resolver to
record which mechanism produced each edge and scoring the same trace again.
The answer was one shape, not a spread: **148 of the 149 were calls to a
`@setupmethod`-wrapped flask method** — `Scaffold.route`,
`Blueprint.register_blueprint` — reached through `app = Flask(__name__)` or an
annotated `Blueprint`. Resolving the receiver had found the right class and
the right method; what it could not know is that `route` is decorated, so the
frame that opens at runtime is `setupmethod.<locals>.wrapper_func` and never
the decorated body. Split by whether the target carried a decorator, the
step's HIGH claims on flask were 60 right and 1 wrong undecorated, against 0
right and 148 wrong decorated. (The suspected cause — a class scored MEDIUM
for structurally satisfying a `Protocol` — was not involved in any of it, and
could not have been: flask defines no Protocol, and conditional precision
scores HIGH edges only.) So a receiver-resolved call to a decorated
definition is now MEDIUM rather than HIGH, which is the whole fix: recall and
recall at HIGH/MEDIUM are untouched at 0.29 and 0.26, no candidate is
dropped, and conditional precision returns to **0.93** (515/551).

#64 asked the same question of the step above, `self.X` resolved through the
class and its bases, which #54 had left alone. The same instrumentation gave
the same answer: **86 right and 3 wrong undecorated, against 0 right and 27
wrong decorated** — `@setupmethod` a third time, now reached from inside
flask's own methods rather than through a receiver. Worth noting is what the
split says about the tier, since `self.X` is the resolver's *strongest* step:
the class is the one the call is written in, not one inferred from an
annotation. That certainty is about which declaration the name reaches and
buys nothing at all against a wrapper — 0 right out of 27 — so the drop is
the same drop, and `super().X` takes the rule with it. Recall, recall at
HIGH/MEDIUM and the miss breakdown are again unchanged, edge for edge, and
conditional precision goes **0.93 -> 0.98** (515/524): 27 claims the trace
had always contradicted stopped being claimed at HIGH, and no right claim was
lost.

Together those four are the property of this benchmark most worth knowing:
**recall does not distinguish a fact from a lucky guess, and a change that
improves the tier an answer is claimed at can cost conditional precision
without touching recall at all** — in either direction, since #54 bought the
0.19 back and #64 another 0.05, and recall did not move for either. Only the
second number ever saw any of it happen.

On `tests/test_utils.py` alone — the scope #35 recorded — requests is **0.93**
recall (83/89), and every one of the 6 misses has a dunder as its target,
invoked by syntax (`d[k]`, `for x in jar`, `len(f)`). Adding
`test_structures.py`, which tests a mapping's dunders directly, drops it to
0.79: the same gap, weighted differently by which tests you run. **Recall is a
property of the target repository and of the suite you trace, not a single
number about codegraph** — which is why a floor is pinned to one target at one
scope, and why `--check-floors` refuses to run against a `--tests` override.

flask is where a static resolver is supposed to do badly, and it does. Every
miss is grouped by mechanism, and the grouping is the finding:

| flask misses (1908 of 2683) | |
|---|---|
| target nested in another function (a view defined inside a test) | 561 |
| reachable only through an out-of-repo frame | 552 |
| target is decorated (`@app.route`, `@setupmethod`) | 524 |
| target is a dunder, invoked by syntax or protocol | 223 |
| target is a constructor — a real resolution gap | 13 |
| target applied as a decorator by the source | 18 |
| no implicit-invocation mechanism recognised | 15 |
| call site attributed to another definition in the same file | 2 |

The 552 "out-of-repo frame" misses are worth understanding before reading the
0.29 as an indictment: those pairs are `test_x -> werkzeug's Client.get ->
FlaskClient.open`, where **no call site anywhere in flask's text names the
pair**. No static analysis of this repository could produce them, so they are
counted apart rather than blamed on the resolver. Excluding them still leaves
recall at 0.36. The honest summary is that on a framework, most of what runs is
reached by decoration and dispatch, and a call-site-based graph sees about a
third of it.

### Effectiveness floors

The floors that say the graph has not got worse on real code live here, on
`bench/` output, one set per target (#39):

```sh
uv run python -m bench.run requests --check-floors   # ~1 min
uv run python -m bench.run flask --check-floors      # ~3 min
```

| floor | psf/requests | pallets/flask |
|---|---|---|
| recall | 0.76 | 0.27 |
| recall at HIGH/MEDIUM | 0.74 | 0.24 |
| conditional precision | 0.95 | 0.95 |

An order of magnitude apart on recall, and that is the point: a library whose
tests call its functions by name and a framework whose tests reach their
targets by decoration and dispatch are not the same measurement, and one number
covering both would be a number about neither.

Each floor sits a little below a figure the benchmark actually printed, with
the run it came from recorded next to it in `bench/run.py`'s `TARGETS` — the
date, codegraph's commit, the target's commit, and the counts behind each
ratio. The headroom is for drift in the target repository, which moves on its
own; it is not slack for the resolver. A failure prints the target's commit
beside the score so the first question — was it the resolver that changed, or
the target? — is answerable from the output.

Both targets carry a floor, and both are enforced only by the command above.
They are deliberately not `pytest` tests, not even `slow` ones: a floor needs a
clone of somebody else's repository, a virtualenv, a network fetch and a few
minutes of their suite, and `uv run pytest -m slow` is a thing you can run on a
plane. What the default suite asserts about resolution is the fixture
regression guard, which is a different claim and is named like one.

### Why precision is reported as *conditional*

A static edge the trace never saw is **not** thereby wrong — the suite may
simply not cover it, and most of a library's surface is not exercised by its
own tests. Unconditional precision is therefore not measurable this way, and
the benchmark does not print a number for it. What is defensible: among static
HIGH edges whose **two endpoints both executed at least once**, how many did
the trace observe? 0.99 on requests, 0.98 on flask. That says most HIGH edges
that could have been checked were taken; it does not say the resolver invents no
edges. It is also the one number here that a resolution improvement can push
*down*, by claiming HIGH on more calls than it gets right — which is exactly
what #50 did and what #54 read back off it; see both above.

### The one filter that decides whether the number is honest

`PY_START` fires when a **module body** or a **class body** starts executing —
import time and definition time, not calls — and codegraph's CALLS edges do not
model either (it has an `imports` table for the first). So a traced edge is
judgeable only when its target is a *function or method* node. Without that
filter requests measures 0.51, with a miss list full of `__init__.py::<module>
-> api.py::<module>`: a wrong number that would send someone chasing a
non-problem. Two neighbouring cases are counted separately rather than folded
in, so that neither can quietly raise the score: a target that is a
comprehension or lambda (never a node, since `nodes` holds definitions), and a
target `nodes` does not contain at all (a hole in codegraph's own view, shown
with examples).

## Does querying beat grepping? (#59)

Everything above measures the *graph*: of the calls a run made, how many does the
graph have. `AGENTS.md` makes a different claim — query the call graph
**instead of** grepping for callers — and until #59 nothing here tested it. The
two can diverge in both directions. A graph at 0.29 recall is useful if the 29%
covers what people ask about; a perfect graph nobody's workflow reaches for is
worth nothing at any recall.

Two experiments, in ascending order of how much they cost and descending order
of how much they can be trusted.

### 1. The tool, against a run: `bench/discovery.py`

Deterministic. No model, no judge, no variance: anybody with the clone, the
trace and this file gets these numbers back.

For every symbol a trace can pose a caller question about, three ways of
answering "what calls this" are scored against the same trace:

- **grep-call** — `\bname\s*\(`, the pattern somebody hunting call sites types.
- **grep-word** — `\bname\b`, the fallback, which also finds a decorator and a
  reference passed as a value.
- **codegraph** — `impact --hops N`, with `codegraph-all` as the `--all`
  variant that merges the LOW-confidence bare-name fan-out in.

Both greps are modelled *at their best*: every matching line is attributed to
its enclosing definition by parsing the file, so grep never mis-reads a hit and
never gets bored at the fortieth match. `cost` is what the answer costs to
read — grep's matching lines, the query's printed rows — and `yield` is true
callers per line of that output.

```sh
uv run python -m bench.run flask                      # leaves a trace in --work
uv run python -m bench.discovery flask --work /tmp/codegraph-bench

uv run python -m bench.run django                     # ~6 min of django's ORM tests
uv run python -m bench.discovery django --work /tmp/codegraph-bench --package-root django/
```

pallets/flask `d73fa1c`, 43 symbols, 2026-09-21, codegraph `ef9f22d`:

| direct callers | recall | cond. precision | cost | yield |
|---|---|---|---|---|
| grep-call | 0.78 | 0.48 | 456 | 0.34 |
| grep-word | **1.00** | 0.29 | 1328 | 0.15 |
| codegraph | 0.72 | **0.61** | **257** | **0.55** |
| codegraph `--all` | 0.79 | 0.53 | 318 | 0.49 |

| within 2 hops | recall | cond. precision | cost | yield |
|---|---|---|---|---|
| grep-call | 0.43 | 0.14 | 2923 | 0.09 |
| grep-word | **0.79** | 0.07 | 15383 | 0.03 |
| codegraph | 0.58 | **0.37** | **1062** | **0.32** |
| codegraph `--all` | 0.67 | 0.27 | 1633 | 0.24 |

psf/requests `dae7ef6`, 10 symbols: every tool scores 1.00 at both depths.
The query costs 44 rows against grep-word's 77 at one hop and 117 against 328
at two, at conditional precision 1.00 against grep-word's 0.57 and 0.45. Ten questions on a small,
conventional library discriminate between nothing; they are here because a
result on one repository is an anecdote and this says so.

**The honest reading on flask: the graph does not find more callers than
grep.** A bare-name grep found every direct caller flask's suite observed, and
the query found 72% of them. Two hops out grep still wins on recall, 0.79 to
0.58.

What the query wins is the reading: 257 rows against 1328 matching lines at one
hop, 1062 against 15383 at two. Per line of output that is between 1.6 and 10
times as many true callers, depending on which of the two greps it is compared
with, at two to five times the conditional precision — and it says which edges
it is unsure of, which a grep hit cannot.

#### The second target: django (#71)

flask's result rested on one repository, and on the one where grep was least
likely to be embarrassed. django is the opposite case — 2,932 files, 124,623
static edges, an ORM that reaches methods through `Manager` / `QuerySet`
indirection and `__getattr__`, and names like `save`, `get` and `delete` in
every file in the tree. Either of grep's two advantages could have collapsed
there. It was traced (2026-09-23) for exactly that reason: `python -m bench.run
django` runs the ORM test apps listed in `bench/run.py`'s `TARGETS` — 5,120
tests, all passing — and observes **57,974 distinct call edges over 14,225
executed functions**. The question rule is the one flask was asked under,
unchanged: every symbol the trace can pose a caller question about, 542 of
them, none dropped.

django/django `951d13c`, 542 symbols, 2026-09-23, codegraph `5c246d5`:

| direct callers | recall | cond. precision | cost | yield |
|---|---|---|---|---|
| grep-call | 0.88 | 0.11 | 69027 | 0.031 |
| grep-word | **0.94** | 0.08 | 118652 | 0.020 |
| codegraph | 0.63 | **0.65** | **5173** | **0.302** |
| codegraph `--all` | 0.87 | 0.23 | 23078 | 0.093 |

| within 2 hops | recall | cond. precision | cost | yield |
|---|---|---|---|---|
| grep-call | 0.15 | 0.07 | 818756 | 0.014 |
| grep-word | **0.28** | 0.05 | 2538109 | 0.008 |
| codegraph | 0.06 | **0.65** | **15859** | **0.277** |
| codegraph `--all` | 0.13 | 0.26 | 97129 | 0.102 |

**django confirms flask on both counts, and makes one flask-specific claim
false.** Grep still wins recall — 0.94 to 0.63, or to 0.87 with `--all` — and
the query still wins the reading, by much more than it did on flask: 5,173 rows
against 118,652 matching lines at one hop, 15,859 against 2,538,109 at two.
That is 23x and 160x, where flask read 5x and 14x. The scaling the issue
guessed at is real, and it runs the way the tool needs it to.

What is new is that a bare-name grep is **no longer perfect**. On flask it
found every observed direct caller; on django it finds 94% of them, and on 20
of the 542 questions it finds *none*. Those twenty are the ORM's own
indirection: a method reached through `property(...)` applied in a class body
(`Model._set_pk_val`, `FieldFile._get_file`), a `partialmethod` attached to a
model class by `setattr` under a name assembled at runtime
(`Model._get_FIELD_display`, `method_set_order`), a lookup registered in a
class-level registry (`RegisterLookupMixin.register_instance_lookup`), and a
function handed to SQLite by name (`_sqlite_regexp`). No text in the repository
puts the caller and the callee in the same search.

It does not follow that the graph finds them. It does not: on all 139 of those
edges `codegraph` scores zero too, and across the whole django question set
there is **not one observed caller the query found that neither grep found**.
Every true caller in the query's answer is in grep's. So the claim #59 corrected stays
corrected, and gains a caveat rather than losing one: "grep finds every caller"
was true of flask and is false of django, but what grep misses on django is
missed by the static graph as well. Only `codegraph trace` reaches it.

What the resolver misses on flask is one shape, and it is the same shape
`bench/`'s 0.29 is made of: a property read as an attribute (`request.blueprint`
has no call syntax to resolve), a decorator applied in a class body
(`@setupmethod`), and a method reached through a context-local proxy
(`g.setdefault`). grep-word finds all three, because all three put the name in
the text — which is exactly why "grep misses dynamic dispatch" was the wrong
claim to make for *direct* callers, and why this section replaced it.

Two honest caveats on the django columns. The query is asked at
`bench/discovery.py`'s `LIMIT`, 500 rows, and django is big enough for that to
bind: 1 of 542 questions came back at the cap at one hop and 7 at two (13 and
96 for `--all`), so those recall figures are a floor rather than the resolver's
ceiling. And the two-hop gold sets average 138 callers per symbol, which is a
listing rather than an answer — every tool collapses there, and the column
worth reading at that depth is the cost.

Where neither can win: a call whose two frames are separated by an out-of-repo
frame. Those are excluded from the gold set here, because no text in the
repository names the pair and grading it would reward guessing — which biases
this comparison *against* the graph and is the direction to err in when
grading your own tool. Only `codegraph trace` finds those at all.

### 2. The agent, with and without the tool: `bench/agent.py`

The A/B the issue asked for, at the smallest scale that can say anything. One
fixed agent, two arms, held equal in model, turn cap, spend cap, prompt,
repository and revision:

- **control** — `Read`, `Glob`, `Grep`.
- **treatment** — the same three, plus `codegraph impact` / `effects` /
  `orphans`, served as MCP tools by `bench/agent_tools.py`.

Nothing tells the treatment arm that `codegraph` exists; it sees three more tools
in its tool list and nothing else changes. Telling one arm about the tool in
its prompt would have made the prompt the variable under test.

```sh
uv run python -m bench.agent flask --repo ... --trace ... --runs 3 --out runs.jsonl
```

12 symbols x 2 arms x 3 runs = 72 runs, sonnet, 15-turn cap, flask `d73fa1c`,
2026-09-21. The question has a set of symbols as its answer and is graded by
set overlap with the trace, so there is no judge whose agreement rate has to be
published — what has to be checked instead is the parse, below.

The question is the one-hop one, "what calls this directly". A pilot at two
hops was abandoned before it was run at scale: flask's two-hop gold sets run to
dozens of symbols, no agent enumerates dozens of symbols inside a budget worth
paying for, and both arms scored 0.12 on the pilot question for that reason
rather than for any reason about tools. Enumerating is free in the
deterministic comparison above, which is where the two-hop numbers are.

| | recall (36 runs per arm) | median | turns/run | $/run |
|---|---|---|---|---|
| control | 0.906 | 1.00 | 5.39 | 0.033 |
| treatment | 0.917 | 1.00 | **4.44** | **0.024** |

Paired by question, treatment − control: recall **+0.010** (95% bootstrap CI
+0.000 to +0.031), turns **−0.94** (CI −1.78 to −0.14). Eleven of the twelve
questions tie exactly; one (`stream_with_context`) goes 0.88 → 1.00.

**The honest reading: the tool did not make the answers better, and it made
them cheaper.** A one-percent recall difference on twelve questions is not a
finding about answer quality; a quarter off the turns and the cost, consistent
across questions, is a finding about how much reading it takes to get there —
which is the same thing the deterministic table says, arrived at independently.
The treatment arm used `impact` in 36 of 36 runs, so the null result is not a
tool nobody reached for.

**The grader's reliability.** There is no judge to agree with, so what can go
wrong is the parse: an answer that names a caller in a form the regex does not
recognise is scored as a miss the agent did not make. Sampled one run per
question with a seeded draw (12 of the 72) and read each raw answer against
its parsed set: 12 of 12 matched exactly, in both directions. That is the
reliability figure, it is small, and it is the whole of it — every run is in
`bench/results/flask-agent-2026-09-21.jsonl` and `--replay` re-scores them
without spending anything, so the check can be repeated or widened by anyone:

```sh
uv run python -m bench.agent flask --repo ... --trace ... \
    --out bench/results/flask-agent-2026-09-21.jsonl --replay
```

Both arms score 0.00 on `setupmethod` and drag both averages down by the same
amount. That one is a question-framing artefact rather than a failure: the
caller of a decorator, as `sys.monitoring` sees it, is the *class body* that
applies it, and no agent in either arm answers "the class body" — they list the
decorated methods. `codegraph` misses it too.

### What neither experiment measures

- **`effects` and `orphans` are untested.** The trace records in-repo frames,
  so it cannot witness `open()` or `os.environ`, and a gold answer for "what
  side effects does this reach" would have to be hand-written against the same
  source the tool reads. That is the self-marking problem this whole section
  exists to avoid, so it was left undone rather than done badly.
- **Two repositories do the work, and only for the deterministic half.**
  requests is in the tables and is too small and too well-behaved to
  discriminate. django is now in them, which is what #71 was for; the agent A/B
  below still rests on flask alone, and nothing here is a second *framework*
  beyond django.
- **A trace is a lower bound.** A caller the suite never exercised is missing
  from the gold set, and a tool that finds it is marked down for being right.
  That falls on both sides equally, and it is why the column beside recall is
  *conditional* precision (same argument as above).
- **The agent arm is 72 runs of one model at one turn cap**, on questions whose
  answers are sets of names. It says nothing about prose answers, about harder
  questions, or about a budget tight enough for the cost difference to become a
  quality difference.

## Why no MCP server

MCP's main advantage is discoverability — the agent sees the tool without
being told. `SKILL.md` already provides that, and it only loads into context
when relevant, whereas MCP tool definitions occupy context on every request
whether used or not. A CLI also works in any harness, composes with `jq` and
shell pipelines, and needs no server process. MCP is an explicit non-goal for
this version; it could become an additive wrapper later for harnesses without
shell access, but nothing here requires it.

`bench/agent_tools.py` is an MCP server and is not a change of position: the
A/B above needs two arms that differ in *tool availability* and in nothing
else, and a tool list is the only place a Claude Code session can be given a
tool without also being told about it in prose. It exists to run an experiment,
it is not installed by anything, and it wraps the same CLI handlers.

## The anti-pattern this displaces

Do not grep for callers *and then read every hit*. Grep gives you a superset
with no way to know when you are done — no signal separates the last caller
from the last line its pattern happened to match — and that superset gets more
expensive the bigger the repository is: on flask it is five times the reading
for the same answer (1328 matching lines against 257 rows, at one hop; 15383
against 1062 at two), on django twenty-three times (118652 against 5173) and at
two hops a hundred and sixty (2538109 against 15859).

The claim this section used to make — that grep *misses* dynamically
dispatched calls — did not survive being measured, and the second target did
not rescue it. Dispatch through a property, a decorator or a proxy still puts
the name in the text: a bare-name grep found every caller flask's suite
observed, including the ones codegraph's resolver could not, and on django it
found 94% of them and every single one the graph found. There are callers
neither finds — django's ORM attaches methods to model classes under names
assembled at runtime, and no search of the text and no static graph connects
those two ends — but that is a case for `codegraph trace`, not for `impact`.
`codegraph impact` walks the graph and reports the set, its ranking and its
confidence per edge; it does not report more callers than grep does.
