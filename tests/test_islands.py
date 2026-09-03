import json

from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.islands import islands_report
from codegraph.store import Store
from tests.conftest import git


def build(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    return store


def rows(report, title):
    return [row for group in report.groups if group.title == title for row in group.rows]


def named_ids(row):
    """The node ids one row names: its `id`, plus any further members its
    `detail` lists after '; also '."""
    _, _, tail = row.detail.partition("; also ")
    return [row.id, *(part for part in tail.split(", ") if part)]


def named(report):
    """Every node id a report mentions -- an island of N members is
    summarized by one row rather than N rows, so this flattens the rows
    back out to the ids they name."""
    return {node_id for group in report.groups for row in group.rows for node_id in named_ids(row)}


def test_a_fully_connected_call_graph_is_one_island(repo, write):
    """The base case the whole report is a generalization of: if every
    symbol is reachable from every other through call edges, there is
    exactly one island and it holds all of them."""
    write(
        "a.py",
        "def alpha():\n    beta()\n\n\ndef beta():\n    gamma()\n\n\ndef gamma():\n    pass\n",
        commit="chain",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary == {
        "symbols": 3,
        "islands": 1,
        "largest": 3,
        "singletons": 0,
        "implicit": 0,
        "network": 0,
        "unexplained": 1,
        "basis": "undirected CALLS edges",
    }
    store.close()


def test_two_separate_call_clusters_are_two_islands(repo, write):
    """Two call clusters with no edge between them must stay apart. This is
    the finding the command exists for -- a partition that merged them
    would report a codebase as one connected region when it is not."""
    write(
        "a.py",
        "def left_top():\n    left_leaf()\n\n\ndef left_leaf():\n    pass\n\n\n"
        "def right_top():\n    right_leaf()\n\n\ndef right_leaf():\n    pass\n",
        commit="two clusters",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["islands"] == 2
    assert report.summary["largest"] == 2
    assert report.summary["singletons"] == 0

    listed = [set(named_ids(row)) for row in rows(report, "islands")]
    assert {"a.py::left_top", "a.py::left_leaf"} in listed
    assert {"a.py::right_top", "a.py::right_leaf"} in listed
    store.close()


def test_a_symbol_with_no_call_in_or_out_is_its_own_island(repo, write):
    """A symbol the resolver linked to nothing in either direction is an
    island of one, reported in the `singletons` group rather than as a
    one-row island group of its own."""
    write(
        "a.py",
        "def linked_top():\n    linked_leaf()\n\n\ndef linked_leaf():\n    pass\n\n\n"
        "def lonely():\n    pass\n",
        commit="one lonely",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["islands"] == 2
    assert report.summary["singletons"] == 1
    assert [row.id for row in rows(report, "singletons")] == ["a.py::lonely"]
    store.close()


def test_island_membership_does_not_depend_on_call_direction(repo, write):
    """`A -> B` and `B -> A` must produce the same partition: an island is
    a region sharing no call relationship of *any* direction, so the
    traversal reads CALLS edges undirected. A directed walk would report
    the mirrored graph differently from the original."""
    write("a.py", "def one():\n    two()\n\n\ndef two():\n    pass\n", commit="forward")
    store = build(repo)
    forward = islands_report(store, "HEAD")
    store.close()

    write("a.py", "def one():\n    pass\n\n\ndef two():\n    one()\n", commit="reversed")
    store = build(repo)
    reversed_report = islands_report(store, "HEAD")

    assert forward.summary["islands"] == reversed_report.summary["islands"] == 1
    assert named(forward) == named(reversed_report) == {"a.py::one", "a.py::two"}
    store.close()


def test_symbols_joined_only_through_a_shared_callee_are_one_island(repo, write):
    """Two callers of one helper are one island even though neither can
    reach the other by following calls forward. Sharing a callee is a call
    relationship; only an undirected traversal sees it."""
    write(
        "a.py",
        "def first():\n    helper()\n\n\ndef second():\n    helper()\n\n\ndef helper():\n    pass\n",
        commit="shared callee",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["islands"] == 1
    assert report.summary["largest"] == 3
    store.close()


def test_a_repo_with_no_python_files_reports_no_islands(repo, write):
    """An empty graph is a real answer, not a crash and not a division by
    zero on `largest`: zero symbols, zero islands, and no groups at all."""
    git(repo, "rm", "-q", "a.py")
    git(repo, "commit", "-qm", "drop the only module")
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary == {
        "symbols": 0,
        "islands": 0,
        "largest": 0,
        "singletons": 0,
        "implicit": 0,
        "network": 0,
        "unexplained": 0,
        "basis": "undirected CALLS edges",
    }
    assert report.groups == []
    assert report.truncated is False
    store.close()


def test_a_repo_where_every_symbol_is_a_singleton(repo, write):
    """The degenerate opposite of one big island: N symbols, N islands, no
    `islands` group at all. The singleton tail is one group of N rows and
    never N groups of one row -- 167 headings reading `island` on
    psf/requests would be noise, not structure."""
    write(
        "a.py",
        "".join(f"def solo_{index}():\n    pass\n\n\n" for index in range(5)),
        commit="five singletons",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["symbols"] == 5
    assert report.summary["islands"] == 5
    assert report.summary["singletons"] == 5
    assert report.summary["largest"] == 1
    assert [group.title for group in report.groups] == ["singletons"]
    assert len(rows(report, "singletons")) == 5
    store.close()


def test_a_module_scope_call_joins_the_symbols_it_links(repo, write):
    """The synthetic `path::<module>` node carries connectivity even though
    it is never a member: two functions called only from module scope share
    an island through it. Dropping those edges would split psf/requests
    into 174 islands instead of 172."""
    write(
        "a.py",
        "def first():\n    pass\n\n\ndef second():\n    pass\n\n\nfirst()\nsecond()\n",
        commit="module scope calls",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["islands"] == 1
    assert report.summary["largest"] == 2
    store.close()


def test_module_nodes_are_never_reported_as_symbols(repo, write):
    """`a.py::<module>` is synthetic, not something anyone wrote, so it is
    excluded from `symbols`, from island sizes and from every row. Counting
    them would invent one island per file whose top level calls nothing --
    32 of them on psf/requests."""
    write("a.py", "def only():\n    pass\n", commit="one symbol")
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["symbols"] == 1
    assert not any("<module>" in node_id for node_id in named(report))
    store.close()


def test_inherits_edges_do_not_join_an_island(repo, write):
    """A subclass and its base are separate islands unless a call links
    them. An island bounds what `impact` and `effects` can say about a
    symbol, and both walk CALLS only -- folding INHERITS in would merge
    psf/requests' 172 islands to 156, claiming boundaries are crossed by a
    walk that cannot cross them."""
    write(
        "a.py",
        "class Base:\n    def shared(self):\n        pass\n\n\n"
        "class Child(Base):\n    def other(self):\n        pass\n",
        commit="inheritance only",
    )
    store = build(repo)
    inherits = store.connection.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE rev='HEAD' AND kind='INHERITS'"
    ).fetchone()["n"]
    assert inherits >= 1  # the edge exists; the report must still not use it
    report = islands_report(store, "HEAD")
    assert report.summary["islands"] == 4
    assert report.summary["singletons"] == 4
    store.close()


def test_limit_is_a_total_budget_and_islands_outrank_singletons(repo, write):
    """`--limit N` caps rows across both groups combined, the contract
    `impact` already documents. Multi-symbol islands take the budget first:
    the long singleton tail must never crowd out the structural finding."""
    write(
        "a.py",
        "def top():\n    leaf()\n\n\ndef leaf():\n    pass\n\n\n"
        + "".join(f"def solo_{index}():\n    pass\n\n\n" for index in range(5)),
        commit="one island and five singletons",
    )
    store = build(repo)
    report = islands_report(store, "HEAD", limit=3)
    assert report.truncated is True
    assert len(rows(report, "islands")) == 1
    assert len(rows(report, "singletons")) == 2
    store.close()


def test_singleton_rows_do_not_claim_the_symbol_is_unused(repo, write):
    """Issue #27 records why 'nothing calls this' is unsound today: a
    dunder, a decorator, framework dispatch or an entry point invokes a
    symbol with no call site anywhere in the source. A singleton row must
    therefore describe the recorded edges, never the symbol's fate --
    including the rows stage 2 could say nothing about, whose wording is
    the weakest negative claim in the tool."""
    write("a.py", "class Thing:\n    def __delitem__(self, key):\n        pass\n", commit="dunder")
    store = build(repo)
    report = islands_report(store, "HEAD")
    details = {row.detail for row in rows(report, "singletons")}
    assert details == {
        "size 1, no resolved call in either direction; implicit: dunder",
        (
            "size 1, no resolved call in either direction;"
            " no implicit-invocation mechanism recognised"
        ),
    }
    text = " ".join(details).lower()
    assert "dead" not in text
    assert "unused" not in text
    assert "unreachable" not in text
    store.close()


def test_islands_command_reports_and_exits_zero(repo, write, capsys):
    """`islands` takes no symbol, so the 0/1/2 exit convention `resolve`,
    `impact` and `effects` share for resolving a name cannot apply: a
    report is always exit 0."""
    write("a.py", "def top():\n    leaf()\n\n\ndef leaf():\n    pass\n", commit="m")
    assert main(["islands", "--path", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("symbols: 2 · islands: 1 · largest: 2 · singletons: 0 ·")


def test_islands_json_output_is_machine_readable(repo, write, capsys):
    """`--json` goes through the same `Report` the text renderer takes, so
    the summary keys are identical in both -- no second output format."""
    write("a.py", "def top():\n    leaf()\n\n\ndef leaf():\n    pass\n", commit="m")
    assert main(["islands", "--path", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["islands"] == 1
    assert payload["groups"][0]["title"] == "islands"


def detail_for(report, node_id):
    """The one row naming `node_id` as its head."""
    return next(
        row.detail for group in report.groups for row in group.rows if row.id == node_id
    )


def test_a_dunder_only_singleton_is_not_reported_as_unexplained(repo, write):
    """`del d[k]` runs `__delitem__` with no call site naming it, which is
    #27's first cause and the reason a singleton count must never be read as
    a dead-code count. On psf/requests `structures.py::CaseInsensitiveDict.
    __delitem__` is an island of one and runs on every deletion."""
    write(
        "a.py",
        "class Store:\n    def __delitem__(self, key):\n        pass\n",
        commit="dunder only",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["implicit"] == 1
    assert report.summary["unexplained"] == 1  # the class itself, which nothing builds
    assert detail_for(report, "a.py::Store.__delitem__").endswith("implicit: dunder")
    store.close()


def test_a_decorated_definition_is_not_reported_as_unexplained(repo, write):
    """A decorator runs at definition time and can register, wrap or replace
    what it decorates -- `@app.route` never produces a call site for the view
    it registers. codegraph does not model what any particular decorator does
    (that is #26's parked annotation treadmill), so the presence of one is
    the whole test."""
    write(
        "a.py",
        "import registry\n\n\n@registry.handler\ndef on_event():\n    pass\n",
        commit="decorated",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert detail_for(report, "a.py::on_event").endswith("implicit: decorator")
    assert report.summary["unexplained"] == 0
    store.close()


def test_a_pytest_entry_point_is_not_reported_as_unexplained(repo, write):
    """A test runner discovers and invokes by naming convention, so a test
    with no caller is the runner's entry point rather than an orphan."""
    write(
        "tests/test_thing.py",
        "class TestThing:\n    def test_works(self):\n        pass\n",
        commit="pytest shapes",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert detail_for(report, "tests/test_thing.py::TestThing.test_works").endswith(
        "implicit: test"
    )
    store.close()


def test_a_helper_in_a_test_file_is_not_excused_by_its_neighbours(repo, write):
    """The pytest rule is deliberately narrower than `impact._is_test`, which
    treats any `tests/` path as a test. Over-matching here would silently
    explain away every unreferenced helper in the test tree -- the exact
    false negative that makes an island report unsafe to act on."""
    write(
        "tests/test_thing.py",
        "def helper():\n    pass\n\n\ndef test_works():\n    pass\n",
        commit="helper beside a test",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert detail_for(report, "tests/test_thing.py::helper").endswith(
        "no implicit-invocation mechanism recognised"
    )
    store.close()


def test_an_override_of_an_inherited_method_is_not_reported_as_unexplained(repo, write):
    """The ABC/subclass shape: a base declares `send`, a subclass overrides
    it, and a caller holding the base type reaches the override by dispatch.
    INHERITS still does not join the two islands (that would misreport what
    an `impact` walk can cross); it labels them instead."""
    write(
        "a.py",
        "class Base:\n    def send(self):\n        pass\n\n\n"
        "class Child(Base):\n    def send(self):\n        pass\n",
        commit="override",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert "override" in detail_for(report, "a.py::Base.send")
    assert "override" in detail_for(report, "a.py::Child.send")
    store.close()


def test_a_nested_definition_is_not_reported_as_unexplained(repo, write):
    """A closure is handed to something as a value -- a callback, a hook, a
    returned function. codegraph records calls and never a reference to a
    name as a value, so its enclosing scope is a mechanism the graph cannot
    show."""
    write(
        "a.py",
        "def outer(bus):\n    def on_event():\n        pass\n\n    bus.append(on_event)\n",
        commit="closure",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert detail_for(report, "a.py::outer.<locals>.on_event").endswith("implicit: nested")
    store.close()


def test_an_imported_symbol_is_not_reported_as_unexplained(repo, write):
    """`from a import Thing` is recorded evidence that something in the tree
    refers to `Thing`, even though referring is not calling and so leaves no
    call edge. This is what covers an exception class named only in a `raise`
    or an `except`: on psf/requests it is why `exceptions.py::Timeout` is not
    left in the unexplained bucket."""
    write("a.py", "class Timeout(Exception):\n    pass\n")
    write("b.py", "from a import Timeout\n\n\ndef go():\n    return 1\n", commit="imported")
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert detail_for(report, "a.py::Timeout").endswith("implicit: import")
    store.close()


def test_an_island_whose_path_leaves_the_process_is_labelled_a_boundary(repo, write):
    """#27's second cause, and the one that is signal rather than defect: the
    call graph ends at the socket because the handler is in another repo, so
    the island boundary IS the service boundary. NETWORK is the only effect
    kind that marks one -- see the module docstring for why DB coupling is
    not, and is not to be reintroduced."""
    write(
        "a.py",
        "import requests\n\n\ndef fetch():\n    return requests.get('http://x')\n",
        commit="network",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["network"] == 1
    assert report.summary["unexplained"] == 0
    assert "boundary: NETWORK" in detail_for(report, "a.py::fetch")
    store.close()


def test_an_env_read_alone_is_a_legend_entry_and_not_a_boundary(repo, write):
    """`ENV_READ` says a region is lit up by a variable, which is worth
    printing; it is not a point where the process ends. Counting it as a
    boundary would claim the graph stops somewhere it does not, and stage 4
    -- which branch of an `if` a variable actually gates -- needs control
    flow codegraph does not have."""
    write(
        "a.py",
        "import os\n\n\ndef configured():\n    return os.getenv('MODE')\n",
        commit="env",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["network"] == 0
    assert report.summary["unexplained"] == 1
    assert "boundary: ENV_READ" in detail_for(report, "a.py::configured")
    store.close()


def test_an_isolated_function_is_still_never_called_dead(repo, write):
    """The strongest negative claim stage 2 permits, and it is still a
    statement about this tool: no resolved call, and no mechanism from a list
    codegraph knows to be incomplete. A user who reads an island row as
    'delete this' has been told something the graph cannot support."""
    write("a.py", "def orphan():\n    pass\n", commit="orphan")
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["unexplained"] == 1
    detail = detail_for(report, "a.py::orphan")
    assert detail == (
        "size 1, no resolved call in either direction;"
        " no implicit-invocation mechanism recognised"
    )
    assert not {"dead", "unused", "unreachable", "orphaned"} & set(detail.lower().split())
    store.close()


def test_a_constructor_call_removes_the_island_the_missing_edge_created(repo, write):
    """The half of stage 2 that is a plain bug rather than a limit: `Cls()`
    implies `Cls.__init__`, so a constructor is not an island of its own.
    Fixing it folded 18 of psf/requests' 172 islands into the rest of the
    graph, taking singletons from 167 to 149 -- every number stage 2 reports
    sits downstream of this edge."""
    write(
        "a.py",
        "class Thing:\n    def __init__(self):\n        pass\n\n\n"
        "def build():\n    return Thing()\n",
        commit="constructed",
    )
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["islands"] == 1
    assert report.summary["largest"] == 3
    assert report.summary["singletons"] == 0
    store.close()
