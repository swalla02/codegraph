# tests/test_viz.py
"""The data-shaping half of the view, which is the half a test can see.

`viz/model.py` turns stored rows into one JSON-ready value and `render.py`
inlines that value into a page. Everything asserted here is on the first
side of that seam: the layout, the islands, the edge collapse, the tiers,
the fan-out index, the highlight. What the second side produces is only
correct when a browser draws it, so the two tests that touch `render.py`
check the one property a browser is not needed for -- that the file is
self-contained, and that its data survives a round trip.

The invariant this file exists to protect is #60's: four edge kinds and
three confidence tiers that arrive at the drawing indistinguishable would
be a step backwards from the text the picture replaces. Several tests
below are that sentence, made checkable.
"""

from __future__ import annotations

import gzip
import json
import re
from base64 import b64decode

import pytest

from codegraph import trace as trace_module
from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.islands import islands_report
from codegraph.resolve import CONFIDENCE_RANK, DEPENDENCY_KINDS
from codegraph.store import Store
from codegraph.viz.model import (
    DIRECTORY,
    EXTENT,
    FILE,
    SYMBOL,
    TIERS,
    build_view,
    highlight_from_report,
)
from codegraph.viz.render import render_html

#: Two modules with one of every dependency kind between them, plus a
#: symbol nothing in the tree reaches -- so the fixture exercises the four
#: kinds, the nesting of methods under their class, and an island of one.
LIB = """\
import typing


class Base:
    def run(self):
        return 1


class Protocolish(typing.Protocol):
    def ping(self) -> int: ...


def helper():
    return 2


def unreached_by_anything():
    return 3
"""

APP = """\
from lib import Base, Protocolish, helper


class Child(Base):
    def run(self):
        return helper()


class Pinger:
    def ping(self) -> int:
        return 4


def entry():
    return Child().run()


MENTION = helper
"""


@pytest.fixture
def two_module_repo(repo, write):
    write("lib.py", LIB)
    write("app.py", APP, commit="two modules")
    return repo


def workspace(root):
    store = Store.open(root)
    return store, Indexer(root, store, GitTreeSource(root))


def built(root, **kwargs):
    store, indexer = workspace(root)
    indexer.reconcile("HEAD")
    view = build_view(store, "HEAD", repo=root.name, config=indexer.config, **kwargs)
    return store, indexer, view


def boxes_by_name(view, name):
    return [box for box in view.boxes if box.name == name]


# -- the layout --------------------------------------------------------------


def test_every_box_is_inside_its_parent(two_module_repo):
    """Containment is the only thing a treemap says, so it has to hold.

    A child escaping its parent would put a symbol visually inside a file
    it is not in, which is worse than no picture: the reader would have no
    way to notice.
    """
    store, _, view = built(two_module_repo)
    stack = [view.root]
    while stack:
        box = stack.pop()
        px, py, pw, ph = box.rect
        for child in box.children:
            cx, cy, cw, ch = child.rect
            assert cx >= px - 1e-9 and cy >= py - 1e-9
            assert cx + cw <= px + pw + 1e-9
            assert cy + ch <= py + ph + 1e-9
            stack.append(child)
    store.close()


def test_siblings_do_not_overlap(two_module_repo):
    store, _, view = built(two_module_repo)
    stack = [view.root]
    while stack:
        box = stack.pop()
        rects = [child.rect for child in box.children]
        for index, (ax, ay, aw, ah) in enumerate(rects):
            for bx, by, bw, bh in rects[index + 1 :]:
                apart = (
                    ax + aw <= bx + 1e-9
                    or bx + bw <= ax + 1e-9
                    or ay + ah <= by + 1e-9
                    or by + bh <= ay + 1e-9
                )
                assert apart, f"{box.name}'s children overlap"
        stack.extend(box.children)
    store.close()


def test_the_layout_is_the_same_on_every_run(two_module_repo):
    """A picture that moves between runs cannot be compared with itself.

    The layout is computed here rather than in the browser precisely so
    that it is a property of the revision; if the ordering ever became
    dependent on dict or SQLite iteration order, two builds of one commit
    would draw two different repositories.
    """
    store, indexer, first = built(two_module_repo)
    second = build_view(store, "HEAD", repo="x", config=indexer.config)
    assert first.pack()["box"]["rect"] == second.pack()["box"]["rect"]
    assert first.pack()["box"]["name"] == second.pack()["box"]["name"]
    store.close()


