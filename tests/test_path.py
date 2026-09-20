"""Behaviours of the `path` report: how two named symbols are connected,
or which of the three ways they are not.

Each test below fixes one claim the report makes. The negative ones matter
most: three different sentences ("nothing connects these at all", "nothing
connects them within the hop budget you set", "no walk can ever connect
them") are three different pieces of advice, and collapsing them into one
"not found" is the failure this command exists to avoid.
"""

import json

from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.path import path_report
from codegraph.store import Store

FORWARD = "forward"
REVERSE = "reverse"


def build(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    return store


def chain(report, title=FORWARD):
    """The node ids one direction's rows name, in walk order."""
    return [row.id for group in report.groups if group.title == title for row in group.rows]


def details(report, title=FORWARD):
    return [row.detail for group in report.groups if group.title == title for row in group.rows]


# -- a path that exists ------------------------------------------------


def test_a_forward_path_names_every_hop_in_walk_order(repo, write):
    write(
        "m.py",
        "def alpha():\n    beta()\n\n\ndef beta():\n    gamma()\n\n\ndef gamma():\n    pass\n",
        commit="m",
    )
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::alpha", "m.py::gamma")
    assert report.summary["direction"] == FORWARD
    assert report.summary["hops"] == 2
    assert chain(report) == ["m.py::alpha", "m.py::beta", "m.py::gamma"]
    store.close()


def test_the_reverse_direction_is_checked_and_reported_as_such(repo, write):
    """`path gamma alpha` must not read as "no connection". The walk runs
    both ways, and the rows still read along the edges -- from the symbol
    that reaches to the one that is reached -- so a reader never has to
    work out which end is which."""
    write(
        "m.py",
        "def alpha():\n    beta()\n\n\ndef beta():\n    gamma()\n\n\ndef gamma():\n    pass\n",
        commit="m",
    )
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::gamma", "m.py::alpha")
    assert report.summary["direction"] == REVERSE
    assert report.summary["forward"] == "none"
    assert chain(report, REVERSE) == ["m.py::alpha", "m.py::beta", "m.py::gamma"]
    store.close()


def test_both_directions_are_reported_when_the_pair_is_in_a_cycle(repo, write):
    write(
        "m.py",
        "def alpha():\n    beta()\n\n\ndef beta():\n    alpha()\n",
        commit="cycle",
    )
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::alpha", "m.py::beta")
    assert report.summary["direction"] == "both"
    assert chain(report, FORWARD) == ["m.py::alpha", "m.py::beta"]
    assert chain(report, REVERSE) == ["m.py::beta", "m.py::alpha"]
    store.close()


def test_a_symbol_reaches_itself_in_no_hops(repo, write):
    write("m.py", "def alpha():\n    pass\n", commit="m")
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::alpha", "m.py::alpha")
    assert report.summary["direction"] == "same symbol"
    assert report.summary["hops"] == 0
    assert report.groups == []
    store.close()


# -- what each hop says ------------------------------------------------


def test_each_hop_names_its_edge_kind(repo, write):
    """Since #52 "connected" means one of four things, so a hop that does
    not say which is not an answer."""
    write(
        "m.py",
        "class Base:\n    pass\n\n\nclass Sub(Base):\n    pass\n",
        commit="inherit",
    )
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::Sub", "m.py::Base")
    assert "INHERITS" in details(report)[1]
    store.close()


def test_a_hop_names_the_call_site_that_makes_it(repo, write):
    """`effects` gives a witness down to the `file:line`; a hop is a claim
    of exactly the same kind, and `edges` already stores the site."""
    write("m.py", "def alpha():\n    beta()\n\n\ndef beta():\n    pass\n", commit="m")
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::alpha", "m.py::beta")
    assert "m.py:2" in details(report)[1]
    store.close()


def test_the_paths_confidence_is_its_weakest_hop(repo, write):
    """Five HIGH hops and four HIGH plus one LOW are different answers, so
    the path reports the weakest of its hops and says which hop that was."""
    write(
        "m.py",
        "def middle(item):\n    item.frobnicate()\n\n\n"
        "def frobnicate():\n    sink()\n\n\n"
        "def sink():\n    pass\n",
        commit="m",
    )
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::middle", "m.py::sink")
    hops = details(report)
    assert report.summary["confidence"] == "MEDIUM"
    assert "MEDIUM confidence" in hops[1] and "weakest hop" in hops[1]
    assert "HIGH confidence" in hops[2] and "weakest hop" not in hops[2]
    store.close()


# -- LOW, and the fan-out that is never in the graph --------------------


def test_low_hops_are_excluded_by_default_and_included_with_all(repo, write):
    """The bare-name fan-out is LOW by construction and is not in `edges`
    at all, so it rides on the same switch as every other LOW hop -- and
    the negative answer has to point at that switch rather than claim
    nothing connects the two."""
    write("a.py", "class One:\n    def save(self):\n        pass\n")
    write("b.py", "class Two:\n    def save(self):\n        pass\n")
    write("c.py", "def caller(item):\n    item.save()\n", commit="ambiguous")
    store = build(repo)

    default = path_report(store, "HEAD", "c.py::caller", "a.py::One.save")
    assert default.summary["direction"] == "none"
    assert "different islands" not in default.summary["reason"]
    assert default.summary["show_path"] == "--all"

    everything = path_report(store, "HEAD", "c.py::caller", "a.py::One.save", include_low=True)
    assert everything.summary["direction"] == FORWARD
    assert everything.summary["confidence"] == "LOW"
    assert chain(everything, FORWARD) == ["c.py::caller", "a.py::One.save"]
    assert "save" in details(everything)[1]
    store.close()


# -- the three negatives -----------------------------------------------


def test_different_islands_is_the_strongest_negative(repo, write):
    """No walk can ever connect them, at any hop count and any confidence.
    That is a different fact from "I did not find one" and must not be
    printed as if it were the same."""
    write("left.py", "def left():\n    pass\n")
    write("right.py", "def right():\n    pass\n", commit="apart")
    store = build(repo)
    report = path_report(store, "HEAD", "left.py::left", "right.py::right")
    assert report.summary["direction"] == "none"
    assert "different islands" in report.summary["reason"]
    assert "show_path" not in report.summary
    store.close()


def test_a_common_caller_puts_two_symbols_on_one_island_with_no_path(repo, write):
    """The middle negative: connected, but not by any directed walk. A
    reader told "different islands" here would conclude the two cannot
    affect each other, which is false -- their caller is the coupling."""
    write(
        "m.py",
        "def caller():\n    left()\n    right()\n\n\n"
        "def left():\n    pass\n\n\ndef right():\n    pass\n",
        commit="fork",
    )
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::left", "m.py::right")
    assert report.summary["direction"] == "none"
    assert report.summary["reason"] == "no directed path in either direction"
    store.close()


def test_a_module_top_level_connects_an_island_without_making_a_path(repo, write):
    """`islands` counts `path::<module>` nodes for connectivity and never
    as members, and nothing in the graph ever points AT one -- so a pair
    joined only by a file's top level is one island with no directed path,
    and both commands have to say so without contradicting each other."""
    write(
        "m.py",
        "def left():\n    pass\n\n\ndef right():\n    pass\n\n\nleft()\nright()\n",
        commit="toplevel",
    )
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::left", "m.py::right")
    assert report.summary["reason"] == "no directed path in either direction"
    store.close()


def test_a_path_beyond_the_hop_budget_is_not_the_same_answer_as_no_path(repo, write):
    """...and the report says how many hops the answer is at, because a
    count of what you cannot see with no way to see it is half an answer
    (the same reasoning as `impact`'s `show_hidden`)."""
    write(
        "m.py",
        "def h0():\n    h1()\n\n\ndef h1():\n    h2()\n\n\n"
        "def h2():\n    h3()\n\n\ndef h3():\n    pass\n",
        commit="chain",
    )
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::h0", "m.py::h3", max_hops=2)
    assert report.summary["direction"] == "none"
    assert report.summary["reason"] == "no directed path within 2 hops"
    assert report.summary["show_path"] == "--hops 3"
    assert path_report(store, "HEAD", "m.py::h0", "m.py::h3", max_hops=3).summary["hops"] == 3
    store.close()


# -- determinism -------------------------------------------------------


def test_the_strongest_of_two_equally_short_paths_wins(repo, write):
    """Widest-path bias, as `impact` and `effects/propagate` already use:
    among paths of equal length the one whose weakest hop is strongest is
    the better answer, even when the tie-break below would prefer the
    other."""
    write(
        "m.py",
        "def alpha(item):\n    item.ambiguous_mid()\n    zz_mid()\n\n\n"
        "def ambiguous_mid():\n    omega()\n\n\n"
        "def zz_mid():\n    omega()\n\n\ndef omega():\n    pass\n",
        commit="two",
    )
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::alpha", "m.py::omega")
    assert report.summary["confidence"] == "HIGH"
    assert chain(report) == ["m.py::alpha", "m.py::zz_mid", "m.py::omega"]
    store.close()


def test_equally_short_and_equally_strong_paths_tie_deterministically(repo, write):
    """Two runs of one command must print one report. Ties are broken on
    the node-id sequence, which is the only total order available that
    survives a rebuild of the store."""
    write(
        "m.py",
        "def alpha():\n    mid_a()\n    mid_b()\n\n\n"
        "def mid_a():\n    omega()\n\n\n"
        "def mid_b():\n    omega()\n\n\ndef omega():\n    pass\n",
        commit="tie",
    )
    store = build(repo)
    first = path_report(store, "HEAD", "m.py::alpha", "m.py::omega")
    second = path_report(store, "HEAD", "m.py::alpha", "m.py::omega")
    assert chain(first) == chain(second) == ["m.py::alpha", "m.py::mid_a", "m.py::omega"]
    store.close()


# -- the command -------------------------------------------------------


def test_cli_reports_a_path_and_exits_zero(repo, write, capsys):
    write("m.py", "def alpha():\n    beta()\n\n\ndef beta():\n    pass\n", commit="m")
    assert main(["path", "m.py::alpha", "m.py::beta", "--path", str(repo)]) == 0
    assert "m.py::beta" in capsys.readouterr().out


def test_cli_exits_zero_when_there_is_no_path(repo, write, capsys):
    """ "Not connected" is an answer, not a failure: the 0/1/2 convention is
    about resolving a name to a node id and nothing else."""
    write("left.py", "def left():\n    pass\n")
    write("right.py", "def right():\n    pass\n", commit="apart")
    assert main(["path", "left.py::left", "right.py::right", "--path", str(repo)]) == 0
    assert "different islands" in capsys.readouterr().out


def test_cli_reports_an_unresolvable_name_like_impact_does(repo, write, capsys):
    write("m.py", "def alpha():\n    pass\n", commit="m")
    assert main(["path", "m.py::alpha", "nosuchname", "--path", str(repo)]) == 1
    assert "no symbol matching 'nosuchname'" in capsys.readouterr().err


def test_cli_reports_an_ambiguous_name_like_impact_does(repo, write, capsys):
    write("a.py", "class One:\n    def save(self):\n        pass\n")
    write("b.py", "class Two:\n    def save(self):\n        pass\n", commit="two")
    assert main(["path", "save", "a.py::One", "--path", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "ambiguous symbol 'save'" in err
    assert "a.py::One.save" in err and "b.py::Two.save" in err


def test_cli_emits_json(repo, write, capsys):
    write("m.py", "def alpha():\n    beta()\n\n\ndef beta():\n    pass\n", commit="m")
    assert main(["path", "m.py::alpha", "m.py::beta", "--path", str(repo), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["direction"] == FORWARD
    assert report["groups"][0]["rows"][-1]["id"] == "m.py::beta"


def test_cli_rejects_hops_below_one(repo, write, capsys):
    write("m.py", "def alpha():\n    beta()\n\n\ndef beta():\n    pass\n", commit="m")
    assert main(["path", "m.py::alpha", "m.py::beta", "--path", str(repo), "--hops", "0"]) == 1
    assert "--hops" in capsys.readouterr().err


def test_cli_with_bad_rev_reports_cleanly(repo, capsys):
    assert main(["path", "alpha", "alpha", "--path", str(repo), "--rev", "nosuchrev"]) == 1
    assert capsys.readouterr().err.strip() == "revision not found: nosuchrev"
