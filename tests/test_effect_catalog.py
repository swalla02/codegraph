from codegraph.config import Config
from codegraph.effects.catalog import EFFECT_KINDS, Catalog


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
