"""The `history` report: the graph over a range of commits -- `git log -L`
at the level of symbols and edges rather than lines.

Two questions, one walk. For a symbol: which commits in the range changed
its behaviour -- its `body_hash`, the confident callees it reaches, or the
side effects reachable from it -- oldest first. For the range as a whole:
per commit, the symbols, edges and effects it added and removed. Both are
`diff` applied to each consecutive pair of commits, compared the way `diff`
compares (`nodes_at`, `confident_edges`), so the two commands cannot come to
disagree about what "changed" means.

## What is materialized, and for how long

Only the range the query names. There is no backfill and nothing is kept:
the walk starts from the first commit's parent, carries one revision's
graph forward commit by commit, and discards every revision it created
when it is done -- including on an error. At most two revisions' graphs
exist at once, and one of them is a Python snapshot, not rows (D7).

Carrying the graph forward is what keeps the cost proportional to the
range. Each commit's Layer 2 rows start life as its parent's
(`Store.seed_revision`), so reconciling the commit is the ordinary
incremental reconcile the working tree gets: the files the commit touched
are re-resolved, narrowed when the symbol table is unchanged, and a commit
that touches no Python file is the unchanged-tree fast path. Parsing is
proportional to the blobs the range introduces, since the parse cache is
keyed on blob sha and shared with every revision ever seen (D6). The one
cold build is the starting revision, unless it was already materialized --
in which case its rows are copied and the original is kept, reconciled
the way any query would reconcile it.

The starting revision is the first commit's first parent. That is `base`
itself whenever `base` sits on `head`'s first-parent line, which is the
ordinary case; when it does not (a branch that has merged the default
branch in), it is the commit the first walked commit was actually made on,
because measuring a commit against anything else would attribute somebody
else's changes to it. Nothing is ever checked out: every tree is read with
`ls-tree` and `cat-file`, exactly as `diff` reads its base.

## Moves are an inference, and are reported as one

A node id is `path::qualname`, so moving a function to another file or
class is, in the graph, a removal and an addition. Pairing the two back up
is a guess the source does not state, so a pairing carries a tier from the
resolver's own vocabulary, with the resolver's own rule for a name matched
by guesswork: **MEDIUM if unique, else LOW** (see "Confidence, and what
earns it" in the README). Unique means one removed and one added symbol of
the same kind share a `body_hash` in the same commit. Never HIGH: HIGH is a
claim the text makes, and no text says two ids are one symbol.

A LOW pairing is reported and not followed. Following it would mean picking
one candidate, and a history silently continued down the wrong lineage is
the confidently-wrong answer this project refuses to give; the candidates
are named instead, and the report carries a `lineage_ambiguous` entry.

`body_hash` covers the whole definition, its own name included. A rename
therefore changes it, and is reported as exactly what the source shows: a
removal and an unrelated addition. What pairs is a move -- the same
definition under another path or class.

## Who depends on a symbol, and how that grows

A symbol's row also names the commits that changed its direct dependents:
the distinct symbols with a confident edge into it, of any kind. They come
from the same snapshots and the same LOW filter as its callees, so a bare
name someone added on the far side of the repository does not show up as
a new dependent. A dependent that moved in the same commit is mapped
through the commit's MEDIUM moves first, so a caller changing files reads
as nothing rather than as one dependent lost and another gained. The
summary gives the count at the end of the walk the symbol was named at.

This is fan-in, one hop. The transitive `impact` set over time would cost
a reverse walk of every changed revision, and is not computed.

## Islands, when asked for

With `islands`, the walk also partitions each changed revision into
islands with `island_roots` -- the partition `islands` itself prints, never
a second one that could come to disagree with it -- and reports every
merge and split between a commit and its parent: an island whose symbols
sat in two or more islands the commit before, and the reverse. It is off
by default, because the partition folds in the bare-name fan-out and
costs a pass over every edge of every changed revision.

Because it is that partition, it inherits that partition's reading of the
fan-out: two islands a new same-named definition bridged through a
bare-name hub are merged here exactly as `islands` would call them one.

## What is compared

The same filters `diff` uses, for the same reasons. Edges exclude LOW:
the bare-name fan-out is a guess about the whole repository and moves when
anyone adds a same-named symbol anywhere (see `confident_edges`). Effects
exclude LOW for the identical reason, since a LOW effect is one that
arrives over that same fan-out; a history that listed them would report an
unrelated commit as the one that changed a symbol's behaviour.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from codegraph import gitio
from codegraph.indexer import Indexer
from codegraph.query.diff import MissingRevisionError, confident_edges, nodes_at, resolve_commit
from codegraph.query.islands import island_roots
from codegraph.render import Group, Report, Row, Unknown, budget
from codegraph.resolve import LOW, MEDIUM, find_symbol
from codegraph.store import Store
from codegraph.uncertainty import LINEAGE_AMBIGUOUS, unknown

#: A node as `history` compares it: `(body_hash, kind, path, line_start)`.
_Node = tuple[str, str, str, int]


@dataclass(frozen=True)
class _Snapshot:
    """What one revision's graph says, held in memory so the rows can go."""

    nodes: dict[str, _Node]
    edges: frozenset[tuple[str, str, str]]
    effects: frozenset[tuple[str, str]]


