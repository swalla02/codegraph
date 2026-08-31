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


def direct_effects(store, path):
    return {
        (row["kind"], row["confidence"])
        for row in store.connection.execute(
            "SELECT kind, confidence FROM effects WHERE rev='HEAD' AND direct=1"
            " AND evidence_path=?",
            (path,),
        )
    }


def test_write_mode_open_is_an_fs_write(repo, write):
    """Regression for F3: `open` used to map to FS_READ regardless of mode.
    A literal write mode must be a genuine FS_WRITE, at HIGH confidence
    (the mode is a literal, so the evidence really does support it)."""
    write("m.py", "def save(p):\n    open(p, 'w')\n", commit="m")
    store = build(repo)
    assert ("FS_WRITE", "HIGH") in direct_effects(store, "m.py")
    assert "FS_READ" not in {kind for kind, _ in direct_effects(store, "m.py")}
    store.close()


def test_read_mode_open_stays_fs_read_at_high_confidence(repo, write):
    write("m.py", "def load(p):\n    open(p)\n    open(p, 'r')\n", commit="m")
    store = build(repo)
    assert direct_effects(store, "m.py") == {("FS_READ", "HIGH")}
    store.close()


def test_non_literal_open_mode_stays_read_but_at_lower_confidence(repo, write):
    """The mode isn't a literal, so the FS_READ default is kept (never drop
    a candidate) but the evidence doesn't actually establish it -- unlike
    the literal cases, this must not claim HIGH."""
    write("m.py", "def load(p, mode):\n    open(p, mode)\n", commit="m")
    store = build(repo)
    assert direct_effects(store, "m.py") == {("FS_READ", "MEDIUM")}
    store.close()


