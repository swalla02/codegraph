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
and never asks the code to import it. It follows the repository through
commits, pulls, and branch changes, applying deltas rather than rebuilding.

## Goals

1. Answer `impact_of(symbol)` and `effects_of(symbol)` more completely and far
   more cheaply than grep.
2. Follow git: one graph for the repository, updated by deltas on every commit,
   pull, and checkout. **After the first index, work is proportional to the
   diff, never to repository size.**
3. Report what a branch changed, semantically — `codegraph diff`.
4. Leave zero footprint in the host repository.
5. Serve agents first (CLI + MCP, distributed as an installable plugin), with a
   schema a visual navigator can later project from.

## Non-goals for v1

Runtime tracing. Any language but Python. Visualization. `path_between`.
`neighbors` as a shipped surface. Cross-service edges. Historical backfill
(eagerly indexing the last N commits). MCP server. Shared/remote graph cache
(designed in D10, deliberately not built).

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
the target. This is what makes incremental invalidation tractable.

### D4 — Two-phase indexing

Naive per-file invalidation is wrong: editing `service.py` invalidates edges
that *live in* `handlers.py`. Splitting parse from resolve fixes this.

- **Phase 1 (parse)** — pure and per-blob: file content -> nodes + *unresolved*
  call references. A reference records the name as written plus import
  provenance (`from pay.service import charge` -> `pay.service.charge`), never
  a resolved target.
- **Phase 2 (resolve)** — unresolved references -> edges, against the global
  symbol table for a revision.

Re-resolution therefore touches `D ∪ dependents(D)` without re-parsing the
dependents. This is salsa's red-green invalidation, scoped down.

### D5 — Lazy refresh on query

Every query first reconciles the working tree, then answers. No daemon
required, no hooks required, never stale — the `git status` model. Explicit
`index` exists only to pay the cold-build cost up front.

Rejected: explicit-command-only (index rots silently — the failure mode that
kills these tools), git hooks as the correctness mechanism (blind to
uncommitted edits, which is exactly the code you most want impact analysis on),
watch daemon (process lifecycle and stale-lock complexity for v1).

Hooks exist in v1 only as an optional **warming** optimization (see D6). If a
hook never fires, answers are identical — merely slower. Staleness bugs stay
structurally impossible.

### D6 — Parse cache is content-addressed by git blob SHA

Phase 1 output is keyed on the **git blob SHA**, which `git ls-tree` supplies
for free. It is immutable and never invalidated, only garbage-collected.

This replaces keying on `(path, size, mtime_ns)`, which was a latent flaw
independent of versioning: `git checkout` rewrites mtimes, so an mtime-keyed
cache re-parses the entire repository after every branch switch despite the
content being byte-identical.

Consequences:

- Identical file content across any number of branches is parsed exactly once,
  for the life of the repository.
- Creating a branch changes no blobs, so it costs **zero**.
- Switching to a branch whose blobs have been seen costs **zero parsing**.

**Constraint this imposes:** phase 1 output must be path-independent, because
one blob may appear at several paths. Node IDs are `path::qualname`, so phase 1
emits qualnames, spans, and refs only; paths are applied at materialization.
This is cheap to honor now and expensive to retrofit.

### D7 — One canonical graph; other revisions materialized on demand

The repository has a single graph, tracking HEAD plus the working tree, moved
forward by deltas — git's own working-tree model. Per-revision snapshots are
not a core structure.

A second revision is materialized only when a query explicitly names one (i.e.
`diff`), then evicted by LRU. Blob contents are read with
`git cat-file --batch`, so **a revision can be indexed without being checked
out** and the working tree is never disturbed.

The working tree is the `WORKTREE` pseudo-revision: HEAD's tree overlaid with
`git status --porcelain`, with uncommitted content hashed directly via
`git hash-object`.

Non-git directories remain supported: file content is hashed with blake2b and
the same two layers apply, losing only the cross-branch sharing.

### D8 — CLI first; MCP deferred

The primary interface is the CLI, not an MCP server. MCP's real advantage is
discoverability — the agent sees the tool without being told — but `SKILL.md`
already provides that, and it loads only when relevant, whereas MCP tool
definitions occupy context on every request whether used or not.

A CLI also works in any harness, composes with `jq` and shell pipelines, needs
no server process, and removes the MCP SDK dependency from v1 entirely.

MCP becomes an additive wrapper later, for harnesses without shell access.

### D9 — Distributed as a Claude Code plugin

