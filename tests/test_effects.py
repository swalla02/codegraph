import pytest

from codegraph.effects.propagate import propagate, witness_path
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.effects import effects_report
from codegraph.store import Store


def build(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    return store


def kinds(report):
    return {row.detail.split()[0] for group in report.groups for row in group.rows}


def test_direct_network_effect_detected(repo, write):
    write("m.py", "import requests\n\n\ndef fetch():\n    requests.get('u')\n", commit="m")
    store = build(repo)
    assert "NETWORK" in kinds(effects_report(store, "HEAD", "m.py::fetch"))
    store.close()


def test_effect_propagates_through_call_chain(repo, write):
    source = (
        "import requests\n\n\n"
        "def low():\n    requests.get('u')\n\n\n"
        "def mid():\n    low()\n\n\n"
        "def high():\n    mid()\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    assert "NETWORK" in kinds(effects_report(store, "HEAD", "m.py::high"))
    store.close()


def test_pure_function_reports_no_effects(repo, write):
    write("m.py", "def pure(x):\n    return x + 1\n", commit="m")
    store = build(repo)
    assert effects_report(store, "HEAD", "m.py::pure").groups == []
    store.close()


def test_recursion_cycle_does_not_hang(repo, write):
    source = (
        "import requests\n\n\n"
        "def ping(n):\n    requests.get('u')\n    return pong(n)\n\n\n"
        "def pong(n):\n    return ping(n)\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    assert "NETWORK" in kinds(effects_report(store, "HEAD", "m.py::pong"))
    store.close()


def test_witness_path_reaches_the_causing_callsite(repo, write):
    source = "import requests\n\n\ndef low():\n    requests.get('u')\n\n\ndef high():\n    low()\n"
    write("m.py", source, commit="m")
    store = build(repo)
    report = effects_report(store, "HEAD", "m.py::high")
    row = report.groups[0].rows[0]
    assert "m.py::low" in row.detail
    assert row.location.startswith("m.py:")
    store.close()


def test_global_mutation_detected_syntactically(repo, write):
    write("m.py", "COUNT = 0\n\n\ndef bump():\n    global COUNT\n    COUNT += 1\n", commit="m")
    store = build(repo)
    assert "GLOBAL_MUTATE" in kinds(effects_report(store, "HEAD", "m.py::bump"))
    store.close()


def test_project_override_tags_house_abstraction(repo, write):
    write("codegraph.toml", '[[effect]]\nmatch = "app.db.*"\nkind = "DB_WRITE"\n')
    write("app/__init__.py", "", commit="pkg")
    write("app/db.py", "def save():\n    pass\n", commit="db")
    write("svc.py", "from app import db\n\n\ndef run():\n    db.save()\n", commit="svc")
    store = build(repo)
    assert "DB_WRITE" in kinds(effects_report(store, "HEAD", "svc.py::run"))
    store.close()


def test_witness_follows_the_path_that_supports_the_reported_confidence(repo, write):
    """`query` has two outgoing calls: a LOW-confidence ambiguous call
    (`thing.shared()`, matching both One.shared and Two.shared) that reaches
    a direct effect through One.shared, and a HIGH-confidence call chain
    (query -> b -> c) that reaches a direct effect through c. The best
    achievable confidence is HIGH (via b -> c); the printed chain must be
    the one that actually supports HIGH, not the shorter LOW one."""
    write(
        "one.py",
        "import requests\n\n\nclass One:\n    def shared(self):\n        requests.get('u')\n",
        commit="one",
    )
    write("two.py", "class Two:\n    def shared(self):\n        pass\n", commit="two")
    write(
        "b.py",
        "import requests\n\n\ndef c():\n    requests.get('u')\n\n\ndef b():\n    c()\n",
        commit="b",
    )
    write(
        "caller.py",
        "from b import b\n\n\ndef query(thing):\n    thing.shared()\n    b()\n",
        commit="caller",
    )
    store = build(repo)
    report = effects_report(store, "HEAD", "caller.py::query")
    group = next(g for g in report.groups if g.title == "NETWORK")
    row = group.rows[0]
    assert row.detail.startswith("NETWORK HIGH via")
    assert "b.py::c" in row.detail
    assert "one.py::One.shared" not in row.detail
    store.close()


def test_cycle_confidence_is_the_bottleneck_out_of_the_asking_node(tmp_path):
    """Direct schema seeding, not a mock: the same `nodes`/`edges`/`effects`
    tables `propagate` and `witness_path` read and write, populated by hand
    so the shape is exact. `A` can only leave via a LOW edge to `B`; `B`
    can call back to `A` at HIGH, but that never gives `A` a *new* way out
    -- A's own only exit is still the LOW edge. `B` carries the direct
    effect. This is the round-2 review shape: SCC condensation used to
    merge B's direct HIGH straight into the shared `{A, B}` component set
    without accounting for the LOW edge needed to reach B from A at all,
    handing out a HIGH confidence that no real path leaving A supported."""
    store = Store.open(tmp_path)
    rev = "HEAD"
    connection = store.connection
    connection.executemany(
        "INSERT INTO nodes(rev, id, path, qualname, kind, line_start, line_end, body_hash,"
        " name_binding) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (rev, "m.py::A", "m.py", "A", "function", 1, 1, "x", "live"),
            (rev, "m.py::B", "m.py", "B", "function", 2, 2, "x", "live"),
        ],
    )
    connection.executemany(
        "INSERT INTO edges(rev, src, dst, kind, confidence, provenance, callsite_path,"
        " callsite_line) VALUES(?,?,?,?,?,?,?,?)",
        [
            (rev, "m.py::A", "m.py::B", "CALLS", "LOW", "static", "m.py", 1),
            (rev, "m.py::B", "m.py::A", "CALLS", "HIGH", "static", "m.py", 2),
        ],
    )
    connection.execute(
        "INSERT INTO effects(rev, node_id, kind, direct, evidence_path, evidence_line,"
        " confidence) VALUES(?,?,?,?,?,?,?)",
        (rev, "m.py::B", "NETWORK", 1, "m.py", 2, "HIGH"),
    )
    connection.commit()

    propagate(store, rev)

    report = effects_report(store, rev, "m.py::A")
    row = next(r for g in report.groups for r in g.rows)
    assert row.detail == "NETWORK LOW via m.py::A -> m.py::B"
    assert row.location == "m.py:2"

    chain = witness_path(store, rev, "m.py::A", "NETWORK", "LOW")
    assert chain == ["m.py::A", "m.py::B"]
    store.close()


_GUARD_FIXTURES = {
    "acyclic_chain": (
        {
            "chain.py": (
                "import requests\n\n\n"
                "def low():\n    requests.get('u')\n\n\n"
                "def mid():\n    low()\n\n\n"
                "def high():\n    mid()\n"
            ),
        },
        "chain.py::high",
    ),
    "recursion_cycle": (
        {
            "recur.py": (
                "import requests\n\n\n"
                "def ping(n):\n    requests.get('u')\n    return pong(n)\n\n\n"
                "def pong(n):\n    return ping(n)\n"
            ),
        },
        "recur.py::pong",
    ),
    "mixed_confidence_acyclic": (
        {
            "one.py": (
                "import requests\n\n\nclass One:\n    def shared(self):\n        requests.get('u')\n"
            ),
            "two.py": "class Two:\n    def shared(self):\n        pass\n",
            "b.py": "import requests\n\n\ndef c():\n    requests.get('u')\n\n\ndef b():\n    c()\n",
            "caller.py": "from b import b\n\n\ndef query(thing):\n    thing.shared()\n    b()\n",
        },
        "caller.py::query",
    ),
    "mixed_confidence_cycle": (
        {
            "decoy.py": "def b():\n    pass\n",
            "cyc_a.py": "def a():\n    return b()\n",
            "cyc_b.py": (
                "import requests\n\nfrom cyc_a import a\n\n\n"
                "def b():\n    requests.get('u')\n    a()\n"
            ),
        },
        "cyc_a.py::a",
    ),
}


@pytest.mark.parametrize("files, node_id", _GUARD_FIXTURES.values(), ids=_GUARD_FIXTURES.keys())
def test_no_effect_row_has_an_empty_witness_or_location(repo, write, files, node_id):
    """Regression guard for the round-2 Critical: if `propagate` ever again
    hands a node a confidence no real path out of it supports, `witness_path`
    (correctly) comes back empty and the report degrades to `"KIND CONF via "`
    with no chain and no location -- exactly the shape that shipped and was
    only caught by manual review. Exercise every shape this module cares
    about (a plain chain, a same-confidence recursion cycle, an acyclic mix
    of confidences, and a genuine mixed-confidence cycle) and make that
    degenerate output structurally impossible to ship again unnoticed."""
    for path, source in files.items():
        write(path, source, commit=path)
    store = build(repo)
    report = effects_report(store, "HEAD", node_id)
    for group in report.groups:
        for row in group.rows:
            assert row.location, f"empty location: {row!r}"
            chain_text = row.detail.split(" via ", 1)[-1]
            assert chain_text.strip(), f"empty witness chain: {row.detail!r}"
    store.close()
