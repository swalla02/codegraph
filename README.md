# codegraph

A standalone navigation layer over a Python codebase — a sidecar index, in the
spirit of `.git/`, that never changes how you write code and never asks the
code to import it.

It answers two questions grep structurally cannot:

- **`codegraph impact <symbol>`** — what transitively depends on this, ranked,
  with the blast radius summarized rather than dumped.
- **`codegraph effects <symbol>`** — does anything downstream write the
  database, hit the network, touch the filesystem, or mutate global state —
  each claim backed by a witness path to the exact `file:line` that causes it.

Composed, they give the answer you actually want before a change: not "47 things
call this," which is anxiety rather than information, but *"47 things call this,
and 3 of the paths end in a database write."*

It follows git. The parse cache is content-addressed by git blob SHA, so a
file's content is analysed once for the life of the repository and shared
across every branch: creating a branch changes no blobs, so it costs zero
parsing, and switching to a branch whose blobs have already been seen also
costs zero parsing — this half of the cost guarantee is measured, not just
claimed (see "The cost guarantee, honestly" below).

That also makes `codegraph diff` possible — the semantic delta of a branch:
which symbols and edges changed, and which side effects newly became
reachable.

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
for by name.

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
| `codegraph index [--rev REV] [--rebuild] [--quiet]` | Reconcile a revision into the graph explicitly, paying the cold-build cost up front. `--rebuild` discards the Layer 1 parse cache first; `--quiet` is what the warming hooks invoke in the background. |
| `codegraph resolve <name>` | Fuzzy-match a name (trailing name, qualname, or full node id) to node ids. |
| `codegraph effects <symbol> [--json]` | Report every side-effect kind reachable from a symbol, each with a witness chain down to the causing `file:line`. |
| `codegraph impact <symbol> [--hops N] [--limit N] [--all] [--json]` | Report the ranked dependents of a symbol — everything a change to it could break. |
| `codegraph islands [--rev REV] [--limit N] [--json]` | Report the connected components of the revision's `CALLS` edges, read as undirected: how many separate regions the codebase is in, how big each is, and which symbols anchor them, plus what the tool can say about why each one stands apart (implicit invocation, a `NETWORK` boundary, or nothing it recognises). An island of one is *not* a dead-code finding (see below). |
| `codegraph diff [<base>..<head>] [--json]` | Report what changed between two revisions by content hash, never by line number: symbols added/removed/changed, plus any side effect newly reachable. Defaults to `merge-base(default branch, HEAD)..WORKTREE` — "what has this branch changed so far." |
| `codegraph gc [--keep REV]` | Prune the Layer 1 parse cache down to what `HEAD`, the worktree, and any `--keep`-named revisions still reference. Never touches the graph itself, so it can only make a future answer slower to rebuild, never wrong. |
| `codegraph init` | Make this repository's coding agents aware of codegraph: an `AGENTS.md` section, the `@AGENTS.md` bridge into an existing `CLAUDE.md`, and a commented `codegraph.toml` stub. Idempotent; never overwrites content it did not write; never touches `.git/`. |
| `codegraph guide` | Print the agent-facing workflow to stdout — the same text the plugin ships as `SKILL.md`, so the short `AGENTS.md` section can defer to it rather than inline it. |
| `codegraph install-hooks` | Install `post-commit`/`post-checkout`/`post-merge` git hooks that warm the cache in the background. Purely an optimization — every query reconciles the working tree itself regardless (see below), so results are identical whether or not a hook ever fires. |

### What an island is, and is not

`codegraph islands` treats call edges as undirected and splits the graph
into connected components. That the call graph is *not* connected is real
structure, not a defect: a service boundary, a config-gated region, and
code nothing references all show up as separate islands. On psf/requests
it reports 807 symbols in 153 islands, the biggest holding 647 of them and
149 being islands of exactly one.

**An island is not a reachability result, and a one-symbol island is not
dead code.** Membership comes from the call edges the resolver recorded,
and a great deal of Python is invoked by mechanisms that leave no call site
in the source: dunders (`__delitem__` runs on every `del d[k]`),
decorators, framework dispatch, ABC overrides, packaging entry points.
`AuthBase.__call__` and `CaseInsensitiveDict.__delitem__` are each an
island of one on requests, and neither is unused.

So each island is labelled with what can be said about why it stands
apart, and no label is ever a claim that code is dead:

- **`implicit: dunder, decorator, test, override, nested, import`** — a
  mechanism found among the island's members by which something could reach
  it without a call site. Not proof that it runs; counter-evidence to
  "nothing reaches this". 124 of requests' 153 islands carry at least one.
- **`boundary: NETWORK`** — a path inside the island leaves the process.
  The handler lives in another repository, so the island boundary *is* the
  service boundary. That is signal, not a false positive. `NETWORK` is
  deliberately the only effect kind that marks one: a socket call leaving
  the process is a structural fact, whereas coupling two functions through
  a database means reading SQL and tracking a schema, which is a different
  tool.
- **`no implicit-invocation mechanism recognised`** — the remainder, 29
  islands on requests, and still a statement about the tool rather than
  about the code. Most of them are the library's own public surface
  (`get_dict`, `dict_from_cookiejar`), called by users of the package and
  by the stdlib — neither of which is in the tree.

