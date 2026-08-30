import pytest

from codegraph.config import Config
from codegraph.effects.catalog import EFFECT_KINDS, Catalog, Rule


def test_nine_effect_kinds_exactly():
    assert set(EFFECT_KINDS) == {
        "DB_READ",
        "DB_WRITE",
        "NETWORK",
        "FS_READ",
        "FS_WRITE",
        "PROCESS",
        "ENV_READ",
        "GLOBAL_MUTATE",
        "NONDETERMINISM",
    }


def test_builtin_rules_cover_common_libraries():
    catalog = Catalog.load(Config())
    assert catalog.match("requests.get") == "NETWORK"
    assert catalog.match("httpx.post") == "NETWORK"
    assert catalog.match("subprocess.run") == "PROCESS"
    assert catalog.match("os.environ.get") == "ENV_READ"
    assert catalog.match("open") == "FS_READ"


def test_unknown_name_matches_nothing():
    assert Catalog.load(Config()).match("my.own.helper") is None


def test_project_override_adds_house_abstractions():
    config = Config(effect_overrides=({"match": "app.db.*", "kind": "DB_WRITE"},))
    assert Catalog.load(config).match("app.db.save") == "DB_WRITE"


def test_override_beats_builtin_on_longer_prefix():
    config = Config(effect_overrides=({"match": "requests.get", "kind": "DB_READ"},))
    catalog = Catalog.load(config)
    assert catalog.match("requests.get") == "DB_READ"
    assert catalog.match("requests.post") == "NETWORK"


def test_fingerprint_changes_with_overrides():
    base = Catalog.load(Config()).fingerprint()
    other = Catalog.load(
        Config(effect_overrides=({"match": "x.*", "kind": "NETWORK"},))
    ).fingerprint()
    assert base != other


def test_execute_on_a_namespaced_db_client_is_still_a_write():
    """`*.execute` (prefix 0) and a namespace rule like `psycopg*` (prefix 7)
    can both match a fully-qualified name; the namespace rule must not win
    and silently turn a write into a read."""
    catalog = Catalog.load(Config())
    assert catalog.match("psycopg2.extensions.cursor.execute") == "DB_WRITE"
    assert catalog.match("psycopg2.cursor.execute") == "DB_WRITE"
    assert catalog.match("cursor.execute") == "DB_WRITE"


def test_fetch_family_is_a_db_read():
    catalog = Catalog.load(Config())
    assert catalog.match("cursor.fetchall") == "DB_READ"
    assert catalog.match("cursor.fetchone") == "DB_READ"
    assert catalog.match("cursor.fetchmany") == "DB_READ"


def test_rule_rejects_unknown_kind():
    with pytest.raises(ValueError, match="NOT_A_KIND"):
        Rule(match="app.thing", kind="NOT_A_KIND")


def test_catalog_load_rejects_unknown_kind_in_override():
    config = Config(effect_overrides=({"match": "app.thing", "kind": "NOT_A_KIND"},))
    with pytest.raises(ValueError, match="NOT_A_KIND"):
        Catalog.load(config)
