"""The picture: one revision's stored graph, drawn.

Every other report this tool produces is a ranked list, which is the right
shape for an agent and a poor one for a person. `islands: 23 · singletons:
20` is a fact about a codebase's structure that a reader can count and
cannot see. This package turns the same stored rows into one self-contained
HTML file with no server, no account and no network fetch -- the way
`islands` is a command rather than a service.

## Inside codegraph, not beside it

Issue #60 left that open, on the grounds that a renderer has a different
dependency profile and release cadence from a CLI. The decision is to ship
it here, for three reasons, and the first is the only one that would be
hard to undo:

*The picture and the text must not be able to disagree.* Island membership
in the view is `islands.connected_components`' partition, labelled by
`islands.labelled_partition` -- the same code that prints an `islands` row,
called once. A separate package could not import a private module of this
one, so it would reimplement the partition, and this repository has already
learned what two implementations of one graph produce (see `islands.py` on
the constructor edge). The acceptance criterion "generated from stored data
only, with no second source of truth" is not satisfiable across a package
boundary that does not exist yet.

*The store's schema is not a public API.* The view reads `nodes`, `edges`
and `unresolved` directly, and `store.SCHEMA_VERSION` moves whenever the
indexer needs it to. Shipping the reader separately would freeze that
schema into a contract between two independently versioned packages, which
is a cost paid on every future index change, for a renderer that has no
independent reason to release.

*The dependency profile argument does not apply to this renderer.* It was
the strongest reason to split, and it dissolves once the output is a
generated file: `render.py` emits HTML, CSS and vanilla JavaScript, so this
package adds exactly nothing to `pyproject.toml`'s empty `dependencies`.

The seam is where the split would be made if that ever changes. `model.py`
is pure data shaping -- store rows in, one JSON-ready value out, no HTML,
no browser, fully testable offline. `render.py` is a template: it takes
that value and inlines it into a page, and is not unit-tested, because what
it produces is only correct when a browser draws it. A renderer that one
day needs a real layout or charting dependency moves out of the tree at
that line and keeps `model.py` behind it.
"""

from codegraph.viz.model import View, build_view, highlight_from_report
from codegraph.viz.render import render_html

__all__ = ["View", "build_view", "highlight_from_report", "render_html"]