_EMPTY = _Snapshot({}, frozenset(), frozenset())


@dataclass(frozen=True)
class Move:
    """A removed and an added symbol whose bodies hash the same, paired as
    one symbol that moved. `confidence` is MEDIUM when the pairing is unique
    and LOW when it is one of several; see the module docstring."""

    removed: str
    added: str
    confidence: str


@dataclass(frozen=True)
class IslandChange:
    """Islands that became one (a merge) or one that became several (a
    split). `parts` is each smaller island as `(representative, size)`,
    the representative being its most-depended-on symbol; `whole` is the
    size of the single island on the other side."""

    parts: tuple[tuple[str, int], ...]
    whole: int


@dataclass(frozen=True)
class Step:
    """One commit, compared against its first parent."""

    sha: str
    parent: str
    subject: str
    added: dict[str, _Node]
    removed: dict[str, _Node]
    changed: dict[str, _Node]
    moves: tuple[Move, ...]
    edges_gained: frozenset[tuple[str, str, str]]
    edges_lost: frozenset[tuple[str, str, str]]
    effects_gained: frozenset[tuple[str, str]]
    effects_lost: frozenset[tuple[str, str]]
    #: Where each symbol named by the edge and effect rows above sits after
    #: the commit (before it, for one the commit removed). An unchanged body
    #: has no row of its own in the step, and a report still has to say
    #: where the symbol whose callees moved is.
    context: dict[str, _Node]
    #: Session pointers recorded on this commit. Always empty from
    #: `walk_history` itself: reading them is not the graph's job, and a
    #: repository without them must answer exactly as it does with them
    #: absent. `history_report` and `range_report` fill it through their
    #: `session_pointers` argument, the one seam a session reader plugs into.
    sessions: tuple[str, ...] = ()
    #: With `walk_history(..., islands=True)`: the island count after the
    #: commit, and the merges and splits against its parent. `None` and
    #: empty otherwise.
    islands: int | None = None
    merges: tuple[IslandChange, ...] = ()
    splits: tuple[IslandChange, ...] = ()

    def touched(self) -> bool:
        return bool(
            self.added
            or self.removed
            or self.changed
            or self.edges_gained
            or self.edges_lost
            or self.effects_gained
            or self.effects_lost
            or self.merges
            or self.splits
        )


@dataclass(frozen=True)
class Walk:
    """The steps of one walk, plus the symbol lookups it made while the
    revisions they need were still materialized."""

    start: str
    head: str
    steps: list[Step]
    head_matches: list[str] = field(default_factory=list)
    start_matches: list[str] = field(default_factory=list)
    #: Direct confident dependents of each match, counted at the revision
    #: it was matched at.
    head_dependents: dict[str, int] = field(default_factory=dict)
    start_dependents: dict[str, int] = field(default_factory=dict)
    #: The starting revision's island count, with `islands=True`.
    start_islands: int | None = None


