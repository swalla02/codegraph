"""One `View`, inlined into one file that opens with no server behind it.

A template and nothing else. Every judgement about what is true lives in
`model.py`; this decides only how the payload is carried and where the
stylesheet and the script are spliced in. It is not unit-tested, and that
is the seam talking: what this produces is correct only when a browser
draws it, so the tests pin the value that goes in and a real run against a
real repository checks what comes out.

## Why the payload is compressed inside the page

django's packed view is ~7 MB of JSON and its source is 19 MB. Written
literally that is a 26 MB file, which is slow to write, slow to open and
absurd to attach to anything. gzipped and base64'd it is a fraction of
that, and the browser has had `DecompressionStream` since 2023, so the cost
of decompressing it is paid once on load, in the background, by code that
is four lines long. The alternative -- a second file beside the HTML, or a
fetch -- breaks the one property #60 asked for: a file you can mail
somebody.

Source text is a *separate* blob from the graph, decompressed only when a
reader first asks to read a symbol. Whether a repository's source is
embedded therefore changes what the file weighs and nothing about how fast
it opens.

The one dependency this takes on the browser is `DecompressionStream`. A
browser without it gets a sentence saying so rather than a blank page.
"""

from __future__ import annotations

import base64
import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from codegraph import __version__
from codegraph.viz.model import View

_HERE = Path(__file__).parent

#: Package data, exactly as `guide.md` and `effects/builtin.toml` are:
#: hatchling ships every file under the packaged directory, so the
#: stylesheet and the script need no manifest entry and cannot go missing
#: from a wheel. They are files rather than Python strings because they are
#: CSS and JavaScript, and a language server that can see them is worth
#: more than the import that saves.
_CSS = _HERE / "view.css"
_JS = _HERE / "view.js"


def _blob(payload: object) -> str:
    """One JSON value as gzipped base64, for a `<script type="text/plain">`.

    Separators without spaces and `ensure_ascii=False` because the result
    is compressed: bytes that only exist to be pretty are bytes the reader
    downloads.
    """
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, 9)).decode("ascii")


def render_html(view: View, *, title: str = "") -> str:
    """The whole page, as a string.

    Nothing is fetched at runtime: the stylesheet, the script, the graph
    and the source are all inside the string this returns.
    """
    packed = view.pack()
    source = packed.pop("source")
    heading = title or f"{view.repo} · {view.rev}"
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    # Substitution by replacement rather than by `str.format`, because the
    # stylesheet and the script are full of braces and a format string
    # would have to escape every one of them -- turning two readable files
    # into two unreadable ones for no gain.
    filled = _PAGE
    for token, value in (
        ("__HEADING__", _escape(heading)),
        ("__VERSION__", _escape(__version__)),
        ("__GENERATED__", _escape(generated)),
        ("__SUMMARY__", _header(view.summary)),
        ("__LEGEND__", _LEGEND),
        ("__CSS__", _CSS.read_text(encoding="utf-8")),
        ("__JS__", _JS.read_text(encoding="utf-8")),
        ("__GRAPH__", _blob(packed)),
        ("__SOURCE__", _blob(source)),
    ):
        filled = filled.replace(token, value)
    return filled


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _header(summary: dict) -> str:
    """The summary line, in the same ` · ` shape every text report leads
    with, so a reader with both open reads one format."""
    pairs = [
        ("symbols", summary["symbols"]),
        ("files", summary["files"]),
        ("edges drawn", f"{summary['edges_drawn']} of {summary['edges_stored']}"),
        ("deferred", summary["deferred"]),
        ("islands", summary["islands"]),
        ("singletons", summary["singletons"]),
        ("unexplained", summary["unexplained"]),
        ("observed", summary["observed"]),
    ]
    return " · ".join(
        f'<span class="k">{_escape(key)}:</span> {_escape(str(value))}' for key, value in pairs
    )