One thing that moved the numbers is worth naming separately, because it was
a plain bug rather than a limit of static analysis: `Cls()` resolves to the
class and nothing in the source ever spells `Cls.__init__`, so constructors
had no incoming edge at all. Implying that edge (following the MRO for an
inherited one) folded 18 of requests' 172 islands into the rest of the
graph.

`resolve`, `impact`, and `effects` share one exit-code convention for
resolving `<symbol>` to a node id: `0` = a single unambiguous match, `1` =
nothing matched, `2` = more than one match (every candidate is printed; pick
the right one and re-run with the full node id).

`islands` takes no symbol, so that convention does not apply to it: it
exits `0` for a report and `1` only for a revision it cannot resolve.

All commands accept `--path <dir>` to run against a different repository
root (default: the current directory).

## Confidence, and what earns it

Every edge carries HIGH, MEDIUM or LOW. The tier is not a score, it is a claim
about *how the target was identified*, and the resolver tries the steps below in
order and stops at the first that matches.

| step | tier | example |
|---|---|---|
| the name is imported | HIGH | `from pay import charge` then `charge()` |
| the name is defined in this module | HIGH | a module-local `def charge` |
| `super().X()` | HIGH | resolved through the enclosing class's bases, skipping the class itself |
| `self.X()` | HIGH + MEDIUM | the inherited method at HIGH, every subclass override at MEDIUM |
| `Cls()` | inherits the class edge's tier | plus an edge to the `__init__` it would run, found up the MRO |
| a bare name matched against the whole repository | MEDIUM if unique, else LOW | `item.save()` where nothing says what `item` is |

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

## How good is the graph, honestly

`tests/test_accuracy.py` reports precision 1.00 / recall 1.00, and that number
means very little: it measures 10 hand-written call sites in a synthetic
11-file repository, each authored to illustrate a rule the resolver already
implements. It is a regression guard wearing a benchmark's clothes.

`bench/` is the real measurement (#35). It runs a target repository's **own
test suite** under `sys.monitoring`, records every `(caller, callee)` pair that
actually executed, and scores the static graph against it. A call the tests
made is a call that exists, so a traced edge missing from the static graph is a
real gap — no labelling judgement involved.

```sh
uv run python -m bench.run requests          # clones, or --source-root DIR to copy a clone
uv run python -m bench.run flask --json /tmp/flask.json
uv run python -m bench.run requests --tests tests/test_utils.py   # narrow the suite
```

Each run copies the clone (an editable install writes into the tree, and the
source clone must stay untouched), builds a venv, installs the target
**editable** — a normal install copies the source into `site-packages`, where
every in-repo frame is then filtered out as external and the trace comes back
nearly empty, silently — traces the suite, indexes the same working tree, and
scores. Nothing about it runs during `pytest -q`; only the scorer's arithmetic
is unit-tested there (`tests/test_bench_scorer.py`).

### What it measures

| | psf/requests | pallets/flask |
|---|---|---|
| suite traced | `test_utils.py`, `test_structures.py` (240 tests) | `tests/` (494 tests) |
| traced call edges (judgeable) | 115 | 2683 |
| **recall** | **0.79** | **0.29** |
| recall at HIGH/MEDIUM | 0.76 | 0.19 |
| conditional precision | 0.99 (81/82) | 0.86 (206/239) |

On `tests/test_utils.py` alone — the scope #35 recorded — requests is **0.93**
recall, and all 6 misses are dunders invoked by syntax (`d[k]`, `for x in jar`,
`len(f)`). Adding `test_structures.py`, which tests a mapping's dunders
directly, drops it to 0.79: the same gap, weighted differently by which tests
you run. **Recall is a property of the target repository and of the suite you
trace, not a single number about codegraph.**

flask is where a static resolver is supposed to do badly, and it does. Every
miss is grouped by mechanism, and the grouping is the finding:

| flask misses (1918 of 2683) | |
|---|---|
| target nested in another function (a view defined inside a test) | 564 |
| reachable only through an out-of-repo frame | 552 |
| target is decorated (`@app.route`, `@setupmethod`) | 523 |
| target is a dunder, invoked by syntax or protocol | 223 |
| target is a constructor — a real resolution gap | 21 |
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

### Why precision is reported as *conditional*

A static edge the trace never saw is **not** thereby wrong — the suite may
simply not cover it, and most of a library's surface is not exercised by its
own tests. Unconditional precision is therefore not measurable this way, and
the benchmark does not print a number for it. What is defensible: among static
HIGH edges whose **two endpoints both executed at least once**, how many did
the trace observe? 0.99 on requests, 0.86 on flask. That says the HIGH edges
that could have been checked were taken; it does not say the resolver invents no
edges.

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

## Why no MCP server

MCP's main advantage is discoverability — the agent sees the tool without
being told. `SKILL.md` already provides that, and it only loads into context
when relevant, whereas MCP tool definitions occupy context on every request
whether used or not. A CLI also works in any harness, composes with `jq` and
shell pipelines, and needs no server process. MCP is an explicit non-goal for
this version; it could become an additive wrapper later for harnesses without
shell access, but nothing here requires it.

## The anti-pattern this displaces

Do not grep for callers. Grep misses dynamically dispatched calls (anything
reached through a method resolution order, an attribute, or an alias) and
gives you no way to know when you are done — there is no signal that you have
found the last caller versus just the last one grep's pattern happened to
match. `codegraph impact` walks the actual call graph and reports both the
full set and how confident it is in each edge.
