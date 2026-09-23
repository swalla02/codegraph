"""Unit tests for the caller-discovery comparison (`bench/callers.py`, #59).

The experiment needs a checkout and a trace; its arithmetic needs neither, and
the arithmetic is what the published numbers are made of. A wrong `enclosing`
attributes a grep hit to the wrong function and hands grep a recall it did not
earn; a wrong `reachers` asks a different question than the one reported.
"""

import re

from bench.callers import (
    PATTERNS,
    Corpus,
    GrepResult,
    Outcome,
    Question,
    enclosing,
    grep_callers,
    grep_rounds,
    impact_callers,
    predecessors,
    questions,
    reachers,
    report,
    scopes,
    spread,
)
from bench.score import Trace


def trace(edges, executed=(), indirect=()):
    return Trace.load(
        {
            "edges": [list(edge) for edge in edges],
            "indirect": [list(edge) for edge in indirect],
            "executed": list(executed),
        }
    )


SOURCE = '''
import os


def helper(value):
    return value


class Widget:
    """A class body is a scope of its own: a decorator applied here is a call
    the class makes."""

    @helper
    def paint(self):
        return helper(1)

    def resize(self):
        def inner():
            return helper(2)

        return inner


TOP = helper(3)
'''


def test_scopes_attribute_a_line_to_the_innermost_definition():
    spans = scopes(SOURCE)
    line = next(
        index + 1 for index, text in enumerate(SOURCE.splitlines()) if "return helper(1)" in text
    )
    assert enclosing(spans, line) == "Widget.paint"


def test_a_decorator_line_belongs_to_the_scope_that_applies_it():
    """`@helper` above `paint` is a call made by the class body, not by
    `paint`. `ast` starts a function at its `def`, which is what makes the
    attribution come out right -- and the trace agrees, because the class
    body is the frame that runs it."""
    spans = scopes(SOURCE)
    line = next(
        index + 1 for index, text in enumerate(SOURCE.splitlines()) if text.strip() == "@helper"
    )
    assert enclosing(spans, line) == "Widget"


def test_a_module_level_call_attributes_to_the_module():
    spans = scopes(SOURCE)
    line = next(
        index + 1 for index, text in enumerate(SOURCE.splitlines()) if text.startswith("TOP")
    )
    assert enclosing(spans, line) == "<module>"


def test_a_nested_function_is_named_the_way_the_tracer_names_it():
    spans = scopes(SOURCE)
    line = next(
        index + 1 for index, text in enumerate(SOURCE.splitlines()) if "return helper(2)" in text
    )
    assert enclosing(spans, line) == "Widget.resize.<locals>.inner"


def test_grep_counts_every_matching_line_as_a_cost():
    """The hit count is the reading cost, and the definition line counts:
    somebody searching for callers has to read it to find out it is not
    one."""
    result = grep_callers({"w.py": SOURCE}, "helper", PATTERNS["grep-call"])
    assert result.hits == 4  # the three calls and the `def helper(value)` line
    assert "w.py::Widget.paint" in result.callers
    assert "w.py::<module>" in result.callers


def test_the_word_pattern_finds_a_decorator_the_call_pattern_cannot():
    call = grep_callers({"w.py": SOURCE}, "helper", PATTERNS["grep-call"])
    word = grep_callers({"w.py": SOURCE}, "helper", PATTERNS["grep-word"])
    assert "w.py::Widget" not in call.callers
    assert "w.py::Widget" in word.callers


def test_transitive_grep_charges_for_every_round():
    """Two hops means grepping the names the first round turned up, and the
    cost of the workflow is the sum of what all of them printed."""
    one = grep_rounds({"w.py": SOURCE}, "helper", PATTERNS["grep-word"], 1)
    two = grep_rounds({"w.py": SOURCE}, "helper", PATTERNS["grep-word"], 2)
    assert two.hits > one.hits
    assert one.callers < two.callers


# -- the index that makes django's two hops affordable (#71) -----------------

#: Every way a name can sit next to a word character, which is what the
#: index has to agree with `\b` about. `unsaved` and `saved` must not answer
#: a search for `save`; `obj.save` and `save (x)` must.
NEIGHBOURS = """
def save(self):
    return 1


def unsaved():
    return 2


def saved():
    return 3


def caller(obj):
    obj.save()
    presave_hook = save
    return save (obj), unsaved(), saved(), "save"
"""


def scan_every_file(files, name, pattern):
    """`grep_callers` with no index: the definition the index has to match."""
    expression = re.compile(pattern.format(name=re.escape(name)))
    callers, hits = set(), 0
    for path, source in files.items():
        spans = scopes(source)
        for index, text in enumerate(source.splitlines(), start=1):
            if expression.search(text):
                hits += 1
                callers.add(f"{path}::{enclosing(spans, index)}")
    return GrepResult(frozenset(callers), hits)


