"""codegraph's queries, exposed to an agent as tools (#59).

    python -m bench.agent_tools --path /path/to/repo

A stdio MCP server, spoken by hand over JSON-RPC because this package has no
dependencies and a benchmark is a poor reason to acquire one. It exists so the
A/B in `bench/agent.py` can differ in *tool availability* and nothing else: the
two arms get the same model, the same system prompt, the same questions and the
same turn cap, and one of them additionally sees these three tools in its tool
list. Telling one arm about codegraph in its prompt instead would have made the
prompt the variable under test.

The descriptions below are the CLI's own help text. Selling the tool harder
than the CLI does would be measuring the description.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codegraph.cli import build_parser
from codegraph.store import WORKTREE

PROTOCOL = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "codegraph_impact",
        "description": (
            "Report the ranked dependents of a symbol: everything a change to it"
            " could break, with each one's hop count and confidence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Node id, qualname, or trailing name",
                },
                "hops": {"type": "integer", "description": "Maximum hops to walk (default 3)"},
                "all": {
                    "type": "boolean",
                    "description": "Merge LOW-confidence dependents into the main groups",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codegraph_effects",
        "description": (
            "Report the side effects reachable from a symbol, each with a witness"
            " path to the call that causes it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Node id, qualname, or trailing name",
                }
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codegraph_orphans",
        "description": "Report definitions nothing in the repository reaches.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Maximum rows to print"}},
        },
    },
]


def _invoke(root: Path, argv: list[str]) -> str:
    """Run one codegraph command in-process and return what it printed.

    In-process, through the CLI's own parser, so the agent sees byte for byte
    what a person running the command would see -- including the exit-code
    conventions and the `unknowns` section, which are half of what the tool
    claims to be for.
    """
    import contextlib
    import io

    parser = build_parser()
    args = parser.parse_args([*argv, "--path", str(root), "--rev", WORKTREE])
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = args.handler(args)
        except SystemExit as exit_code:  # argparse inside a handler
            code = int(exit_code.code or 0)
    text = out.getvalue() + err.getvalue()
    return text if text.strip() else f"(no output, exit {code})"


def handlers(root: Path) -> dict[str, Callable[[dict], str]]:
    return {
        "codegraph_impact": lambda arguments: _invoke(
            root,
            [
                "impact",
                str(arguments["symbol"]),
                "--hops",
                str(arguments.get("hops", 3)),
                *(["--all"] if arguments.get("all") else []),
            ],
        ),
        "codegraph_effects": lambda arguments: _invoke(root, ["effects", str(arguments["symbol"])]),
        "codegraph_orphans": lambda arguments: _invoke(
            root, ["orphans", "--limit", str(arguments.get("limit", 40))]
        ),
    }


def respond(request: dict, root: Path) -> dict | None:
    """Answer one JSON-RPC message, or None for a notification.

    Pure apart from the codegraph queries themselves, so
    `tests/test_bench_agent.py` can drive the whole protocol without a
    subprocess or a socket.
    """
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        return None  # a notification: `initialized`, `cancelled`, ...
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "codegraph-bench", "version": "0.1.0"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        handler = handlers(root).get(name)
        if handler is None:
            return _error(request_id, -32602, f"unknown tool: {name}")
        try:
            text = handler(params.get("arguments") or {})
        except Exception as failure:  # noqa: BLE001 -- a tool error is a result, not a crash
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"{type(failure).__name__}: {failure}"}],
                    "isError": True,
                },
            }
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    elif method == "ping":
        result = {}
    else:
        return _error(request_id, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(root: Path, stream_in=sys.stdin, stream_out=sys.stdout) -> None:
    for line in stream_in:
        line = line.strip()
        if not line:
            continue
        reply = respond(json.loads(line), root)
        if reply is None:
            continue
        stream_out.write(json.dumps(reply) + "\n")
        stream_out.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.agent_tools")
    parser.add_argument("--path", required=True, help="Repository the queries answer about")
    args = parser.parse_args(argv)
    serve(Path(args.path).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
