"""`orphans`: functions whose every recorded caller is a test.

The bug class (#37) is a helper that is defined, tested, and never invoked
by the code that was supposed to invoke it -- and the reason it needs its
own command is that it is invisible to every other one. `impact` needs the
symbol's name, which is the thing the reader does not have yet, and
`islands` cannot see it at all: the test's call is a real edge, so the
function is not a one-symbol island.

Each test below pins one filter, because the filters are what make the
output reviewable rather than a second 5,000-row list. The ones that pin an
exclusion assert on the summary counter for the filter under test as well
as on the rows, so a test that "passes because something else also excluded
it" fails instead.
"""

import json

from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.islands import is_test_path
from codegraph.query.orphans import orphans_report
from codegraph.render import render_text
from codegraph.store import Store


def build(repo):
    """Reconcile HEAD and hand back the store plus the tree source.

    The source is not optional: the last filter reads the revision's source
    text, and every caller of `orphans_report` has to supply a way to.
    """
    store = Store.open(repo)
    source = GitTreeSource(repo)
    Indexer(repo, store, source).reconcile("HEAD")
    return store, source


def report(repo, **kwargs):
    store, source = build(repo)
    try:
        return orphans_report(store, "HEAD", source, **kwargs)
    finally:
        store.close()


def ids(result):
    return {row.id for group in result.groups for row in group.rows}


def detail_for(result, node_id):
    for group in result.groups:
        for row in group.rows:
            if row.id == node_id:
                return row.detail
    raise AssertionError(f"{node_id} not in report: {sorted(ids(result))}")


CALLED_BY_A_TEST = "from a import _looks_like_jsonl\n\n\ndef test_it():\n    assert _looks_like_jsonl('{}')\n"


def test_a_function_only_its_test_calls_is_the_whole_point(repo, write):
    """The finding the command exists for, in its smallest form.

    `_looks_like_jsonl` on the project in #37 was born dead in the same
    commit that added a comment claiming it was invoked; its test called it
    directly, so the test passed and the call graph recorded a real caller.
    That caller being a test is the entire signal.
    """
    write("a.py", "def _looks_like_jsonl(line):\n    return line.startswith('{')\n")
    write("tests/test_a.py", CALLED_BY_A_TEST, commit="tested helper")
    result = report(repo)
    assert ids(result) == {"a.py::_looks_like_jsonl"}
    assert result.summary["test_callers_only"] == 1
    assert result.summary["candidates"] == 1
    assert result.summary["reported"] == 1
    assert "1 test caller" in detail_for(result, "a.py::_looks_like_jsonl")
    assert "tests/test_a.py::test_it" in detail_for(result, "a.py::_looks_like_jsonl")


def test_a_function_production_also_calls_is_not_reported(repo, write):
    """The other half of the question, and the half that keeps the report
    from being `islands` with extra steps: a helper the application itself
    calls is doing its job, however many tests also call it.

    Asserted on `test_callers_only` rather than only on the rows, because
    the row would also have been removed by the name scan (a call site IS a
    textual mention). Pinning the counter is what makes this a test of the
    caller filter specifically.
    """
    write(
        "a.py",
        "def _helper():\n    return 1\n\n\ndef run():\n    return _helper()\n",
    )
    write(
        "tests/test_a.py",
        "from a import _helper\n\n\ndef test_it():\n    assert _helper() == 1\n",
        commit="both",
    )
    result = report(repo)
    assert result.summary["test_callers_only"] == 0
    assert ids(result) == set()


def test_a_function_nothing_calls_at_all_is_left_to_islands(repo, write):
    """Deliberately disjoint from `islands`, which already reports this as a
    one-symbol island -- and the case a static call graph is weakest on, since
    "no caller anywhere" is exactly what framework dispatch looks like.

    A recorded test caller is the stronger position and the one this command
    is willing to stand behind: something DOES call it, codegraph found the
    call site, and it is a test.
    """
    write("a.py", "def _never_called():\n    return 1\n", commit="orphan")
    result = report(repo)
    assert result.summary["functions"] == 1
    assert result.summary["test_callers_only"] == 0
    assert ids(result) == set()


def test_a_name_the_source_passes_as_a_value_is_not_reported(repo, write):
    """The filter that is not optional, and has no flag (#37).

    A static call graph cannot see a function handed over as a value:
    `signal.signal(SIGINT, _handle_sigint)` records a call to
    `signal.signal` and nothing at all about `_handle_sigint`, whose only
    recorded caller is therefore its test. Shipping this query without the
    source-text scan would put a live signal handler at the top of a list
    users read as "delete these".
    """
    write(
        "a.py",
        "import signal\n\n\ndef _handle_sigint(signum, frame):\n    return signum\n\n\n"
        "def install():\n    signal.signal(signal.SIGINT, _handle_sigint)\n",
    )
    write(
        "tests/test_a.py",
        "from a import _handle_sigint\n\n\ndef test_it():\n"
        "    assert _handle_sigint(2, None) == 2\n",
        commit="callback",
    )
    result = report(repo)
    assert result.summary["test_callers_only"] == 1
    assert result.summary["candidates"] == 1
    assert result.summary["name_referenced"] == 1
    assert result.summary["reported"] == 0
    assert ids(result) == set()


