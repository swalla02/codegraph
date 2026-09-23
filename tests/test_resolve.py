import pytest

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


def test_super_call_resolves_to_the_base_at_high_confidence(repo, write):
    """`super().helper()` names the enclosing class's base. That is one of the
    most certain calls Python has -- the starting class is the one the call is
    written in, and the lookup skips it -- so it resolves at HIGH.

    It used to be MEDIUM here and LOW on any real repo. The receiver is not a
    flattenable Name/Attribute chain, so the parser filed it under the
    unknown-receiver marker and it fell to the repo-wide name match: on
    psf/requests that was 26 candidates per call site, all LOW, exactly one
    right, so `impact BaseAdapter.__init__` reported `symbols: 0` by default.
    Which is the worst possible shape, since adding a required argument to a
    base `__init__` breaks every subclass.

    Supersedes the F2 regression this test used to pin (the ref must not be
    dropped): resolving it at HIGH is strictly stronger than not losing it.
    """
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
    assert ("child.py::Child.go", "base.py::Base.helper", "HIGH") in edges(store)
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
    # A call nothing can answer. This used to be `requests.get`, which is no
    # longer a gap: it is a call into a module the repository does not contain,
    # recorded as 'external' and kept out of this count like a builtin (#47).
    write("m.py", "def fetch(client):\n    client.frobnicate('u')\n", commit="m")
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

    `factory.Widget()` is an all-LOW fan-out made entirely of classes, so since #25
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
    write("use.py", "def build(factory):\n    return factory.Widget()\n", commit="ambiguous")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")

    stored = store.connection.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE rev='HEAD' AND kind='CALLS'"
    ).fetchone()["n"]
    assert stored == 0, "the fixture stopped exercising the unmaterialized path"

    offered = set(Ambiguity(store, "HEAD").candidates("factory.Widget"))
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


