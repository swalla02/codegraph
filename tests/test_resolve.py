from codegraph.ambiguity import Ambiguity
from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.impact import impact_report
from codegraph.resolve import (
    is_derivable_fanout,
    module_for_path,
)
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


def test_ambiguous_method_name_names_every_candidate_without_storing_one(repo, write):
    """The over-approximation bias, relocated by #25: every candidate is still
    reachable, none of them is an edge."""
    write("one.py", "class One:\n    def shared(self):\n        pass\n", commit="1")
    write("two.py", "class Two:\n    def shared(self):\n        pass\n", commit="2")
    write("caller.py", "def go(thing):\n    thing.shared()\n", commit="c")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert not [dst for src, dst, _ in edges(store) if src == "caller.py::go"]
    ambiguity = Ambiguity(store, "HEAD")
    assert set(ambiguity.candidates("thing.shared")) == {
        "one.py::One.shared",
        "two.py::Two.shared",
    }
    assert ambiguity.callers("one.py::One.shared") == ["caller.py::go"]
    assert ambiguity.callers("two.py::Two.shared") == ["caller.py::go"]
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


def test_resolve_is_case_consistent_not_disjoint_across_query_case(repo, write, capsys):
    """B5 regression: steps 1-2 of `find_symbol` compared with binary `=`
    while step 3's `LIKE` was already case-insensitive. A query differing
    only in case from the real name could fall through the (missed) exact
    steps and land on step 3's dot-anchored suffix pattern instead --
    which can never match a top-level, dot-free qualname at all -- so
    `resolve charge` (1 match, the top-level function, via the exact-match
    step) and `resolve CHARGE` (2 disjoint matches, two unrelated nested
    methods, via the suffix step) used to return completely different
    result sets and flip exit code 0 -> 2."""
    write(
        "pay.py",
        (
            "def charge():\n    pass\n\n\n"
            "class PaymentService:\n    def charge(self):\n        pass\n\n\n"
            "class Refund:\n    def charge(self):\n        pass\n"
        ),
        commit="pay",
    )
    assert main(["resolve", "charge", "--path", str(repo), "--rev", "HEAD"]) == 0
    lower = capsys.readouterr().out.strip()
    assert main(["resolve", "CHARGE", "--path", str(repo), "--rev", "HEAD"]) == 0
    upper = capsys.readouterr().out.strip()
    assert lower == "pay.py::charge"
    assert upper == lower


def test_status_reports_the_unresolved_count(repo, write, capsys):
    write("m.py", "import requests\n\n\ndef fetch():\n    requests.get('u')\n", commit="m")
    assert main(["status", "--path", str(repo), "--rev", "HEAD"]) == 0
    assert "unresolved: 1" in capsys.readouterr().out


# -- the bare-name fan-out is derived, not stored ---------------------------
#
# The last-resort step matches a call's final dotted segment against every live
# definition in the revision. Measured on django (2,930 files) that produced 971
# candidates for a single call site and 2.09M LOW edges -- 96.6% of the graph --
# because the candidate list grows with the repo (#6). #6 capped that at write
# time; #25 established that the cap was in the wrong place, because the
# candidate set is `name_index[name]` and the `nodes` table already determines
# it. Nothing about it is stored now, at any size, and `ambiguity.py` recovers
# it exactly. These tests pin both halves: the graph does not hold it, and a
# query gets all of it back.


def unresolved_rows(store, rev="HEAD"):
    return [
        dict(row)
        for row in store.connection.execute(
            "SELECT src, path, line, raw_name, ref_kind, reason, candidates FROM unresolved"
            " WHERE rev=? ORDER BY path, line",
            (rev,),
        )
    ]


def many_savers(count):
    """`count` classes that each define `save`, plus one caller that can only be
    matched against all of them by name."""
    classes = "\n\n".join(
        f"class C{i}:\n    def save(self):\n        return {i}" for i in range(count)
    )
    return f"{classes}\n\n\ndef persist(item):\n    return item.save()\n"


