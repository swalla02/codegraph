import json

import pytest

from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.diff import MissingRevisionError, confident_edges, nodes_at
from codegraph.query.history import history_report, range_report, walk_history
from codegraph.store import Store
from codegraph.uncertainty import LINEAGE_AMBIGUOUS, is_incomplete
from tests.conftest import git

CHARGE = "def charge():\n    pass\n\n\ndef checkout():\n    charge()\n"
CHARGE_NETWORK = (
    "import requests\n\n\ndef charge():\n    requests.post('u')\n\n\n"
    "def checkout():\n    charge()\n"
)


def build(repo):
    store = Store.open(repo)
    return store, Indexer(repo, store, GitTreeSource(repo))


def sha(repo, rev="HEAD"):
    return git(repo, "rev-parse", rev).strip()


def rows(report):
    return [row for group in report.groups for row in group.rows]


def history(repo, symbol, base, head="HEAD", **kwargs):
    store, indexer = build(repo)
    try:
        walk = walk_history(store, indexer, base, head, symbol)
        forward = not walk.head_matches
        matches = walk.start_matches if forward else walk.head_matches
        assert len(matches) == 1, matches
        return history_report(walk, matches[0], forward=forward, **kwargs)
    finally:
        store.close()


def test_lists_the_commits_that_changed_a_symbol_oldest_first(repo, write):
    base = sha(repo)
    write("m.py", CHARGE, commit="add charge")
    write("m.py", CHARGE.replace("pass", "return 1"), commit="charge returns")
    write("README", "docs\n", commit="docs only")
    write("other.py", "def unrelated():\n    pass\n", commit="unrelated")
    write("m.py", CHARGE.replace("pass", "return 2"), commit="charge returns two")

    report = history(repo, "charge", base)

    subjects = [row.detail.split(" · ")[-1] for row in rows(report)]
    assert subjects == ['"add charge"', '"charge returns"', '"charge returns two"']
    assert rows(report)[0].detail.startswith("added")
    assert rows(report)[1].detail.startswith("body changed")
    assert report.summary["symbol"] == "m.py::charge"
    assert report.summary["commits"] == 5
    assert report.summary["changed_in"] == 3


def test_a_new_effect_downstream_changes_behaviour_without_touching_the_body(repo, write):
    """`checkout`'s text never changes; what it reaches does. That is the
    commit a reviewer needs, and `git log -L` on `checkout` cannot find it."""
    write("m.py", CHARGE, commit="m")
    base = sha(repo)
    write("m.py", CHARGE_NETWORK, commit="charge hits the network")

    report = history(repo, "checkout", base)

    (row,) = rows(report)
    assert row.id == sha(repo)
    assert "body changed" not in row.detail
    assert "effects +NETWORK" in row.detail


def test_a_move_is_followed_and_marked_medium_never_the_same_id(repo, write):
    write("m.py", "def charge():\n    return 1\n", commit="m")
    base = sha(repo)
    write("m.py", "def charge():\n    return 2\n", commit="edit in m")
    (repo / "m.py").unlink()
    write("pay.py", "def charge():\n    return 2\n", commit="move to pay")

    report = history(repo, "charge", base)

    first, second = rows(report)
    assert first.detail.startswith("body changed")
    assert first.location.startswith("m.py:")
    assert "moved from m.py::charge to pay.py::charge (MEDIUM)" in second.detail
    assert report.summary["symbol"] == "pay.py::charge"
    assert report.unknowns == []


def test_an_ambiguous_move_is_reported_low_and_not_followed(repo, write):
    body = "def helper():\n    return 0\n"
    write("a.py", body, commit="a")
    write("b.py", body, commit="b")
    base = sha(repo)
    (repo / "a.py").unlink()
    (repo / "b.py").unlink()
    write("c.py", body, commit="collapse")

    report = history(repo, "helper", base)

    (row,) = rows(report)
    assert "a.py::helper" in row.detail and "b.py::helper" in row.detail
    assert "(LOW)" in row.detail
    assert [item.reason for item in report.unknowns] == [LINEAGE_AMBIGUOUS]
    assert is_incomplete(report)


def test_a_rename_is_an_unpaired_removal_and_addition(repo, write):
    """The def's own name is part of its body hash, so a rename is not a
    move and must not be dressed up as one."""
    write("m.py", "def old():\n    return 1\n", commit="m")
    base = sha(repo)
    write("m.py", "def new():\n    return 1\n", commit="rename")

    report = history(repo, "new", base)

    (row,) = rows(report)
    assert row.detail.startswith("added ·")
    assert "moved" not in row.detail


def test_a_symbol_the_range_deleted_is_followed_forward(repo, write):
    write("m.py", "def doomed():\n    return 1\n", commit="m")
    base = sha(repo)
    write("m.py", "def doomed():\n    return 2\n", commit="edit")
    write("m.py", "X = 1\n", commit="delete")

    report = history(repo, "doomed", base)

    details = [row.detail for row in rows(report)]
    assert details[0].startswith("body changed")
    assert details[1].startswith("removed")


# -- what the walk materializes -----------------------------------------------