def history_range(root: Path, revspec: str | None) -> tuple[str, str]:
    """Split a revspec the way `diff` does, with one difference: the head
    side defaults to `HEAD`, never the worktree. History is a list of
    commits, and uncommitted edits are not one -- `diff` is the command for
    those. With no revspec, the range is `merge-base(default branch,
    HEAD)..HEAD`: what this branch has committed so far.
    """
    if revspec:
        base, _, head = revspec.partition("..")
        if not base:
            raise MissingRevisionError("<base>")
        return base, head or "HEAD"
    if not gitio.is_repo(root):
        raise MissingRevisionError("HEAD")
    try:
        branch = gitio.default_branch(root)
        return gitio.merge_base(root, branch, "HEAD"), "HEAD"
    except gitio.GitError as exc:
        raise MissingRevisionError(str(exc)) from exc


def walk_history(
    store: Store,
    indexer: Indexer,
    base: str,
    head: str,
    symbol: str | None = None,
    *,
    islands: bool = False,
) -> Walk:
    """Materialize `base..head` one commit at a time, compare each commit
    with its first parent, and release every revision the walk created.

    `symbol`, when given, is looked up with `find_symbol` at the head and
    at the starting revision while each is materialized, so the caller can
    resolve it without keeping either one.

    `islands` partitions every changed revision as `islands` does and
    records the merges and splits; see the module docstring for the cost.
    """
    root = indexer.root
    base_sha = resolve_commit(indexer, base)
    head_sha = resolve_commit(indexer, head)
    try:
        commits = gitio.first_parent_log(root, base_sha, head_sha)
    except gitio.GitError as exc:
        raise MissingRevisionError(f"{base}..{head}") from exc
    if not commits:
        return Walk(start=base_sha, head=head_sha, steps=[])

    start = commits[0][1]
    retained = store.revisions()
    created: set[str] = set()
    steps: list[Step] = []
    head_matches: list[str] = []
    start_matches: list[str] = []
    head_dependents: dict[str, int] = {}
    start_dependents: dict[str, int] = {}
    start_islands: int | None = None
    try:
        previous_rev: str | None = None
        previous = _EMPTY
        previous_roots: dict[str, str] = {}
        if start:
            if start not in retained:
                created.add(start)
            indexer.reconcile(start)
            previous_rev, previous = start, _snapshot(store, start)
            if symbol is not None:
                start_matches = [row["id"] for row in find_symbol(store, start, symbol)]
                start_dependents = _dependent_counts(previous, start_matches)
            if islands:
                previous_roots = island_roots(store, start)
        if islands:
            start_islands = len(set(previous_roots.values()))

        for sha, parent, subject in commits:
            inherited = None
            if sha not in retained:
                if previous_rev is not None:
                    inherited = _fingerprint(store, previous_rev)
                    # Moving is the release: a revision the walk created is
                    # never needed again once its child holds its rows.
                    store.seed_revision(previous_rev, sha, move=previous_rev in created)
                    created.discard(previous_rev)
                created.add(sha)
            stats = indexer.reconcile(sha)
            # Rows inherited from the parent, no path dirty, and the same
            # fingerprint afterwards: the reconcile took the unchanged-tree
            # fast path and the graph is the parent's, row for row -- a
            # commit that touched no Python file. Re-reading it buys nothing.
            # The fingerprint is what rules out a rebuild for some reason
            # outside the tree, such as a trace imported for one of the two.
            unchanged = (
                inherited is not None
                and stats.paths_dirty == 0
                and _fingerprint(store, sha) == inherited
            )
            current = previous if unchanged else _snapshot(store, sha)
            step = _step(sha, parent, subject, previous, current)
            if islands:
                roots = previous_roots if unchanged else island_roots(store, sha)
                step = replace(
                    step,
                    islands=len(set(roots.values())),
                    merges=_island_changes(previous_roots, roots, previous),
                    splits=_island_changes(roots, previous_roots, current),
                )
                previous_roots = roots
            steps.append(step)
            if previous_rev in created:
                store.drop_revision(previous_rev)
                created.discard(previous_rev)
            previous_rev, previous = sha, current

        if symbol is not None:
            head_matches = [row["id"] for row in find_symbol(store, head_sha, symbol)]
            head_dependents = _dependent_counts(previous, head_matches)
    finally:
        for rev in created:
            store.drop_revision(rev)

    return Walk(
        start=start or base_sha,
        head=head_sha,
        steps=steps,
        head_matches=head_matches,
        start_matches=start_matches,
        head_dependents=head_dependents,
        start_dependents=start_dependents,
        start_islands=start_islands,
    )