def test_a_functions_own_body_and_docstring_do_not_count_as_a_reference(repo, write):
    """The other side of that scan: the name is guaranteed to appear in the
    file that defines it -- on the `def` line, in its docstring, and in its
    own recursive call -- and none of that is evidence anything reaches it.

    So the scan discounts the definition's own line span, and a self-call is
    not a caller. Without both, the filter would reject every candidate and
    the command would always report nothing, which is the failure mode that
    looks exactly like a clean repository.
    """
    write(
        "a.py",
        "def _countdown(n):\n"
        '    """_countdown calls _countdown."""\n'
        "    if n <= 0:\n        return 0\n    return _countdown(n - 1)\n",
    )
    write(
        "tests/test_a.py",
        "from a import _countdown\n\n\ndef test_it():\n    assert _countdown(3) == 0\n",
        commit="recursive",
    )
    result = report(repo)
    assert ids(result) == {"a.py::_countdown"}


def test_a_decorated_function_is_not_reported_unless_asked_for(repo, write):
    """A decorator runs at definition time and can register, wrap or replace
    what it decorates, so the call site can be inside a framework that is not
    in this tree -- the same counter-evidence `islands` labels an island
    with. `--include-decorated` is there for a reader who knows the
    decorator in their own repository is inert.
    """
    write(
        "a.py",
        "def register(fn):\n    return fn\n\n\n@register\ndef _handler():\n    return 1\n",
    )
    write(
        "tests/test_a.py",
        "from a import _handler\n\n\ndef test_it():\n    assert _handler() == 1\n",
        commit="decorated",
    )
    result = report(repo)
    assert result.summary["test_callers_only"] == 1
    assert result.summary["candidates"] == 0
    assert ids(result) == set()

    relaxed = report(repo, include_decorated=True)
    assert ids(relaxed) == {"a.py::_handler"}


def test_a_dunder_is_never_reported(repo, write):
    """`Thing(1)` in a test is a recorded call to `Thing.__init__` and often
    the only one in the tree, so a dunder would otherwise head the report.

    It is invoked by syntax rather than by name, which also makes the
    source-text scan meaningless for it: nothing anywhere spells
    `__init__`. Excluded outright rather than by flag.
    """
    write("a.py", "class Thing:\n    def __init__(self, value):\n        self.value = value\n")
    write(
        "tests/test_a.py",
        "from a import Thing\n\n\ndef test_it():\n    assert Thing(1).value == 1\n",
        commit="dunder",
    )
    result = report(repo)
    assert result.summary["test_callers_only"] == 1
    assert "a.py::Thing.__init__" not in ids(result)
    assert ids(result) == set()


def test_a_public_function_is_not_reported_unless_asked_for(repo, write):
    """Pinned to EXCLUDED by default, because the likeliest explanation for
    a public function whose only caller is a test is a caller that is not in
    this repository at all -- a library's own surface. That is the exact
    misreading of `islands`' unexplained bucket the README warns about, and
    a report that led with it would earn the same disclaimer.

    A leading underscore is the author stating that no external caller is
    supposed to exist, which is what makes the absence of an internal one
    worth a reader's time. `--include-public` is the way to ask anyway.
    """
    write("a.py", "def looks_like_jsonl(line):\n    return line.startswith('{')\n")
    write(
        "tests/test_a.py",
        "from a import looks_like_jsonl\n\n\ndef test_it():\n"
        "    assert looks_like_jsonl('{}')\n",
        commit="public",
    )
    result = report(repo)
    assert result.summary["test_callers_only"] == 1
    assert result.summary["candidates"] == 0
    assert ids(result) == set()

    relaxed = report(repo, include_public=True)
    assert ids(relaxed) == {"a.py::looks_like_jsonl"}


def test_a_helper_in_the_test_tree_is_a_test_caller_not_a_candidate(repo, write):
    """"Is this a test?" is a file-level question here, not a
    pytest-collection one: a fixture in `conftest.py` and a plain helper in
    `tests/support.py` are as much test code as a collected `test_foo`, and
    neither is a production caller.

    Both directions are pinned. `tests/support.py::call_it` counts as a
    test caller of `_helper` (so `_helper` is reported), and `call_it`
    itself is not a candidate however few things call it -- a helper in the
    test tree called only from the test tree is a fixture doing its job.
    """
    write("a.py", "def _helper():\n    return 1\n")
    write("tests/support.py", "from a import _helper\n\n\ndef call_it():\n    return _helper()\n")
    write(
        "tests/test_a.py",
        "from tests.support import call_it\n\n\ndef test_it():\n    assert call_it() == 1\n",
        commit="support",
    )
    result = report(repo)
    assert ids(result) == {"a.py::_helper"}
    assert result.summary["functions"] == 1  # nothing under tests/ was even considered


