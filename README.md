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

It follows git. The parse cache is content-addressed by blob SHA, so a file's
content is analysed once for the life of the repository and shared across every
branch. Creating a branch costs nothing; switching to one you've seen re-parses
nothing. After the first index, work is proportional to the diff, never to
repository size.

That also makes `codegraph diff` possible — the semantic delta of a branch:
which symbols and edges changed, and which side effects newly became reachable.

Built agent-first: a CLI and an MCP server, installable as a Claude Code plugin
(`/plugin marketplace add swalla02/codegraph`), with a schema a visual
navigator can project from later.

**Status:** pre-implementation. See
[the design spec](docs/superpowers/specs/2026-08-29-codegraph-design.md).