def test_the_index_returns_exactly_what_scanning_every_file_returns():
    """The optimisation must not move a published number by one hit.

    Skipping a file is only sound because both patterns are anchored by a
    word boundary on both sides, so every match is a whole word the index
    knows about. If that ever stops being true the numbers change silently,
    so it is asserted against the unindexed scan rather than reasoned about.
    """
    files = {"n.py": NEIGHBOURS, "w.py": SOURCE, "empty.py": "x = 1\n"}
    for name in ("save", "unsaved", "saved", "helper", "absent"):
        for pattern in PATTERNS.values():
            assert grep_callers(files, name, pattern) == scan_every_file(files, name, pattern)


def test_a_name_inside_a_longer_word_is_not_a_hit():
    """The case the index could plausibly get wrong, spelled out."""
    result = grep_callers({"n.py": NEIGHBOURS}, "save", PATTERNS["grep-word"])
    # Four matching LINES: the `def`, the attribute call, the line that
    # binds the bare name (where `presave_hook` is not a hit), and the return
    # (where `save (obj)` and the string are, but `unsaved` and `saved` are
    # not).
    assert result.hits == 4
    assert result.callers == frozenset({"n.py::save", "n.py::caller"})


def test_a_corpus_answers_a_repeated_question_from_the_first_answer():
    """Memoisation is the other half of the speedup, and it has to be
    transparent: the second answer is the first one, not a new one."""
    tree = Corpus({"w.py": SOURCE})
    first = tree.callers("helper", PATTERNS["grep-word"])
    assert tree.callers("helper", PATTERNS["grep-word"]) is first


def test_reachers_walks_backwards_hop_by_hop():
    by_target = predecessors({("a", "b"), ("b", "c"), ("c", "d")})
    assert reachers(by_target, "d", 1) == {"c"}
    assert reachers(by_target, "d", 2) == {"c", "b"}


def test_a_cycle_does_not_loop_and_the_symbol_is_not_its_own_dependent():
    by_target = predecessors({("a", "b"), ("b", "a")})
    assert reachers(by_target, "a", 3) == {"b"}


def test_questions_are_filtered_on_direct_callers_at_every_depth():
    """Asking at two hops must ask about the same symbols as asking at one,
    or the two tables describe different experiments."""
    edges = {(f"src/t.py::c{index}", "src/x.py::target") for index in range(3)}
    edges |= {("src/t.py::outer", "src/t.py::c0")}
    one = questions(trace(edges), package_root="src/", hops=1)
    two = questions(trace(edges), package_root="src/", hops=2)
    assert [item.symbol for item in one] == [item.symbol for item in two] == ["src/x.py::target"]
    assert len(two[0].callers) == len(one[0].callers) + 1


def test_an_indirect_edge_is_not_gold():
    """A pair only an out-of-repo frame connects is named by no text in the
    repository, so no reader of the source could produce it."""
    edges = {(f"src/t.py::c{index}", "src/x.py::target") for index in range(4)}
    indirect = {("src/t.py::c3", "src/x.py::target")}
    asked = questions(trace(edges, indirect=indirect), package_root="src/", hops=1)
    assert asked[0].callers == frozenset(f"src/t.py::c{index}" for index in range(3))


def test_questions_exclude_dunders_nested_functions_and_other_packages():
    edges = set()
    for target in (
        "src/x.py::Widget.__eq__",
        "src/x.py::outer.<locals>.inner",
        "tests/t.py::helper",
    ):
        edges |= {(f"src/t.py::c{index}", target) for index in range(3)}
    assert questions(trace(edges), package_root="src/", hops=1) == []


def test_conditional_precision_only_judges_claims_whose_caller_ran():
    question = Question("src/x.py::target", frozenset({"src/t.py::real"}))
    found = frozenset({"src/t.py::real", "src/t.py::ran_but_never_called", "src/t.py::never_ran"})
    built = report(
        "tool",
        [Outcome(question, "tool", found, cost=3)],
        frozenset({"src/t.py::real", "src/t.py::ran_but_never_called"}),
    )
    assert built.testable == 2
    assert built.observed == 1
    assert built.conditional_precision == 0.5
    assert built.recall == 1.0


def test_yield_is_true_callers_per_line_of_output():
    question = Question("src/x.py::target", frozenset({"src/t.py::real"}))
    built = report(
        "tool", [Outcome(question, "tool", frozenset({"src/t.py::real"}), 4)], frozenset()
    )
    assert built.yield_per_row == 0.25


def test_spread_reports_quartiles_not_an_average():
    assert spread([0.0, 0.0, 1.0, 1.0]) == (0.0, 0.5, 1.0)


def test_impact_callers_reads_every_group_of_the_report():
    payload = {
        "groups": [
            {"title": "dependents", "rows": [{"id": "src/a.py::one"}]},
            {"title": "tests", "rows": [{"id": "tests/b.py::two"}]},
        ]
    }
    assert impact_callers(payload) == {"src/a.py::one", "tests/b.py::two"}
