from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.resolve import module_for_path
from codegraph.store import Store


def build(repo):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    return store, indexer


def edges(store, rev="HEAD"):
    return {
        (row["src"], row["dst"], row["confidence"])
        for row in store.connection.execute(
            "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
        )
    }


def test_module_for_path_handles_src_layout_and_packages():
    roots = ("", "src")
    assert module_for_path("src/pay/service.py", roots) == "pay.service"
    assert module_for_path("pay/__init__.py", roots) == "pay"
    assert module_for_path("a.py", roots) == "a"


def test_same_module_call_is_high_confidence(repo, write):
    write("m.py", "def helper():\n    pass\n\n\ndef caller():\n    helper()\n", commit="m")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("m.py::caller", "m.py::helper", "HIGH") in edges(store)
    store.close()


def test_module_node_is_materialized_and_owns_module_scope_edges(repo, write):
    """`path::<module>` is a real edge source (an import-time side effect
    like `app = create_app()`), so it must have a row in `nodes` — a reverse
    BFS that joins edges to nodes must not silently drop it.
    """
    write("app.py", "def create_app():\n    pass\n\n\napp = create_app()\n", commit="app")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    node = store.connection.execute(
        "SELECT kind FROM nodes WHERE rev='HEAD' AND id='app.py::<module>'"
    ).fetchone()
    assert node is not None
    assert node["kind"] == "module"
    module_edges = {(src, dst) for src, dst, _ in edges(store) if src == "app.py::<module>"}
    assert module_edges == {("app.py::<module>", "app.py::create_app")}
    store.close()


