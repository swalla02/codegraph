"""Store rows in, one JSON-ready value out. No HTML here, and no browser.

This is the testable half of the view (see the package docstring for why
the seam is here). Everything that decides *what is true* about the picture
happens in this module: which boxes exist, where they are, which island
each symbol belongs to, what an edge's kind, tier and provenance are. The
renderer decides only how that is painted.

## The layout is computed here, not in the browser

A squarified treemap of the directory tree, nested: package, module, then
the symbols inside a module with methods inside their class. It is computed
once, in Python, and shipped as coordinates, for the reason the whole view
exists -- django is 2,932 files and 43,843 symbols, and a layout recomputed
in a page load is a layout the reader waits for. Shipping rectangles also
makes the layout a property of the data rather than of the browser: the
same revision draws the same picture on every machine, and
`test_viz.py` can assert on it without one.

The tree is the base layout on purpose (#60, and CBRV's reasoning before
it): a reader already knows their own directory structure, so putting edges
over it costs them no new mental model, where a force-directed placement
asks them to learn one that changes every time the graph does.

## What a box is

Three kinds, all rectangles, nested by containment:

*A directory*, which holds directories and files.

*A file*, which IS that file's `path::<module>` node -- the synthetic node
`islands.py` describes as carrying connectivity without ever being a
member. Drawing it as the file box rather than as a symbol inside the file
is what makes a module-scope call (`_init()` at the foot of a module) an
edge that visibly leaves the file, instead of an edge to a box nobody can
find.

*A symbol*, one per `nodes` row that is not a module: a class, a function
or a method, nested under its qualname's parent when that parent is also a
node. `outer.<locals>.inner` has no node at `outer.<locals>`, so the walk
up the qualname continues to `outer`, which does.

Box area is source lines, which is the one weight a reader can check
against the file they are looking at. A container's weight is the sum of
its children's, so a class's own body outside its methods is not drawn --
an approximation, and the only one in the layout.

## What is drawn, and what is deliberately not

The stored `edges` rows, collapsed to one entry per `(src, dst, kind)` at
the strongest confidence the duplicates claim, with `observed` carried
beside the tier rather than folded into it -- the same pair of axes
`impact.py` keeps apart, for the same reason.

Not the bare-name fan-out. `ambiguity.py` expands 33,329 deferred
relationships on django into up to 2.07M pairs, and a picture that drew
them would be a claim about density that the graph does not make. It is not
dropped either, which would leave a reader wondering why two symbols share
an island with no edge between them: the per-name index the expansion is
computed from is small (one entry per name, one per ambiguous reference)
and is shipped whole, so the view can expand the fan-out for ONE selected
symbol, on demand, and draw it as what it is -- LOW, derived, and not in
the stored graph. That is the same pointwise/whole-graph split
`Ambiguity.callers` and `Ambiguity.hub_edges` already make.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from codegraph.ambiguity import Ambiguity
from codegraph.config import Config
from codegraph.indexer import TreeSource
from codegraph.query.islands import (
    ENV_READ,
    MECHANISMS,
    NETWORK,
    IslandLabel,
    Partition,
    labelled_partition,
)
from codegraph.resolve import CONFIDENCE_RANK, DEPENDENCY_KINDS, RUNTIME, stronger
from codegraph.store import Store
from codegraph.trace import observed_nodes
from codegraph.trace import summary as trace_summary

#: The layout's coordinate space: integer, and the shape of a screen
#: rather than a square, so that a fitted view fills the window instead of
#: leaving two margins the width of a legend. Rectangles are rounded into
#: it at pack time, so a box narrower than one unit rounds to zero width
#: and is never drawn -- on django the smallest symbol box is tens of
#: units across, which leaves several orders of magnitude of headroom
#: under the browser's zoom.
EXTENT = 1_000_000
EXTENT_Y = EXTENT * 9 // 16

#: Fraction of a box's shorter side left as a gutter around its children.
#: Relative rather than absolute so that nesting reads the same at every
#: zoom: a package's children are inset from it by the same proportion as a
#: class's methods are from the class.
PADDING = 0.055

#: Box kinds, as the payload's `type` field indexes them.
DIRECTORY, FILE, SYMBOL = 0, 1, 2

#: Edge confidence tiers, in the order the payload indexes them and the
#: legend lists them. Derived from `resolve.CONFIDENCE_RANK` rather than
#: written out, so the legend cannot come to disagree with the resolver
#: about which tier is the strong one -- which is the one thing a picture
#: of a graph must not get wrong (#37).
TIERS = tuple(sorted(CONFIDENCE_RANK, key=lambda tier: -CONFIDENCE_RANK[tier]))

_TIER_INDEX = {name: index for index, name in enumerate(TIERS)}
_KIND_INDEX = {name: index for index, name in enumerate(DEPENDENCY_KINDS)}

#: Reasons a reference produced no edge, in the order the payload indexes
#: them. Only `unknown` is a gap in the graph; the others are answers (see
#: `uncertainty.SETTLED`), and the view says so rather than counting them
#: together.
REASONS = ("unknown", "ambiguous", "external", "builtin")


@dataclass
class Box:
    """One rectangle: a directory, a file, or a symbol.

    Mutable during construction and frozen by convention afterwards -- the
    tree is built top-down and laid out bottom-up, and a frozen dataclass
    would buy nothing but a rebuild of every node twice.
    """

    name: str
    type: int
    #: The `nodes` row this box is, for a file (its `path::<module>` node)
    #: or a symbol. Empty for a directory, which is not in the graph.
    node_id: str = ""
    path: str = ""
    kind: str = ""
    line_start: int = 0
    line_end: int = 0
    children: list[Box] = field(default_factory=list)
    weight: float = 0.0
    rect: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    #: Index in the packed preorder, filled by `_number`.
    index: int = -1
    #: One past the last index of this box's subtree, so that "every
    #: descendant of i" is the contiguous range `i+1 .. subtree_end`. The
    #: view needs that range once per frame to map a symbol to whichever
    #: ancestor is currently drawn; a contiguous range makes it a fill
    #: rather than a walk.
    subtree_end: int = -1


@dataclass(frozen=True)
class Island:
    """One connected component, as the view labels it.

    The same label an `islands` row carries -- `IslandLabel` verbatim --
    plus the index the palette colours it by. `explained` is the field the
    drawing reads: an unexplained island is rendered as a hole rather than
    as a footnote, which is the whole of #60's complaint about the text
    form.
    """

    index: int
    size: int
    label: IslandLabel

    @property
    def explained(self) -> bool:
        return self.label.explained


@dataclass(frozen=True)
class View:
    """Everything one page needs, before it is a page.

    `pack` is the only thing the renderer calls. Holding the boxes as a
    tree until then keeps the shaping readable and the packing in one
    place, where the array layout the browser wants can be changed without
    touching how the tree is built.
    """

    repo: str
    rev: str
    root: Box
    boxes: list[Box]
    islands: list[Island]
    #: box index -> island index, -1 for a directory, a file, or a symbol
    #: with no island (which cannot happen: a symbol with no edge is an
    #: island of one).
    island_of: list[int]
    #: (src box, dst box, kind index, tier index, observed) per edge.
    edges: list[tuple[int, int, int, int, int]]
    #: Per box, the count of references in that symbol's body that produced
    #: no edge, indexed by `REASONS`.
    unresolved: dict[int, tuple[int, int, int, int]]
    #: Names the bare-name fan-out routes through: per name, the boxes
    #: that CALL it ambiguously, the boxes that SUBCLASS it ambiguously,
    #: and the boxes it could mean. Calls and bases stay apart because the
    #: view draws kind, and a derived relationship whose kind had been
    #: flattened would be exactly the lie the legend promises it is not.
    fanout: dict[str, tuple[list[int], list[int], list[int]]]
    #: box index -> the fan-out names that could reach it, as indices into
    #: `fanout`'s key order. `Ambiguity.reaching_names` verbatim, computed
    #: here so the browser never has to re-derive a rule this graph owns.
    reached_by: dict[int, list[int]]
    #: Box indices a run was watched entering.
    traced: set[int]
    #: Box indices the highlighted report names, and what it was.
    highlight: set[int]
    highlight_label: str
    #: path -> source text, empty when source was not embedded.
    source: dict[str, str]
    summary: dict

    def pack(self) -> dict:
        """The JSON-ready form: parallel arrays, integers where possible.

        Parallel arrays rather than one object per box because the payload
        is gzipped into the page and a column of small integers compresses
        to a fraction of what 46,775 repetitions of the same seven keys do
        -- and because the browser wants typed arrays anyway.
        """
        scale = EXTENT
        names = list(self.fanout)
        return {
            "repo": self.repo,
            "rev": self.rev,
            "extent": [EXTENT, EXTENT_Y],
            "kinds": list(DEPENDENCY_KINDS),
            "tiers": list(TIERS),
            "reasons": list(REASONS),
            "mechanisms": list(MECHANISMS),
            "summary": self.summary,
            "box": {
                "name": [box.name for box in self.boxes],
                "type": [box.type for box in self.boxes],
                "kind": [box.kind for box in self.boxes],
                "path": [box.path for box in self.boxes],
                "line": [[box.line_start, box.line_end] for box in self.boxes],
                "id": [box.node_id for box in self.boxes],
                "end": [box.subtree_end for box in self.boxes],
                "rect": [
                    [
                        round(box.rect[0] * scale),
                        round(box.rect[1] * scale),
                        round(box.rect[2] * scale),
                        round(box.rect[3] * scale),
                    ]
                    for box in self.boxes
                ],
                "island": self.island_of,
            },
            "islands": [
                {
                    "size": island.size,
                    "found": list(island.label.found),
                    "missing": list(island.label.missing),
                    "boundary": list(island.label.boundary),
                    "traced": island.label.traced,
                    "explained": island.explained,
                }
                for island in self.islands
            ],
            "edges": {
                "src": [edge[0] for edge in self.edges],
                "dst": [edge[1] for edge in self.edges],
                "kind": [edge[2] for edge in self.edges],
                "tier": [edge[3] for edge in self.edges],
                "observed": [edge[4] for edge in self.edges],
            },
            "unresolved": {str(index): list(counts) for index, counts in self.unresolved.items()},
            "fanout": {
                "name": names,
                "from": [self.fanout[name][0] for name in names],
                "base": [self.fanout[name][1] for name in names],
                "to": [self.fanout[name][2] for name in names],
                "reached": {str(index): value for index, value in self.reached_by.items()},
            },
            "traced": sorted(self.traced),
            "highlight": sorted(self.highlight),
            "highlight_label": self.highlight_label,
            "source": self.source,
        }


# -- the box tree ------------------------------------------------------------


def _insert(root: Box, parts: list[str]) -> Box:
    """Walk `parts` down from `root`, creating directory boxes as needed."""
    node = root
    for part in parts:
        for child in node.children:
            if child.name == part and child.type == DIRECTORY:
                node = child
                break
        else:
            child = Box(name=part, type=DIRECTORY)
            node.children.append(child)
            node = child
    return node


def _symbol_parent(qualname: str, by_qualname: dict[str, Box]) -> Box | None:
    """The innermost enclosing symbol box, or None for a top-level symbol.

    Walks up the qualname rather than assuming one dot of nesting, because
    `outer.<locals>.inner` has a parent (`outer`) two segments up and
    nothing at all one segment up.
    """
    owner = qualname.rpartition(".")[0]
    while owner:
        parent = by_qualname.get(owner)
        if parent is not None:
            return parent
        owner = owner.rpartition(".")[0]
    return None


def _tree(rows: list[tuple[str, str, str, str, int, int]]) -> tuple[Box, dict[str, Box]]:
    """The box tree for a revision's `nodes`, and the index of it by node id.

    Rows arrive sorted by path then by line, so a symbol's enclosing class
    is always built before it.
    """
    root = Box(name="", type=DIRECTORY)
    by_id: dict[str, Box] = {}
    files: dict[str, tuple[Box, dict[str, Box]]] = {}
    for node_id, path, qualname, kind, line_start, line_end in rows:
        if path not in files:
            parts = path.split("/")
            directory = _insert(root, parts[:-1])
            box = Box(name=parts[-1], type=FILE, path=path, kind="module")
            directory.children.append(box)
            files[path] = (box, {})
        file_box, by_qualname = files[path]
        if kind == "module":
            file_box.node_id = node_id
            file_box.line_start, file_box.line_end = line_start, line_end
            by_id[node_id] = file_box
            continue
        box = Box(
            name=qualname.rpartition(".")[2],
            type=SYMBOL,
            node_id=node_id,
            path=path,
            kind=kind,
            line_start=line_start,
            line_end=line_end,
        )
        parent = _symbol_parent(qualname, by_qualname) or file_box
        parent.children.append(box)
        by_qualname[qualname] = box
        by_id[node_id] = box
    return root, by_id


def _weigh(box: Box) -> float:
    """Source lines, summed up the tree, with a floor of one.

    A container's weight is its children's, so an empty file and a class
    with no methods both still get a box -- a repository's picture should
    not silently omit a file because the indexer found nothing in it.
    """
    if box.children:
        box.weight = sum(_weigh(child) for child in box.children)
    else:
        box.weight = float(max(1, box.line_end - box.line_start + 1))
    return box.weight


def _collapse(box: Box) -> None:
    """Fold a directory that holds exactly one directory into its child.

    `src/codegraph/query` is three nested rectangles with nothing beside
    them, and each one costs a gutter and a label for no information. The
    joined name (`src/codegraph`) is what a reader would have said anyway.
    """
    for child in box.children:
        _collapse(child)
    while (
        box.type == DIRECTORY
        and len(box.children) == 1
        and box.children[0].type == DIRECTORY
        and box.name
    ):
        only = box.children[0]
        box.name = f"{box.name}/{only.name}"
        box.children = only.children


# -- the layout --------------------------------------------------------------


def _worst(row: list[float], side: float, total: float) -> float:
    """The worst aspect ratio in `row` if it is laid along `side`.

    The squarified treemap's objective function, in the standard form: the
    row's area fixes the depth of the strip, so the extremes decide it.
    """
    if not row or side <= 0 or total <= 0:
        return float("inf")
    depth = total / side
    sides = [value / depth for value in row if value > 0]
    if not sides:
        return float("inf")
    return max(max(depth / length, length / depth) for length in sides)


def _squarify(weights: list[float], x: float, y: float, w: float, h: float) -> list[tuple]:
    """Rectangles for `weights` filling `(x, y, w, h)`, in input order.

    Squarified treemap (Bruls, Huizing, van Wijk): lay children into strips
    along the shorter side, extending a strip while it makes the worst
    aspect ratio in it better and closing it when it does not. Boxes near a
    square are what make a treemap readable at all -- a slice-and-dice
    layout at this depth produces splinters nothing can be clicked or
    labelled.

    Deterministic for a given input order, which is what lets the layout be
    asserted on: the caller sorts, this does not.
    """
    rects: list[tuple] = [(x, y, 0.0, 0.0)] * len(weights)
    total = sum(weights)
    if total <= 0 or w <= 0 or h <= 0:
        return rects
    scale = (w * h) / total
    areas = [weight * scale for weight in weights]

    index = 0
    while index < len(areas):
        side = min(w, h)
        row: list[float] = []
        row_total = 0.0
        end = index
        while end < len(areas):
            candidate = row + [areas[end]]
            if row and _worst(candidate, side, row_total + areas[end]) > _worst(
                row, side, row_total
            ):
                break
            row, row_total = candidate, row_total + areas[end]
            end += 1
        depth = row_total / side if side > 0 else 0.0
        offset = 0.0
        for position, area in enumerate(row):
            length = (area / depth) if depth > 0 else 0.0
            if w >= h:
                rects[index + position] = (x, y + offset, depth, length)
            else:
                rects[index + position] = (x + offset, y, length, depth)
            offset += length
        if w >= h:
            x, w = x + depth, w - depth
        else:
            y, h = y + depth, h - depth
        index = end
    return rects


def _layout(box: Box, x: float, y: float, w: float, h: float) -> None:
    box.rect = (x, y, w, h)
    if not box.children:
        return
    pad = min(w, h) * PADDING
    inner = (x + pad, y + pad, w - 2 * pad, h - 2 * pad)
    if inner[2] <= 0 or inner[3] <= 0:
        return
    # Largest first, and by name within a tie, so the picture is stable
    # across runs and the eye finds the big things in the same corner.
    box.children.sort(key=lambda child: (-child.weight, child.name))
    rects = _squarify([child.weight for child in box.children], *inner)
    for child, rect in zip(box.children, rects, strict=True):
        _layout(child, *rect)


def _number(box: Box, boxes: list[Box]) -> int:
    """Assign preorder indices and subtree ranges; returns one past the end."""
    box.index = len(boxes)
    boxes.append(box)
    end = box.index + 1
    for child in box.children:
        end = _number(child, boxes)
    box.subtree_end = end
    return end


# -- the report a view can be highlighted with -------------------------------


def highlight_from_report(text: str) -> tuple[set[str], str]:
    """Node ids named by a `--json` report, and a label for them.

    Any of them: `impact`, `effects`, `path`, `islands`, `unknowns` and
    `diff` all print `groups[].rows[].id`, and the ones that take a symbol
    name it in `summary`. Reading the reports rather than re-running a
    query is what keeps #60's "adds no extraction and changes no edge"
    true, and it means a result a reader already has on their terminal can
    be dropped into the picture without the view knowing what produced it.
    """
    report = json.loads(text)
    found = {
        row["id"]
        for group in report.get("groups", [])
        for row in group.get("rows", [])
        if isinstance(row.get("id"), str)
    }
    summary = report.get("summary", {})
    subject = ""
    for key in ("symbol", "from", "to"):
        value = summary.get(key)
        if isinstance(value, str) and "::" in value:
            found.add(value)
            subject = subject or value
    titles = [
        group["title"] for group in report.get("groups", []) if isinstance(group.get("title"), str)
    ]
    label = subject or ", ".join(titles) or "report"
    return found, label


# -- the build ---------------------------------------------------------------


def _edges(store: Store, rev: str, by_id: dict[str, Box]) -> list[tuple[int, int, int, int, int]]:
    """One entry per `(src, dst, kind)` at its strongest tier, with
    `observed` beside it.

    Duplicate rows are collapsed before anything is drawn, exactly as
    `impact.py` collapses them before it walks: one relationship written
    twice in a body is one line in the picture, or the edge widths would
    report how often a call was typed rather than how much depends on it.
    A pair a trace confirmed holds two rows and they are not competing
    answers -- the static row keeps the tier the text earns and the runtime
    row sets the bit.
    """
    marks = ",".join("?" * len(DEPENDENCY_KINDS))
    tier: dict[tuple[int, int, int], str] = {}
    observed: set[tuple[int, int, int]] = set()
    for row in store.connection.execute(
        f"SELECT src, dst, kind, confidence, provenance FROM edges"
        f" WHERE rev=? AND kind IN ({marks})",
        (rev, *DEPENDENCY_KINDS),
    ):
        source, target = by_id.get(row["src"]), by_id.get(row["dst"])
        if source is None or target is None or source.index == target.index:
            continue
        key = (source.index, target.index, _KIND_INDEX[row["kind"]])
        confidence = row["confidence"]
        tier[key] = stronger(tier[key], confidence) if key in tier else confidence
        if row["provenance"] == RUNTIME:
            observed.add(key)
    return sorted(
        (src, dst, kind, _TIER_INDEX[value], int((src, dst, kind) in observed))
        for (src, dst, kind), value in tier.items()
    )


def _unresolved(
    store: Store, rev: str, by_id: dict[str, Box]
) -> dict[int, tuple[int, int, int, int]]:
    """Per box, how many references in its body produced no edge, by reason.

    The absence, made countable. A symbol with eleven unresolved references
    is not a symbol the picture knows eleven things about, and the view
    says so on the box rather than in a footnote.
    """
    counts: dict[int, list[int]] = {}
    for row in store.connection.execute(
        "SELECT src, reason, COUNT(*) AS n FROM unresolved WHERE rev=? AND src<>''"
        " GROUP BY src, reason",
        (rev,),
    ):
        box = by_id.get(row["src"])
        if box is None or row["reason"] not in REASONS:
            continue
        counts.setdefault(box.index, [0, 0, 0, 0])[REASONS.index(row["reason"])] += row["n"]
    return {index: tuple(value) for index, value in counts.items()}


def _fanout(
    ambiguity: Ambiguity, by_id: dict[str, Box]
) -> tuple[dict[str, tuple[list[int], list[int], list[int]]], dict[int, list[int]]]:
    """The bare-name fan-out, shipped as the index it is computed from.

    One entry per name: the boxes that make an ambiguous reference to it,
    and the boxes it could mean. That is `Ambiguity.call_refs` and
    `Ambiguity.base_refs` joined to `Ambiguity.by_name` -- linear in the
    references and in the definitions, where the pairs they stand for are
    the product of the two. Per box, the names that could reach it come
    from `Ambiguity.reaching_names`, so the constructor case (`Widget()`
    reaches `Widget.__init__` under the name `Widget`) is the resolver's
    own rule rather than a second one written in JavaScript.
    """
    calls: dict[str, set[int]] = {}
    bases: dict[str, set[int]] = {}
    for refs, into in ((ambiguity.call_refs, calls), (ambiguity.base_refs, bases)):
        for name, nodes in refs.items():
            into.setdefault(name, set()).update(
                by_id[node_id].index for node_id in nodes if node_id in by_id
            )
    fanout: dict[str, tuple[list[int], list[int], list[int]]] = {}
    for name in sorted(calls.keys() | bases.keys()):
        targets = [
            by_id[node_id].index for node_id in ambiguity.candidates(name) if node_id in by_id
        ]
        from_calls = sorted(calls.get(name, ()))
        from_bases = sorted(bases.get(name, ()))
        if targets and (from_calls or from_bases):
            fanout[name] = (from_calls, from_bases, targets)
    order = {name: index for index, name in enumerate(fanout)}
    reached: dict[int, list[int]] = {}
    for node_id, box in by_id.items():
        indices = [order[name] for name in ambiguity.reaching_names(node_id) if name in order]
        if indices:
            reached[box.index] = indices
    return fanout, reached


def _source(
    store: Store, rev: str, source: TreeSource, paths: set[str], budget: int
) -> dict[str, str]:
    """The revision's own bytes for the files the view draws, up to `budget`.

    Read through the same `TreeSource` the indexer reads, keyed by the
    `tree` table's blob shas, so the source in the page is the source the
    graph was built from and not whatever is on disk now. A file that is
    not valid UTF-8, or that arrives after the budget is spent, is simply
    absent, and the view says "source not embedded" rather than showing
    something else's text.
    """
    if budget <= 0:
        return {}
    wanted = {
        row["path"]: row["blob_sha"]
        for row in store.connection.execute("SELECT path, blob_sha FROM tree WHERE rev=?", (rev,))
        if row["path"] in paths
    }
    by_sha: dict[str, list[str]] = {}
    for path, sha in wanted.items():
        by_sha.setdefault(sha, []).append(path)
    found: dict[str, str] = {}
    spent = 0
    for sha, data in source.read(list(by_sha)):
        if spent + len(data) > budget:
            break
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        spent += len(data)
        for path in by_sha.get(sha, ()):
            found[path] = text
    return found


def build_view(
    store: Store,
    rev: str,
    *,
    repo: str = "",
    config: Config | None = None,
    source: TreeSource | None = None,
    source_budget: int = 0,
    highlight: set[str] | None = None,
    highlight_label: str = "",
) -> View:
    """One revision, shaped into the value `render.render_html` draws.

    Costs one `islands.labelled_partition` (which is what `codegraph
    islands` costs: one pass over the edges and one over the nodes), one
    further pass over the edges to collapse them, and one grouped read of
    `unresolved`. Nothing here walks the graph per node.
    """
    rows = [
        (row["id"], row["path"], row["qualname"], row["kind"], row["line_start"], row["line_end"])
        for row in store.connection.execute(
            "SELECT id, path, qualname, kind, line_start, line_end FROM nodes"
            " WHERE rev=? ORDER BY path, line_start, id",
            (rev,),
        )
    ]
    root, by_id = _tree(rows)
    _collapse(root)
    _weigh(root)
    _layout(root, 0.0, 0.0, 1.0, EXTENT_Y / EXTENT)
    boxes: list[Box] = []
    _number(root, boxes)

    partition = labelled_partition(store, rev, config)
    islands, island_of = _islands(partition, by_id, len(boxes))
    edges = _edges(store, rev, by_id)
    ambiguity = Ambiguity(store, rev)
    fanout, reached = _fanout(ambiguity, by_id)
    traced = {by_id[node_id].index for node_id in observed_nodes(store, rev) if node_id in by_id}
    highlighted = {by_id[node_id].index for node_id in (highlight or ()) if node_id in by_id}
    embedded = (
        _source(store, rev, source, {box.path for box in boxes if box.path}, source_budget)
        if source is not None
        else {}
    )
    return View(
        repo=repo or Path.cwd().name,
        rev=rev,
        root=root,
        boxes=boxes,
        islands=islands,
        island_of=island_of,
        edges=edges,
        unresolved=_unresolved(store, rev, by_id),
        fanout=fanout,
        reached_by=reached,
        traced=traced,
        highlight=highlighted,
        highlight_label=highlight_label,
        source=embedded,
        summary=_summary(store, rev, boxes, edges, islands, partition, ambiguity, embedded),
    )


def _islands(
    partition: Partition, by_id: dict[str, Box], count: int
) -> tuple[list[Island], list[int]]:
    """Islands, numbered largest first, and the box -> island mapping.

    Largest first so that index 0 is the mainland on every repository and
    the palette can give it the one neutral colour: an archipelago read
    against a coloured continent is a picture of the continent.
    """
    roots = sorted(partition.grouped, key=lambda root: (-len(partition.grouped[root]), root))
    islands: list[Island] = []
    island_of = [-1] * count
    for index, root in enumerate(roots):
        members = partition.grouped[root]
        found = tuple(name for name in MECHANISMS if name in partition.mechanisms.get(root, set()))
        boundary = tuple(
            kind for kind in (NETWORK, ENV_READ) if kind in partition.boundaries.get(root, set())
        )
        label = IslandLabel(
            size=len(members),
            found=found,
            missing=tuple(name for name in MECHANISMS if name not in found),
            boundary=boundary,
            traced=partition.traced.get(root, 0),
        )
        islands.append(Island(index=index, size=len(members), label=label))
        for node_id in members:
            box = by_id.get(node_id)
            if box is not None:
                island_of[box.index] = index
    return islands, island_of


def _summary(
    store: Store,
    rev: str,
    boxes: list[Box],
    edges: list,
    islands: list[Island],
    partition: Partition,
    ambiguity: Ambiguity,
    source: dict[str, str],
) -> dict:
    """The numbers the page prints in its header.

    Deliberately the same numbers the text reports print -- a reader who
    has both open should never have to work out which one is lying. The
    ones that are new are about the drawing itself: how many edges were
    collapsed into how many lines, and how many relationships are deferred
    and therefore not drawn.
    """
    kinds = Counter(edge[2] for edge in edges)
    tiers = Counter(edge[3] for edge in edges)
    stored = store.connection.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE rev=?", (rev,)
    ).fetchone()["n"]
    return {
        "symbols": len(partition.members),
        "files": sum(1 for box in boxes if box.type == FILE),
        "boxes": len(boxes),
        "edges_stored": stored,
        "edges_drawn": len(edges),
        "by_kind": {DEPENDENCY_KINDS[index]: count for index, count in sorted(kinds.items())},
        "by_tier": {TIERS[index]: count for index, count in sorted(tiers.items())},
        "observed": sum(edge[4] for edge in edges),
        "deferred": ambiguity.relationships(),
        "islands": len(islands),
        "largest": islands[0].size if islands else 0,
        "singletons": sum(1 for island in islands if island.size == 1),
        "unexplained": sum(1 for island in islands if not island.explained),
        "traced_islands": sum(1 for island in islands if island.label.traced),
        "trace": trace_summary(store, rev),
        "source_files": len(source),
        "source_bytes": sum(len(text) for text in source.values()),
    }


__all__ = [
    "EXTENT",
    "EXTENT_Y",
    "PADDING",
    "REASONS",
    "TIERS",
    "Box",
    "Island",
    "View",
    "build_view",
    "highlight_from_report",
]