def test_every_test_tree_spelling_counts(repo, write):
    """Soup uses `tests/`; the projects this will next be pointed at use
    `test/`, `*_test.py`, `conftest.py`, or a package-internal `tests`
    subpackage. All four are one predicate, `islands.is_test_path`, shared
    with the mechanism `islands` labels an island `test` with -- so the two
    commands cannot come to disagree about what a test is.
    """
    assert is_test_path("tests/test_a.py")
    assert is_test_path("test/helpers.py")
    assert is_test_path("a/b_test.py")
    assert is_test_path("conftest.py")
    assert is_test_path("src/pkg/tests/support.py")
    assert not is_test_path("src/pkg/latest/thing.py")
    assert not is_test_path("src/pkg/contest.py")

    write("a.py", "def _one():\n    return 1\n\n\ndef _two():\n    return 2\n")
    write("pkg_test.py", "from a import _one\n\n\ndef test_one():\n    assert _one() == 1\n")
    write(
        "conftest.py",
        "import pytest\nfrom a import _two\n\n\n@pytest.fixture\ndef two():\n    return _two()\n",
        commit="spellings",
    )
    result = report(repo)
    assert ids(result) == {"a.py::_one", "a.py::_two"}


def test_no_row_says_dead_unused_or_unreachable(repo, write):
    """The discipline `islands` established and this command inherits. Three
    of the five survivors on the project in #37 were deliberate -- two
    documented back-compat shims and one import probe whose docstring says
    test-only is the point -- and a user who deletes one on this tool's say-so
    must not be able to point at its wording.

    The strongest available claim is about codegraph's knowledge: no caller
    outside the test tree was recorded, and the name is not in the source
    text. The blind spot rides in the summary, where the rows are, rather
    than in prose in a README the reader may never have opened.
    """
    write("a.py", "def _looks_like_jsonl(line):\n    return line.startswith('{')\n")
    write("tests/test_a.py", CALLED_BY_A_TEST, commit="wording")
    result = report(repo)
    text = render_text(result).lower()
    for word in ("dead", "unused", "unreachable", "orphaned", "safe to delete"):
        assert word not in text
    assert "getattr" in text  # the callback caveat, on the page with the rows
    assert "list to review, not a list to delete" in text
    assert result.summary["basis"] == "recorded callers plus a name scan of the source text"


def test_the_caveat_is_printed_even_when_nothing_matched(repo, write):
    """An empty report is the one most likely to be read as "there is nothing
    here", so it carries the same caveat as a full one. `basis` and `caveat`
    are summary fields, not row decoration, for exactly that reason."""
    write("b.py", "def beta():\n    return 2\n", commit="empty")
    result = report(repo)
    assert result.groups == []
    assert result.summary["reported"] == 0
    assert "getattr" in render_text(result)


def test_limit_budgets_the_rows_and_flags_truncation(repo, write):
    """Same `budget` contract as every other report: `--limit` bounds the
    page and `truncated` says the page is not the whole answer."""
    source = "".join(f"def _helper{n}():\n    return {n}\n\n\n" for n in range(4))
    calls = "".join(f"    assert _helper{n}() == {n}\n" for n in range(4))
    imports = ", ".join(f"_helper{n}" for n in range(4))
    write("a.py", source)
    write("tests/test_a.py", f"from a import {imports}\n\n\ndef test_all():\n{calls}", commit="many")
    result = report(repo, limit=2)
    assert result.summary["reported"] == 4
    assert len(ids(result)) == 2
    assert result.truncated


def test_orphans_command_reports_and_exits_zero(repo, write, capsys):
    """Global like `islands`, so it shares `islands`' exit convention rather
    than the 0/1/2 one the symbol-taking commands use for resolving a name:
    a report is exit 0, and only an unresolvable `--rev` is 1."""
    write("a.py", "def _looks_like_jsonl(line):\n    return line.startswith('{')\n")
    write("tests/test_a.py", CALLED_BY_A_TEST, commit="cli")
    assert main(["orphans", "--path", str(repo), "--rev", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert "a.py::_looks_like_jsonl" in out
    assert "reported: 1" in out

    assert main(["orphans", "--path", str(repo), "--rev", "HEAD", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["reported"] == 1
    assert payload["groups"][0]["title"] == "orphans"
    assert payload["summary"]["caveat"]

    assert main(["orphans", "--path", str(repo), "--rev", "nope"]) == 1
    assert "revision not found" in capsys.readouterr().err