def _dependent_counts(snapshot: _Snapshot, node_ids: list[str]) -> dict[str, int]:
    wanted = set(node_ids)
    sources: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for src, dst, _ in snapshot.edges:
        if dst in wanted:
            sources[dst].add(src)
    return {node_id: len(found) for node_id, found in sources.items()}


def _island_changes(
    before: dict[str, str], after: dict[str, str], parts_snapshot: _Snapshot
) -> tuple[IslandChange, ...]:
    """The islands of `after` whose symbols sat in two or more islands of
    `before`: merges when called as (parent, commit), splits as (commit,
    parent). Only symbols present on both sides count, so a symbol the
    commit added is growth, not a merge. `parts_snapshot` is the revision
    the parts belong to, used to pick each part's representative."""
    spans: dict[str, set[str]] = {}
    for node_id, root in after.items():
        if node_id in before:
            spans.setdefault(root, set()).add(before[node_id])
    joined = {root: parts for root, parts in spans.items() if len(parts) > 1}
    if not joined:
        return ()
    part_roots = set().union(*joined.values())
    members: dict[str, list[str]] = {}
    for node_id, root in before.items():
        if root in part_roots:
            members.setdefault(root, []).append(node_id)
    fan_in = Counter(dst for _, dst in {(src, dst) for src, dst, _ in parts_snapshot.edges})
    whole_size = Counter(after.values())
    changes = []
    for root, parts in joined.items():
        described = [
            (
                min(members[part], key=lambda node_id: (-fan_in[node_id], node_id)),
                len(members[part]),
            )
            for part in parts
        ]
        described.sort(key=lambda part: (-part[1], part[0]))
        changes.append(IslandChange(tuple(described), whole_size[root]))
    changes.sort(key=lambda change: (-change.whole, change.parts))
    return tuple(changes)


def _fingerprint(store: Store, rev: str) -> str | None:
    row = store.connection.execute(
        "SELECT fingerprint FROM revisions WHERE rev=?", (rev,)
    ).fetchone()
    return row["fingerprint"] if row else None


def _snapshot(store: Store, rev: str) -> _Snapshot:
    nodes = {
        node_id: (row["body_hash"], row["kind"], row["path"], row["line_start"])
        for node_id, row in nodes_at(store, rev).items()
    }
    edges = frozenset(
        (src, dst, kind)
        for src, targets in confident_edges(store, rev).items()
        for dst, kind in targets
    )
    effects = frozenset(
        (row["node_id"], row["kind"])
        for row in store.connection.execute(
            "SELECT DISTINCT node_id, kind FROM effects WHERE rev=? AND confidence != ?",
            (rev, LOW),
        )
    )
    return _Snapshot(nodes, edges, effects)