def test_super_skips_the_class_the_call_is_written_in(repo, write):
    """`super()` walks strictly upwards. `super().__init__()` inside
    `Child.__init__` must reach the base's `__init__`, never `Child`'s own --
    the whole point of the call is to reach the one it overrides."""
    write("base.py", "class Base:\n    def __init__(self):\n        self.x = 1\n")
    write(
        "child.py",
        "from base import Base\n\n\n"
        "class Child(Base):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n",
        commit="two",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    targets = {dst for src, dst, _ in edges(store) if src == "child.py::Child.__init__"}
    assert "base.py::Base.__init__" in targets
    assert "child.py::Child.__init__" not in targets, "super() resolved to itself"
    store.close()


def test_super_does_not_reach_a_subclass_override(repo, write):
    """The one place this differs from `self.X`. `self.area()` may run a
    subclass's override because `self` may be an instance of a subclass;
    `super().area()` exists precisely to bypass overrides, so a subclass's
    version must never be a candidate."""
    write(
        "chain.py",
        "class Base:\n    def area(self):\n        return 0\n"
        "\n\n"
        "class Mid(Base):\n"
        "    def area(self):\n"
        "        return super().area()\n"
        "\n\n"
        "class Leaf(Mid):\n    def area(self):\n        return 2\n",
        commit="chain",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    targets = {dst for src, dst, _ in edges(store) if src == "chain.py::Mid.area"}
    assert targets == {"chain.py::Base.area"}


def test_the_explicit_two_argument_super_is_not_claimed_as_certain(repo, write):
    """`super(Other, self)` names a starting class that need not be the
    enclosing one, so resolving it as though it were would be a guess dressed
    as a fact. It keeps falling through to the weak path instead."""
    write("base.py", "class Base:\n    def helper(self):\n        pass\n")
    write(
        "child.py",
        "from base import Base\n\n\n"
        "class Child(Base):\n"
        "    def go(self):\n"
        "        super(Child, self).helper()\n",
        commit="two",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = {(dst, conf) for src, dst, conf in edges(store) if src == "child.py::Child.go"}
    assert ("base.py::Base.helper", "HIGH") not in found
    store.close()


# -- #38: package re-exports ---------------------------------------------------


def test_package_reexport_resolves_to_where_the_name_is_defined(repo, write):
    """`pkg.Thing` where `pkg/__init__.py` says `from .app import Thing`.

    This is the shape of almost every public API in Python, and it used to
    resolve to nothing: `_lookup_dotted` found the module `pkg`, looked for a
    qualname `Thing` defined in `__init__.py`, did not find one, and gave up.
    The reference then fell to the repo-wide bare-name step -- LOW, and hidden
    from `impact` by default -- so the part of a framework's graph codegraph
    was least confident about was its entry points. See #38.

    HIGH, because `from .app import Thing` is an exact recorded fact about the
    source text: it is the same evidence the single-hop imported-name step has
    always claimed HIGH for, read in another file.
    """
    write("pkg/__init__.py", "from .app import Thing\n")
    write("pkg/app.py", "class Thing:\n    def run(self):\n        pass\n")
    write("use.py", "import pkg\n\n\ndef go():\n    pkg.Thing()\n", commit="reexport")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("use.py::go", "pkg/app.py::Thing", "HIGH") in edges(store)
    store.close()


def test_reexported_class_is_an_inherits_edge_at_high(repo, write):
    """The reported case verbatim: `class CustomFlask(flask.Flask)`.

    A base reference goes through the same resolver, so the fix has to show up
    as a HIGH INHERITS edge -- which matters twice over, because only HIGH
    INHERITS links feed the MRO walk that resolves `self.X` in every subclass.
    """
    write("flask/__init__.py", "from .app import Flask\n")
    write("flask/app.py", "class Flask:\n    def run(self):\n        pass\n")
    write("use.py", "import flask\n\n\nclass CustomFlask(flask.Flask):\n    pass\n", commit="base")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    rows = {
        tuple(row)
        for row in store.connection.execute(
            "SELECT src, dst, confidence FROM edges WHERE rev='HEAD' AND kind='INHERITS'"
        )
    }
    assert ("use.py::CustomFlask", "flask/app.py::Flask", "HIGH") in rows
    store.close()


def test_reexport_under_an_alias_is_followed(repo, write):
    """`from .app import Thing as Widget` binds `pkg.Widget`, and the import
    map already records the alias, so the rewrite has to go through the alias's
    target rather than through the name as written at the call site."""
    write("pkg/__init__.py", "from .app import Thing as Widget\n")
    write("pkg/app.py", "def thing():\n    pass\n\n\nclass Thing:\n    pass\n")
    write("use.py", "import pkg\n\n\ndef go():\n    pkg.Widget()\n", commit="alias")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("use.py::go", "pkg/app.py::Thing", "HIGH") in edges(store)
    store.close()


def test_a_two_hop_reexport_chain_is_followed(repo, write):
    """`pkg/__init__.py` re-exports from `pkg/middle.py`, which re-exports from
    `pkg/deep.py`. Each hop is an exact recorded import, so the conjunction is
    one too and the tier does not decay with depth."""
    write("pkg/__init__.py", "from .middle import Thing\n")
    write("pkg/middle.py", "from .deep import Thing\n")
    write("pkg/deep.py", "class Thing:\n    pass\n")
    write("use.py", "import pkg\n\n\ndef go():\n    pkg.Thing()\n", commit="chain")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("use.py::go", "pkg/deep.py::Thing", "HIGH") in edges(store)
    store.close()


def test_a_reexport_cycle_terminates(repo, write):
    """`pkg/__init__.py` imports `Thing` from `pkg.other`, which imports
    `Thing` back from `pkg`. Nothing defines it, so the honest answer is no
    edge -- the point of the test is that the walk returns at all.

    The `seen` set is what guarantees that: a cycle necessarily reproduces a
    dotted name already tried. `REEXPORT_HOPS` is a second, looser guard.
    """
    write("pkg/__init__.py", "from .other import Thing\n")
    write("pkg/other.py", "from pkg import Thing\n")
    write("use.py", "import pkg\n\n\ndef go():\n    pkg.Thing()\n", commit="cycle")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert not [edge for edge in edges(store) if edge[0] == "use.py::go"]
    store.close()


def test_a_local_definition_beats_a_reexport_of_the_same_name(repo, write):
    """`pkg/__init__.py` both imports a `Thing` and defines its own.

    The definition in the file is what a reader looking `pkg.Thing` up would
    find, so it wins -- the same rule `constructor_target` applies to a method
    declared on a class and on its base. A re-export must never shadow a real
    local definition, or the fix would trade one wrong answer for another.
    """
    write("pkg/__init__.py", "from .app import Thing\n\n\nclass Thing:\n    pass\n")
    write("pkg/app.py", "class Thing:\n    pass\n")
    write("use.py", "import pkg\n\n\ndef go():\n    pkg.Thing()\n", commit="shadow")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    targets = {dst for src, dst, _ in edges(store) if src == "use.py::go"}
    assert "pkg/__init__.py::Thing" in targets
    assert "pkg/app.py::Thing" not in targets
    store.close()


def test_a_name_the_package_does_not_reexport_is_not_invented(repo, write):
    """`pkg.Thing` where `pkg/__init__.py` neither defines nor imports `Thing`.

    Following an import is only sound because the import is written down. When
    it is not, there is nothing to follow, and the reference must keep falling
    through to the weak bare-name path exactly as it does today -- one
    plausible definition elsewhere in the repo is a MEDIUM guess, not a HIGH
    fact, and the fix must not launder it into one.
    """
    write("pkg/__init__.py", "from .app import Other\n")
    write("pkg/app.py", "class Other:\n    pass\n")
    write("elsewhere.py", "class Thing:\n    pass\n")
    write("use.py", "import pkg\n\n\ndef go():\n    pkg.Thing()\n", commit="missing")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = {(dst, conf) for src, dst, conf in edges(store) if src == "use.py::go"}
    assert ("elsewhere.py::Thing", "HIGH") not in found
    assert ("elsewhere.py::Thing", "MEDIUM") in found, "the weak path must still see it"
    store.close()


def test_a_star_import_is_not_followed(repo, write):
    """`from .app import *` is deliberately not expanded.

    Which names it binds depends on `__all__`, which is not recorded and which
    real packages compute at runtime (`__all__ = [...] + other.__all__`).
    Guessing "everything without a leading underscore" would claim HIGH for
    names that may not be exported at all, so a name reachable only through a
    star import keeps falling through to the weak path. Over-claiming here is
    worse than not handling it.
    """
    write("pkg/__init__.py", "from .app import *\n")
    write("pkg/app.py", "class Thing:\n    pass\n")
    write("use.py", "import pkg\n\n\ndef go():\n    pkg.Thing()\n", commit="star")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = {(dst, conf) for src, dst, conf in edges(store) if src == "use.py::go"}
    assert ("pkg/app.py::Thing", "HIGH") not in found
    # Not dropped either: the bare-name step still sees the one `Thing`.
    assert ("pkg/app.py::Thing", "MEDIUM") in found
    store.close()


def test_the_qualname_after_a_reexported_name_rides_along(repo, write):
    """`pkg.Thing.run()` where `pkg/__init__.py` says `from .app import Thing`.

    The rewrite has to carry the segments AFTER the re-exported name: `Thing`
    becomes `pkg.app.Thing`, and `run` is still `run` on whatever `Thing`
    turned out to be. Dropping the tail resolves the call to the class instead
    of to the method on it -- a confidently wrong edge, which is worse than the
    LOW guess it replaces. (An earlier version of this test used
    `pkg.helpers.build()` through a re-exported submodule and was VACUOUS: the
    ordinary longest-module-prefix lookup answers that one before the re-export
    step ever runs, so dropping the tail did not fail it.)
    """
    write("pkg/__init__.py", "from .app import Thing\n")
    write("pkg/app.py", "class Thing:\n    @staticmethod\n    def run():\n        pass\n")
    write("use.py", "import pkg\n\n\ndef go():\n    pkg.Thing.run()\n", commit="tail")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    targets = {(dst, conf) for src, dst, conf in edges(store) if src == "use.py::go"}
    assert ("pkg/app.py::Thing.run", "HIGH") in targets
    assert not any(dst == "pkg/app.py::Thing" for dst, _ in targets), "the tail was dropped"
    store.close()


def test_from_package_import_name_also_follows_the_reexport(repo, write):
    """`from pkg import Thing` is the other half of the same gap, and by volume
    the bigger one: the alias map records the target as `pkg.Thing`, which is
    the identical dotted name `pkg.Thing` at a call site produces, so both
    forms were unresolvable for the same reason and both are fixed by the same
    step."""
    write("pkg/__init__.py", "from .app import Thing\n")
    write("pkg/app.py", "class Thing:\n    pass\n")
    write("use.py", "from pkg import Thing\n\n\ndef go():\n    Thing()\n", commit="fromimport")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("use.py::go", "pkg/app.py::Thing", "HIGH") in edges(store)
    store.close()


# -- the external boundary ---------------------------------------------------
#
# `import pytest` names a module this repository does not contain. A call on it
# still reached the last-resort name match, which projected `pytest.main` onto
# every repo function called `main`: `codegraph.tracer`'s own `pytest.main(...)`
# call reported three candidates, none of them right. See #47.


def repo_calling_an_external_module(write, call="pytest.main([])", header="import pytest"):
    write("cli.py", "def main():\n    pass\n")
    write("bench.py", "def main():\n    pass\n")
    write("run.py", f"{header}\n\n\ndef go():\n    {call}\n", commit="external")


def test_a_call_on_an_external_module_is_not_projected_onto_repo_symbols(repo, write):
    repo_calling_an_external_module(write)
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert not [dst for src, dst, _ in edges(store) if src == "run.py::go"]
    rows = [row for row in unresolved_rows(store) if row["path"] == "run.py"]
    assert [(row["raw_name"], row["reason"], row["candidates"]) for row in rows] == [
        ("pytest.main", "external", 0)
    ]
    # Understood and deliberately unlinked: neither a fan-out nor a gap.
    assert stats.ambiguous == 0
    assert stats.unresolved == 0
    store.close()


def test_an_external_call_with_one_same_named_repo_symbol_gets_no_edge(repo, write):
    """The single-candidate case was the worse half: one repo `dumps` turned
    `json.dumps(x)` into a MEDIUM edge -- confidently wrong, not a LOW guess."""
    write("codec.py", "def dumps(value):\n    return value\n")
    write("use.py", "import json\n\n\ndef go(x):\n    return json.dumps(x)\n", commit="json")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert not [dst for src, dst, _ in edges(store) if src == "use.py::go"]
    reasons = {row["reason"] for row in unresolved_rows(store) if row["path"] == "use.py"}
    assert reasons == {"external"}
    store.close()


def test_a_name_imported_from_an_external_module_is_external_too(repo, write):
    repo_calling_an_external_module(
        write, call="run_tests()", header="from pytest import main as run_tests"
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    rows = [row for row in unresolved_rows(store) if row["path"] == "run.py"]
    assert [(row["raw_name"], row["reason"]) for row in rows] == [("run_tests", "external")]
    store.close()


def test_a_missing_attribute_of_a_repo_module_is_not_external(repo, write):
    """The boundary is the repository, not "the lookup failed". `pkg` is a repo
    package, so a `pkg.main` the index cannot find may still be bound at
    runtime; it keeps the name match it had rather than being written off."""
    write("pkg/__init__.py", "")
    write("cli.py", "def main():\n    pass\n")
    write("run.py", "import pkg\n\n\ndef go():\n    pkg.main()\n", commit="internal")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("run.py::go", "cli.py::main", "MEDIUM") in edges(store)
    store.close()


def test_a_module_reached_through_sys_path_is_not_external(repo, write):
    """django's test runner puts `tests/` on `sys.path`, so its test apps import
    each other as `from model_fields.models import Foo` -- a top-level name that
    `module_for_path` spells `tests.model_fields.models`. A head that names ANY
    directory or file in the repository is not provably someone else's code,
    so it keeps falling through exactly as before."""
    write("tests/model_fields/__init__.py", "")
    write("tests/model_fields/models.py", "def build():\n    pass\n")
    write(
        "tests/other/use.py",
        "import model_fields.models\n\n\ndef go():\n    model_fields.models.build()\n",
        commit="syspath",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = {dst for src, dst, _ in edges(store) if src == "tests/other/use.py::go"}
    assert found == {"tests/model_fields/models.py::build"}
    store.close()


def test_a_parameter_named_like_an_external_module_is_not_external(repo, write):
    """`def go(json): json.dumps()` -- the parameter shadows the import for the
    whole function body, so the call is on whatever was passed, not on the
    standard library."""
    write("codec.py", "def dumps(value):\n    return value\n")
    write("use.py", "import json\n\n\ndef go(json):\n    return json.dumps(1)\n", commit="shadow")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("use.py::go", "codec.py::dumps", "MEDIUM") in edges(store)
    store.close()


# -- receiver types (#47) ------------------------------------------------------
#
# Everything above resolves a NAME. A call written on a variable -- most method
# calls in most Python -- used to reach the bare-name match with nothing but its
# final segment, even when the variable's type was written a few lines up.


def targets_of(store, src, rev="HEAD"):
    return {(dst, conf) for s, dst, conf in edges(store) if s == src}


def ambiguous_names(store, rev="HEAD"):
    return {
        row["raw_name"]
        for row in store.connection.execute(
            "SELECT raw_name FROM unresolved WHERE rev=? AND reason='ambiguous'", (rev,)
        )
    }


def two_savers(write):
    write(
        "models.py",
        "class Item:\n    def save(self):\n        pass\n\n\n"
        "class Settings:\n    def save(self):\n        pass\n",
    )


def test_an_annotated_parameter_resolves_its_method_at_high(repo, write):
    write("catalog.py", "class Catalog:\n    def fingerprint(self):\n        return 1\n")
    write("other.py", "def fingerprint():\n    return 2\n")
    write(
        "use.py",
        "from catalog import Catalog\n\n\n"
        "def digest(catalog: Catalog):\n    return catalog.fingerprint()\n",
        commit="annotated",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert targets_of(store, "use.py::digest") == {("catalog.py::Catalog.fingerprint", "HIGH")}
    assert "catalog.fingerprint" not in ambiguous_names(store)
    store.close()


def test_an_annotation_also_reaches_subclass_overrides_at_medium(repo, write):
    """`catalog: Catalog` may be passed a subclass, exactly as `self` may be one
    -- the reasoning, and the tiers, are `_through_self`'s (#14)."""
    two_savers(write)
    write("special.py", "from models import Item\n\n\nclass Special(Item):\n    def save(self):\n        pass\n")
    write(
        "use.py",
        "from models import Item\n\n\ndef persist(item: Item):\n    return item.save()\n",
        commit="override",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert targets_of(store, "use.py::persist") == {
        ("models.py::Item.save", "HIGH"),
        ("special.py::Special.save", "MEDIUM"),
    }
    store.close()


def test_a_local_constructed_in_the_body_resolves_its_method(repo, write):
    two_savers(write)
    write("special.py", "from models import Item\n\n\nclass Special(Item):\n    def save(self):\n        pass\n")
    write(
        "use.py",
        "from models import Item\n\n\ndef build():\n    item = Item()\n    item.save()\n",
        commit="constructed",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    # `Item()` names the exact class, so -- unlike an annotation -- a subclass
    # override is not a candidate. Same rule as `constructor_target`.
    assert targets_of(store, "use.py::build") == {
        ("models.py::Item", "HIGH"),
        ("models.py::Item.save", "HIGH"),
    }
    store.close()


def test_a_reassigned_receiver_is_medium(repo, write):
    two_savers(write)
    write(
        "use.py",
        "from models import Item, Settings\n\n\n"
        "def build(flag):\n    target = Item()\n    if flag:\n        target = Settings()\n"
        "    target.save()\n",
        commit="reassigned",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = targets_of(store, "use.py::build")
    assert ("models.py::Item.save", "MEDIUM") in found
    assert ("models.py::Settings.save", "MEDIUM") in found
    store.close()


@pytest.mark.parametrize(
    "label, body",
    [
        ("rebound by a loop", "    for item in rows:\n        pass\n    item = Item()\n"),
        ("an unannotated parameter", ""),
        ("assigned from a function call", "    item = load()\n"),
        ("assigned from a subscript", "    item = rows[0]\n"),
    ],
)
def test_a_receiver_whose_bindings_are_not_all_known_falls_through(repo, write, label, body):
    """One binding the resolver cannot type makes every other one untrustworthy:
    claiming `Item.save` at HIGH while `item` is also a loop variable would be
    narrower than today's fan-out AND wrong. So the reference keeps exactly the
    answer it had."""
    two_savers(write)
    signature = "item, rows" if label == "an unannotated parameter" else "rows"
    write(
        "use.py",
        "from models import Item\n\n\ndef load():\n    return None\n\n\n"
        f"def build({signature}):\n{body}    item.save()\n",
        commit="opaque",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert "item.save" in ambiguous_names(store), label
    store.close()


def test_self_attribute_assigned_from_an_annotated_init_parameter(repo, write):
    two_savers(write)
    write(
        "service.py",
        "from models import Item\n\n\n"
        "class Service:\n"
        "    def __init__(self, item: Item):\n        self.item = item\n\n"
        "    def run(self):\n        return self.item.save()\n",
        commit="attribute",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert targets_of(store, "service.py::Service.run") == {("models.py::Item.save", "HIGH")}
    store.close()


def test_a_class_defined_inside_the_function_is_found(repo, write):
    two_savers(write)
    write(
        "use.py",
        "def check():\n"
        "    class Local:\n        def save(self):\n            pass\n"
        "    thing = Local()\n    thing.save()\n",
        commit="local-class",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("use.py::check.<locals>.Local.save", "HIGH") in targets_of(store, "use.py::check")
    assert not any("models.py" in dst for dst, _ in targets_of(store, "use.py::check"))
    store.close()


def test_a_module_level_instance_is_seen_from_a_function(repo, write):
    two_savers(write)
    write(
        "app.py",
        "from models import Item\n\nitem = Item()\n\n\ndef persist():\n    item.save()\n",
        commit="global",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert targets_of(store, "app.py::persist") == {("models.py::Item.save", "HIGH")}
    store.close()


# What the attribute holds (#54). Resolving the receiver says which class the
# method is looked up on; it does not say that the class attribute of that
# name still holds the function written under it. A decorator is free to
# return something else, and then the call enters that something else.
#
# This is flask's `@setupmethod`, which is how the regression #54 records was
# produced: 148 of the 149 wrong HIGH claims #50 added there were calls to a
# `@setupmethod`-wrapped Flask method through `app = Flask(__name__)` or an
# annotated `Blueprint`.

WRAPPED_APP = (
    "import functools\n\n\n"
    "def setupmethod(f):\n"
    "    @functools.wraps(f)\n"
    "    def wrapper_func(self, *args, **kwargs):\n"
    "        return f(self, *args, **kwargs)\n\n"
    "    return wrapper_func\n\n\n"
    "class App:\n"
    "    @setupmethod\n"
    "    def route(self, rule):\n        return rule\n\n"
    "    def run(self):\n        return 1\n"
)


@pytest.mark.parametrize(
    "receiver",
    [
        ("constructed", "def make():\n    app = App()\n    return app.route('/')\n"),
        ("annotated", "def make(app: App):\n    return app.route('/')\n"),
    ],
    ids=lambda case: case[0],
)
def test_a_wrapped_target_reached_through_a_receiver_is_medium(repo, write, receiver):
    """`App.route` is decorated, so `app.route` is the decorator's return value.

    The candidate is right -- `route`'s body is where a reader goes and where
    an edit lands -- but "this call site runs that definition" is a claim the
    resolver cannot make without reading the decorator, so MEDIUM is the tier.
    Nothing is dropped: the edge is still there, and `impact` still shows it.
    """
    write("app.py", WRAPPED_APP)
    write("use.py", f"from app import App\n\n\n{receiver[1]}", commit="wrapped")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = targets_of(store, "use.py::make")
    assert ("app.py::App.route", "MEDIUM") in found
    assert ("app.py::App.route", "HIGH") not in found
    assert "app.route" not in ambiguous_names(store)
    store.close()


def test_an_undecorated_sibling_of_a_wrapped_target_stays_high(repo, write):
    """The weakening is per definition, not per class: `App.run` carries no
    decorator, so the same receiver still reaches it at HIGH."""
    write("app.py", WRAPPED_APP)
    write(
        "use.py",
        "from app import App\n\n\ndef make():\n    app = App()\n    return app.run()\n",
        commit="undecorated",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("app.py::App.run", "HIGH") in targets_of(store, "use.py::make")
    store.close()


@pytest.mark.parametrize(
    "imports, decorator",
    [
        ("", "staticmethod"),
        ("", "classmethod"),
        ("import abc\n\n\n", "abc.abstractmethod"),
        ("import typing as t\n\n\n", "t.final"),
    ],
)
def test_a_decorator_that_replaces_nothing_leaves_the_claim_high(repo, write, imports, decorator):
    """Not every decorator wraps. These four are the language's own markers and
    descriptors: each leaves the same body as what `x.build(...)` invokes, so
    weakening the claim would cost a certain answer for nothing."""
    write(
        "app.py",
        f"{imports}class Registry:\n    @{decorator}\n    def build(x):\n        return x\n",
    )
    write(
        "use.py",
        "from app import Registry\n\n\n"
        "def make():\n    registry = Registry()\n    return registry.build(1)\n",
        commit="descriptor",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("app.py::Registry.build", "HIGH") in targets_of(store, "use.py::make")
    store.close()


# The same question one step up the ladder (#64). `self.X` and `super().X`
# reach a declaration by an attribute lookup on a class the source text names
# exactly, which is better evidence than a receiver's type -- but it is better
# evidence about WHICH CLASS, and the decorator takes away the other half of
# the claim. Measured on flask, this step's HIGH claims on a decorated target
# were 0 right and 27 wrong, every one of them `@setupmethod` again.

WRAPPED_METHODS = (
    "import functools\n\n\n"
    "def setupmethod(f):\n"
    "    @functools.wraps(f)\n"
    "    def wrapper_func(self, *args, **kwargs):\n"
    "        return f(self, *args, **kwargs)\n\n"
    "    return wrapper_func\n\n\n"
    "class Scaffold:\n"
    "    @setupmethod\n"
    "    def add_url_rule(self, rule):\n        return rule\n\n"
    "    def make_config(self):\n        return {}\n\n"
    "    def route(self, rule):\n"
    "        return self.add_url_rule(rule)\n\n"
    "    def configure(self):\n"
    "        return self.make_config()\n"
)


def test_a_self_call_to_a_wrapped_method_is_medium(repo, write):
    """`self.add_url_rule` is whatever `setupmethod` returned, so the frame
    that opens is the wrapper and never the decorated body. The candidate is
    still right about where an edit lands, so it stays -- one tier down."""
    write("app.py", WRAPPED_METHODS, commit="wrapped")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = targets_of(store, "app.py::Scaffold.route")
    assert ("app.py::Scaffold.add_url_rule", "MEDIUM") in found
    assert ("app.py::Scaffold.add_url_rule", "HIGH") not in found
    assert "self.add_url_rule" not in ambiguous_names(store)
    store.close()


def test_a_self_call_to_an_undecorated_method_stays_high(repo, write):
    """The weakening is per declaration, not per class: the sibling that
    carries no decorator is still reached at HIGH from the same `self`."""
    write("app.py", WRAPPED_METHODS, commit="wrapped")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("app.py::Scaffold.make_config", "HIGH") in targets_of(
        store, "app.py::Scaffold.configure"
    )
    store.close()


def test_a_wrapped_method_inherited_from_a_base_is_medium(repo, write):
    """flask's own shape: the call is in `Blueprint`, the `@setupmethod` is on
    `Scaffold`. The MRO walk is what finds it, and what the walk cannot see is
    the same thing whichever class the declaration turns up on."""
    write("app.py", WRAPPED_METHODS)
    write(
        "blueprint.py",
        "from app import Scaffold\n\n\n"
        "class Blueprint(Scaffold):\n"
        "    def register(self, rule):\n"
        "        return self.add_url_rule(rule)\n",
        commit="inherited",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = targets_of(store, "blueprint.py::Blueprint.register")
    assert ("app.py::Scaffold.add_url_rule", "MEDIUM") in found
    assert ("app.py::Scaffold.add_url_rule", "HIGH") not in found
    store.close()


def test_a_wrapped_override_reached_through_self_is_still_a_candidate(repo, write):
    """An override is MEDIUM already -- "runs depending on the instance" --
    and a decorator on it does not make it less of a candidate than that.
    Nothing is dropped and nothing falls to LOW: both tiers say the same
    thing, that the body is where the edit lands and the frame is not
    promised."""
    write("app.py", WRAPPED_METHODS)
    write(
        "blueprint.py",
        "from app import Scaffold, setupmethod\n\n\n"
        "class Blueprint(Scaffold):\n"
        "    @setupmethod\n"
        "    def make_config(self):\n        return {'nested': True}\n",
        commit="override",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("blueprint.py::Blueprint.make_config", "MEDIUM") in targets_of(
        store, "app.py::Scaffold.configure"
    )
    store.close()


@pytest.mark.parametrize(
    "imports, decorator",
    [
        ("", "staticmethod"),
        ("import abc\n\n\n", "abc.abstractmethod"),
        ("import typing as t\n\n\n", "t.final"),
    ],
)
def test_a_self_call_through_a_transparent_decorator_stays_high(repo, write, imports, decorator):
    """The language's own markers and descriptors leave the decorated body as
    what the attribute invokes, so `self.build()` still certainly runs it."""
    write(
        "app.py",
        f"{imports}class Registry:\n"
        f"    @{decorator}\n"
        "    def build(x):\n        return x\n\n"
        "    def run(self):\n        return self.build(1)\n",
        commit="transparent",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("app.py::Registry.build", "HIGH") in targets_of(store, "app.py::Registry.run")
    store.close()


def test_a_super_call_to_a_wrapped_method_is_medium(repo, write):
    """`super().X()` is the same attribute lookup with a different starting
    point, so the same decorator stands between the call and the body. The
    benchmark repositories have no instance of this -- the rule is here
    because the mechanism is identical, not because a number moved."""
    write("app.py", WRAPPED_METHODS)
    write(
        "blueprint.py",
        "from app import Scaffold\n\n\n"
        "class Blueprint(Scaffold):\n"
        "    def add_url_rule(self, rule):\n"
        "        return super().add_url_rule(rule)\n",
        commit="super",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = targets_of(store, "blueprint.py::Blueprint.add_url_rule")
    assert ("app.py::Scaffold.add_url_rule", "MEDIUM") in found
    assert ("app.py::Scaffold.add_url_rule", "HIGH") not in found
    store.close()


def test_a_super_call_to_an_undecorated_method_stays_high(repo, write):
    """`super().X()` remains one of the most certain calls Python has when
    nothing is wrapping the declaration it reaches."""
    write("app.py", WRAPPED_METHODS)
    write(
        "blueprint.py",
        "from app import Scaffold\n\n\n"
        "class Blueprint(Scaffold):\n"
        "    def make_config(self):\n"
        "        return super().make_config()\n",
        commit="super plain",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("app.py::Scaffold.make_config", "HIGH") in targets_of(
        store, "blueprint.py::Blueprint.make_config"
    )
    store.close()


def test_a_mention_of_a_wrapped_method_is_still_high(repo, write):
    """A `REFERENCES` edge is not weakened, because its tier answers a
    different question. `self.add_url_rule` handed to `register(...)` names
    that definition as certainly as any name does; whether anything then
    invokes it is what the edge KIND says, and no frame is being claimed."""
    write(
        "app.py",
        WRAPPED_METHODS + "\n    def install(self):\n        return register(self.add_url_rule)\n",
        commit="mention",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert (
        "app.py::Scaffold.install",
        "app.py::Scaffold.add_url_rule",
        "HIGH",
    ) in references(store)
    store.close()


# The Protocol question. A `typing.Protocol` is satisfied structurally: its
# implementations need not subclass it, so the hierarchy has no link from the
# Protocol to the code that runs. Binding a call to the stub alone would repeat
# #14 -- narrower, more confident, more wrong than the fan-out it replaced.

TREE_SOURCES = (
    "from typing import Protocol\n\n\n"
    "class TreeSource(Protocol{generic}):\n"
    "    def tree(self, rev): ...\n\n"
    "    def read(self, shas): ...\n\n\n"
    "class GitTreeSource:\n"
    "    def tree(self, rev):\n        return {{}}\n\n"
    "    def read(self, shas):\n        return []\n\n\n"
    "class FsTreeSource:\n"
    "    def tree(self, rev):\n        return {{}}\n\n"
    "    def read(self, shas):\n        return []\n\n\n"
    "class Unrelated:\n"
    "    def read(self):\n        return b''\n"
)

INDEXER = (
    "from source import TreeSource\n\n\n"
    "class Indexer:\n"
    "    def __init__(self, source: TreeSource):\n        self.source = source\n\n"
    "    def reconcile(self):\n        return self.source.read([])\n"
)


@pytest.mark.parametrize("generic", ["", "[T]"])
def test_a_protocol_receiver_reaches_its_structural_implementers(repo, write, generic):
    write("source.py", TREE_SOURCES.format(generic=generic))
    write("indexer.py", INDEXER, commit="protocol")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = targets_of(store, "indexer.py::Indexer.reconcile")
    assert ("source.py::TreeSource.read", "HIGH") in found
    assert ("source.py::GitTreeSource.read", "MEDIUM") in found
    assert ("source.py::FsTreeSource.read", "MEDIUM") in found
    store.close()


def test_a_protocol_receiver_never_loses_a_candidate_the_fan_out_had(repo, write):
    """The floor, by construction. `Unrelated.read` does not implement the
    Protocol, and today's LOW fan-out still contains it; the receiver step
    keeps it, at LOW, rather than deciding it away. Structural matching is an
    approximation -- an implementer can inherit a method from a class outside
    the repository, or satisfy a member with an attribute set in `__init__` --
    and over-approximation is this resolver's bias."""
    write("source.py", TREE_SOURCES.format(generic=""))
    write("indexer.py", INDEXER, commit="protocol")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    today = set(Ambiguity(store, "HEAD").candidates("self.source.read"))
    found = targets_of(store, "indexer.py::Indexer.reconcile")
    assert today <= {dst for dst, _ in found}
    assert ("source.py::Unrelated.read", "LOW") in found
    store.close()


def test_a_union_with_a_protocol_and_a_reassignment_is_medium(repo, write):
    """`resolver: Resolver | None = None` then `resolver = resolver or
    AstResolver()` -- `resolve.py`'s own shape, and two of this repository's
    twelve ambiguous references."""
    write(
        "resolver.py",
        "from typing import Protocol\n\n\n"
        "class Resolver(Protocol):\n    def resolve_call(self, ref): ...\n\n\n"
        "class AstResolver:\n    def resolve_call(self, ref):\n        return []\n",
    )
    write(
        "run.py",
        "from resolver import AstResolver, Resolver\n\n\n"
        "def run(resolver: Resolver | None = None):\n"
        "    resolver = resolver or AstResolver()\n"
        "    return resolver.resolve_call(1)\n",
        commit="union",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = targets_of(store, "run.py::run")
    assert ("resolver.py::Resolver.resolve_call", "MEDIUM") in found
    assert ("resolver.py::AstResolver.resolve_call", "MEDIUM") in found
    assert not [conf for _, conf in found if conf == "HIGH" and _ != "resolver.py::AstResolver"]
    store.close()


# Value references (#45). A name used as a value -- handed to a library,
# listed in a dispatch table, passed as a callback -- is a real relationship
# between two definitions, and the graph held nothing for it at all.


def references(store, rev="HEAD"):
    return {
        (row["src"], row["dst"], row["confidence"])
        for row in store.connection.execute(
            "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='REFERENCES'", (rev,)
        )
    }


def test_a_class_handed_to_a_library_gets_a_reference_edge(repo, write):
    """`store.py`'s own shape: `connection.row_factory = _Row` is the only
    mention of `_Row` in this repository, and sqlite is what calls it."""
    write(
        "store.py",
        "class _Row:\n    pass\n\n\ndef open_store(connection):\n"
        "    connection.row_factory = _Row\n    return connection\n",
        commit="row factory",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("store.py::open_store", "store.py::_Row", "HIGH") in references(store)
    store.close()


def test_a_method_named_in_a_dispatch_table_gets_a_reference_edge(repo, write):
    """`resolve.py`'s step table, which is why `AstResolver._module_local`
    was an island of one in this repository's own graph."""
    write(
        "resolver.py",
        "class AstResolver:\n"
        "    def resolve_call(self, ref):\n"
        "        for step in (self._module_local,):\n"
        "            step(ref)\n\n"
        "    def _module_local(self, ref):\n"
        "        return []\n",
        commit="steps",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert (
        "resolver.py::AstResolver.resolve_call",
        "resolver.py::AstResolver._module_local",
        "HIGH",
    ) in references(store)
    store.close()


def test_a_mention_never_takes_the_bare_name_fan_out(repo, write):
    """The flood guard. A call whose name matches many definitions is
    deferred to `ambiguity` and expanded on demand; a *mention* does not even
    get that far. `self.handler` is an attribute read, and matching it against
    every definition named `handler` in the repository would connect regions
    that have nothing to do with each other -- in the one report whose whole
    value is that the regions it prints are really apart."""
    write("a.py", "def handler():\n    pass\n")
    write(
        "b.py",
        "def handler():\n    pass\n\n\nclass Runner:\n"
        "    def run(self):\n        return register(self.handler)\n",
        commit="fan-out",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert not [edge for edge in references(store) if edge[0] == "b.py::Runner.run"]
    assert "self.handler" not in ambiguous_names(store)
    store.close()


def test_an_unresolved_mention_is_not_counted_as_a_gap(repo, write):
    """`unresolved` is a health signal about the CALL graph -- "this many
    calls found no callee". A name read as a value that turns out to name
    nothing in the repository (a builtin, a local attribute, a library
    symbol) is not a gap in it, and counting one would bury the real gaps."""
    write("m.py", "def run(connection):\n    connection.row_factory = dict\n", commit="builtin")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.unresolved == 0
    store.close()


# Protocol implementation (#45). A class that structurally satisfies a
# Protocol has no textual link to it: no import, no base, no call. The
# receiver step has computed exactly this relationship since #50 and threw it
# away; this writes it down.


def implementations(store, rev="HEAD"):
    return {
        (row["src"], row["dst"], row["confidence"])
        for row in store.connection.execute(
            "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='IMPLEMENTS'", (rev,)
        )
    }


def test_a_structural_implementer_gets_an_implements_edge(repo, write):
    """`GitTreeSource` and `FsTreeSource` define every method `TreeSource`
    declares, so each of them is what an annotation naming the Protocol
    reaches at runtime. `Unrelated` defines `read` alone and is not."""
    write("source.py", TREE_SOURCES.format(generic=""), commit="protocol")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = implementations(store)
    assert ("source.py::GitTreeSource", "source.py::TreeSource", "MEDIUM") in found
    assert ("source.py::FsTreeSource", "source.py::TreeSource", "MEDIUM") in found
    assert not [edge for edge in found if edge[0] == "source.py::Unrelated"]
    store.close()


def test_a_nominal_subclass_of_a_protocol_gets_no_implements_edge(repo, write):
    """It already has an INHERITS edge saying the same thing, more strongly.
    A second edge would double the subclass's fan-in for one declaration."""
    write(
        "source.py",
        "from typing import Protocol\n\n\n"
        "class TreeSource(Protocol):\n    def tree(self, rev): ...\n\n\n"
        "class GitTreeSource(TreeSource):\n    def tree(self, rev):\n        return {}\n",
        commit="nominal",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert implementations(store) == set()
    store.close()
