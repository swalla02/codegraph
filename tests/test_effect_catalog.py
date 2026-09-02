import pytest

from codegraph.config import Config
from codegraph.effects.catalog import EFFECT_KINDS, Catalog, Rule
from codegraph.resolve import HIGH, LOW, MEDIUM


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


def test_wildcard_head_match_is_not_high_confidence():
    """Regression for F3: `*.execute` matches a completely uninferable
    receiver -- it must not claim the same HIGH tier as a literal name
    like `requests.post`."""
    catalog = Catalog.load(Config())
    kind, confidence = catalog.match_with_confidence("cursor.execute")
    assert kind == "DB_WRITE"
    assert confidence == LOW


def test_fully_literal_match_is_high_confidence():
    catalog = Catalog.load(Config())
    kind, confidence = catalog.match_with_confidence("os.getenv")
    assert kind == "ENV_READ"
    assert confidence == HIGH


def test_partial_prefix_match_is_medium_confidence():
    """`requests.*` and `boto3.*` have a real but partial literal prefix --
    more specific than a bare wildcard head, but not a literal name. Uses
    `requests.codes`, not `requests.get`: the literal HTTP-verb rules below
    make `.get` HIGH now, so `.codes` -- a catch-all-only member with no
    verb-specific rule of its own -- is the one that still exercises the
    bare `requests.*` MEDIUM tier."""
    catalog = Catalog.load(Config())
    assert catalog.match_with_confidence("requests.codes") == ("NETWORK", MEDIUM)
    assert catalog.match_with_confidence("boto3.client") == ("DB_WRITE", MEDIUM)


@pytest.mark.parametrize(
    "dotted",
    [
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.patch",
        "requests.delete",
        "requests.head",
        "requests.options",
        "requests.request",
    ],
)
def test_requests_http_verbs_are_high_confidence(dotted):
    """C3 follow-up: the literal HTTP verbs are the most common and least
    ambiguous network call in Python and must not be worse-labeled than
    before `requests.*` was tiered MEDIUM."""
    catalog = Catalog.load(Config())
    assert catalog.match_with_confidence(dotted) == ("NETWORK", HIGH)


def test_requests_verb_literal_beats_requests_namespace_catchall():
    """Longest-literal-prefix-wins must actually favor the literal verb
    rule over the `requests.*` catch-all, not just declare it in a
    docstring -- `requests.get`'s prefix (13 chars, no wildcard) is longer
    than `requests.*`'s (9 chars, up to the `*`), so it wins regardless of
    declaration order in builtin.toml."""
    catalog = Catalog.load(Config())
    kind, confidence = catalog.match_with_confidence("requests.get")
    assert (kind, confidence) == ("NETWORK", HIGH)
    # And the catch-all is still there for everything else in the namespace.
    assert catalog.match_with_confidence("requests.codes") == ("NETWORK", MEDIUM)


def test_indexers_own_source_reconcile_is_no_longer_a_high_confidence_db_write():
    """The concrete repro from the finding: `Indexer.reconcile` calling
    `cursor.execute("SELECT path, blob_sha FROM tree")` used to be reported
    as `DB_WRITE HIGH` -- the same tier as a literal call -- purely because
    `*.execute` matched. It is still DB_WRITE (never drop a candidate), but
    now LOW."""
    catalog = Catalog.load(Config())
    assert catalog.match_with_confidence("connection.execute") == ("DB_WRITE", LOW)


def test_rule_confidence_override_is_honored_over_derived_specificity():
    rule = Rule(match="app.thing", kind="NETWORK", confidence=LOW)
    catalog = Catalog((rule,), 0)
    assert catalog.match_with_confidence("app.thing") == ("NETWORK", LOW)


def test_rule_rejects_unknown_confidence():
    with pytest.raises(ValueError, match="NOT_A_TIER"):
        Rule(match="app.thing", kind="NETWORK", confidence="NOT_A_TIER")


def test_no_match_returns_none_with_confidence():
    assert Catalog.load(Config()).match_with_confidence("my.own.helper") is None


def test_rule_rejects_unknown_kind():
    with pytest.raises(ValueError, match="NOT_A_KIND"):
        Rule(match="app.thing", kind="NOT_A_KIND")


def test_catalog_load_rejects_unknown_kind_in_override():
    config = Config(effect_overrides=({"match": "app.thing", "kind": "NOT_A_KIND"},))
    with pytest.raises(ValueError, match="NOT_A_KIND"):
        Catalog.load(config)