def test_imported_call_is_high_confidence(repo, write):
    write("pay/__init__.py", "", commit="pkg")
    write("pay/service.py", "def charge():\n    pass\n", commit="svc")
    write(
        "handlers.py", "from pay.service import charge\n\n\ndef run():\n    charge()\n", commit="h"
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("handlers.py::run", "pay/service.py::charge", "HIGH") in edges(store)
    store.close()


def test_self_call_resolves_through_class(repo, write):
    source = (
        "class Service:\n"
        "    def run(self):\n        self.step()\n"
        "    def step(self):\n        pass\n"
    )
    write("s.py", source, commit="s")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("s.py::Service.run", "s.py::Service.step", "HIGH") in edges(store)
    store.close()


def test_self_call_resolves_through_base_class(repo, write):
    write("base.py", "class Base:\n    def step(self):\n        pass\n", commit="base")
    write(
        "child.py",
        "from base import Base\n\n\nclass Child(Base):\n    def run(self):\n        self.step()\n",
        commit="child",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("child.py::Child.run", "base.py::Base.step", "HIGH") in edges(store)
    store.close()


def test_unique_method_name_is_medium_confidence(repo, write):
    write("owner.py", "class Owner:\n    def unique_op(self):\n        pass\n", commit="o")
    write("caller.py", "def go(thing):\n    thing.unique_op()\n", commit="c")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("caller.py::go", "owner.py::Owner.unique_op", "MEDIUM") in edges(store)
    store.close()


def test_ambiguous_method_name_emits_low_edges_to_every_candidate(repo, write):
    write("one.py", "class One:\n    def shared(self):\n        pass\n", commit="1")
    write("two.py", "class Two:\n    def shared(self):\n        pass\n", commit="2")
    write("caller.py", "def go(thing):\n    thing.shared()\n", commit="c")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = {(dst, conf) for src, dst, conf in edges(store) if src == "caller.py::go"}
    assert found == {("one.py::One.shared", "LOW"), ("two.py::Two.shared", "LOW")}
    store.close()


def test_shadowed_definition_does_not_win_name_lookup(repo, write):
    source = "def alpha():\n    return 1\n\n\ndef alpha():\n    return 2\n\n\ndef caller():\n    alpha()\n"
    write("m.py", source, commit="m")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    targets = {dst for src, dst, _ in edges(store) if src == "m.py::caller"}
    assert targets == {"m.py::alpha"}
    store.close()


def test_call_inside_a_shadowed_definition_is_attributed_to_it(repo, write):
    """A definition shadowed by a later one of the same name still runs (a
    framework may hold a reference to it, e.g. `@app.route` handlers), so a
    call made from inside it must originate from the shadowed node, not the
    live one that happens to share its name.
    """
    source = (
        "def helper():\n    pass\n\n\ndef handle():\n    helper()\n\n\ndef handle():\n    pass\n"
    )
    write("m.py", source, commit="m")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = {(src, dst) for src, dst, _ in edges(store) if dst == "m.py::helper"}
    assert found == {("m.py::handle#1", "m.py::helper")}
    store.close()


def test_external_call_is_unresolved_not_an_edge(repo, write):
    write("m.py", "import requests\n\n\ndef fetch():\n    requests.get('u')\n", commit="m")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    rows = store.connection.execute("SELECT raw_name FROM unresolved WHERE rev='HEAD'").fetchall()
    assert "requests.get" in {row["raw_name"] for row in rows}
    store.close()


def test_super_call_resolves_weakly_by_name_not_dropped(repo, write):
    """Regression for F2: `super().helper()`'s receiver (`super()`) isn't a
    flattenable Name/Attribute chain, so `visit_Call` used to drop the ref
    entirely -- no edge, no `unresolved` row. `helper` is unique in this
    repo (unlike the overriding method's own name), so the existing
    repo-wide by-last-segment step should now weakly resolve it at MEDIUM,
    exactly as `test_unique_method_name_is_medium_confidence` does for
    `thing.unique_op()`."""
    write(
        "base.py",
        "class Base:\n    def helper(self):\n        pass\n",
        commit="base",
    )
    write(
        "child.py",
        "from base import Base\n\n\n"
        "class Child(Base):\n    def go(self):\n        super().helper()\n",
        commit="child",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("child.py::Child.go", "base.py::Base.helper", "MEDIUM") in edges(store)
    store.close()


def test_call_on_non_flattenable_receiver_with_no_match_is_unresolved_not_dropped(repo, write):
    """`PaymentService().charge(x)` has no `charge` defined anywhere in the
    repo, so it cannot resolve -- but it must still show up in `unresolved`
    rather than vanishing without a trace."""
    write(
        "m.py",
        "class PaymentService:\n    pass\n\n\n"
        "def run():\n    PaymentService().charge(1)\n",
        commit="m",
    )
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.unresolved >= 1
    rows = store.connection.execute("SELECT raw_name FROM unresolved WHERE rev='HEAD'").fetchall()
    assert "<attr>.charge" in {row["raw_name"] for row in rows}
    store.close()


def test_dynamic_call_with_no_attribute_is_unresolved_not_dropped(repo, write):
    """`handlers[i]()` -- the callable isn't even an attribute access, so
    there is no name to key on at all; it still must be counted."""
    write(
        "m.py",
        "def dispatch(handlers, i):\n    handlers[i]()\n",
        commit="m",
    )
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.unresolved >= 1
    rows = store.connection.execute("SELECT raw_name FROM unresolved WHERE rev='HEAD'").fetchall()
    assert "<dynamic>" in {row["raw_name"] for row in rows}
    store.close()


def test_editing_a_module_updates_edges_in_its_importers(repo, write):
    write("dep.py", "def target():\n    pass\n", commit="dep")
    write("user.py", "from dep import target\n\n\ndef go():\n    target()\n", commit="user")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    write("dep.py", "def renamed():\n    pass\n", commit="rename target")
    indexer.reconcile("HEAD")
    assert not [e for e in edges(store) if e[1] == "dep.py::target"]
    store.close()


# -- beyond the brief: relative imports, dependents, and the CLI surface -----


def test_relative_import_resolves_through_the_package(repo, write):
    write("pkg/__init__.py", "", commit="pkg")
    write("pkg/service.py", "def charge():\n    pass\n", commit="svc")
    write("pkg/sub/__init__.py", "", commit="sub")
    write(
        "pkg/sub/handler.py",
        "from ..service import charge\n\n\ndef run():\n    charge()\n",
        commit="handler",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("pkg/sub/handler.py::run", "pkg/service.py::charge", "HIGH") in edges(store)
    store.close()


def test_inherits_edges_are_recorded(repo, write):
    write("base.py", "class Base:\n    pass\n", commit="base")
    write("child.py", "from base import Base\n\n\nclass Child(Base):\n    pass\n", commit="child")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    rows = store.connection.execute(
        "SELECT src, dst, confidence FROM edges WHERE rev='HEAD' AND kind='INHERITS'"
    ).fetchall()
    assert ("child.py::Child", "base.py::Base", "HIGH") in {tuple(row) for row in rows}
    store.close()


def test_dependents_reports_importers_of_a_module(repo, write):
    write("dep.py", "def target():\n    pass\n", commit="dep")
    write("user.py", "from dep import target\n\n\ndef go():\n    target()\n", commit="user")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    from codegraph.resolve import dependents

    assert dependents(store, "HEAD", {"dep"}) == {"user.py"}
    assert dependents(store, "HEAD", set()) == set()
    store.close()


def test_resolve_command_prints_the_single_match(repo, write, capsys):
    write("m.py", "def only_one():\n    pass\n", commit="m")
    assert main(["resolve", "only_one", "--path", str(repo), "--rev", "HEAD"]) == 0
    assert capsys.readouterr().out.strip() == "m.py::only_one"


def test_resolve_command_exits_two_on_ambiguity(repo, write, capsys):
    write("one.py", "def shared():\n    pass\n", commit="1")
    write("two.py", "def shared():\n    pass\n", commit="2")
    assert main(["resolve", "shared", "--path", str(repo), "--rev", "HEAD"]) == 2
    out = capsys.readouterr().out
    assert "one.py::shared" in out
    assert "two.py::shared" in out


def test_resolve_command_exits_one_when_nothing_matches(repo, capsys):
    assert main(["resolve", "nope", "--path", str(repo), "--rev", "HEAD"]) == 1


def test_status_reports_the_unresolved_count(repo, write, capsys):
    write("m.py", "import requests\n\n\ndef fetch():\n    requests.get('u')\n", commit="m")
    assert main(["status", "--path", str(repo), "--rev", "HEAD"]) == 0
    assert "unresolved: 1" in capsys.readouterr().out
