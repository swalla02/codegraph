"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codegraph import __version__, gitio
from codegraph.indexer import FsTreeSource, GitTreeSource, Indexer
from codegraph.maintenance import gc, install_hooks
from codegraph.query.diff import MissingRevisionError, diff_report
from codegraph.query.effects import effects_report
from codegraph.query.impact import impact_report
from codegraph.render import render_json, render_text
from codegraph.resolve import find_symbol
from codegraph.store import WORKTREE, Store


def open_workspace(root: Path) -> tuple[Store, Indexer]:
    """Open the store and build an indexer for `root`, choosing a git-backed
    tree source when `root` is a git repository and falling back to a plain
    filesystem walk otherwise."""
    store = Store.open(root)
    source = GitTreeSource(root) if gitio.is_repo(root) else FsTreeSource(root)
    return store, Indexer(root, store, source)


def _print_stats(stats) -> None:
    print(f"paths: {stats.paths_total} ({stats.paths_dirty} dirty)")
    print(f"blobs: {stats.blobs_parsed} parsed, {stats.blobs_cached} cached")
    print(f"edges: {stats.edges}, unresolved: {stats.unresolved}")
    if stats.parse_errors:
        print(f"parse errors: {stats.parse_errors}")
    if stats.shadowed:
        print(f"warning: {stats.shadowed} shadowed definition(s)")


def _cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        _print_stats(indexer.reconcile(args.rev))
    finally:
        store.close()
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        if args.rebuild:
            store.connection.execute("DELETE FROM blobs")
            store.connection.commit()
        stats = indexer.reconcile(args.rev)
        if not args.quiet:
            _print_stats(stats)
    finally:
        store.close()
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    """Fuzzy symbol lookup. Ambiguity is reported, never silently picked."""
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        indexer.reconcile(args.rev)
        matches = find_symbol(store, args.rev, args.query)
        for row in matches:
            print(row["id"])
        if not matches:
            print(f"no symbol matching {args.query!r}", file=sys.stderr)
            return 1
        return 2 if len(matches) > 1 else 0
    finally:
        store.close()