def test_packed_rectangles_are_integers_in_the_declared_extent(two_module_repo):
    store, _, view = built(two_module_repo)
    packed = view.pack()
    assert packed["extent"][0] == EXTENT
    for x, y, w, h in packed["box"]["rect"]:
        assert all(isinstance(value, int) for value in (x, y, w, h))
        assert 0 <= x <= EXTENT and 0 <= y <= EXTENT
    store.close()


# -- what a box is -----------------------------------------------------------


def test_a_file_box_is_that_file_s_module_node(two_module_repo):
    """The synthetic `path::<module>` node is drawn as the file itself.

    It is the node a module-scope call is recorded against, so an edge
    from a file's top level has to land on the file's rectangle. Drawing
    it as one more symbol inside the file would put that edge on a box
    nobody can find, and leaving it out would drop the edge.
    """
    store, _, view = built(two_module_repo)
    (app,) = [box for box in view.boxes if box.type == FILE and box.name == "app.py"]
    assert app.node_id == "app.py::<module>"
    assert app.kind == "module"
    store.close()


def test_methods_are_nested_inside_their_class(two_module_repo):
    store, _, view = built(two_module_repo)
    (child,) = boxes_by_name(view, "Child")
    assert child.kind == "class"
    assert [box.name for box in child.children] == ["run"]
    store.close()


def test_a_directory_holding_only_one_directory_is_folded_into_it(repo, write):
    """Three nested rectangles with nothing beside them cost three gutters
    and three labels to say one path."""
    write("a/b/c/m.py", "def f():\n    return 1\n", commit="deep")
    store, _, view = built(repo)
    names = {box.name for box in view.boxes if box.type == DIRECTORY}
    assert "a/b/c" in names
    assert "a" not in names and "b" not in names
    store.close()


def test_every_symbol_has_a_box_and_every_box_has_a_rectangle(two_module_repo):
    store, _, view = built(two_module_repo)
    symbols = {box.node_id for box in view.boxes if box.type == SYMBOL}
    rows = {
        row["id"]
        for row in store.connection.execute(
            "SELECT id FROM nodes WHERE rev='HEAD' AND kind<>'module'"
        )
    }
    assert symbols == rows
    assert all(box.rect[2] >= 0 and box.rect[3] >= 0 for box in view.boxes)
    store.close()


# -- kinds, tiers, provenance ------------------------------------------------


def test_the_tier_order_comes_from_the_resolver(two_module_repo):
    """`TIERS` is derived, not written out. A legend that called LOW the
    strong one would be the exact failure #37 is about, and a literal list
    is how that happens."""
    assert list(TIERS) == sorted(CONFIDENCE_RANK, key=lambda tier: -CONFIDENCE_RANK[tier])
    assert TIERS[0] == "HIGH"


def test_all_four_kinds_survive_into_the_payload(two_module_repo):
    """The fixture has one of each, and the packed edges must still be
    able to tell them apart."""
    store, _, view = built(two_module_repo)
    kinds = {DEPENDENCY_KINDS[edge[2]] for edge in view.edges}
    assert kinds == set(DEPENDENCY_KINDS), f"missing: {set(DEPENDENCY_KINDS) - kinds}"
    store.close()


def test_duplicate_edge_rows_collapse_to_one_line_at_the_strongest_tier(repo, write):
    """One relationship written twice is one line, not two.

    Edge width says how much depends on a box. If the same call written
    twice in a body counted twice, width would be reporting how often
    somebody typed a call.
    """
    write(
        "m.py",
        "def target():\n    return 1\n\n\ndef caller():\n    target()\n    return target()\n",
        commit="twice",
    )
    store, _, view = built(repo)
    lines = [
        edge
        for edge in view.edges
        if view.boxes[edge[0]].name == "caller" and view.boxes[edge[1]].name == "target"
    ]
    assert len(lines) == 1
    assert TIERS[lines[0][3]] == "HIGH"
    store.close()


def test_an_observed_edge_keeps_its_static_tier_and_gains_the_bit(repo, write):
    """Provenance is the other axis, never a stronger tier.

    A run being watched taking an edge does not make the resolver more
    certain the name meant that symbol; it is a different claim, and the
    drawing has to carry both or it flattens the two things #56 exists to
    keep apart.
    """
    write(
        "m.py",
        "class H:\n    def handle(self):\n        return 1\n\n\n"
        "def dispatch(name):\n    return getattr(H(), name)()\n",
        commit="dynamic",
    )
    store, indexer = workspace(repo)
    indexer.reconcile("HEAD")
    trace_module.import_trace(
        store,
        "HEAD",
        {"root": ".", "edges": [["m.py::dispatch", "m.py::H.handle"]], "executed": []},
        "trace.json",
    )
    indexer.reconcile("HEAD")
    view = build_view(store, "HEAD", repo="r", config=indexer.config)
    observed = [edge for edge in view.edges if edge[4]]
    assert observed, "a trace was imported and nothing in the view says so"
    assert view.summary["observed"] == len(observed)
    store.close()


