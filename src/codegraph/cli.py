"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codegraph import __version__, gitio
from codegraph.indexer import FsTreeSource, GitTreeSource, Indexer
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
