# codegraph — Design

**Date:** 2026-08-29
**Status:** Approved, pre-implementation

## Problem

A codebase is navigated today as a collection of files. Agents explore it with
grep, glob, and read; humans do roughly the same by hand. That works because
identifiers are near-unique, so grep acts as a crude reference resolver. It
fails on the questions that matter most before a change:

- **Impact.** "What breaks if I change this function?" is a closure over the
  call graph. Grep gives one hop and no termination condition — you never know
  whether you have seen all callers or merely the ones that matched.
- **Side effects.** "Does anything downstream of this write to the database?"
  requires transitive reasoning grep cannot perform at all.

codegraph is a standalone navigation and tracking layer over a codebase: a
sidecar index, in the spirit of `.git/`, that never changes how you write code
and never asks the code to import it.

## Goals

1. Answer `impact_of(symbol)` and `effects_of(symbol)` more completely and far
   more cheaply than grep.
2. Stay fresh automatically, updating only what changed, like `git status`.
3. Leave zero footprint in the host repository.
4. Serve agents first (CLI + MCP), with a schema that a visual navigator can
   later project from.

## Non-goals for v1

Runtime tracing. Any language but Python. Visualization. `path_between`.
`neighbors` as a shipped surface. Cross-service edges.

`neighbors` exists internally as a graph primitive and comes for free from the
schema; it is simply not a supported product surface in v1.

## Decisions

### D1 — Static analysis only

Runtime tracing gives ground truth for dynamic dispatch, but it requires
executing the code and a per-language harness, which conflicts with being a
passive sidecar. The schema reserves an edge `provenance` field
(`static` | `observed` | `both`) so traces can be layered on later without
migration, but v1 populates only `static`.

### D2 — Resolution engine: stdlib `ast` + scope-aware heuristics

Rejected: jedi/pyright inference (heavy dependency, inherits their failure
modes, precision not yet known to be needed) and tree-sitter (strictly worse
than `ast` for Python; buys multi-language optionality before the
single-language case is proven).

Resolution order at a call site: local scope, then enclosing scopes, then
module scope, then imported names. `self.foo()` resolves through the enclosing
class and its MRO. `obj.foo()` with a non-inferable receiver falls back to
"any method named `foo`" as a LOW-confidence edge.

Over-approximation is the correct bias here: for "what might break," a
spurious edge is far cheaper than a missing one, provided confidence is
recorded so ranking can demote it.

A `Resolver` interface (`resolve_call(callsite, context) -> [(target, confidence)]`)
is defined from the start, so a more precise engine is a swap-in and another
language is an added backend — neither is a rewrite.

### D3 — Edges are owned by the call-site file

An edge belongs to the file containing the call site, not the file containing
the target. This is what makes incremental invalidation tractable (see D4).

### D4 — Two-phase indexing

Naive per-file hashing is wrong: editing `service.py` invalidates edges that
*live in* `handlers.py`. Splitting parse from resolve fixes this.

- **Phase 1 (parse)** — pure and per-file: file -> nodes + *unresolved* call
  references. A reference records the name as written plus import provenance
  (`from pay.service import charge` -> `pay.service.charge`), never a resolved
  target. Cached on file content hash with no cross-file dependency.
- **Phase 2 (resolve)** — unresolved references -> edges, against the global
  symbol table.

Re-resolution therefore touches `D ∪ dependents(D)` without re-parsing the
dependents. This is salsa's red-green invalidation, scoped down.

### D5 — Lazy refresh on query

Every query first reconciles the working tree, then answers. No daemon, no git
hooks, never stale — the `git status` model. Explicit `index` exists only to
pay the cold-build cost up front.

Rejected: explicit-command-only (index rots silently — the failure mode that
kills these tools), git hooks (blind to uncommitted edits, which is exactly the
code you most want impact analysis on), watch daemon (process lifecycle and
stale-lock complexity for v1).

## Architecture

### Data model