def test_no_revision_created_by_the_walk_outlives_it(repo, write):
    base = sha(repo)
    write("m.py", CHARGE, commit="one")
    write("m.py", CHARGE_NETWORK, commit="two")
    store, indexer = build(repo)
    indexer.reconcile("WORKTREE")
    before = store.revisions()

    walk_history(store, indexer, base, "HEAD")

    assert store.revisions() == before
    for table in ("tree", "nodes", "edges", "effects", "imports", "unresolved"):
        stray = store.connection.execute(
            f"SELECT DISTINCT rev FROM {table} WHERE rev NOT IN (SELECT rev FROM revisions)"
        ).fetchall()
        assert not stray, (table, [tuple(r) for r in stray])
    store.close()


def test_a_revision_already_materialized_is_left_exactly_as_it_was(repo, write):
    base = sha(repo)
    write("m.py", CHARGE, commit="one")
    store, indexer = build(repo)
    indexer.reconcile(base)
    snapshot = sorted(
        tuple(r) for r in store.connection.execute("SELECT * FROM nodes WHERE rev=?", (base,))
    )

    walk_history(store, indexer, base, "HEAD")

    assert base in store.revisions()
    assert snapshot == sorted(
        tuple(r) for r in store.connection.execute("SELECT * FROM nodes WHERE rev=?", (base,))
    )
    store.close()


def test_nothing_outside_the_range_is_reconciled(repo, write, monkeypatch):
    write("m.py", CHARGE, commit="before the range")
    base = sha(repo)
    write("m.py", CHARGE_NETWORK, commit="in range")
    write("n.py", "def n():\n    pass\n", commit="also in range")
    in_range = {base, sha(repo, "HEAD~1"), sha(repo)}

    seen = []
    original = Indexer.reconcile

    def spy(self, rev="WORKTREE"):
        seen.append(rev)
        return original(self, rev)

    monkeypatch.setattr(Indexer, "reconcile", spy)
    store, indexer = build(repo)
    walk_history(store, indexer, base, "HEAD")
    store.close()

    assert set(seen) == in_range


def test_the_walk_never_touches_the_working_tree(repo, write):
    base = sha(repo)
    write("m.py", CHARGE, commit="one")
    write("m.py", CHARGE_NETWORK, commit="two")
    write("m.py", "# uncommitted\n")
    store, indexer = build(repo)
    walk_history(store, indexer, base, "HEAD")
    store.close()
    assert (repo / "m.py").read_text() == "# uncommitted\n"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(repo, "status", "--porcelain").strip() == "M m.py"


def test_carrying_the_graph_forward_matches_a_cold_build(repo, write):
    """Every commit's rows start as its parent's and are reconciled from
    there. That is only sound if the result is the graph a cold build of the
    same commit produces -- checked here against exactly that."""
    write("m.py", CHARGE, commit="m")
    base = sha(repo)
    write("m.py", CHARGE_NETWORK, commit="network")
    write("pay.py", "from m import charge\n\n\ndef pay():\n    charge()\n", commit="pay")
    store, indexer = build(repo)
    walk = walk_history(store, indexer, base, "HEAD")

    for step in walk.steps:
        cold = {}
        for rev in (step.parent, step.sha):
            indexer.reconcile(rev)
            cold[rev] = (nodes_at(store, rev), confident_edges(store, rev))
        before_nodes, after_nodes = cold[step.parent][0], cold[step.sha][0]
        assert set(step.added) == after_nodes.keys() - before_nodes.keys()
        assert set(step.removed) == before_nodes.keys() - after_nodes.keys()
        assert set(step.changed) == {
            i
            for i in after_nodes.keys() & before_nodes.keys()
            if after_nodes[i]["body_hash"] != before_nodes[i]["body_hash"]
        }
        flat = {
            rev: {(s, d, k) for s, targets in edges.items() for d, k in targets}
            for rev, (_, edges) in cold.items()
        }
        assert step.edges_gained == flat[step.sha] - flat[step.parent]
        assert step.edges_lost == flat[step.parent] - flat[step.sha]
    store.close()


def test_an_unknown_revision_is_reported_by_name(repo):
    store, indexer = build(repo)
    with pytest.raises(MissingRevisionError, match="nope"):
        walk_history(store, indexer, "nope", "HEAD")
    store.close()


# -- the whole-range report ---------------------------------------------------


def test_range_report_lists_edges_and_effects_gained_and_lost_per_commit(repo, write):
    write("m.py", CHARGE, commit="m")
    base = sha(repo)
    write("m.py", CHARGE_NETWORK, commit="network")
    write("README", "docs\n", commit="docs")
    write("m.py", "def charge():\n    pass\n\n\ndef checkout():\n    pass\n", commit="decouple")
    store, indexer = build(repo)
    report = range_report(walk_history(store, indexer, base, "HEAD"))
    store.close()

    assert report.summary["commits"] == 3
    assert report.summary["changed"] == 2
    network, decouple = report.groups
    assert network.title.endswith('"network"')
    assert decouple.title.endswith('"decouple"')
    gained = {(r.id, r.location) for r in network.rows if r.detail == "effect gained"}
    assert ("m.py::checkout", "NETWORK") in gained
    lost_edges = {r.id for r in decouple.rows if r.detail == "edge lost"}
    assert "m.py::checkout -> m.py::charge" in lost_edges
    lost_effects = {(r.id, r.location) for r in decouple.rows if r.detail == "effect lost"}
    assert ("m.py::checkout", "NETWORK") in lost_effects