def test_a_revision_with_no_trace_reports_no_observed_edge(two_module_repo):
    store, _, view = built(two_module_repo)
    assert view.summary["observed"] == 0
    assert not view.traced
    assert view.summary["trace"] == "none"
    store.close()


# -- islands -----------------------------------------------------------------


def test_the_view_and_the_islands_report_describe_the_same_partition(two_module_repo):
    """The whole reason this ships inside codegraph.

    The picture colours symbols by island and the report counts them; if
    the two ever came from different code they would come to disagree, and
    a reader with both open would have no way to tell which was wrong.
    """
    store, indexer, view = built(two_module_repo)
    report = islands_report(store, "HEAD", indexer.config)
    assert view.summary["islands"] == report.summary["islands"]
    assert view.summary["singletons"] == report.summary["singletons"]
    assert view.summary["unexplained"] == report.summary["unexplained"]
    assert view.summary["largest"] == report.summary["largest"]
    assert view.summary["symbols"] == report.summary["symbols"]
    store.close()


def test_islands_are_numbered_largest_first(two_module_repo):
    store, _, view = built(two_module_repo)
    sizes = [island.size for island in view.islands]
    assert sizes == sorted(sizes, reverse=True)
    store.close()


def test_an_unexplained_island_is_marked_as_one(two_module_repo):
    """`unexplained` has to reach the drawing per symbol, not as a count.

    The view renders it as an absence -- the share of a box left unpainted
    -- and that is only possible if the flag is on the island each symbol
    belongs to.
    """
    store, _, view = built(two_module_repo)
    unexplained = {
        view.boxes[index].name
        for index, island in enumerate(view.island_of)
        if island >= 0 and not view.islands[island].explained
    }
    assert "unreached_by_anything" in unexplained
    store.close()


def test_every_symbol_belongs_to_an_island(two_module_repo):
    """A symbol with no edge at all is an island of one, never nothing --
    so there is no symbol the fill rule has no colour for."""
    store, _, view = built(two_module_repo)
    for index, box in enumerate(view.boxes):
        if box.type == SYMBOL:
            assert view.island_of[index] >= 0, box.node_id
    store.close()


# -- the absence, counted ----------------------------------------------------


def test_references_that_produced_no_edge_are_counted_per_box(repo, write):
    store, _, view = built_with_unresolved(repo, write)
    counts = {view.boxes[index].name: value for index, value in view.unresolved.items()}
    assert counts["f"][0] >= 1, counts  # 'unknown' is the first reason
    store.close()


def built_with_unresolved(repo, write):
    write("m.py", "def f(x):\n    return x.no_such_method()\n", commit="unresolved")
    return built(repo)


# -- the bare-name fan-out ---------------------------------------------------


def test_the_fanout_keeps_calls_and_bases_apart(repo, write):
    """A derived relationship still has a kind.

    The fan-out is drawn on demand and LOW by construction; if the calls
    and the base references were merged into one list, the LOW lines would
    all read as CALLS, which is the flattening the legend promises does
    not happen.
    """
    write(
        "one.py",
        "class Shape:\n    def draw(self):\n        return 1\n",
        commit="one",
    )
    write(
        "two.py",
        "class Shape:\n    def draw(self):\n        return 2\n",
    )
    write(
        "use.py",
        "def go(thing):\n    return thing.draw()\n",
        commit="ambiguous",
    )
    store, _, view = built(repo)
    assert "draw" in view.fanout, sorted(view.fanout)
    calls, bases, targets = view.fanout["draw"]
    assert calls and not bases
    assert len(targets) >= 2
    store.close()


def test_the_fanout_is_linear_in_references_rather_than_their_product(two_module_repo):
    """It is shipped as the index, never as the pairs.

    `ambiguity.py` exists because the cross product is quadratic; a view
    that expanded it at build time would have undone that decision inside
    a file somebody has to download.
    """
    store, _, view = built(two_module_repo)
    packed = view.pack()["fanout"]
    entries = sum(
        len(packed["from"][i]) + len(packed["base"][i]) + len(packed["to"][i])
        for i in range(len(packed["name"]))
    )
    assert entries <= 4 * len([box for box in view.boxes if box.type == SYMBOL]) + 64
    store.close()