def _step(sha: str, parent: str, subject: str, before: _Snapshot, after: _Snapshot) -> Step:
    added = {i: after.nodes[i] for i in after.nodes.keys() - before.nodes.keys()}
    removed = {i: before.nodes[i] for i in before.nodes.keys() - after.nodes.keys()}
    changed = {
        i: after.nodes[i]
        for i in after.nodes.keys() & before.nodes.keys()
        if after.nodes[i][0] != before.nodes[i][0]
    }
    edges_gained = after.edges - before.edges
    edges_lost = before.edges - after.edges
    effects_gained = after.effects - before.effects
    effects_lost = before.effects - after.effects
    named = {node for src, dst, _ in edges_gained | edges_lost for node in (src, dst)}
    named |= {node for node, _ in effects_gained | effects_lost}
    context = {
        i: after.nodes.get(i) or before.nodes[i]
        for i in named
        if i in after.nodes or i in before.nodes
    }
    return Step(
        sha=sha,
        parent=parent,
        subject=subject,
        added=added,
        removed=removed,
        changed=changed,
        moves=_pair_moves(removed, added),
        edges_gained=edges_gained,
        edges_lost=edges_lost,
        effects_gained=effects_gained,
        effects_lost=effects_lost,
        context=context,
    )


def _pair_moves(removed: dict[str, _Node], added: dict[str, _Node]) -> tuple[Move, ...]:
    """Pair removals with additions by `(body_hash, kind)`: MEDIUM when the
    match is one-to-one, LOW for every candidate pair when it is not."""
    by_key_removed: dict[tuple[str, str], list[str]] = {}
    by_key_added: dict[tuple[str, str], list[str]] = {}
    for node_id, (body_hash, kind, _, _) in removed.items():
        by_key_removed.setdefault((body_hash, kind), []).append(node_id)
    for node_id, (body_hash, kind, _, _) in added.items():
        by_key_added.setdefault((body_hash, kind), []).append(node_id)
    moves: list[Move] = []
    for key, gone in by_key_removed.items():
        came = by_key_added.get(key, [])
        tier = MEDIUM if len(gone) == 1 and len(came) == 1 else LOW
        moves.extend(Move(old, new, tier) for old in sorted(gone) for new in sorted(came))
    return tuple(moves)


# -- the per-symbol report ---------------------------------------------------


def symbol_history(
    walk: Walk, node_id: str, *, forward: bool = False
) -> tuple[list[Row], list[Unknown]]:
    """The commits that changed `node_id`, oldest first, following its
    lineage across moves.

    `node_id` is an id at the head, and the lineage is followed backwards;
    with `forward`, it is an id at the starting revision, followed towards
    the head -- the case of a symbol the range deleted. Returns the rows
    and the envelope entries.
    """
    tracked = node_id
    rows: list[Row] = []
    holes: list[Unknown] = []
    steps = walk.steps if forward else list(reversed(walk.steps))
    for step in steps:
        ending, continuing = (step.removed, step.added) if forward else (step.added, step.removed)
        if tracked in ending:
            # Born in this commit (walking back) or deleted in it (walking
            # forward). Either way the id ends here unless a move continues it.
            candidates = [
                (move.added if forward else move.removed, move.confidence)
                for move in step.moves
                if (move.removed if forward else move.added) == tracked
            ]
            node = ending[tracked]
            if len(candidates) == 1 and candidates[0][1] == MEDIUM:
                other = candidates[0][0]
                old, new = (tracked, other) if forward else (other, tracked)
                reasons = [f"moved from {old} to {new} ({MEDIUM})"]
                reasons += _behaviour(step, old, new)
                location = _location(continuing[other] if forward else node)
                rows.append(_commit_row(step, location, reasons))
                tracked = other
                continue
            verb = "removed" if forward else "added"
            if candidates:
                names = ", ".join(sorted(c for c, _ in candidates))
                rows.append(
                    _commit_row(
                        step,
                        _location(node),
                        [f"{verb}; same body as {len(candidates)} symbols: {names} ({LOW})"],
                    )
                )
                holes.append(
                    unknown(
                        LINEAGE_AMBIGUOUS,
                        f"{tracked} at {step.sha[:12]}: {len(candidates)} candidates",
                    )
                )
            else:
                rows.append(_commit_row(step, _location(node), [verb]))
            break
        reasons = []
        if tracked in step.changed:
            reasons.append("body changed")
        reasons += _behaviour(step, tracked, tracked)
        if reasons:
            node = step.changed.get(tracked) or step.context[tracked]
            rows.append(_commit_row(step, _location(node), reasons))
    if not forward:
        rows.reverse()
    return rows, holes