Nodes are **symbols**, not files. Stable ID is `path::qualname`, e.g.
`src/pay/service.py::PaymentService.charge`. Line numbers are attributes, never
identity — otherwise every upstream edit churns IDs and unchanged code appears
to have changed. Kinds: `module`, `class`, `function`, `method`.

Edges: `(src, dst, kind, confidence, provenance, callsite_file, callsite_line)`.
Kinds: `CALLS`, `IMPORTS`, `INHERITS`, `CONTAINS`.

Confidence tiers:

| Tier | Meaning |
|---|---|
| `HIGH` | Exact: local, module, or imported name; `self.method` via MRO |
| `MEDIUM` | Unique method name repo-wide |
| `LOW` | Ambiguous name with several candidates |

Effects are a node property table, not edges:
`(node_id, effect_kind, evidence_line, direct)`.

### Storage

SQLite at `.codegraph/graph.db` (stdlib `sqlite3`; no external dependency),
WAL mode. Tables: `files`, `nodes`, `edges`, `effects`, `unresolved_calls`,
`imports`, `meta` (schema version). The load-bearing index is on `edges(dst_id)`,
which is what `impact_of` traverses.

`.codegraph/` contains a `.gitignore` holding `*`, so the index self-ignores
without ever modifying the host repo's `.gitignore`.

### Indexing pipeline

1. Enumerate tracked files — `git ls-files` inside a repo, otherwise walk plus
   `.gitignore` handling.
2. Fast-path `stat` on `(size, mtime_ns)`; blake2b hash only on mismatch.
3. Dirty set **D** = changed + added + deleted.
4. Re-parse **D** (phase 1).
5. Re-resolve **D ∪ dependents(D)** (phase 2), where `dependents` is a single
   indexed query against `imports`.

Deletes and renames fall out naturally: a removed file drops its nodes and its
owned edges, and references into it become unresolved and are *reported*
rather than silently disappearing.

## Effect analysis

Taxonomy: `DB_READ`, `DB_WRITE`, `NETWORK`, `FS_READ`, `FS_WRITE`, `PROCESS`,
`ENV_READ`, `GLOBAL_MUTATE`, `NONDETERMINISM`.

Direct detection is catalog-driven, matching dotted-prefix patterns against
resolved callee names:

```toml
[[effect]]
match = "requests.*"
kind  = "NETWORK"

[[effect]]
match = "*.session.commit"
kind  = "DB_WRITE"
```

A built-in catalog ships with the tool (stdlib, requests/httpx, sqlalchemy,
psycopg, boto3). `.codegraph/effects.toml` merges over it. That override is
essential rather than optional: in a real codebase most side effects sit behind
house abstractions, not behind `requests` directly.

`GLOBAL_MUTATE` is detected syntactically (`global`/`nonlocal` statements,
assignment to module-level names from within a function). This is documented as
partial — it does not catch mutation through an aliased object.

Propagation: `effects(n) = direct(n) ∪ ⋃ effects(callees)`, over the `CALLS`
closure. Cycles are handled by condensing strongly-connected components
(Tarjan) so every member of a recursion cycle shares the union. Confidence
propagates as the minimum along the path.

Every reported effect carries a **witness path**: the shortest chain from the
queried symbol to the concrete call site producing it, ending at `file:line`.
An effect claim that cannot be verified in one click is one users stop trusting
after the first false positive.

## Query semantics

### `effects_of(symbol)`

Returns each reachable effect kind with its confidence and witness path, sorted
by severity then confidence.

### `impact_of(symbol, max_hops=3)`

Reverse BFS over `CALLS`. Each hit carries hop distance, minimum path
confidence, and a witness path. Ranking combines hop distance (dominant),
path confidence, and node salience (entry point, public vs. underscore-private,
fan-in).

Over-reporting is the primary failure mode — reverse reachability touches half
a real repo by hop 3 — so output is shaped against it:

1. **Summary first, list second.** *"47 symbols across 12 modules · 9 entry
   points · 3 paths newly reach DB_WRITE · 31 low-confidence hits not shown."*
2. **Tests bucketed separately.** "12 tests cover this" is a different fact from
   "12 things depend on this"; merging them corrupts ranking and hides real
   dependents.
