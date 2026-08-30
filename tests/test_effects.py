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