def _cmd_effects(args: argparse.Namespace) -> int:
    """Report the side effects transitively reachable from a symbol."""
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        indexer.reconcile(args.rev)
        matches = find_symbol(store, args.rev, args.symbol)
        if not matches:
            print(f"no symbol matching {args.symbol!r}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            print(f"ambiguous symbol {args.symbol!r}:", file=sys.stderr)
            for row in matches:
                print(f"  {row['id']}", file=sys.stderr)
            return 2
        report = effects_report(store, args.rev, matches[0]["id"])
        print(render_json(report) if args.json else render_text(report))
        return 0
    finally:
        store.close()


def _cmd_impact(args: argparse.Namespace) -> int:
    """Report the ranked dependents of a symbol -- everything a change to
    it could break."""
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        indexer.reconcile(args.rev)
        matches = find_symbol(store, args.rev, args.symbol)
        if not matches:
            print(f"no symbol matching {args.symbol!r}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            print(f"ambiguous symbol {args.symbol!r}:", file=sys.stderr)
            for row in matches:
                print(f"  {row['id']}", file=sys.stderr)
            return 2
        report = impact_report(
            store,
            args.rev,
            matches[0]["id"],
            max_hops=args.hops,
            include_low=args.all,
        )
        print(render_json(report) if args.json else render_text(report))
        return 0
    finally:
        store.close()


def _diff_revspec(root: Path, revspec: str | None) -> tuple[str, str]:
    """Split `<base>..<head>` into its two sides. A bare `<base>` (no `..`)
    diffs it against the worktree. With no argument at all, base defaults
    to `merge_base(default_branch, HEAD)` and head to the worktree --
    "what has this branch changed so far."
    """
    if revspec:
        if ".." in revspec:
            base, _, head = revspec.partition("..")
            if not base:
                # "..HEAD" -- an empty base has no sensible default (unlike
                # an empty head, which reasonably falls back to WORKTREE),
                # so name the missing side rather than passing "" through
                # to raise a blank, nameless error later.
                raise MissingRevisionError("<base>")
            return base, head or WORKTREE
        return revspec, WORKTREE
    if not gitio.is_repo(root):
        raise MissingRevisionError("HEAD")
    try:
        branch = gitio.default_branch(root)
        base = gitio.merge_base(root, branch, "HEAD")
    except gitio.GitError as exc:
        raise MissingRevisionError(str(exc)) from exc
    return base, WORKTREE


def _cmd_diff(args: argparse.Namespace) -> int:
    """Report what changed between two revisions, compared on body_hash."""
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        try:
            base, head = _diff_revspec(root, args.revspec)
            report = diff_report(store, indexer, base, head)
        except MissingRevisionError as exc:
            print(f"revision not found: {exc.rev}", file=sys.stderr)
            return 1
        print(render_json(report) if args.json else render_text(report))
        return 0
    finally:
        store.close()


def _cmd_gc(args: argparse.Namespace) -> int:
    """Prune Layer 1 (the blob parse cache) down to what HEAD, the worktree,
    and any `--keep`-named revisions still reference. Never touches Layer 2,
    so this can never make an existing answer stale -- only slower to
    rebuild for an evicted revision."""
    root = Path(args.path).resolve()
    store = Store.open(root)
    try:
        keep_revs = {"HEAD", WORKTREE, *args.keep}
        removed = gc(store, keep_revs)
        print(f"gc: removed {removed} blob(s) unreferenced by {', '.join(sorted(keep_revs))}")
    finally:
        store.close()
    return 0


def _cmd_install_hooks(args: argparse.Namespace) -> int:
    """Install post-commit/post-checkout/post-merge hooks that warm the
    cache in the background. Purely an optimization -- see D5: every query
    reconciles the working tree itself, so results are identical whether or
    not these ever fire."""
    root = Path(args.path).resolve()
    for path in install_hooks(root):
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codegraph")
    parser.add_argument("--version", action="version", version=__version__)
    parser.set_defaults(handler=None)
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser("status", help="Reconcile and summarize a revision")
    status_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    status_parser.add_argument("--rev", default=WORKTREE, help="Revision to reconcile")
    status_parser.set_defaults(handler=_cmd_status)

    index_parser = subparsers.add_parser("index", help="Reconcile a revision into the graph")
    index_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    index_parser.add_argument("--rev", default=WORKTREE, help="Revision to reconcile")
    index_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Discard the Layer 1 parse cache before reconciling",
    )
    index_parser.add_argument(
        "--quiet", action="store_true", help="Suppress stats output (used by warming hooks)"
    )
    index_parser.set_defaults(handler=_cmd_index)

    resolve_parser = subparsers.add_parser("resolve", help="Resolve a name to node ids")
    resolve_parser.add_argument("query", help="Node id, qualname, or trailing name")
    resolve_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    resolve_parser.add_argument("--rev", default=WORKTREE, help="Revision to resolve against")
    resolve_parser.set_defaults(handler=_cmd_resolve)

    effects_parser = subparsers.add_parser(
        "effects", help="Report side effects reachable from a symbol"
    )
    effects_parser.add_argument("symbol", help="Node id, qualname, or trailing name")
    effects_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    effects_parser.add_argument("--rev", default=WORKTREE, help="Revision to query")
    effects_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    effects_parser.set_defaults(handler=_cmd_effects)

    impact_parser = subparsers.add_parser("impact", help="Report the ranked dependents of a symbol")
    impact_parser.add_argument("symbol", help="Node id, qualname, or trailing name")
    impact_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    impact_parser.add_argument("--rev", default=WORKTREE, help="Revision to query")
    impact_parser.add_argument(
        "--hops", type=int, default=3, help="Maximum hops to walk (default: 3)"
    )
    impact_parser.add_argument(
        "--all", action="store_true", help="Include LOW-confidence dependents"
    )
    impact_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    impact_parser.set_defaults(handler=_cmd_impact)

    diff_parser = subparsers.add_parser(
        "diff", help="Report what changed between two revisions"
    )
    diff_parser.add_argument(
        "revspec",
        nargs="?",
        default=None,
        help="<base>..<head> (default: merge-base(default branch, HEAD)..WORKTREE)",
    )
    diff_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    diff_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    diff_parser.set_defaults(handler=_cmd_diff)

    gc_parser = subparsers.add_parser(
        "gc", help="Prune Layer 1 cache entries unreachable from retained revisions"
    )
    gc_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    gc_parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="REV",
        help="Additional revision to retain besides HEAD and the worktree (repeatable)",
    )
    gc_parser.set_defaults(handler=_cmd_gc)

    hooks_parser = subparsers.add_parser(
        "install-hooks", help="Install git hooks that warm the cache in the background"
    )
    hooks_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    hooks_parser.set_defaults(handler=_cmd_install_hooks)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    if args.handler is None:
        parser.print_help()
        return 0
    return args.handler(args)


def run() -> None:
    sys.exit(main())
