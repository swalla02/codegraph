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
