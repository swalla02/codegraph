"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from codegraph import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codegraph")
    parser.add_argument("--version", action="version", version=__version__)
    parser.set_defaults(handler=None)
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
