# codegraph

A standalone navigation layer over a Python codebase — a sidecar index, in the
spirit of `.git/`, that never changes how you write code and never asks the
code to import it.

It answers two questions grep structurally cannot:

- **`impact_of(symbol)`** — what transitively depends on this, ranked, with the
  blast radius summarized rather than dumped.
- **`effects_of(symbol)`** — does anything downstream write the database, hit
  the network, touch the filesystem, or mutate global state — each claim backed
  by a witness path to the exact `file:line` that causes it.

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

Built agent-first: a CLI, installable as a Claude Code plugin
(`/plugin marketplace add swalla02/codegraph`), driven through `SKILL.md`
rather than an MCP server (see "Why no MCP server" below), with a schema a
visual navigator can project from later.

**Status:** implemented and in use. See
[the design spec](docs/superpowers/specs/2026-08-29-codegraph-design.md) for
the full rationale behind every decision below.

## Install

Requires Python 3.12+.

```sh
pip install /path/to/codegraph   # or: uv pip install /path/to/codegraph
```

This installs the `codegraph` console script (`pyproject.toml`'s
`[project.scripts]` entry point). Not yet published to PyPI — install from a
checkout or a git URL until it is.

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
| `codegraph diff [<base>..<head>] [--json]` | Report what changed between two revisions by content hash, never by line number: symbols added/removed/changed, plus any side effect newly reachable. Defaults to `merge-base(default branch, HEAD)..WORKTREE` — "what has this branch changed so far." |
| `codegraph gc [--keep REV]` | Prune the Layer 1 parse cache down to what `HEAD`, the worktree, and any `--keep`-named revisions still reference. Never touches the graph itself, so it can only make a future answer slower to rebuild, never wrong. |
| `codegraph install-hooks` | Install `post-commit`/`post-checkout`/`post-merge` git hooks that warm the cache in the background. Purely an optimization — every query reconciles the working tree itself regardless (see below), so results are identical whether or not a hook ever fires. |

`resolve`, `impact`, and `effects` share one exit-code convention for
resolving `<symbol>` to a node id: `0` = a single unambiguous match, `1` =
nothing matched, `2` = more than one match (every candidate is printed; pick
the right one and re-run with the full node id).

All commands accept `--path <dir>` to run against a different repository
root (default: the current directory).

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

It lives at the repository root, not inside `.codegraph/`, because it is
hand-written configuration meant to be committed and shared, whereas
`.codegraph/` self-ignores and holds only derived cache: config is tracked,
everything derived is disposable.

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
- **Resolve (Layer 2) — not yet proportional.** `resolve_revision` currently
  re-resolves the *entire* symbol table for a revision on every reconcile,
  rather than the design's intended `D ∪ dependents(D)` incremental
  invalidation (`resolve.dependents` exists and is tested, but has no
  production caller yet). For a small-to-medium repository this is fast
  enough not to notice; for a large one, the resolve step — not the parse
  step — is where reconcile time will scale with repository size today, not
  with the size of your diff. Tracked as follow-up work; this README will
  stop calling this out honestly the day it's fixed, not before.

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