The repository doubles as its own marketplace, so installation is
`/plugin marketplace add swalla02/codegraph`. The plugin ships `SKILL.md`,
which teaches the agent to invoke the CLI; no server process is involved.

### D10 — Shared graphs are a build cache, not a sync problem

The graph is a pure function of (repository content, codegraph version):
identical blob content always yields identical parse output. Nobody therefore
needs to synchronize correctness — any participant can recompute. Sharing
exists only to avoid paying for the same computation twice.

That reframing removes the hard parts. Cache entries are content-addressed and
immutable, so merge conflicts are impossible, and partial or absent sync is
harmless because a missing entry is simply recomputed locally. The cache key
includes the codegraph version and effect-catalog hash, so upgrades invalidate
cleanly.

Publication is open: because entries are immutable, a developer's post-push
hook can publish just as validly as CI. CI is preferable only for consistency
(it always runs) and trust (see below).

Two transports, deferred to a later version:

- **Git ref namespace** (`refs/codegraph/cache`) — travels with `git fetch`
  once the refspec is configured, requiring no infrastructure, but grows every
  clone over time.
- **CI artifact storage** — does not bloat the repository, but is no longer
  "just a fetch."

**Not built in v1.** A single local index is a one-time cost; sharing only pays
off with a team or a repository large enough for the cold index to hurt.
Recorded here so the content-addressed storage design stays compatible with it.

Security note for whenever it does get built: a poisoned cache entry could make
impact analysis quietly lie. Acceptable within a trusted team, and the reason
CI would eventually be the only trusted publisher.

## Architecture

### Data model

Nodes are **symbols**, not files. Stable ID is `path::qualname`, e.g.
`src/pay/service.py::PaymentService.charge`. Line numbers are attributes, never
identity — otherwise every upstream edit churns IDs and unchanged code appears
to have changed. Kinds: `module`, `class`, `function`, `method`.

The path segment stays a real filesystem path (`/`-separated) rather than being
flattened into the `::` separator, so an ID remains pasteable into an editor.
Because the path is part of the ID, two same-named classes in different modules
are simply different nodes; no collision is possible across files.

**Shadowing within a file.** The same qualname can be defined more than once in
one module — accidental redefinition, but also `@overload` stubs and
`if TYPE_CHECKING:` / `try: import … except ImportError:` branches. Every
definition becomes its own node, carrying a `name_binding` field of `live` or
`shadowed`. The last definition wins the name and keeps the clean ID, matching
Python's runtime semantics; earlier ones take a `#2`, `#3` suffix in definition
order.

Shadowed definitions are **not** treated as dead code, because they frequently
still execute:

```python
@app.route("/pay")
def handle(): ...        # name shadowed below, but the framework holds a reference

def handle(): ...
```

They lose name lookups, but retain any edge that reaches them another way —
decorator registration, or an alias capturing the earlier binding
(`handler = process` before `process` is redefined). Shadowing is reported by
`status` as a warning, since most occurrences are genuine mistakes;
`@overload` and `TYPE_CHECKING` branches are recognized and excluded from that
warning.

Nested functions need no special handling: Python's `__qualname__` already
distinguishes them as `outer.<locals>.inner`.

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

SQLite at `.codegraph/graph.db` (stdlib `sqlite3`; no external dependency), WAL
mode. Two layers:

**Layer 1 — immutable, content-addressed, shared across all revisions**

| Table | Contents |
|---|---|
| `blobs` | `blob_sha`, parse status, error text |
| `blob_nodes` | path-independent symbol structure per blob |
| `blob_refs` | path-independent unresolved references per blob |

**Layer 2 — materialized per revision, evictable**

| Table | Contents |
|---|---|
| `revisions` | revision id, kind (`commit` / `WORKTREE`), materialized_at, pinned |
| `tree` | (revision, path, blob_sha) |
| `nodes` | (revision, node id, path, qualname, span, kind) |
| `edges` | (revision, src, dst, kind, confidence, provenance, callsite) |
| `effects` | (revision, node id, effect kind, evidence, direct) |
| `imports` | (revision, importer path, imported module) — powers `dependents()` |
| `meta` | schema version |

The load-bearing index is on `edges(revision, dst_id)`, which is what
`impact_of` traverses.

`.codegraph/` contains a `.gitignore` holding `*`, so the index self-ignores
without ever modifying the host repo's `.gitignore`.

### Indexing pipeline

1. Determine the target revision's tree: `git ls-tree -r <rev>` inside a repo
   (giving `(path, blob_sha)` directly), or a filesystem walk with `.gitignore`
   handling outside one. For `WORKTREE`, overlay `git status --porcelain`.