def _behaviour(step: Step, old: str, new: str) -> list[str]:
    """What changed about what `old` (before) / `new` (after) reaches and
    what reaches it: its confident callees, its direct dependents and its
    reachable effect kinds. For an unmoved symbol the two ids are the
    same."""
    reasons = []
    gained = sorted(dst for src, dst, _ in step.edges_gained if src == new)
    lost = sorted(dst for src, dst, _ in step.edges_lost if src == old)
    if old != new:
        # A moved symbol loses every edge under its old id and gains every
        # one under its new id; only the difference between the two is news.
        gained, lost = sorted(set(gained) - set(lost)), sorted(set(lost) - set(gained))
    if gained:
        reasons.append(f"calls +{', +'.join(gained)}")
    if lost:
        reasons.append(f"calls -{', -'.join(lost)}")
    moved = {move.removed: move.added for move in step.moves if move.confidence == MEDIUM}
    dependents_gained = {src for src, dst, _ in step.edges_gained if dst == new}
    dependents_lost = {moved.get(src, src) for src, dst, _ in step.edges_lost if dst == old}
    # Always a difference: a dependent that moved files re-points its edge,
    # which is one lost and one gained under the id `moved` already maps.
    dependents_gained, dependents_lost = (
        dependents_gained - dependents_lost,
        dependents_lost - dependents_gained,
    )
    if dependents_gained:
        reasons.append(f"dependents +{', +'.join(sorted(dependents_gained))}")
    if dependents_lost:
        reasons.append(f"dependents -{', -'.join(sorted(dependents_lost))}")
    effects_gained = {kind for node, kind in step.effects_gained if node == new}
    effects_lost = {kind for node, kind in step.effects_lost if node == old}
    if old != new:
        effects_gained, effects_lost = (
            effects_gained - effects_lost,
            effects_lost - effects_gained,
        )
    if effects_gained:
        reasons.append(f"effects +{', +'.join(sorted(effects_gained))}")
    if effects_lost:
        reasons.append(f"effects -{', -'.join(sorted(effects_lost))}")
    return reasons


def _location(node: _Node) -> str:
    return f"{node[2]}:{node[3]}"


def _commit_label(step: Step) -> str:
    label = f'"{step.subject}"'
    if step.sessions:
        label += f" · session: {', '.join(step.sessions)}"
    return label


def _commit_row(step: Step, location: str, reasons: list[str]) -> Row:
    return Row(
        id=step.sha,
        location=location,
        detail=f"{'; '.join(reasons)} · {_commit_label(step)}",
        score=1.0,
    )


def _attach_sessions(walk: Walk, session_pointers: Callable[[str], Sequence[str]] | None) -> Walk:
    if session_pointers is None:
        return walk
    steps = [replace(step, sessions=tuple(session_pointers(step.sha))) for step in walk.steps]
    return replace(walk, steps=steps)


def history_report(
    walk: Walk,
    node_id: str,
    *,
    forward: bool = False,
    limit: int = 40,
    session_pointers: Callable[[str], Sequence[str]] | None = None,
) -> Report:
    """The commits in the walk that changed `node_id`, oldest first.

    `session_pointers(sha)` is the seam for linking a commit to the session
    that produced it: given one, each row carries what it returns. Without
    it the report is exactly what the graph alone says.
    """
    walk = _attach_sessions(walk, session_pointers)
    rows, holes = symbol_history(walk, node_id, forward=forward)
    kept, truncated = budget(rows, limit)
    summary = {
        "symbol": node_id,
        "commits": len(walk.steps),
        "changed_in": len(rows),
        "dependents": (walk.start_dependents if forward else walk.head_dependents).get(node_id, 0),
        "base": walk.start,
        "head": walk.head,
    }
    groups = [Group("commits", kept)] if kept else []
    return Report(summary=summary, groups=groups, truncated=truncated, unknowns=holes)


# -- the whole-range report --------------------------------------------------