def test_super_delegation_chain_reports_effects(repo, write):
    """Regression for F2 end-to-end: before the fix, a handler whose entire
    call chain ran through `super()` reported ZERO effects -- the call ref
    was dropped by the parser (unflattenable receiver), so no edge was ever
    recorded from the override to the base method carrying the direct
    effect, and propagation had nothing to walk."""
    write(
        "base.py",
        "import requests\n\n\nclass Base:\n    def helper(self):\n        requests.get('u')\n",
        commit="base",
    )
    write(
        "child.py",
        "from base import Base\n\n\n"
        "class Child(Base):\n    def go(self):\n        super().helper()\n",
        commit="child",
    )
    store = build(repo)
    assert "NETWORK" in kinds(effects_report(store, "HEAD", "child.py::Child.go"))
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
    (query -> b -> c) that reaches a direct effect through c. Both direct
    effects use `os.getenv`, a fully-literal (no wildcard) catalog pattern
    that is HIGH on its own -- so the only thing distinguishing the two
    paths is the CALLS-edge confidence, not the direct effect's own tier
    (see `test_direct_effect_confidence_bounds_the_propagated_confidence`
    for that case). The best achievable confidence is HIGH (via b -> c);
    the printed chain must be the one that actually supports HIGH, not the
    shorter LOW one."""
    write(
        "one.py",
        "import os\n\n\nclass One:\n    def shared(self):\n        os.getenv('u')\n",
        commit="one",
    )
    write("two.py", "class Two:\n    def shared(self):\n        pass\n", commit="two")
    write(
        "b.py",
        "import os\n\n\ndef c():\n    os.getenv('u')\n\n\ndef b():\n    c()\n",
        commit="b",
    )
    write(
        "caller.py",
        "from b import b\n\n\ndef query(thing):\n    thing.shared()\n    b()\n",
        commit="caller",
    )
    store = build(repo)
    report = effects_report(store, "HEAD", "caller.py::query")
    group = next(g for g in report.groups if g.title == "ENV_READ")
    row = group.rows[0]
    assert row.detail.startswith("ENV_READ HIGH via")
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


def test_direct_effect_confidence_bounds_the_propagated_confidence(tmp_path):
    """B0: a caller reached only by HIGH `CALLS` edges must not inherit HIGH
    for an effect whose own direct detection is LOW -- the binding
    constraint here is the direct effect's own tier, not any edge. Schema
    seeded by hand (same tables `propagate`/`witness_path` read and write):
    `A --HIGH--> B --HIGH--> C`, `C` carries a LOW-confidence direct effect
    (the shape a bare-wildcard-head catalog match like `*.execute`
    produces). Every edge on the only path out of A is HIGH, so before the
    fix A reported HIGH; the direct detection at the end of the chain is
    the true bottleneck and both A and B must report LOW."""
    store = Store.open(tmp_path)
    rev = "HEAD"
    connection = store.connection
    connection.executemany(
        "INSERT INTO nodes(rev, id, path, qualname, kind, line_start, line_end, body_hash,"
        " name_binding) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (rev, "m.py::A", "m.py", "A", "function", 1, 1, "x", "live"),
            (rev, "m.py::B", "m.py", "B", "function", 2, 2, "x", "live"),
            (rev, "m.py::C", "m.py", "C", "function", 3, 3, "x", "live"),
        ],
    )
    connection.executemany(
        "INSERT INTO edges(rev, src, dst, kind, confidence, provenance, callsite_path,"
        " callsite_line) VALUES(?,?,?,?,?,?,?,?)",
        [
            (rev, "m.py::A", "m.py::B", "CALLS", "HIGH", "static", "m.py", 1),
            (rev, "m.py::B", "m.py::C", "CALLS", "HIGH", "static", "m.py", 2),
        ],
    )
    connection.execute(
        "INSERT INTO effects(rev, node_id, kind, direct, evidence_path, evidence_line,"
        " confidence) VALUES(?,?,?,?,?,?,?)",
        (rev, "m.py::C", "DB_WRITE", 1, "m.py", 3, "LOW"),
    )
    connection.commit()

    propagate(store, rev)

    for asking_node in ("m.py::A", "m.py::B"):
        report = effects_report(store, rev, asking_node)
        row = next(r for g in report.groups for r in g.rows)
        assert row.detail.startswith("DB_WRITE LOW via")
        assert row.location == "m.py:3"

    chain = witness_path(store, rev, "m.py::A", "DB_WRITE", "LOW")
    assert chain == ["m.py::A", "m.py::B", "m.py::C"]
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


def test_effects_report_picks_the_strongest_confidence_for_a_repeated_kind(tmp_path):
    """B8 regression: `query/effects.py` used to define its own
    `_CONFIDENCE_RANK` with the OPPOSITE polarity from `effects/
    propagate.py` and `query/impact.py` (lower-is-stronger there, versus
    higher-is-stronger everywhere else), using bare string literals
    instead of importing `HIGH`/`MEDIUM`/`LOW` from `resolve.py`. Two rows
    for the same node/kind at different confidence (a direct HIGH
    detection plus a weaker LOW propagated row that would not normally
    coexist, but the code has to pick correctly regardless) must resolve
    to the STRONGER one, HIGH -- exercising the same `stronger()` helper
    and `CONFIDENCE_RANK` table `propagate.py` and `impact.py` use, now
    imported from `resolve.py` rather than redefined."""
    store = Store.open(tmp_path)
    rev = "HEAD"
    connection = store.connection
    connection.execute(
        "INSERT INTO nodes(rev, id, path, qualname, kind, line_start, line_end, body_hash,"
        " name_binding) VALUES(?,?,?,?,?,?,?,?,?)",
        (rev, "m.py::A", "m.py", "A", "function", 1, 1, "x", "live"),
    )
    connection.executemany(
        "INSERT INTO effects(rev, node_id, kind, direct, evidence_path, evidence_line,"
        " confidence) VALUES(?,?,?,?,?,?,?)",
        [
            (rev, "m.py::A", "NETWORK", 1, "m.py", 1, "LOW"),
            (rev, "m.py::A", "NETWORK", 1, "m.py", 1, "HIGH"),
        ],
    )
    connection.commit()

    report = effects_report(store, rev, "m.py::A")
    row = next(r for g in report.groups for r in g.rows)
    assert row.detail.startswith("NETWORK HIGH")
    store.close()
