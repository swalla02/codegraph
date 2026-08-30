import sqlite3

import pytest

from codegraph.store import SCHEMA_VERSION, Store


def test_open_creates_self_ignoring_directory(tmp_path):
    store = Store.open(tmp_path)
    ignore = tmp_path / ".codegraph" / ".gitignore"
    assert ignore.read_text().strip() == "*"
    assert (tmp_path / ".codegraph" / "graph.db").exists()
    store.close()


def test_schema_version_recorded(tmp_path):
    store = Store.open(tmp_path)
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    store.close()


def test_expected_tables_exist(tmp_path):
    store = Store.open(tmp_path)
    rows = store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {row[0] for row in rows}
    assert {
        "meta", "blobs", "blob_nodes", "blob_refs", "blob_imports",
        "revisions", "tree", "nodes", "edges", "effects", "imports", "unresolved",
    } <= names
    store.close()


def test_reopen_preserves_data(tmp_path):
    store = Store.open(tmp_path)
    store.set_meta("probe", "value")
    store.close()
    reopened = Store.open(tmp_path)
    assert reopened.get_meta("probe") == "value"
    reopened.close()


def test_schema_mismatch_triggers_rebuild(tmp_path):
    store = Store.open(tmp_path)
    store.set_meta("probe", "value")
    store.set_meta("schema_version", "-1")
    store.close()
    reopened = Store.open(tmp_path)
    assert reopened.get_meta("probe") is None
    assert reopened.get_meta("schema_version") == str(SCHEMA_VERSION)
    reopened.close()


def test_edges_have_reverse_index(tmp_path):
    store = Store.open(tmp_path)
    plan = store.connection.execute(
        "EXPLAIN QUERY PLAN SELECT src FROM edges WHERE rev=? AND dst=?", ("r", "n")
    ).fetchall()
    assert any("idx_edges_dst" in str(row) for row in plan)
    store.close()


def test_busy_timeout_is_set(tmp_path):
    store = Store.open(tmp_path)
    timeout = store.connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout >= 10000
    store.close()
