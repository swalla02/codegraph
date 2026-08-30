# codegraph

A standalone navigation layer over a Python codebase — a sidecar index, in the
spirit of `.git/`, that never changes how you write code and never asks the
code to import it.

It answers two questions grep structurally cannot:

- **`impact`** — what transitively depends on this symbol, ranked, with the
  blast radius summarized rather than dumped.
- **`effects`** — does anything downstream write the database, hit the network,
  touch the filesystem, or mutate global state — each claim backed by a witness
  path to the exact `file:line` that causes it.

Composed, they give the answer you actually want before a change: not "47 things
call this," which is anxiety rather than information, but *"47 things call this,
and 3 of the paths end in a database write."*

```
codegraph status                    # freshness, counts, dirty files
codegraph index [--rebuild]         # force full build
codegraph impact <sym> [--hops N] [--all] [--json]
codegraph effects <sym> [--json]
codegraph diff [<base>..<head>] [--json]
codegraph resolve <query>           # fuzzy name -> node ids
codegraph gc                        # prune unreachable cache entries
codegraph install-hooks             # optional cache warming
```

## It follows git

The parse cache is content-addressed by git blob SHA, so a file's content is
analysed once for the life of the repository and shared across every branch.

| Action | Blobs parsed |
|---|---|
| Create a branch | 0 |
| Switch to a branch you've seen | 0 |
| Switch, 12 files differ | ≤ 12 |
| First index ever | whole repo |

After the first index, work is proportional to the diff, never to repository
size. That is a tested property, not a claim.

It also makes `codegraph diff` possible — the semantic delta of a branch: which
symbols and edges changed, and which side effects newly became reachable.

> *"This branch puts a `DB_WRITE` downstream of `checkout`, which 14 symbols
> depend on."*

## Design

Two storage layers in one SQLite file under `.codegraph/`, which self-ignores so
the host repository stays untouched. Layer 1 is an immutable parse cache keyed
on blob SHA; Layer 2 materializes a resolved graph for a revision. Every command
reconciles the working tree before answering, so the index is never stale.

Static analysis only, Python only, stdlib only. Built CLI-first for agents,
installable as a Claude Code plugin
(`/plugin marketplace add swalla02/codegraph`), with a schema a visual navigator
can project from later.

## Status

Pre-implementation — design and plan are complete, no source yet.

- [Design spec](docs/superpowers/specs/2026-08-29-codegraph-design.md)
- [Implementation plan](docs/superpowers/plans/2026-08-29-codegraph-v1.md) — 15
  tasks across four PRs