2. Diff that tree against the stored `tree` for the canonical graph. Dirty set
   **D** = paths whose blob SHA changed, plus additions and deletions.
3. Parse blobs in **D** not already present in Layer 1, reading content via
   `git cat-file --batch`.
4. Re-resolve **D ∪ dependents(D)**, where `dependents` is a single indexed
   query against `imports`.
5. Recompute effect propagation for the affected strongly-connected components.

### Cost model

The guarantee, stated as the property the implementation must hold:

| Action | Parse | Resolve |
|---|---|---|
| Create a branch | 0 | 0 |
| Switch, 12 files differ | ≤12, and 0 if the blobs were seen before | 12 + importers |
| Switch back | 0 | same set |
| Pull, 200 files changed | ≤200 | 200 + importers |
| First index ever | whole repo | whole repo |

Known caveat: resolution has a blast radius. Editing a module imported
everywhere re-resolves most of its importers. This is bounded by the importer
set and resolution is far cheaper than parsing, but it is not always literally
"the size of the diff."

Deletes and renames fall out naturally: a removed path drops its nodes and its
owned edges, references into it become unresolved and are *reported* rather
than silently disappearing, and a rename is a delete plus an add whose blob is
already cached (so it costs no parsing).

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
psycopg, boto3). A project-level `codegraph.toml` at the repository root merges
over it. That override is essential rather than optional: in a real codebase
most side effects sit behind house abstractions, not behind `requests` directly.

