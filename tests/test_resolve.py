from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.impact import impact_report
from codegraph.resolve import module_for_path, split_by_ambiguity_limit
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


# -- the ambiguity cap -------------------------------------------------
#
# The last-resort step matches a call's final dotted segment against every live
# definition in the revision. Measured on django (2,930 files) that produced 971
# candidates for a single call site and 2.09M LOW edges -- 96.6% of the graph --
# because the candidate list grows with the repo. See issue #6.


def unresolved_rows(store, rev="HEAD"):
    return [
        dict(row)
        for row in store.connection.execute(
            "SELECT path, line, raw_name, ref_kind, reason, candidates FROM unresolved"
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


def test_ambiguity_under_the_limit_is_still_fully_enumerated(repo, write):
    write("m.py", many_savers(3), commit="m")
    write("codegraph.toml", "ambiguity_limit = 25\n", commit="cfg")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.ambiguous == 0
    assert {dst for src, dst, _ in edges(store) if src == "m.py::persist"} == {
        f"m.py::C{i}.save" for i in range(3)
    }
    store.close()


def test_ambiguity_over_the_limit_is_recorded_once_instead_of_enumerated(repo, write):
    write("m.py", many_savers(6), commit="m")
    write("codegraph.toml", "ambiguity_limit = 4\n", commit="cfg")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")

    # Not enumerated...
    assert not [dst for src, dst, _ in edges(store) if src == "m.py::persist"]
    # ...but not dropped either: the claim survives, with its size.
    ambiguous = [row for row in unresolved_rows(store) if row["reason"] == "ambiguous"]
    assert len(ambiguous) == 1
    assert ambiguous[0]["raw_name"] == "item.save"
    assert ambiguous[0]["ref_kind"] == "call"
    assert ambiguous[0]["candidates"] == 6
    assert stats.ambiguous == 1
    store.close()


def test_ambiguous_is_counted_separately_from_unknown(repo, write):
    """They are opposite failures -- blind vs. dazzled -- and collapsing them
    into one number makes the health signal unreadable."""
    write("m.py", many_savers(6) + "\n\ndef gone():\n    no_such_name_anywhere()\n", commit="m")
    write("codegraph.toml", "ambiguity_limit = 4\n", commit="cfg")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.ambiguous == 1
    assert stats.unresolved >= 1
    reasons = {row["reason"] for row in unresolved_rows(store)}
    assert reasons == {"ambiguous", "unknown"}
    store.close()


def test_a_crowded_name_does_not_weaken_a_call_that_resolves_confidently(repo, write):
    """`AstResolver` stops at the first step that matches, so a module-local
    call never reaches the last-resort step at all -- the cap must not change
    that just because the name is crowded elsewhere in the repo."""
    write("crowd.py", many_savers(6), commit="crowd")
    write(
        "m.py",
        "def save():\n    return 0\n\n\ndef caller():\n    return save()\n",
        commit="m",
    )
    write("codegraph.toml", "ambiguity_limit = 2\n", commit="cfg")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    # module-local, so HIGH -- it never reaches the last-resort step at all
    assert ("m.py::caller", "m.py::save", "HIGH") in edges(store)
    store.close()


def test_ambiguous_base_class_is_capped_without_disturbing_the_mro(repo, write):
    """Bases fan out exactly like calls (on django they were the larger half of
    the blowup), but only HIGH links feed the MRO walk and the cap only ever
    collapses LOW ones -- so inheritance resolution is unchanged."""
    write("crowd.py", "\n\n".join(f"class Base{i}:\n    pass" for i in range(6)), commit="crowd")
    write("dup.py", "\n\n".join(f"class C{i}:\n    class Base:\n        pass" for i in range(6)),
          commit="dup")
    write(
        "child.py",
        "from crowd import Base0\n\n\nclass Child(Base0):\n    pass\n",
        commit="child",
    )
    write("codegraph.toml", "ambiguity_limit = 3\n", commit="cfg")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    inherits = {
        (row["src"], row["dst"], row["confidence"])
        for row in store.connection.execute(
            "SELECT src, dst, confidence FROM edges WHERE rev='HEAD' AND kind='INHERITS'"
        )
    }
    # The import makes this one certain, so the cap cannot touch it.
    assert ("child.py::Child", "crowd.py::Base0", "HIGH") in inherits
    store.close()


def test_ambiguity_limit_zero_disables_the_cap(repo, write):
    write("m.py", many_savers(6), commit="m")
    write("codegraph.toml", "ambiguity_limit = 0\n", commit="cfg")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.ambiguous == 0
    assert len([dst for src, dst, _ in edges(store) if src == "m.py::persist"]) == 6
    store.close()


def test_split_by_ambiguity_limit_keeps_strong_candidates_alongside_a_collapsed_tail():
    """`AstResolver` returns the first matching step's hits, so today a result is
    either confident or entirely LOW and this mix cannot arise from it. The
    `Resolver` protocol is a documented swap-in seam, though, and a smarter
    engine can return both -- the cap must collapse only the part it cannot
    distinguish, never the part it can."""
    hits = [("a.py::x", "HIGH"), ("b.py::y", "MEDIUM")] + [
        (f"c.py::z{i}", "LOW") for i in range(6)
    ]
    kept, collapsed = split_by_ambiguity_limit(hits, 4)
    assert kept == [("a.py::x", "HIGH"), ("b.py::y", "MEDIUM")]
    assert collapsed == 6


def test_split_by_ambiguity_limit_counts_only_the_weak_candidates():
    """A long list of candidates the resolver could actually tell apart is not
    ambiguity, and must not be collapsed however long it is."""
    hits = [(f"a.py::x{i}", "HIGH") for i in range(50)]
    assert split_by_ambiguity_limit(hits, 4) == (hits, 0)


def test_split_by_ambiguity_limit_leaves_a_list_at_exactly_the_limit_alone():
    hits = [(f"a.py::x{i}", "LOW") for i in range(4)]
    assert split_by_ambiguity_limit(hits, 4) == (hits, 0)


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