def test_a_two_way_bare_name_call_is_not_materialized(repo, write):
    """Two candidates is as ambiguous as 971 for storage purposes: the set is
    `name_index['save']` either way, and the graph does not hold either."""
    write("m.py", many_savers(2), commit="m")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert not [dst for src, dst, _ in edges(store) if src == "m.py::persist"]
    assert stats.ambiguous == 1
    store.close()


def test_the_ambiguous_row_carries_everything_the_expansion_needs(repo, write):
    write("m.py", many_savers(6), commit="m")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")

    assert not [dst for src, dst, _ in edges(store) if src == "m.py::persist"]
    ambiguous = [row for row in unresolved_rows(store) if row["reason"] == "ambiguous"]
    assert len(ambiguous) == 1
    assert ambiguous[0]["raw_name"] == "item.save"
    assert ambiguous[0]["ref_kind"] == "call"
    assert ambiguous[0]["candidates"] == 6
    # `src` is the one thing about the reference the name index cannot
    # rederive, so it is the one thing the row has to carry.
    assert ambiguous[0]["src"] == "m.py::persist"
    assert stats.ambiguous == 1
    store.close()


def test_the_expansion_returns_exactly_what_the_resolver_would_have(repo, write):
    """The property #25 is about: the answer is still reachable, and the graph
    still does not contain it. 60 candidates is well past any cap that ever
    existed."""
    write("m.py", many_savers(60), commit="m")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")

    assert not [dst for src, dst, _ in edges(store) if src == "m.py::persist"]
    ambiguity = Ambiguity(store, "HEAD")
    assert set(ambiguity.candidates("item.save")) == {f"m.py::C{i}.save" for i in range(60)}
    for i in range(60):
        assert ambiguity.callers(f"m.py::C{i}.save") == ["m.py::persist"]
    store.close()


