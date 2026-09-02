"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codegraph import __version__, gitio
from codegraph.indexer import FsTreeSource, GitTreeSource, Indexer
from codegraph.query.effects import effects_report
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
        _print_stats(indexer.reconcile(args.rev))
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
