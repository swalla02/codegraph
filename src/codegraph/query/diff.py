"""The `diff` report: what changed between two revisions, compared on
`body_hash` -- never on line span.

The whole point of this module is that moving a symbol's lines around (an
inserted blank line, a reordered file) must not read as a change. Every
symbol's identity is its node id (`path::qualname`); every symbol's
"did it change" question is answered by comparing `body_hash` (and, for a
symbol present in both revisions, its outgoing edge set) rather than line
numbers.

The base revision is read through `Indexer.reconcile`, which in turn reads
trees and blobs through `gitio`'s `ls-tree`/`cat-file` plumbing -- it is
never checked out, so a diff against an arbitrary commit leaves the
working tree and `HEAD` untouched.
"""

from __future__ import annotations

import sqlite3

from codegraph import gitio
from codegraph.indexer import Indexer
from codegraph.query.impact import impact_report
from codegraph.render import Group, Report, Row, budget
from codegraph.store import Store


class MissingRevisionError(Exception):
    """A requested revision could not be resolved to a real commit.

    Raised instead of silently falling back to some other base -- a
    shallow clone missing history, a detached `HEAD` with no default
    branch, or a non-git directory should fail loudly, by name.
    """

    def __init__(self, rev: str) -> None:
        super().__init__(f"revision not found: {rev}")
        self.rev = rev


def _resolve(indexer: Indexer, rev: str) -> str:
    """Resolve `rev` to a commit sha, verifying the commit actually exists.

    `git rev-parse <40-hex-chars>` alone echoes back a well-formed-looking
    sha without checking the object exists -- appending `^{commit}` forces
    git to dereference it, which fails loudly for a sha (or shallow-missing
    ref) with no backing object, instead of letting a later `ls-tree` fail
    with a confusing "not a tree object".
    """
    root = indexer.root
    if not gitio.is_repo(root):
        raise MissingRevisionError(rev)
    try:
        return gitio.rev_parse(root, f"{rev}^{{commit}}")
    except gitio.GitError as exc:
        raise MissingRevisionError(rev) from exc


def _nodes(store: Store, rev: str) -> dict[str, sqlite3.Row]:
    """Every node at `rev`, including the synthetic per-path module-scope
    node (`path::<module>`, `kind="module"`). It is not special-cased out:
    `parse.py` gives it a `body_hash` over the module's top-level
    statements with nested def/class bodies elided, so it is exactly as
    line-shift-insensitive as every other node here -- and it is where a
    new import or a module-level side-effecting call (code with no def of
    its own to attach to) actually lives, so excluding it would make the
    single most effect-dense kind of change invisible to `diff`."""
    return {
        row["id"]: row
        for row in store.connection.execute(
            "SELECT id, path, qualname, kind, line_start, body_hash FROM nodes WHERE rev=?",
            (rev,),
        )
    }


def _edges_by_src(store: Store, rev: str) -> dict[str, set[tuple[str, str]]]:
    """Outgoing edges per symbol, EXCLUDING the low-confidence fan-out.

    A symbol whose body hash is unchanged is still reported as `changed` when
    its callees changed -- `foo()` now reaching a different `foo` is a real
    change in behaviour even though not one character of this function moved.
    That is worth catching, but only for edges the resolver was actually
    confident about.

    A LOW edge is not a statement about this symbol. It is a guess about the
    whole repository: the bare-name fallback matches a call's last segment
    against every definition there is, so the LOW set of an untouched function
    moves whenever anyone anywhere adds or deletes a same-named symbol.
    Comparing those guesses across two revisions manufactures change out of
    edits that never came near the file.

    Measured on `psf/requests` before this filter: one new `AttrProxy.__init__`
    in a test file added a candidate to every `super().__init__()` call site in
    the repository, and `diff` reported `HTTPAdapter.__init__`,
    `BaseAdapter.__init__`, `LookupDict.__init__`, `JSONDecodeError.__init__`
    and others as `changed` -- in files whose git blob SHA was byte-identical
    across the range. 7 of 20 `changed` rows, 35% of the list. See issue #13.
    """
    edges: dict[str, set[tuple[str, str]]] = {}
    for row in store.connection.execute(
        "SELECT src, dst, kind FROM edges WHERE rev=? AND confidence != 'LOW'", (rev,)
    ):
        edges.setdefault(row["src"], set()).add((row["dst"], row["kind"]))
    return edges