# -- the session seam -------------------------------------------------------


def test_session_pointers_ride_on_the_commit_row_when_a_reader_supplies_them(repo, write):
    base = sha(repo)
    write("m.py", CHARGE, commit="add charge")
    head = sha(repo)
    store, indexer = build(repo)
    walk = walk_history(store, indexer, base, "HEAD", "charge")
    store.close()

    plain = history_report(walk, "m.py::charge")
    linked = history_report(
        walk, "m.py::charge", session_pointers=lambda commit: [f"s://{commit[:7]}"]
    )

    assert "session" not in rows(plain)[0].detail
    assert f"session: s://{head[:7]}" in rows(linked)[0].detail


# -- the CLI ----------------------------------------------------------------


def test_cli_reads_session_trailers_into_both_reports(repo, write, capsys):
    base = sha(repo)
    write("m.py", CHARGE, commit="add charge")
    git(repo, "commit", "--amend", "-q", "-m", "add charge\n\nSession: s://one")
    write("m.py", CHARGE_NETWORK, commit="network")
    root = str(repo)

    assert main(["history", "charge", f"{base}..HEAD", "--path", root]) == 0
    out = capsys.readouterr().out
    assert out.count("session:") == 1
    assert "session: s://one" in out

    assert main(["history", f"{base}..HEAD", "--path", root]) == 0
    assert "session: s://one" in capsys.readouterr().out


def test_cli_symbol_and_range_positionals(repo, write, capsys):
    base = sha(repo)
    write("m.py", CHARGE, commit="add charge")
    write("m.py", CHARGE_NETWORK, commit="network")

    assert main(["history", "checkout", f"{base}..HEAD", "--path", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["symbol"] == "m.py::checkout"
    assert len(payload["groups"][0]["rows"]) == 2

    assert main(["history", f"{base}..HEAD", "--path", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("commits: 2")


def test_cli_defaults_to_what_the_branch_has_committed(repo, write, capsys):
    write("m.py", CHARGE, commit="on main")
    git(repo, "checkout", "-qb", "feature")
    write("m.py", CHARGE_NETWORK, commit="on feature")

    assert main(["history", "--path", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["commits"] == 1
    assert payload["groups"][0]["title"].endswith('"on feature"')


def test_cli_exit_codes_follow_the_symbol_convention(repo, write, capsys):
    base = sha(repo)
    write("m.py", "def run():\n    pass\n", commit="m")
    write("n.py", "def run():\n    pass\n", commit="n")
    root = str(repo)

    assert main(["history", "nothing_like_it", f"{base}..HEAD", "--path", root]) == 1
    assert main(["history", "run", f"{base}..HEAD", "--path", root]) == 2
    assert main(["history", "run", "nope..HEAD", "--path", root]) == 1
    assert "revision not found: nope" in capsys.readouterr().err


def test_cli_strict_refuses_an_ambiguous_lineage(repo, write, capsys):
    body = "def helper():\n    return 0\n"
    write("a.py", body, commit="a")
    write("b.py", body, commit="b")
    base = sha(repo)
    (repo / "a.py").unlink()
    (repo / "b.py").unlink()
    write("c.py", body, commit="collapse")

    code = main(["history", "helper", f"{base}..HEAD", "--path", str(repo), "--strict"])
    assert code == 3
    assert LINEAGE_AMBIGUOUS in capsys.readouterr().err


def test_a_merge_is_one_step_and_the_walk_starts_at_the_first_commits_parent(
    repo, write, monkeypatch
):
    """A branch that merged the default branch in: `merge-base` is the
    merged tip, which is not on the branch's first-parent line. The walk
    measures the first branch commit against the commit it was made on, and
    the merge against its first parent -- so what the merge brought in is
    the merge's change, and nobody else's commit is attributed to the branch."""
    fork = sha(repo)
    git(repo, "checkout", "-qb", "feature")
    write("f.py", "def feature():\n    pass\n", commit="feature work")
    git(repo, "checkout", "-q", "main")
    write("g.py", "def upstream():\n    pass\n", commit="upstream work")
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-edit", "main")

    seen = []
    original = Indexer.reconcile

    def spy(self, rev="WORKTREE"):
        seen.append(rev)
        return original(self, rev)

    monkeypatch.setattr(Indexer, "reconcile", spy)
    store, indexer = build(repo)
    report = range_report(walk_history(store, indexer, "main", "HEAD"))
    store.close()

    feature, merge = report.groups
    assert feature.title.endswith('"feature work"')
    assert "f.py::feature" in {r.id for r in feature.rows}
    assert "g.py::upstream" not in {r.id for r in feature.rows}
    assert "g.py::upstream" in {r.id for r in merge.rows}
    assert seen[0] == fork
    assert set(seen) == {fork, sha(repo, "HEAD^1"), sha(repo)}
