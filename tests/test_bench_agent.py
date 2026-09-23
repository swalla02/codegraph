"""Unit tests for the agent A/B harness (`bench/agent.py`, #59).

The experiment costs money and cannot run here. What can run here is
everything that decides what the money bought: that the two arms differ only
in their tools, that an answer is parsed into the symbols it named, and that
the paired difference and its interval are computed the way the README says.
"""

import json
from dataclasses import asdict
from pathlib import Path

from bench.agent import (
    CODEGRAPH_TOOLS,
    NODE_ID,
    TOOLS,
    Run,
    bootstrap,
    command,
    paired,
    parse_answer,
    read_runs,
    read_stream,
)
from bench.agent_tools import TOOLS as SERVER_TOOLS
from bench.agent_tools import respond
from bench.callers import Question


def test_an_answer_is_parsed_out_of_whatever_the_agent_wrapped_it_in():
    answer = "- `src/flask/app.py::Flask.run`\n2. tests/test_cli.py::test_one\nnothing here"
    assert parse_answer(answer) == {
        "src/flask/app.py::Flask.run",
        "tests/test_cli.py::test_one",
    }


def test_an_answer_naming_a_nested_function_parses():
    assert NODE_ID.findall("tests/t.py::test_x.<locals>.index") == [
        ("tests/t.py", "test_x.<locals>.index")
    ]


def test_the_stream_gives_back_the_result_and_what_the_run_called():
    stdout = """\
{"type":"system","subtype":"init"}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Grep"}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Grep"}]}}
not json at all
{"type":"result","result":"src/a.py::one","num_turns":3}"""
    result, tools = read_stream(stdout)
    assert result["result"] == "src/a.py::one"
    assert tools == {"Grep": 2}


def test_the_tool_server_answers_the_handshake_and_lists_its_tools(tmp_path):
    handshake = respond({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, tmp_path)
    assert handshake["result"]["serverInfo"]["name"] == "codegraph-bench"
    listed = respond({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, tmp_path)
    assert [tool["name"] for tool in listed["result"]["tools"]] == [
        tool["name"] for tool in SERVER_TOOLS
    ]


def test_a_notification_is_not_answered(tmp_path):
    assert respond({"jsonrpc": "2.0", "method": "notifications/initialized"}, tmp_path) is None


def test_an_unknown_tool_is_an_error_and_not_a_crash(tmp_path):
    reply = respond(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}},
        tmp_path,
    )
    assert reply["error"]["code"] == -32602


def test_a_failing_query_comes_back_as_a_tool_error_the_agent_can_read(tmp_path):
    """A tool that raises should leave the agent with something to act on.
    An exception escaping the server would kill the run instead, and the run
    would be scored as an empty answer."""
    reply = respond(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "codegraph_impact", "arguments": {"symbol": "nothing_here"}},
        },
        tmp_path / "not-a-repository",
    )
    assert reply["result"]["isError"] is True


def run(arm, symbol, named, turns=5, cost=0.02):
    return Run(
        arm=arm,
        symbol=symbol,
        repetition=0,
        answer="",
        named=list(named),
        turns=turns,
        cost_usd=cost,
        seconds=1.0,
        tools={},
    )


def test_the_two_arms_differ_only_in_the_tools_they_are_given():
    """The claim the whole experiment rests on, asserted rather than
    described: every flag but the MCP one is shared, and the treatment's
    allow-list is the control's plus the codegraph tools."""

    def without_tools(argv):
        at = argv.index("--allowedTools")
        return argv[:at] + argv[at + 2 :]

    control = command("control", "q", Path("/repo"), "{}")
    treatment = command("treatment", "q", Path("/repo"), "{}")
    shared = without_tools(control)
    assert without_tools(treatment) == [*shared, "--mcp-config", "{}"]
    assert control[control.index("--allowedTools") + 1] == TOOLS
    assert treatment[treatment.index("--allowedTools") + 1] == ",".join((TOOLS, *CODEGRAPH_TOOLS))


def test_a_paired_difference_averages_the_repeats_before_subtracting():
    question = Question("src/x.py::target", frozenset({"a", "b"}))
    runs = [
        run("control", question.symbol, ["a"]),
        run("control", question.symbol, ["a", "b"]),
        run("treatment", question.symbol, ["a", "b"]),
        run("treatment", question.symbol, ["a", "b"]),
    ]
    assert paired(runs, {question.symbol: question}, "recall") == [0.25]


def test_a_question_only_one_arm_answered_is_not_paired():
    question = Question("src/x.py::target", frozenset({"a"}))
    runs = [run("control", question.symbol, ["a"])]
    assert paired(runs, {question.symbol: question}, "recall") == []


def test_the_bootstrap_interval_is_seeded_and_brackets_the_observed_mean():
    observed, low, high = bootstrap([0.0, 0.0, 0.5, 1.0], resamples=2000)
    assert observed == 0.375
    assert low <= observed <= high
    assert bootstrap([0.0, 0.0, 0.5, 1.0], resamples=2000) == (observed, low, high)


def test_an_interval_around_no_difference_at_all_is_empty():
    assert bootstrap([0.0, 0.0, 0.0], resamples=500) == (0.0, 0.0, 0.0)


def test_recorded_runs_are_re_readable_so_a_number_can_be_rechecked(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text(json.dumps(asdict(run("control", "src/x.py::target", ["a"]))) + "\n")
    assert read_runs(path) == [run("control", "src/x.py::target", ["a"])]
