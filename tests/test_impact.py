import pytest

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.impact import impact_report
from codegraph.query.rank import salience, score
from codegraph.store import Store


def build(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    return store


def listed(report):
    return {row.id for group in report.groups for row in group.rows}


def test_closer_hops_score_higher():
    assert score(1, "HIGH", 1.0) > score(3, "HIGH", 1.0)


def test_higher_confidence_scores_higher():
    assert score(1, "HIGH", 1.0) > score(1, "LOW", 1.0)


def test_direct_and_transitive_callers_are_found(repo, write):
    source = (
        "def target():\n    pass\n\n\n"
        "def direct():\n    target()\n\n\n"
        "def indirect():\n    direct()\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    found = listed(impact_report(store, "HEAD", "m.py::target"))
    assert {"m.py::direct", "m.py::indirect"} <= found
    store.close()


def test_hop_limit_is_respected(repo, write):
    source = (
        "def target():\n    pass\n\n\n"
        "def h1():\n    target()\n\n\n"
        "def h2():\n    h1()\n\n\n"
        "def h3():\n    h2()\n\n\n"
        "def h4():\n    h3()\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    found = listed(impact_report(store, "HEAD", "m.py::target", max_hops=2))
    assert "m.py::h2" in found
    assert "m.py::h4" not in found
    store.close()


def test_tests_are_bucketed_separately(repo, write):
    write("m.py", "def target():\n    pass\n", commit="m")
    write("tests/__init__.py", "", commit="pkg")
    write(
        "tests/test_m.py",
        "from m import target\n\n\ndef test_target():\n    target()\n",
        commit="t",
    )
    store = build(repo)
    report = impact_report(store, "HEAD", "m.py::target")
    test_groups = [g for g in report.groups if g.title == "tests"]
    assert test_groups and test_groups[0].rows
    other = {row.id for g in report.groups if g.title != "tests" for row in g.rows}
    assert "tests/test_m.py::test_target" not in other
    store.close()


def test_low_confidence_counted_but_not_listed(repo, write):
    write("one.py", "class One:\n    def shared(self):\n        pass\n", commit="1")
    write("two.py", "class Two:\n    def shared(self):\n        pass\n", commit="2")
    write("caller.py", "def go(thing):\n    thing.shared()\n", commit="c")
    store = build(repo)
    report = impact_report(store, "HEAD", "one.py::One.shared")
    assert "caller.py::go" not in listed(report)
    assert report.summary["low_confidence_hidden"] >= 1
    with_low = impact_report(store, "HEAD", "one.py::One.shared", include_low=True)
    assert "caller.py::go" in listed(with_low)
    store.close()


def test_summary_reports_reachable_effects(repo, write):
    source = (
        "import requests\n\n\n"
        "def target():\n    requests.get('u')\n\n\n"
        "def caller():\n    target()\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    report = impact_report(store, "HEAD", "m.py::target")
    assert "NETWORK" in str(report.summary)
    store.close()


def test_entry_points_excludes_popular_intermediaries(repo, write):
    """Regression for the round-2 Major: `entry_points` must count nodes by
    their OWN fan_in == 0, not by threshold-sniffing `salience`'s combined
    score. `popular` is public and has 10 distinct callers -- its salience
    (0 + 0.3 public + 0.5 fan-in-capped = 0.8) crosses 0.5 with no help from
    the entry-point term at all, so it must NOT be counted. Each `callerN`
    has no callers of its own and must be."""
    lines = ["def target():\n    pass\n\n\ndef popular():\n    target()\n"]
    lines += [f"def caller{i}():\n    popular()\n" for i in range(10)]
    write("m.py", "\n\n".join(lines), commit="m")
    store = build(repo)
    report = impact_report(store, "HEAD", "m.py::target")
    assert report.summary["entry_points"] == 10
    found = listed(report)
    assert "m.py::popular" in found
    assert all(f"m.py::caller{i}" in found for i in range(10))
    store.close()


def test_duplicate_edge_rows_do_not_double_count_fan_in(tmp_path):
    """A caller that calls the target twice (two rows in `edges` for the
    same (src, dst) pair -- e.g. the same call appearing twice in a body)
    must contribute fan_in 1, not 2. Schema seeded directly so the edge
    duplication is exact, mirroring test_effects.py's direct-seed style."""
    store = Store.open(tmp_path)
    rev = "HEAD"
    connection = store.connection
    connection.execute(
        "INSERT INTO nodes(rev, id, path, qualname, kind, line_start, line_end, body_hash,"
        " name_binding) VALUES(?,?,?,?,?,?,?,?,?)",
        (rev, "m.py::mid", "m.py", "mid", "function", 1, 1, "x", "live"),
    )
    connection.executemany(
        "INSERT INTO edges(rev, src, dst, kind, confidence, provenance, callsite_path,"
        " callsite_line) VALUES(?,?,?,?,?,?,?,?)",
        [
            (rev, "m.py::top", "m.py::mid", "CALLS", "HIGH", "static", "m.py", 1),
            (rev, "m.py::top", "m.py::mid", "CALLS", "HIGH", "static", "m.py", 2),
        ],
    )
    connection.commit()

    # 0.3 (not private) + min(1, 10) / 20 (one distinct caller, not two rows).
    assert salience(store, rev, "m.py::mid") == pytest.approx(0.35)
    store.close()


def test_limit_sets_truncated_flag(repo, write):
    lines = ["def target():\n    pass\n"]
    lines += [f"def c{i}():\n    target()\n" for i in range(60)]
    write("m.py", "\n\n".join(lines), commit="m")
    store = build(repo)
    report = impact_report(store, "HEAD", "m.py::target", limit=10)
    assert report.truncated is True
    store.close()


def test_limit_is_a_total_budget_across_dependents_and_tests(repo, write):
    """B3 regression: `dependents` and `tests` used to each be budgeted at
    `limit` independently, so a report could print up to 2x its documented
    budget (52 rows against a limit of 40, observed on codegraph's own
    source). 15 production callers plus 15 test callers against a limit of
    20 must keep at most 20 rows TOTAL, with `dependents` -- production
    callers should never be crowded out by tests -- claiming its rows
    first."""
    prod_lines = ["def target():\n    pass\n"]
    prod_lines += [f"def c{i}():\n    target()\n" for i in range(15)]
    write("m.py", "\n\n".join(prod_lines), commit="m")
    write("tests/__init__.py", "", commit="pkg")
    test_lines = ["from m import target\n"]
    test_lines += [f"def test_{i}():\n    target()\n" for i in range(15)]
    write("tests/test_m.py", "\n\n".join(test_lines), commit="t")
    store = build(repo)
    report = impact_report(store, "HEAD", "m.py::target", limit=20)
    total_kept = sum(len(g.rows) for g in report.groups)
    assert total_kept == 20
    assert report.truncated is True
    dependents_group = next(g for g in report.groups if g.title == "dependents")
    assert len(dependents_group.rows) == 15
    store.close()