#: The legend, written here rather than generated, because it is prose: it
#: says what a reader is looking at, and the mapping it describes is fixed
#: by `view.css` and `view.js` together. The one rule it must never break
#: is #60's: four kinds and three tiers that read identically would be a
#: step backwards from the text this replaces, so every channel the drawing
#: uses is named here.
_LEGEND = """
<div class="legend-group">
  <h4>edge kind <span class="hint">colour + dash · click to filter</span></h4>
  <div class="row" data-kind="0"><svg width="46" height="10"><line x1="1" y1="5" x2="45" y2="5"
    class="e-calls"/></svg><span>CALLS</span></div>
  <div class="row" data-kind="1"><svg width="46" height="10"><line x1="1" y1="5" x2="45" y2="5"
    class="e-inherits"/></svg><span>INHERITS</span></div>
  <div class="row" data-kind="2"><svg width="46" height="10"><line x1="1" y1="5" x2="45" y2="5"
    class="e-implements"/></svg><span>IMPLEMENTS</span></div>
  <div class="row" data-kind="3"><svg width="46" height="10"><line x1="1" y1="5" x2="45" y2="5"
    class="e-references"/></svg><span>REFERENCES</span></div>
</div>
<div class="legend-group">
  <h4>confidence <span class="hint">weight + opacity · click to filter</span></h4>
  <div class="row" data-tier="0"><svg width="46" height="10"><line x1="1" y1="5" x2="45" y2="5"
    class="e-calls t-high"/></svg><span>HIGH</span></div>
  <div class="row" data-tier="1"><svg width="46" height="10"><line x1="1" y1="5" x2="45" y2="5"
    class="e-calls t-medium"/></svg><span>MEDIUM</span></div>
  <div class="row" data-tier="2"><svg width="46" height="10"><line x1="1" y1="5" x2="45" y2="5"
    class="e-calls t-low"/></svg><span>LOW <em>derived, on demand</em></span></div>
</div>
<div class="legend-group">
  <h4>provenance <span class="hint">who says so</span></h4>
  <div class="row"><svg width="46" height="10"><line x1="1" y1="5" x2="45" y2="5"
    class="e-calls t-high glow"/></svg><span>observed <em>a run was watched taking it</em></span></div>
  <div class="row"><span class="swatch traced"></span><span>a symbol the run entered</span></div>
</div>
<div class="legend-group">
  <h4>islands <span class="hint">fill</span></h4>
  <div class="row"><span class="swatch mainland"></span><span>the largest island</span></div>
  <div class="row"><span class="swatch isle"></span><span>another island <em>one hue per
    island, on a symbol; a container is tinted by the share of its symbols that are off the
    mainland</em></span></div>
  <div class="row"><span class="swatch hollow"></span><span>unexplained <em>nothing recognised
    reaches it</em></span></div>
  <div class="row"><span class="swatch part"></span><span>a box's empty band is the share of
    its symbols on unexplained islands</span></div>
</div>
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>codegraph · __HEADING__</title>
<style>
__CSS__
</style>
</head>
<body>
<header id="top">
  <div class="title">
    <strong>__HEADING__</strong>
    <span class="sub">codegraph __VERSION__ · __GENERATED__</span>
  </div>
  <div class="summary">__SUMMARY__</div>
  <div class="tools">
    <input id="search" type="search" placeholder="find a symbol" spellcheck="false">
    <button id="fit" title="Frame everything (Home)">fit</button>
    <button id="clear" title="Drop the highlight (Esc)">clear</button>
    <button id="legend-toggle" title="Show or hide the legend">legend</button>
  </div>
</header>
<main>
  <canvas id="canvas"></canvas>
  <div id="crumb"></div>
  <div id="hlbar" hidden></div>
  <div id="tip" hidden></div>
  <aside id="panel" hidden></aside>
  <div id="legend">__LEGEND__</div>
  <div id="status">decompressing…</div>
</main>
<script type="text/plain" id="graph-data">__GRAPH__</script>
<script type="text/plain" id="source-data">__SOURCE__</script>
<script>
__JS__
</script>
</body>
</html>
"""

__all__ = ["render_html"]
