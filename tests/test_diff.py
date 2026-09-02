from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.diff import diff_report
from codegraph.store import Store
from tests.conftest import git


def build(repo):
    store = Store.open(repo)
    return store, Indexer(repo, store, GitTreeSource(repo))


def rows(report, title):
    return {r.id for g in report.groups if g.title == title for r in g.rows}


def test_blank_line_insertion_produces_empty_diff(repo, write):
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("a.py", "\n\n\ndef alpha():\n    return 1\n", commit="shift lines")
    report = diff_report(store, indexer, base, "HEAD")
    assert report.groups == []
    store.close()


def test_added_symbol_reported(repo, write):
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("a.py", "def alpha():\n    return 1\n\n\ndef added():\n    pass\n", commit="add")
    report = diff_report(store, indexer, base, "HEAD")
    assert "a.py::added" in rows(report, "added")
    store.close()


def test_removed_symbol_reported(repo, write):
    write("b.py", "def beta():\n    pass\n", commit="b")
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "b.py").unlink()
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "remove b")
    report = diff_report(store, indexer, base, "HEAD")
    assert "b.py::beta" in rows(report, "removed")
    store.close()


def test_changed_body_reported(repo, write):
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("a.py", "def alpha():\n    return 999\n", commit="change body")
    report = diff_report(store, indexer, base, "HEAD")
    assert "a.py::alpha" in rows(report, "changed")
    store.close()


def test_newly_reachable_effect_is_headlined(repo, write):
    write("m.py", "def charge():\n    pass\n\n\ndef checkout():\n    charge()\n", commit="m")
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write(
        "m.py",
        "import requests\n\n\ndef charge():\n    requests.post('u')\n\n\ndef checkout():\n    charge()\n",
        commit="add network call",
    )
    report = diff_report(store, indexer, base, "HEAD")
    assert "NETWORK" in str(report.summary)
    store.close()


def test_base_revision_is_not_checked_out(repo, write):
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("a.py", "def alpha():\n    return 2\n", commit="edit")
    before = (repo / "a.py").read_text()
    diff_report(store, indexer, base, "HEAD")
    assert (repo / "a.py").read_text() == before
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    store.close()


def test_missing_base_revision_reports_name_and_raises(repo, write):
    store, indexer = build(repo)
    write("a.py", "def alpha():\n    return 2\n", commit="edit")
    import pytest

    from codegraph.query.diff import MissingRevisionError

    with pytest.raises(MissingRevisionError, match="deadbeef"):
        diff_report(store, indexer, "deadbeef" * 5, "HEAD")
    store.close()


def test_new_module_only_file_appears_in_added(repo, write):
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("config.py", "TIMEOUT = 30\n", commit="add config")
    report = diff_report(store, indexer, base, "HEAD")
    assert "config.py::<module>" in rows(report, "added")
    store.close()


def test_deleted_module_only_file_appears_in_removed(repo, write):
    write("config.py", "TIMEOUT = 30\n", commit="add config")
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "config.py").unlink()
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "remove config")
    report = diff_report(store, indexer, base, "HEAD")
    assert "config.py::<module>" in rows(report, "removed")
    store.close()


def test_new_symbols_effects_are_reported_as_new(repo, write):
    """B6 regression: `new_effects` used to union `_effect_kinds` over
    `changed_ids` only, so a brand-new function's effects were invisible
    -- `changed_ids` by construction only ever contains ids present in
    BOTH revisions, so a symbol that didn't exist at `base` could never
    land in it, no matter what it reaches. This is the same blind-spot
    class Task 12 already fixed once for module scope
    (`test_module_level_effect_addition_is_changed_and_new_effect`); here
    it's a whole new function, not an edit to an existing one."""
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write(
        "m.py",
        "def alpha():\n    return 1\n\n\ndef beta():\n    import requests\n    requests.post('u')\n",
        commit="add a function with a network call",
    )
    report = diff_report(store, indexer, base, "HEAD")
    assert "m.py::beta" in rows(report, "added")
    assert "NETWORK" in report.summary["new_effects"]
    store.close()


def test_module_level_effect_addition_is_changed_and_new_effect(repo, write):
    write("m.py", "TIMEOUT = 30\n", commit="m")
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write(
        "m.py",
        "import requests\n\nTIMEOUT = 30\nrequests.post('u')\n",
        commit="add module-level network call",
    )
    report = diff_report(store, indexer, base, "HEAD")
    assert "m.py::<module>" in rows(report, "changed")
    assert "NETWORK" in str(report.summary)
    store.close()


# -- the LOW fan-out must not read as a change ------------------------------
#
# `changed` also fires when a symbol's outgoing edges move, so that `foo()` now
# reaching a different `foo` counts even when the body is untouched. LOW edges
# broke that: they are a guess about the whole repo, so an untouched function's
# LOW set moves whenever anyone adds a same-named symbol anywhere. On
# `psf/requests` one new `AttrProxy.__init__` in a test marked every
# `super().__init__()` caller changed -- 7 of 20 rows, in files whose git blob
# was byte-identical across the range. See #13.


def test_an_unrelated_same_named_symbol_does_not_mark_a_file_changed(repo, write):
    write(
        "stable.py",
        "class Thing:\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        self.x = 1\n",
        commit="stable",
    )
    write("other.py", "class One:\n    def __init__(self):\n        pass\n", commit="other")
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()

    # A brand-new class in a DIFFERENT file. `stable.py` is not touched.
    write(
        "other.py",
        "class One:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n\n"
        "class Two:\n"
        "    def __init__(self):\n"
        "        pass\n",
        commit="add Two",
    )
    assert git(repo, "rev-parse", f"{base}:stable.py") == git(repo, "rev-parse", "HEAD:stable.py")

    report = diff_report(store, indexer, base, "HEAD")
    changed = rows(report, "changed")
    assert not [node_id for node_id in changed if node_id.startswith("stable.py")], (
        f"stable.py is byte-identical across the range but was reported changed: {changed}"
    )
    assert "other.py::Two.__init__" in rows(report, "added")
    store.close()


def test_a_confident_callee_disappearing_still_reads_as_changed(repo, write):
    """Non-vacuity guard for the filter above: the edge-set comparison must
    still fire for edges the resolver was confident about, or it has just been
    switched off. `caller`'s body is identical in both revisions -- only what
    `helper()` resolves to changed."""
    write(
        "m.py",
        "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n",
        commit="m",
    )
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("m.py", "def caller():\n    return helper()\n", commit="drop helper")

    report = diff_report(store, indexer, base, "HEAD")
    assert "m.py::caller" in rows(report, "changed")
    assert "m.py::helper" in rows(report, "removed")
    store.close()
