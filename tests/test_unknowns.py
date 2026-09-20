"""Behaviours of `unknowns`, the reason -> next-action table, and the
uncertainty envelope every report that can be incomplete now carries.

The claim under test is narrow and worth stating: codegraph already knew
*that* it was uncertain, and said so in aggregate -- a `low_confidence_hidden`
count, an `unexplained` island tally. What it could not do was answer "what
do you not know about THIS symbol", and it never said what would settle it.
So the tests below fix three things: that the per-symbol answer is derived
from stored rows and not invented, that every reason carries exactly one
next action defined in one place, and that `--strict` turns "do not act on
this answer" into an exit code rather than a judgement.
"""

import json

from codegraph import uncertainty
from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.islands import MECHANISMS, island_label
from codegraph.query.unknowns import unknowns_report, unresolved_references
from codegraph.resolve import AMBIGUOUS, BUILTIN, EXTERNAL, UNKNOWN, UNRESOLVED_REASONS
from codegraph.store import Store

#: Exit code for a `--strict` run whose report is incomplete. Deliberately
#: not `1` or `2`: those already mean "no symbol matched" and "more than one
#: symbol matched", and an agent that cannot tell a missing symbol from an
#: incomplete answer has been handed the same ambiguity this feature exists
#: to remove.
INCOMPLETE = 3