It lives at the repository root rather than inside `.codegraph/` because it is
hand-written configuration that should be **committed and shared**, whereas
`.codegraph/` self-ignores and holds only derived cache. Config is tracked;
everything derived is ignored.

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
confidence, and a witness path. Ranking combines hop distance (dominant), path
confidence, and node salience (entry point, public vs. underscore-private,
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

### `diff <base>..<head>`

Materializes the base revision alongside the current graph (no checkout) and
reports the semantic delta:

- symbols added, removed, or whose **normalized body hash** changed;
- edges that appeared or disappeared;
- **effects newly reachable** from changed symbols;
- aggregate impact of the change set — the union of `impact_of` over every
  changed symbol.

Comparison is deliberately on a hash of each symbol's normalized body, never on
its line span. Inserting a blank line at the top of a file shifts every span
below it; comparing spans would report the whole file as changed. Pure line
movement must produce an empty diff.

Default base is the merge-base with the repository's default branch, making the
bare `codegraph diff` mean "what does my branch change." The headline output is
the composition of all three analyses:

> *"This branch puts a `DB_WRITE` downstream of `checkout`, which 14 symbols
> depend on."*

### Composition

`impact_of` and `effects_of` compose, and that composition is the product:
impact results annotated with the effects each dependent path carries. Not "47
things call this," which is anxiety rather than information, but *"47 things
call this, and 3 of the paths end in a database write."*

## Surface

```
codegraph status                    # freshness, node/edge counts, dirty files, cache stats
codegraph index [--rebuild]         # force full build
codegraph impact <sym> [--hops N] [--all] [--json]
codegraph effects <sym> [--json]
codegraph diff [<base>..<head>] [--json]
codegraph resolve <query>           # fuzzy name -> node ids
codegraph gc                        # prune blob cache unreachable from retained revisions
codegraph install-hooks             # optional post-commit/checkout/merge warming
```

`resolve` is a first-class primitive. Nobody types
`src/pay/service.py::PaymentService.charge`; they type `charge`. Every query
accepts a loose symbol and either resolves uniquely or returns ranked
candidates. It never guesses — a wrong guess yields a confidently wrong impact
report, which is worse than no tool.

One core, two renderers: compact text for humans, token-budgeted JSON for
agents.

### Distribution

The repository is its own marketplace:

```
.claude-plugin/marketplace.json   # name, owner, plugins[{name, source:"./", ...}]
.claude-plugin/plugin.json        # name, version, description, repository, license, keywords
.mcp.json                         # {"codegraph": {"command": ..., "args": [...]}}
skills/codegraph/SKILL.md
```

Install: `/plugin marketplace add swalla02/codegraph`.

`SKILL.md` determines whether any of this gets used — a CLI is invisible to an
agent unless something tells it the tool exists. It must encode:

- **Trigger** — before modifying any function; when asked "what breaks if…",
  "what does this affect", "is this safe to change", "what did this branch
  change".
- **Workflow** — `codegraph resolve` → `impact` → `effects` → read only the
  top-ranked hits.
- **The anti-pattern it displaces** — do not grep for callers; you will miss
  dynamically dispatched ones and have no way to know when you are done.

### Layout

```
codegraph/
  cli.py  store.py  scanner.py
  git.py              # ls-tree, cat-file --batch, status, hash-object
  parse.py            # phase 1, path-independent, blob-keyed
  resolve.py          # phase 2: Resolver interface + AstResolver
  effects/            # catalog.py detect.py propagate.py
  query/              # impact.py effects.py diff.py rank.py
  render.py
skills/codegraph/SKILL.md
tests/fixtures/
```

Python 3.12, `uv`, pytest, ruff, console script `codegraph`. No runtime
dependencies beyond the stdlib. Git is invoked as a subprocess — no libgit2
dependency.

## Error handling

- **Unparseable file** (syntax error, Python 2, generated): recorded against the
  blob with an `error` status, excluded from the graph, surfaced in `status`.
  Never fails the whole index. Because the record is blob-keyed, a bad file is
  not re-parsed on every pass.
- **Unresolved reference**: retained in Layer 1 and counted in `status`, never
  silently discarded. A rising unresolved count is the primary health signal
  that resolution is degrading.
- **Not a git repository**: fall back to filesystem walk and blake2b hashing;
  `diff` is unavailable and says so.
- **Detached HEAD / shallow clone / missing base**: `diff` reports the missing
  revision rather than guessing a base.
- **Schema version mismatch** in `meta`: automatic full rebuild.
- **Concurrent access**: SQLite WAL plus a lock file; a second writer waits
  rather than corrupting.
- **Ambiguous symbol** in a query: return candidates and a non-zero exit code.
- **Shadowed definition**: kept as a node with `name_binding = shadowed` and
  surfaced as a `status` warning, never dropped — it may still execute via a
  decorator registry or a captured alias.

## Testing

**The invariant the project rests on: incremental result ≡ cold rebuild.** A
property test drives a fixture repository through a randomized sequence of
operations — edits, adds, deletes, renames, commits, branch creation, checkout,
merge, rebase — re-indexing incrementally throughout, and asserts the resulting
graph is identical to a from-scratch index of the same final state. If this
drifts, every answer becomes irreproducible and the tool is worthless
regardless of analysis quality. Written early, not last.

**The cost guarantee is also a test, not just a claim.** Instrument parse and
resolve counts, then assert directly: creating a branch parses zero blobs;
A → B → A re-parses zero blobs on the return; a 12-file switch parses at most
12. These are the properties that make the tool usable day to day, so they are
regression-tested rather than trusted.

Also:

- **Resolution goldens** on fixture packages — relative imports, aliases,
  `__init__.py` re-exports, `self` via MRO, inheritance, decorators.
- **Propagation tests** — SCC cycles, min-confidence, witness-path correctness.
- **Diff goldens** — added/removed symbols and edges, newly reachable effects.
- **Span-shift test** — inserting a blank line at the top of a file must produce
  an empty diff, proving comparison is on body hash rather than span.
- **Shadowing goldens** — repeated definitions, `@overload`, `TYPE_CHECKING`
  branches, and a decorator-registered definition that is shadowed by name yet
  must keep its edges.
- **Ranking goldens**, so ranking changes appear as reviewable diffs rather
  than silent behavior drift.
- **Accuracy harness** — labeled call sites with expected targets, reporting
  precision and recall, so "is jedi worth it?" becomes an empirical question
  rather than an argument.
- **Perf smoke** (marked slow) — cold-index a real repo such as flask; assert
  warm query latency under ~300ms and a branch switch under ~1s.

## Risks

| Risk | Mitigation |
|---|---|
| `impact_of` over-reports and gets ignored | Ranking, hop cap, summary-first output, LOW tier counted not listed |
| Effect catalog misses house abstractions | Project-level `codegraph.toml` override; unresolved/untagged counts in `status` |
| Index goes stale and loses trust | Lazy refresh on every query; incremental ≡ rebuild invariant test; hooks warm only |
| Branch switching feels expensive | Blob-keyed parse cache; cost guarantee enforced by test |
| Resolution blast radius on core modules | Bounded by importer set; resolution far cheaper than parsing; measured in perf smoke |
| Blob cache grows without bound | `codegraph gc` prunes entries unreachable from retained revisions |
| Dynamic dispatch limits accuracy | LOW-confidence over-approximation rather than omission; `provenance` reserved for later runtime traces |
| Agents do not adopt the CLI | `SKILL.md` encodes trigger, workflow, and displaced anti-pattern; token-budgeted output |