def range_report(
    walk: Walk,
    *,
    limit: int = 40,
    session_pointers: Callable[[str], Sequence[str]] | None = None,
) -> Report:
    """Per commit, oldest first: the symbols added, removed, moved and
    changed, the confident edges gained and lost, and the effects gained and
    lost -- and, for a walk made with `islands`, the islands that merged or
    split. A commit that changed none of those gets no group; `commits` in
    the summary still counts it."""
    walk = _attach_sessions(walk, session_pointers)
    groups: list[Group] = []
    truncated = False
    for step in walk.steps:
        if not step.touched():
            continue
        rows = _range_rows(step)
        kept, was_truncated = budget(rows, limit)
        truncated = truncated or was_truncated
        groups.append(Group(f"{step.sha} {_commit_label(step)}", kept))

    steps = walk.steps
    island_summary: dict[str, int | None] = {}
    if walk.start_islands is not None:
        counted = [step.islands for step in steps if step.islands is not None]
        island_summary = {
            "islands_start": walk.start_islands,
            "islands_head": counted[-1] if counted else walk.start_islands,
            "merges": sum(len(step.merges) for step in steps),
            "splits": sum(len(step.splits) for step in steps),
        }
    summary = {
        "commits": len(steps),
        "changed": sum(1 for step in steps if step.touched()),
        "edges_gained": sum(len(step.edges_gained) for step in steps),
        "edges_lost": sum(len(step.edges_lost) for step in steps),
        "effects_gained": sum(len(step.effects_gained) for step in steps),
        "effects_lost": sum(len(step.effects_lost) for step in steps),
        **island_summary,
        "base": walk.start,
        "head": walk.head,
    }
    return Report(summary=summary, groups=groups, truncated=truncated)


def _range_rows(step: Step) -> list[Row]:
    """One commit's rows, symbols first, then effects, then edges -- a
    symbol-level change is usually the cause of the rows below it. `budget`
    keeps the highest scores, so the order is also what survives `--limit`."""
    rows: list[Row] = []
    moved = {move.added: move for move in step.moves if move.confidence == MEDIUM}
    moved_from = {move.removed for move in moved.values()}
    for node_id in sorted(step.added):
        move = moved.get(node_id)
        detail = f"moved from {move.removed} ({MEDIUM})" if move else "added"
        low = sorted(m.removed for m in step.moves if m.added == node_id and m.confidence == LOW)
        if low:
            detail = f"added; same body as {', '.join(low)} ({LOW})"
        rows.append(Row(node_id, _location(step.added[node_id]), detail, 3.0))
    for node_id in sorted(step.removed):
        if node_id in moved_from:
            continue
        rows.append(Row(node_id, _location(step.removed[node_id]), "removed", 3.0))
    for node_id in sorted(step.changed):
        rows.append(Row(node_id, _location(step.changed[node_id]), "body changed", 3.0))
    for verb, changes in (("merged", step.merges), ("split", step.splits)):
        for change in changes:
            parts = " + ".join(f"{node_id} ({size})" for node_id, size in change.parts)
            detail = f"islands {verb}: {parts}" + (" into " if verb == "merged" else " from ")
            detail += f"one of {change.whole}"
            rows.append(Row(change.parts[0][0], f"{len(change.parts)} islands", detail, 2.5))
    for node_id, kind in sorted(step.effects_gained):
        rows.append(Row(node_id, kind, "effect gained", 2.0))
    for node_id, kind in sorted(step.effects_lost):
        rows.append(Row(node_id, kind, "effect lost", 2.0))
    for src, dst, kind in sorted(step.edges_gained):
        rows.append(Row(f"{src} -> {dst}", kind, "edge gained", 1.0))
    for src, dst, kind in sorted(step.edges_lost):
        rows.append(Row(f"{src} -> {dst}", kind, "edge lost", 1.0))
    return rows


__all__ = [
    "IslandChange",
    "Move",
    "Step",
    "Walk",
    "history_range",
    "history_report",
    "range_report",
    "symbol_history",
    "walk_history",
]