def build(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    return store


def rows_by_reason(report, reason):
    return [row for group in report.groups if group.title == reason for row in group.rows]


def entry(report, reason):
    found = [item for item in report.unknowns if item.reason == reason]
    return found[0] if found else None


# -- 2. the reason -> next-action table --------------------------------


def test_every_reason_maps_to_exactly_one_next_action():
    """Totality, which is the whole point of a table: a reason with no
    action would put the reader back where #55 found them -- told that
    something is unknown and not told what settles it."""
    assert set(uncertainty.NEXT_ACTION) == set(uncertainty.REASONS)
    assert set(UNRESOLVED_REASONS) <= set(uncertainty.REASONS)
    assert all(uncertainty.NEXT_ACTION[reason] for reason in uncertainty.REASONS)


def test_a_new_unresolved_reason_cannot_be_added_without_an_action():
    """The mapping is total over what the RESOLVER writes, not just over
    what this module happens to list. `UNRESOLVED_REASONS` is the
    resolver's own declaration of the reasons it can write, so a fifth one
    added there and nowhere else fails here rather than printing a
    reference with no next move."""
    for reason in UNRESOLVED_REASONS:
        assert reason in uncertainty.NEXT_ACTION
    assert uncertainty.unknown(UNKNOWN, "1 reference").action == uncertainty.NEXT_ACTION[UNKNOWN]


def test_an_unlisted_reason_is_refused_rather_than_described_vaguely():
    try:
        uncertainty.unknown("telepathy", "1 reference")
    except KeyError:
        return
    raise AssertionError("an unknown reason must not produce an entry with no action")


def test_the_action_is_one_constant_string_never_composed_per_case(repo, write):
    """Two references with the same reason produce the same sentence, and
    it is the table's -- not one assembled around each case, which is how
    two callers come to describe one reason two different ways."""
    write(
        "m.py",
        "def alpha(thing):\n    thing.no_such_method()\n    thing.also_missing()\n",
        commit="m",
    )
    store = build(repo)
    report = unknowns_report(store, "HEAD", "m.py::alpha")
    assert len(rows_by_reason(report, UNKNOWN)) == 2
    assert entry(report, UNKNOWN).action == uncertainty.NEXT_ACTION[UNKNOWN]
    store.close()


def test_a_settled_reason_is_reported_but_never_makes_a_report_incomplete(repo, write):
    """`builtin` and `external` are answers, not gaps: the resolver knows
    exactly what they are and knows no repository symbol is the target. An
    entry for them is information; a `--strict` that refused on them would
    refuse on every body that calls `len()`, which is a flag nobody can
    use."""
    write("m.py", "def alpha(items):\n    return len(items)\n\n\nalpha([])\n", commit="m")
    store = build(repo)
    report = unknowns_report(store, "HEAD", "m.py::alpha")
    assert entry(report, BUILTIN) is not None
    assert entry(report, BUILTIN).blocking is False
    assert uncertainty.is_incomplete(report) is False
    store.close()


# -- 1. `codegraph unknowns <symbol>` ----------------------------------


def test_a_reference_that_produced_no_edge_is_named_with_reason_and_line(repo, write):
    write(
        "m.py",
        "def alpha(thing):\n    pass\n\n\ndef beta(thing):\n    thing.gone()\n",
        commit="m",
    )
    store = build(repo)
    report = unknowns_report(store, "HEAD", "m.py::beta")
    rows = rows_by_reason(report, UNKNOWN)
    assert [row.id for row in rows] == ["thing.gone"]
    assert rows[0].location == "m.py:6"
    store.close()


def test_an_ambiguous_reference_reports_its_candidate_count(repo, write):
    write("a.py", "class One:\n    def save(self):\n        return 1\n")
    write("b.py", "class Two:\n    def save(self):\n        return 2\n")
    write("c.py", "def caller(item):\n    item.save()\n", commit="ambiguous")
    store = build(repo)
    report = unknowns_report(store, "HEAD", "c.py::caller")
    rows = rows_by_reason(report, AMBIGUOUS)
    assert len(rows) == 1
    assert "2 candidates" in rows[0].detail
    store.close()


def test_the_resolved_ratio_counts_this_body_and_nothing_else(repo, write):
    write(
        "m.py",
        "def target():\n    pass\n\n\ndef alpha(thing):\n    target()\n    thing.gone()\n",
        commit="m",
    )
    store = build(repo)
    report = unknowns_report(store, "HEAD", "m.py::alpha")
    assert report.summary["references"] == 2
    assert report.summary["resolved"] == 1
    assert report.summary["unresolved"] == 1
    store.close()


def test_a_body_whose_every_reference_resolved_says_so(repo, write):
    write(
        "m.py",
        "def target():\n    pass\n\n\ndef alpha():\n    target()\n\n\nalpha()\n",
        commit="m",
    )
    store = build(repo)
    report = unknowns_report(store, "HEAD", "m.py::alpha")
    assert report.summary["references"] == report.summary["resolved"] == 1
    assert report.groups == []
    assert uncertainty.is_incomplete(report) is False
    store.close()


def test_an_unexplained_island_is_named_with_the_mechanisms_checked(repo, write):
    """The `islands` report says 17 islands are unexplained. That is a
    property of the report; this says it of one symbol, and lists what was
    looked for -- an agent that cannot see the list cannot tell an
    unexplained island from an unexamined one."""
    write("m.py", "def alone():\n    pass\n", commit="m")
    store = build(repo)
    report = unknowns_report(store, "HEAD", "m.py::alone")
    assert report.summary["island"] == "unexplained"
    assert report.summary["mechanisms_not_found"] == list(MECHANISMS)
    assert entry(report, uncertainty.UNEXPLAINED_ISLAND) is not None
    store.close()


def test_an_explained_island_names_the_mechanism_that_explains_it(repo, write):
    write(
        "m.py",
        "def mark(fn):\n    return fn\n\n\n@mark\ndef decorated():\n    pass\n",
        commit="m",
    )
    store = build(repo)
    report = unknowns_report(store, "HEAD", "m.py::decorated")
    assert report.summary["island"].startswith("explained")
    assert "decorator" in report.summary["island"]
    assert entry(report, uncertainty.UNEXPLAINED_ISLAND) is None
    store.close()


def test_the_island_label_is_the_same_partition_islands_prints(repo, write):
    write("m.py", "def alone():\n    pass\n", commit="m")
    store = build(repo)
    label = island_label(store, "HEAD", "m.py::alone")
    assert label.size == 1
    assert label.explained is False
    assert label.missing == MECHANISMS
    store.close()


def test_an_impact_walk_that_would_stop_at_its_hop_budget_says_so(repo, write):
    """`impact --hops 1` on a symbol with a three-deep caller chain prints
    a report that reads as complete. It is not, and this is the one place
    that can be said without the reader already knowing to suspect it."""
    write(
        "m.py",
        "def one():\n    two()\n\n\ndef two():\n    three()\n\n\ndef three():\n    pass\n",
        commit="m",
    )
    store = build(repo)
    cramped = unknowns_report(store, "HEAD", "m.py::three", max_hops=1)
    assert entry(cramped, uncertainty.HOP_LIMIT) is not None
    assert uncertainty.is_incomplete(cramped) is True

    roomy = unknowns_report(store, "HEAD", "m.py::three", max_hops=3)
    assert entry(roomy, uncertainty.HOP_LIMIT) is None
    store.close()


def test_unresolved_references_read_only_this_symbols_rows(repo, write):
    write("m.py", "def alpha(thing):\n    thing.gone()\n\n\ndef beta(thing):\n    thing.also()\n")
    write("n.py", "def gamma(thing):\n    thing.elsewhere()\n", commit="m")
    store = build(repo)
    found = unresolved_references(store, "HEAD", "m.py::alpha")
    assert [ref.raw_name for ref in found] == ["thing.gone"]
    store.close()


# -- 3. the envelope on the other reports ------------------------------


def test_impact_carries_the_hop_limit_it_stopped_at(repo, write):
    from codegraph.query.impact import impact_report

    write(
        "m.py",
        "def one():\n    two()\n\n\ndef two():\n    three()\n\n\ndef three():\n    pass\n",
        commit="m",
    )
    store = build(repo)
    cramped = impact_report(store, "HEAD", "m.py::three", max_hops=1)
    assert entry(cramped, uncertainty.HOP_LIMIT) is not None
    roomy = impact_report(store, "HEAD", "m.py::three", max_hops=3)
    assert roomy.unknowns == []
    store.close()


def test_islands_carries_its_unexplained_count(repo, write):
    from codegraph.query.islands import islands_report

    write("m.py", "def alone():\n    pass\n", commit="m")
    store = build(repo)
    report = islands_report(store, "HEAD")
    assert report.summary["unexplained"] >= 1
    assert entry(report, uncertainty.UNEXPLAINED_ISLAND) is not None
    store.close()


def test_a_path_cut_short_by_its_hop_budget_is_an_incomplete_report(repo, write):
    from codegraph.query.path import path_report

    write(
        "m.py",
        "def one():\n    two()\n\n\ndef two():\n    three()\n\n\ndef three():\n    pass\n",
        commit="m",
    )
    store = build(repo)
    cramped = path_report(store, "HEAD", "m.py::one", "m.py::three", max_hops=1)
    assert entry(cramped, uncertainty.HOP_LIMIT) is not None
    found = path_report(store, "HEAD", "m.py::one", "m.py::three")
    assert found.unknowns == []
    store.close()


def test_a_path_negative_that_only_a_low_walk_could_answer_says_so(repo, write):
    from codegraph.query.path import path_report

    write("a.py", "class One:\n    def save(self):\n        return 1\n")
    write("b.py", "class Two:\n    def save(self):\n        return 2\n")
    write("c.py", "def caller(item):\n    item.save()\n", commit="ambiguous")
    store = build(repo)
    report = path_report(store, "HEAD", "c.py::caller", "a.py::One.save")
    assert report.summary["show_path"] == "--all"
    assert entry(report, uncertainty.LOW_CONFIDENCE) is not None
    store.close()


def test_a_path_on_different_islands_is_a_complete_answer(repo, write):
    from codegraph.query.path import path_report

    write("m.py", "def alpha():\n    pass\n\n\ndef beta():\n    pass\n", commit="m")
    store = build(repo)
    report = path_report(store, "HEAD", "m.py::alpha", "m.py::beta")
    assert "different islands" in report.summary["reason"]
    assert report.unknowns == []
    store.close()


def test_effects_says_when_a_call_it_could_not_follow_leaves_the_body(repo, write):
    from codegraph.query.effects import effects_report

    write("m.py", "def alpha(thing):\n    thing.gone()\n", commit="m")
    store = build(repo)
    report = effects_report(store, "HEAD", "m.py::alpha")
    assert entry(report, UNKNOWN) is not None
    assert uncertainty.is_incomplete(report) is True
    store.close()


def test_effects_on_a_body_it_could_follow_completely_is_complete(repo, write):
    from codegraph.query.effects import effects_report

    write("m.py", "def target():\n    pass\n\n\ndef alpha():\n    target()\n", commit="m")
    store = build(repo)
    report = effects_report(store, "HEAD", "m.py::alpha")
    assert report.unknowns == []
    store.close()


def test_orphans_has_no_strict_flag_because_its_caveat_never_clears(repo):
    """Deliberate and worth pinning: `orphans`' uncertainty is a standing
    claim on every row (a name resolved at runtime leaves nothing to find),
    not a hole one run has and the next does not. A `--strict` there would
    refuse on every non-empty report, which says nothing the `caveat` field
    does not already say."""
    assert main(["orphans", "--strict", "--path", str(repo)]) != 0


# -- the CLI: exit codes, --strict, --json -----------------------------


def test_unknowns_takes_the_symbol_exit_convention(repo, write, capsys):
    write("m.py", "def alpha():\n    pass\n", commit="m")
    write("n.py", "def alpha():\n    pass\n", commit="n")
    assert main(["unknowns", "nosuchsymbol", "--path", str(repo)]) == 1
    assert "no symbol matching" in capsys.readouterr().err
    assert main(["unknowns", "alpha", "--path", str(repo)]) == 2
    assert "ambiguous symbol" in capsys.readouterr().err
    assert main(["unknowns", "m.py::alpha", "--path", str(repo)]) == 0


def test_unknowns_with_bad_rev_reports_cleanly(repo, capsys):
    assert main(["unknowns", "alpha", "--path", str(repo), "--rev", "nosuchrev"]) == 1
    assert capsys.readouterr().err.strip() == "revision not found: nosuchrev"


def test_strict_exits_nonzero_on_an_incomplete_report(repo, write, capsys):
    write("m.py", "def alpha(thing):\n    thing.gone()\n", commit="m")
    assert main(["unknowns", "m.py::alpha", "--path", str(repo), "--strict"]) == INCOMPLETE
    assert "incomplete" in capsys.readouterr().err


def test_strict_exits_zero_on_a_complete_report(repo, write):
    write(
        "m.py",
        "def target():\n    pass\n\n\ndef alpha():\n    target()\n\n\nalpha()\n",
        commit="m",
    )
    assert main(["unknowns", "m.py::alpha", "--path", str(repo), "--strict"]) == 0


def test_strict_is_silent_about_a_report_that_is_merely_low_confidence(repo, write):
    """A LOW row is an answer the report gives, labelled for what it is. A
    report is incomplete when it does not show you something, not when what
    it shows is uncertain -- otherwise `--strict` fails on every real
    repository and stops meaning anything."""
    write("a.py", "class One:\n    def save(self):\n        return 1\n")
    write("b.py", "class Two:\n    def save(self):\n        return 2\n")
    write("c.py", "def caller(item):\n    item.save()\n", commit="ambiguous")
    assert main(["impact", "a.py::One.save", "--path", str(repo), "--strict", "--all"]) == 0


def test_json_carries_the_unknowns_array_beside_the_results(repo, write, capsys):
    write("m.py", "def alpha(thing):\n    thing.gone()\n", commit="m")
    assert main(["unknowns", "m.py::alpha", "--path", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["unresolved"] == 1
    assert payload["unknowns"][0]["reason"] == UNKNOWN
    assert payload["unknowns"][0]["action"] == uncertainty.NEXT_ACTION[UNKNOWN]
    assert payload["unknowns"][0]["blocking"] is True


def test_impact_json_carries_the_envelope_too(repo, write, capsys):
    write(
        "m.py",
        "def one():\n    two()\n\n\ndef two():\n    three()\n\n\ndef three():\n    pass\n",
        commit="m",
    )
    assert main(["impact", "m.py::three", "--path", str(repo), "--hops", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["unknowns"][0]["reason"] == uncertainty.HOP_LIMIT


def test_text_output_prints_the_envelope_under_its_own_heading(repo, write, capsys):
    write("m.py", "def alpha(thing):\n    thing.gone()\n", commit="m")
    assert main(["unknowns", "m.py::alpha", "--path", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "unknowns" in out
    assert uncertainty.NEXT_ACTION[UNKNOWN] in out


def test_an_external_call_is_reported_as_settled(repo, write, capsys):
    write(
        "m.py",
        "import pytest\n\n\ndef alpha():\n    pytest.main([])\n\n\nalpha()\n",
        commit="m",
    )
    assert main(["unknowns", "m.py::alpha", "--path", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    reasons = {item["reason"]: item for item in payload["unknowns"]}
    assert reasons[EXTERNAL]["blocking"] is False
    assert main(["unknowns", "m.py::alpha", "--path", str(repo), "--strict"]) == 0