def test_a_shadowed_definition_is_not_a_candidate_of_the_expansion(repo, write):
    """The expansion rebuilds the resolver's LIVE name index, not every node
    that ever had the name -- a shadowed definition can still be an edge target
    by another route but never wins a name lookup."""
    write(
        "m.py",
        "class C:\n    def save(self):\n        return 1\n\n\n"
        "class C:\n    def save(self):\n        return 2\n\n\n"
        "class D:\n    def save(self):\n        return 3\n\n\n"
        "def persist(item):\n    return item.save()\n",
        commit="m",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    ambiguity = Ambiguity(store, "HEAD")
    candidates = ambiguity.candidates("item.save")
    assert set(candidates) == {"m.py::C.save", "m.py::D.save"}
    shadowed = [
        row["id"]
        for row in store.connection.execute(
            "SELECT id FROM nodes WHERE rev='HEAD' AND name_binding != 'live'"
        )
    ]
    assert shadowed, "the fixture stopped producing a shadowed definition"
    for node_id in shadowed:
        assert ambiguity.callers(node_id) == []
    store.close()


def test_ambiguous_is_counted_separately_from_unknown(repo, write):
    """They are opposite failures -- blind vs. dazzled -- and collapsing them
    into one number makes the health signal unreadable."""
    write("m.py", many_savers(6) + "\n\ndef gone():\n    no_such_name_anywhere()\n", commit="m")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.ambiguous == 1
    assert stats.unresolved >= 1
    reasons = {row["reason"] for row in unresolved_rows(store)}
    assert reasons == {"ambiguous", "unknown"}
    store.close()


def test_a_crowded_name_does_not_weaken_a_call_that_resolves_confidently(repo, write):
    """`AstResolver` stops at the first step that matches, so a module-local
    call never reaches the last-resort step at all -- deferring the fan-out must
    not change that just because the name is crowded elsewhere in the repo."""
    write("crowd.py", many_savers(6), commit="crowd")
    write(
        "m.py",
        "def save():\n    return 0\n\n\ndef caller():\n    return save()\n",
        commit="m",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    # module-local, so HIGH -- it never reaches the last-resort step at all
    assert ("m.py::caller", "m.py::save", "HIGH") in edges(store)
    store.close()


def test_an_ambiguous_base_class_is_deferred_without_disturbing_the_mro(repo, write):
    """Bases fan out exactly like calls (on django they were the larger half of
    the blowup), but only HIGH links feed the MRO walk and only an all-LOW set
    is deferred -- so inheritance resolution is unchanged."""
    write("crowd.py", "\n\n".join(f"class Base{i}:\n    pass" for i in range(6)), commit="crowd")
    write("dup.py", "\n\n".join(f"class C{i}:\n    class Base:\n        pass" for i in range(6)),
          commit="dup")
    write(
        "child.py",
        "from crowd import Base0\n\n\nclass Child(Base0):\n    pass\n",
        commit="child",
    )
    write(
        "guess.py",
        "\n\n".join(f"class Guess{i}(Base):\n    pass" for i in range(2)),
        commit="guess",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    inherits = {
        (row["src"], row["dst"], row["confidence"])
        for row in store.connection.execute(
            "SELECT src, dst, confidence FROM edges WHERE rev='HEAD' AND kind='INHERITS'"
        )
    }
    # The import makes this one certain, so the fan-out rule cannot touch it.
    assert ("child.py::Child", "crowd.py::Base0", "HIGH") in inherits
    # The bare `Base` matches six nested classes and is deferred instead.
    assert not [dst for src, dst, _ in inherits if src.startswith("guess.py::")]
    base_rows = [
        row
        for row in unresolved_rows(store)
        if row["reason"] == "ambiguous" and row["ref_kind"] == "base"
    ]
    assert len(base_rows) == 2
    ambiguity = Ambiguity(store, "HEAD")
    assert ambiguity.inheritors("dup.py::C0.Base") == [
        "guess.py::Guess0",
        "guess.py::Guess1",
    ]
    # ...and a base reference is NOT a call: `impact` walks CALLS only.
    assert ambiguity.callers("dup.py::C0.Base") == []
    store.close()


def test_a_deprecated_ambiguity_limit_warns_rather_than_changing_the_graph(repo, write, capsys):
    """The migration promise: an existing codegraph.toml keeps working, says so
    once, and gets the same graph as one without the setting."""
    write("m.py", many_savers(6), commit="m")
    store, indexer = build(repo)
    baseline = indexer.reconcile("HEAD").ambiguous
    store.close()

    write("codegraph.toml", "ambiguity_limit = 100\n", commit="cfg")
    capsys.readouterr()
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert "ambiguity_limit is deprecated" in capsys.readouterr().err
    assert stats.ambiguous == baseline
    assert not [dst for src, dst, _ in edges(store) if src == "m.py::persist"]
    store.close()


def test_no_ambiguity_limit_at_all_still_works(repo, write, capsys):
    write("m.py", many_savers(6), commit="m")
    write("codegraph.toml", 'source_roots = ["", "src"]\n', commit="cfg")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert "deprecated" not in capsys.readouterr().err
    assert stats.ambiguous == 1
    store.close()


def test_is_derivable_fanout_only_claims_a_set_the_resolver_could_not_tell_apart():
    """`AstResolver` returns the first matching step's hits, so today a result is
    either confident or entirely LOW and this mix cannot arise from it. The
    `Resolver` protocol is a documented swap-in seam, though, and a smarter
    engine can return both -- a set holding anything the resolver DID
    distinguish is not the derivable fan-out and must still be materialized."""
    weak = [(f"c.py::z{i}", "LOW") for i in range(6)]
    assert is_derivable_fanout(weak)
    assert not is_derivable_fanout([("a.py::x", "HIGH"), *weak])
    assert not is_derivable_fanout([("b.py::y", "MEDIUM"), *weak])
    assert not is_derivable_fanout([(f"a.py::x{i}", "HIGH") for i in range(50)])
    assert not is_derivable_fanout([])


# -- self.X finds overrides, not just the inherited declaration -------------
#
# Walking only UP the MRO and stopping at the first hit drops every subclass
# override, and `self` is an instance of the enclosing class or any subclass of
# it. Worst case measured on `psf/requests`: `SessionRedirectMixin.send` is a
# `...` stub that `Session` overrides, so `self.send()` inside
# `resolve_redirects` bound to the stub at HIGH and `impact Session.send` found
# nothing -- the edge that drives every redirect hop. See #14.


def test_self_call_finds_the_subclass_override_as_well_as_the_base(repo, write):
    write(
        "shapes.py",
        "class Shape:\n"
        "    def area(self):\n"
        "        return 0\n"
        "\n"
        "    def describe(self):\n"
        "        return self.area()\n"
        "\n\n"
        "class Square(Shape):\n"
        "    def area(self):\n"
        "        return 4\n",
        commit="shapes",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = {(dst, conf) for src, dst, conf in edges(store) if src == "shapes.py::Shape.describe"}
    assert ("shapes.py::Shape.area", "HIGH") in found
    assert ("shapes.py::Square.area", "MEDIUM") in found
    store.close()


def test_a_stub_base_does_not_hide_the_real_implementation(repo, write):
    """The requests shape, minimised: the base declares the method with an
    empty body and a subclass supplies the real one. First-match-wins bound
    `self.send()` to the stub and stopped, making the real implementation
    unreachable from `impact`."""
    write(
        "svc.py",
        "class Mixin:\n"
        "    def send(self):\n"
        "        ...\n"
        "\n"
        "    def retry(self):\n"
        "        return self.send()\n"
        "\n\n"
        "class Real(Mixin):\n"
        "    def send(self):\n"
        "        return 'sent'\n",
        commit="svc",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    targets = {dst for src, dst, _ in edges(store) if src == "svc.py::Mixin.retry"}
    assert "svc.py::Real.send" in targets, (
        "the real implementation must be reachable, not just the stub"
    )

    report = impact_report(store, "HEAD", "svc.py::Real.send")
    assert "svc.py::Mixin.retry" in {row.id for group in report.groups for row in group.rows}
    store.close()


def test_an_override_further_down_a_chain_is_still_found(repo, write):
    write(
        "chain.py",
        "class A:\n"
        "    def run(self):\n"
        "        return 0\n"
        "\n"
        "    def go(self):\n"
        "        return self.run()\n"
        "\n\n"
        "class B(A):\n"
        "    pass\n"
        "\n\n"
        "class C(B):\n"
        "    def run(self):\n"
        "        return 1\n",
        commit="chain",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    targets = {dst for src, dst, _ in edges(store) if src == "chain.py::A.go"}
    assert targets == {"chain.py::A.run", "chain.py::C.run"}
    store.close()


def test_an_unrelated_class_with_the_same_method_name_is_not_pulled_in(repo, write):
    """The override walk follows the class hierarchy, not the name. A class
    that merely shares a method name must not become a HIGH/MEDIUM candidate --
    that is the LOW fallback's job, at LOW."""
    write(
        "sep.py",
        "class Base:\n"
        "    def act(self):\n"
        "        return 0\n"
        "\n"
        "    def trigger(self):\n"
        "        return self.act()\n"
        "\n\n"
        "class Child(Base):\n"
        "    def act(self):\n"
        "        return 1\n"
        "\n\n"
        "class Stranger:\n"
        "    def act(self):\n"
        "        return 2\n",
        commit="sep",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    targets = {dst for src, dst, _ in edges(store) if src == "sep.py::Base.trigger"}
    assert targets == {"sep.py::Base.act", "sep.py::Child.act"}
    assert "sep.py::Stranger.act" not in targets
    store.close()


def test_an_inheritance_cycle_does_not_hang_the_override_walk(repo, write):
    """`class A(B)` / `class B(A)` is not valid Python at runtime, but it is
    parseable, and the resolver reads text -- the downward walk has to be as
    cycle-safe as the MRO walk above it."""
    write(
        "cyc.py",
        "class A(B):\n"
        "    def run(self):\n"
        "        return 0\n"
        "\n"
        "    def go(self):\n"
        "        return self.run()\n"
        "\n\n"
        "class B(A):\n"
        "    def run(self):\n"
        "        return 1\n",
        commit="cyc",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")  # must terminate
    targets = {dst for src, dst, _ in edges(store) if src == "cyc.py::A.go"}
    assert "cyc.py::A.run" in targets
    store.close()


# -- builtins are not repo symbols -----------------------------------------
#
# The last-resort step matches a call's final segment against every definition
# in the repo, and plenty of builtins share a name with a plausible method. On
# `psf/requests`, `badargs = set(kwargs) - set(result)` inside `create_cookie`
# became an edge to `RequestsCookieJar.set`, which then carried a NONDETERMINISM
# effect into a witness path presented to the user as evidence. See #17.


def repo_with_a_method_named_set(write):
    write(
        "jar.py",
        "class Jar:\n"
        "    def set(self, k, v):\n"
        "        self._d[k] = v\n",
    )
    write(
        "make.py",
        "def build(kwargs, result):\n"
        "    badargs = set(kwargs) - set(result)\n"
        "    return badargs\n",
        commit="jar",
    )


def test_a_bare_builtin_call_is_not_an_edge_to_a_same_named_method(repo, write):
    repo_with_a_method_named_set(write)
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert not [dst for src, dst, _ in edges(store) if src == "make.py::build"], (
        "the builtin set() was linked to a repo method named set"
    )
    store.close()


def test_a_builtin_is_recorded_as_such_and_not_counted_as_a_gap(repo, write):
    repo_with_a_method_named_set(write)
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    rows = [row for row in unresolved_rows(store) if row["path"] == "make.py"]
    assert {row["reason"] for row in rows} == {"builtin"}
    assert {row["raw_name"] for row in rows} == {"set"}
    # Recorded, but a builtin is not a hole in the graph.
    assert stats.unresolved == 0
    store.close()


def test_a_dotted_call_ending_in_a_builtin_name_still_falls_through(repo, write):
    """Only a BARE name can be the builtin. `x.set(...)` is a method call on
    something and must keep reaching the name match -- otherwise this fix would
    blind the resolver to every `.set()`, `.list()` and `.format()` in the repo."""
    write("jar.py", "class Jar:\n    def set(self, k):\n        return k\n")
    write("use.py", "def store(j, k):\n    return j.set(k)\n", commit="use")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("use.py::store", "jar.py::Jar.set", "MEDIUM") in edges(store)
    store.close()


def test_a_module_local_definition_still_shadows_the_builtin(repo, write):
    """The skip is only safe because the earlier steps run first. A repo that
    really does define `set` must still resolve to its own."""
    write(
        "own.py",
        "def set(x):\n    return x\n\n\ndef caller():\n    return set(1)\n",
        commit="own",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("own.py::caller", "own.py::set", "HIGH") in edges(store)
    store.close()


def test_an_imported_definition_still_shadows_the_builtin(repo, write):
    write("lib.py", "def set(x):\n    return x\n")
    write(
        "app.py",
        "from lib import set\n\n\ndef caller():\n    return set(1)\n",
        commit="imported",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("app.py::caller", "lib.py::set", "HIGH") in edges(store)
    store.close()


def test_instantiating_a_class_calls_its_own_constructor(repo, write):
    """`Cls()` runs `Cls.__init__`, and no source line anywhere spells that
    name -- so without an implied edge the constructor of every class in the
    repository has zero callers. #27 measured the result on psf/requests:
    `adapters.py::BaseAdapter.__init__` reported as an island of one."""
    write(
        "a.py",
        "class Thing:\n    def __init__(self):\n        self.x = 1\n\n\n"
        "def build():\n    return Thing()\n",
        commit="constructor",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("a.py::build", "a.py::Thing", "HIGH") in edges(store)
    assert ("a.py::build", "a.py::Thing.__init__", "HIGH") in edges(store)
    store.close()


def test_instantiating_a_class_calls_the_constructor_it_inherits(repo, write):
    """A subclass that defines no `__init__` runs its base's, so the edge has
    to follow the MRO rather than stopping at the class named. This is the
    shape that matters in practice: a base holding the only `__init__` and
    every subclass relying on it."""
    write(
        "a.py",
        "class Base:\n    def __init__(self, tag):\n        self.tag = tag\n\n\n"
        "class Middle(Base):\n    pass\n\n\n"
        "class Leaf(Middle):\n    def run(self):\n        return self.tag\n\n\n"
        "def build():\n    return Leaf('x')\n",
        commit="inherited constructor",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("a.py::build", "a.py::Base.__init__", "HIGH") in edges(store)
    store.close()


def test_the_nearest_constructor_in_the_mro_wins(repo, write):
    """The base's `__init__` is shadowed by the subclass's own, exactly as
    Python's attribute lookup shadows it -- one constructor edge, not both."""
    write(
        "a.py",
        "class Base:\n    def __init__(self):\n        pass\n\n\n"
        "class Child(Base):\n    def __init__(self):\n        pass\n\n\n"
        "def build():\n    return Child()\n",
        commit="shadowed constructor",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = edges(store)
    assert ("a.py::build", "a.py::Child.__init__", "HIGH") in found
    assert ("a.py::build", "a.py::Base.__init__", "HIGH") not in found
    store.close()


def test_a_class_with_no_constructor_anywhere_implies_no_edge(repo, write):
    """`Thing()` on a class that neither defines nor inherits an `__init__`
    runs `object.__init__`, which is not a repository symbol. Inventing an
    edge to some same-named `__init__` elsewhere in the tree would be the
    over-approximation bias pointing at a definition Python never reaches."""
    write(
        "a.py",
        "class Other:\n    def __init__(self):\n        pass\n\n\n"
        "class Thing:\n    pass\n\n\n"
        "def build():\n    return Thing()\n",
        commit="no constructor",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert not [dst for src, dst, _ in edges(store) if src == "a.py::build" and "__init__" in dst]
    store.close()


def test_an_ambiguous_constructor_still_reaches_the_init_it_would_run(repo, write):
    """A LOW guess at which class a bare name means stays a LOW guess about
    which `__init__` runs -- but it must still be reachable.

    `box.Widget()` is an all-LOW fan-out made entirely of classes, so since #25
    it is not materialized at all. Merging #25 with the constructor edge lost
    this link on both paths at once: deferred at index time, and missing from
    the query-time expansion, which only knew about name matches. Query-time
    expansion has to produce exactly what index time would have.

    The confidence half of the original property is pinned through `impact`,
    which is where a user actually reads it: the reported dependent must be LOW,
    never laundered into something stronger by the implied edge.
    """
    write("one.py", "class Widget:\n    def __init__(self):\n        pass\n")
    write("two.py", "class Widget:\n    def __init__(self):\n        pass\n")
    write("use.py", "def build(box):\n    return box.Widget()\n", commit="ambiguous")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")

    stored = store.connection.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE rev='HEAD' AND kind='CALLS'"
    ).fetchone()["n"]
    assert stored == 0, "the fixture stopped exercising the unmaterialized path"

    offered = set(Ambiguity(store, "HEAD").candidates("box.Widget"))
    assert offered == {
        "one.py::Widget",
        "two.py::Widget",
        "one.py::Widget.__init__",
        "two.py::Widget.__init__",
    }

    report = impact_report(store, "HEAD", "one.py::Widget.__init__", include_low=True)
    found = {(row.id, row.detail) for group in report.groups for row in group.rows}
    assert any(node_id == "use.py::build" for node_id, _ in found)
    assert all("LOW" in detail for node_id, detail in found if node_id == "use.py::build")
    store.close()
