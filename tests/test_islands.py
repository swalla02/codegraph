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
    therefore describe the recorded edges, never the symbol's fate."""
    write("a.py", "class Thing:\n    def __delitem__(self, key):\n        pass\n", commit="dunder")
    store = build(repo)
    report = islands_report(store, "HEAD")
    details = {row.detail for row in rows(report, "singletons")}
    assert details == {"size 1, no resolved call in either direction"}
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