def _effect_kinds(store: Store, rev: str, node_id: str) -> set[str]:
    return {
        row["kind"]
        for row in store.connection.execute(
            "SELECT DISTINCT kind FROM effects WHERE rev=? AND node_id=?", (rev, node_id)
        )
    }


def _dependent_count(store: Store, rev: str, node_id: str) -> int:
    """Blast radius of `node_id` at `rev`, via the same walk `impact`
    itself uses -- not reimplemented here."""
    return impact_report(store, rev, node_id, limit=10_000).summary["symbols"]


def diff_report(store: Store, indexer: Indexer, base: str, head: str, limit: int = 40) -> Report:
    """Compare `base` and `head`, reconciling both first.

    Symbol identity is the node id; the only question asked of a symbol
    present in both revisions is whether its `body_hash` or its outgoing
    edge set (`(src, dst, kind)`) changed -- never where its lines sit.
    """
    resolved_base = _resolve(indexer, base)

    indexer.reconcile(resolved_base)
    indexer.reconcile(head)

    base_nodes = _nodes(store, resolved_base)
    head_nodes = _nodes(store, head)
    base_edges = _edges_by_src(store, resolved_base)
    head_edges = _edges_by_src(store, head)

    added_ids = set(head_nodes) - set(base_nodes)
    removed_ids = set(base_nodes) - set(head_nodes)
    common_ids = set(head_nodes) & set(base_nodes)
    changed_ids = {
        node_id
        for node_id in common_ids
        if head_nodes[node_id]["body_hash"] != base_nodes[node_id]["body_hash"]
        or head_edges.get(node_id, set()) != base_edges.get(node_id, set())
    }

    added_rows = [
        Row(
            id=node_id,
            location=f"{head_nodes[node_id]['path']}:{head_nodes[node_id]['line_start']}",
            detail=head_nodes[node_id]["kind"],
            score=1.0,
        )
        for node_id in added_ids
    ]
    removed_rows = [
        Row(
            id=node_id,
            location=f"{base_nodes[node_id]['path']}:{base_nodes[node_id]['line_start']}",
            detail=(
                f"{base_nodes[node_id]['kind']}, "
                f"{_dependent_count(store, resolved_base, node_id)} dependent(s)"
            ),
            score=1.0,
        )
        for node_id in removed_ids
    ]
    changed_rows = [
        Row(
            id=node_id,
            location=f"{head_nodes[node_id]['path']}:{head_nodes[node_id]['line_start']}",
            detail=(
                f"{head_nodes[node_id]['kind']}, "
                f"{_dependent_count(store, head, node_id)} dependent(s)"
            ),
            score=1.0,
        )
        for node_id in changed_ids
    ]

    groups: list[Group] = []
    truncated = False
    for title, group_rows in (
        ("added", added_rows),
        ("removed", removed_rows),
        ("changed", changed_rows),
    ):
        if not group_rows:
            continue
        kept, was_truncated = budget(group_rows, limit)
        groups.append(Group(title, kept))
        truncated = truncated or was_truncated

    # A newly-added symbol is exactly as "newly reachable" as an edited
    # one -- Task 12's fix for this same blind spot at module scope. A
    # brand-new function that calls `requests.post` has no entry in
    # `base_nodes` at all, so `changed_ids` alone (which only ever
    # contains ids present in BOTH revisions) can never surface it;
    # `_effect_kinds(store, resolved_base, node_id)` against a nonexistent
    # node id is just an empty result set, so folding `added_ids` in here
    # costs nothing for ids that legitimately have no base side.
    new_effects: set[str] = set()
    for node_id in changed_ids | added_ids:
        new_effects |= _effect_kinds(store, head, node_id) - _effect_kinds(
            store, resolved_base, node_id
        )

    summary = {
        "new_effects": sorted(new_effects),
        "added": len(added_ids),
        "removed": len(removed_ids),
        "changed": len(changed_ids),
        "base": resolved_base,
        "head": head,
    }

    return Report(summary=summary, groups=groups, truncated=truncated)


__all__ = ["MissingRevisionError", "diff_report"]