3. **LOW-confidence hits counted but not listed** by default. Nothing is
   dropped; `--all` reveals them.

Defaults: 3 hops, ~40 nodes, explicit `truncated: true` in JSON output.

### Composition

The two queries compose, and that composition is the product: impact results
annotated with the effects each dependent path carries. Not "47 things call
this," which is anxiety rather than information, but *"47 things call this, and
3 of the paths end in a database write."*

## Surface

```
codegraph status                    # freshness, node/edge counts, dirty files
codegraph index [--rebuild]         # force full build
codegraph impact <sym> [--hops N] [--all] [--json]
codegraph effects <sym> [--json]
codegraph resolve <query>           # fuzzy name -> node ids
codegraph serve-mcp                 # stdio MCP server
```

`resolve` is a first-class primitive. Nobody types
`src/pay/service.py::PaymentService.charge`; they type `charge`. Every query
accepts a loose symbol and either resolves uniquely or returns ranked
candidates. It never guesses — a wrong guess yields a confidently wrong impact
report, which is worse than no tool.

MCP tools: `resolve_symbol`, `impact_of`, `effects_of`. Their descriptions must
state when to use them *instead of* grep; an MCP tool an agent does not reach
for is dead weight.

One core, two renderers: compact text for humans, token-budgeted JSON for
agents.

### Layout

```
codegraph/
  cli.py  mcp_server.py  store.py  scanner.py
  parse.py            # phase 1
  resolve.py          # phase 2: Resolver interface + AstResolver
  effects/            # catalog.py detect.py propagate.py
  query/              # impact.py effects.py rank.py
  render.py
tests/fixtures/
```

Python 3.12, `uv`, pytest, ruff, console script `codegraph`. No runtime
dependencies beyond the stdlib for the core; the MCP SDK is required only by
`serve-mcp`.

## Error handling

- **Unparseable file** (syntax error, Python 2, generated): recorded in `files`
  with an `error` status, excluded from the graph, surfaced in `status`. Never
  fails the whole index.
- **Unresolved reference**: retained in `unresolved_calls` and counted in
  `status`, never silently discarded. A rising unresolved count is the primary
  health signal that resolution is degrading.
- **Schema version mismatch** in `meta`: automatic full rebuild.
- **Concurrent access**: SQLite WAL plus a lock file; a second writer waits
  rather than corrupting.
- **Ambiguous symbol** in a query: return candidates and a non-zero exit code.

## Testing

**The invariant the project rests on: incremental result ≡ cold rebuild.** A
property test indexes a fixture repo, applies a randomized sequence of edits,
adds, deletes, and renames, re-indexes incrementally, and asserts the resulting
database is identical to a from-scratch build. If this drifts, every answer
becomes irreproducible and the tool is worthless regardless of analysis
quality. Written early, not last.

Also:

- **Resolution goldens** on fixture packages — relative imports, aliases,
  `__init__.py` re-exports, `self` via MRO, inheritance, decorators.
- **Propagation tests** — SCC cycles, min-confidence, witness-path correctness.
- **Ranking goldens**, so ranking changes appear as reviewable diffs rather
  than silent behavior drift.
- **Accuracy harness** — labeled call sites with expected targets, reporting
  precision and recall, so "is jedi worth it?" becomes an empirical question
  rather than an argument.
- **Perf smoke** (marked slow) — cold-index a real repo such as flask; assert
  warm query latency under ~300ms.

## Risks

| Risk | Mitigation |
|---|---|
| `impact_of` over-reports and gets ignored | Ranking, hop cap, summary-first output, LOW tier counted not listed |
| Effect catalog misses house abstractions | Project-level `effects.toml` override; unresolved/untagged counts in `status` |
| Index goes stale and loses trust | Lazy refresh on every query; the incremental ≡ rebuild invariant test |
| Dynamic dispatch limits accuracy | LOW-confidence over-approximation rather than omission; `provenance` field reserved for later runtime traces |
| Agents do not adopt the MCP tools | Tool descriptions explicitly contrast with grep; token-budgeted output |