# -- the highlight -----------------------------------------------------------


def test_a_json_report_is_read_for_the_symbols_it_names():
    """Any report, not one blessed shape. `impact`, `effects`, `path`,
    `islands` and `unknowns` all print rows with an `id`."""
    report = json.dumps(
        {
            "summary": {"symbol": "a.py::x", "symbols": 2},
            "groups": [
                {"title": "dependents", "rows": [{"id": "a.py::y"}, {"id": "b.py::z"}]},
                {"title": "tests", "rows": [{"id": "tests/t.py::test_y"}]},
            ],
        }
    )
    found, label = highlight_from_report(report)
    assert found == {"a.py::x", "a.py::y", "b.py::z", "tests/t.py::test_y"}
    assert label == "a.py::x"


def test_a_report_with_no_symbol_is_labelled_by_its_groups():
    report = json.dumps(
        {"summary": {"symbols": 1}, "groups": [{"title": "dependents", "rows": []}]}
    )
    assert highlight_from_report(report)[1] == "dependents"


def test_highlighted_ids_become_box_indices(two_module_repo):
    store, indexer, _ = built(two_module_repo)
    view = build_view(
        store,
        "HEAD",
        repo="r",
        config=indexer.config,
        highlight={"app.py::entry", "nowhere.py::gone"},
        highlight_label="entry",
    )
    assert {view.boxes[index].name for index in view.highlight} == {"entry"}
    assert view.highlight_label == "entry"
    store.close()


# -- the page ----------------------------------------------------------------


def test_the_page_carries_its_data_and_fetches_nothing(two_module_repo):
    """Self-contained is the whole delivery promise: no server, no
    account, no network. A stylesheet or a script from a CDN would make
    the file useless exactly where it is most wanted -- offline, or
    attached to a message."""
    store, _, view = built(two_module_repo)
    html = render_html(view)
    assert "<canvas" in html
    assert not re.search(r"(src|href)\s*=\s*[\"']https?://", html)
    store.close()


def test_the_payload_round_trips_out_of_the_page(two_module_repo):
    """What the browser reads is what the model produced.

    The graph is gzipped and base64'd into a script block, so this
    unpacks it the way `view.js` does and checks the box count survived.
    """
    store, _, view = built(two_module_repo)
    html = render_html(view)
    blob = re.search(r'id="graph-data">([^<]+)<', html).group(1)
    payload = json.loads(gzip.decompress(b64decode(blob)))
    assert len(payload["box"]["name"]) == len(view.boxes)
    assert payload["kinds"] == list(DEPENDENCY_KINDS)
    assert payload["tiers"] == list(TIERS)
    store.close()


def test_source_is_embedded_under_the_budget_and_dropped_without_it(two_module_repo):
    store, indexer, _ = built(two_module_repo)
    with_source = build_view(
        store,
        "HEAD",
        repo="r",
        config=indexer.config,
        source=indexer.source,
        source_budget=1_000_000,
    )
    assert "app.py" in with_source.source
    assert "def entry" in with_source.source["app.py"]
    without = build_view(store, "HEAD", repo="r", config=indexer.config)
    assert without.source == {}
    assert without.summary["source_files"] == 0
    store.close()


# -- the command -------------------------------------------------------------


def test_visualize_writes_one_file(two_module_repo, tmp_path, capsys):
    out = tmp_path / "view.html"
    code = main(["visualize", "--path", str(two_module_repo), "--out", str(out), "--no-source"])
    assert code == 0
    assert out.exists() and out.stat().st_size > 1000
    assert str(out) in capsys.readouterr().out


def test_visualize_reports_a_revision_it_cannot_resolve(two_module_repo, tmp_path, capsys):
    """Global like `islands` and `orphans`: `1` for a bad `--rev`, and
    never the symbol-resolving `2`, which has nothing to resolve here."""
    code = main(
        ["visualize", "--path", str(two_module_repo), "--rev", "nope", "--out", str(tmp_path / "v")]
    )
    assert code == 1
    assert "revision not found" in capsys.readouterr().err


def test_visualize_refuses_a_highlight_file_it_cannot_read(two_module_repo, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    code = main(
        [
            "visualize",
            "--path",
            str(two_module_repo),
            "--out",
            str(tmp_path / "v.html"),
            "--highlight",
            str(bad),
        ]
    )
    assert code == 1
    assert "--json report" in capsys.readouterr().err
