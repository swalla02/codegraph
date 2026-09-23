/* The view, drawn. Inlined into the generated page by render.py.
 *
 * Semantic zoom over a treemap of the directory tree: the layout arrives
 * as rectangles (model.py computes it), and this decides, per frame, what
 * is legible at the current scale. A box is drawn when it is big enough to
 * see and descended into when it is big enough to hold its children, so
 * the reader gets packages, then modules, then symbols, then source, in
 * one view that never changes what it is.
 *
 * Canvas rather than SVG. django is 47,451 boxes and 94,831 drawn edges,
 * and an SVG of that is 142,000 DOM nodes whose attributes the browser
 * restyles on every pan. The canvas draws only what passes the level of
 * detail -- a few thousand rectangles -- and redraws in a couple of
 * milliseconds. Nothing here needs a framework, and nothing here loads
 * one: the page is meant to open from a filesystem with no network.
 *
 * Three invariants, which are the point of the whole exercise (#60):
 *   - four edge kinds never read alike: hue AND dash, per kind;
 *   - three confidence tiers never read alike: width AND opacity;
 *   - an observed edge is the brightest thing on screen, because being
 *     watched running is the strongest evidence this graph holds.
 */

(function () {
  "use strict";

  var DIRECTORY = 0, FILE = 1, SYMBOL = 2;

  /* Level of detail, in CSS pixels. MIN_DRAW is the smallest box worth a
   * rectangle; CHILD_MIN is the size a box must reach before its children
   * are drawn instead of it, and so is what the word "zoom level" means
   * here -- there is no discrete level, only a size at which the next
   * layer of the tree becomes legible. */
  var MIN_DRAW = 1.5, CHILD_MIN = 26;
  /* Edges get their own, coarser level of detail. Boxes become legible
   * one at a time; a hundred thousand lines between them do not, and a
   * picture of every symbol-level call at once is the hairball #60 set
   * out to avoid. So an edge is drawn between the boxes that represent
   * its endpoints at EDGE_MIN rather than at CHILD_MIN: at the whole
   * repository you see module against module, and a file has to be wide
   * enough to read before its symbols carry their own lines. */
  var EDGE_MIN = 420;
  var LABEL_W = 48, LABEL_H = 11, MAX_LABELS = 500;
  var LABEL_FONT = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
  /* A cap on drawn edge bundles, with the overflow reported on screen
   * rather than dropped silently -- a picture that quietly shows half the
   * edges is the failure this tool exists to avoid.
   *
   * The cap is spent per (kind, tier) rather than on the heaviest bundles
   * overall, and that is not a detail. django's top-level directories are
   * nearly completely connected by CALLS, so a global top-N is entirely
   * CALLS at HIGH, and the three other kinds -- the ones a reader most
   * often came for -- disappear at exactly the zoom where the picture is
   * meant to be about structure. A quota each keeps all twelve
   * combinations on screen. */
  var MAX_BUNDLES = 1440;
  var BUNDLE_QUOTA = MAX_BUNDLES / 12;

  var TIER_WIDTH = [2.3, 1.5, 1.0];
  var TIER_ALPHA = [0.95, 0.58, 0.34];
  var KIND_DASH = [[], [7, 3], [2, 3], [1, 4]];

  var $ = function (id) { return document.getElementById(id); };
  var canvas = $("canvas"), ctx = canvas.getContext("2d");
  var css = getComputedStyle(document.documentElement);
  var COLOR = {
    bg: css.getPropertyValue("--bg").trim() || "#0d1017",
    fg: css.getPropertyValue("--fg").trim(),
    muted: css.getPropertyValue("--muted").trim(),
    edge: css.getPropertyValue("--edge-ui").trim(),
    accent: css.getPropertyValue("--accent").trim(),
    mainland: css.getPropertyValue("--mainland").trim(),
    archipelago: css.getPropertyValue("--archipelago").trim(),
    traced: css.getPropertyValue("--traced").trim()
  };
  var KIND_COLOR = [
    css.getPropertyValue("--calls").trim(),
    css.getPropertyValue("--inherits").trim(),
    css.getPropertyValue("--implements").trim(),
    css.getPropertyValue("--references").trim()
  ];

  var D = null;              // the decompressed payload
  var N = 0, E = 0;
  var rx, ry, rw, rh;        // world rectangles, one array each
  var btype, bend, bisland, bparent;
  var esrc, edst, ekind, etier, eobs;
  var symCount, unexplained, offMainland, tracedCount;
  var rep, repEdge, hl;
  var islandColors = [];
  var charWidth = 0, lastCrumb = "";
  /* Which kinds and tiers are drawn. Every one is on to begin with,
   * because the honest first sight of a repository includes all of it;
   * the legend doubles as the switch, so a reader who wants to see only
   * what INHERITS does, or only what the resolver is certain of, turns
   * the rest off in the same place the encoding is explained. */
  var kindOn = [1, 1, 1, 1], tierOn = [1, 1, 1];

  var view = { x: 0, y: 0, scale: 1 };   // world -> screen
  var W = 0, H = 0, dpr = 1;
  var drawn = { i: [], x: [], y: [], w: [], h: [], leaf: [] };
  var bundles = null, bundleOverflow = 0, bundleStamp = null;
  var derived = null;        // the LOW fan-out expanded for one selection
  var selected = -1, hovered = -1, hlActive = false, highlighted = [];
  var sourceText = null, sourcePending = null;

  /* ---- loading ---------------------------------------------------- */

  function inflate(id) {
    var text = $(id).textContent.trim();
    var raw = atob(text);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    if (typeof DecompressionStream === "undefined") {
      return Promise.reject(new Error(
        "this browser has no DecompressionStream; the page's data is gzipped inside it"));
    }
    var stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
    return new Response(stream).text().then(JSON.parse);
  }

  function boot() {
    inflate("graph-data").then(function (data) {
      D = data;
      prepare();
      wire();
      fit();
      $("status").className = "hidden";
      draw();
    }).catch(function (err) {
      var status = $("status");
      status.className = "error";
      status.textContent = "could not read this page's data: " + err.message;
    });
  }

  function prepare() {
    var box = D.box;
    N = box.type.length;
    rx = new Float64Array(N); ry = new Float64Array(N);
    rw = new Float64Array(N); rh = new Float64Array(N);
    for (var i = 0; i < N; i++) {
      var r = box.rect[i];
      rx[i] = r[0]; ry[i] = r[1]; rw[i] = r[2]; rh[i] = r[3];
    }
    btype = Uint8Array.from(box.type);
    bend = Int32Array.from(box.end);
    bisland = Int32Array.from(box.island);

    /* Parents, from the preorder ranges. Every box's descendants are the
     * contiguous range (i, end[i]), which is what lets a frame map a
     * symbol to its currently drawn ancestor with a typed-array fill
     * instead of a walk. */
    bparent = new Int32Array(N).fill(-1);
    var stack = [];
    for (i = 0; i < N; i++) {
      while (stack.length && i >= bend[stack[stack.length - 1]]) stack.pop();
      bparent[i] = stack.length ? stack[stack.length - 1] : -1;
      stack.push(i);
    }

    var edges = D.edges;
    E = edges.src.length;
    esrc = Int32Array.from(edges.src); edst = Int32Array.from(edges.dst);
    ekind = Uint8Array.from(edges.kind); etier = Uint8Array.from(edges.tier);
    eobs = Uint8Array.from(edges.observed);

    /* Per-box aggregates, accumulated up the tree once. These are what
     * makes a package legible before any of its symbols are: how much
     * code it holds, how much of it is off the mainland, and how much of
     * it sits on an island nothing recognised explains. */
    symCount = new Int32Array(N);
    unexplained = new Int32Array(N);
    offMainland = new Int32Array(N);
    tracedCount = new Int32Array(N);
    for (i = 0; i < N; i++) {
      if (btype[i] !== SYMBOL) continue;
      symCount[i] = 1;
      var island = bisland[i];
      if (island > 0) offMainland[i] = 1;
      if (island >= 0 && !D.islands[island].explained) unexplained[i] = 1;
    }
    for (var t = 0; t < D.traced.length; t++) tracedCount[D.traced[t]] = 1;
    for (i = N - 1; i > 0; i--) {
      var p = bparent[i];
      if (p < 0) continue;
      symCount[p] += symCount[i];
      unexplained[p] += unexplained[i];
      offMainland[p] += offMainland[i];
      tracedCount[p] += tracedCount[i];
    }

    rep = new Int32Array(N);
    repEdge = new Int32Array(N);
    hl = new Uint8Array(N);
    islandColors = new Array(D.islands.length);
    if (D.highlight.length) {
      setHighlight(D.highlight, D.highlight_label || "report");
    }
  }

  /* ---- colour ------------------------------------------------------ */

  function islandColor(index) {
    if (index <= 0) return COLOR.mainland;
    if (!islandColors[index]) {
      /* Golden-angle hues so that adjacent island numbers -- which are
       * adjacent in size and nothing else -- never land on adjacent
       * colours. Saturation and lightness are fixed, so the only thing a
       * fill's hue means is "a different island". */
      var hue = (index * 137.508) % 360;
      islandColors[index] = "hsl(" + hue.toFixed(1) + ",46%,44%)";
    }
    return islandColors[index];
  }

  function mix(a, b, amount) {
    var pa = parseHex(a), pb = parseHex(b), out = "#";
    for (var i = 0; i < 3; i++) {
      var value = Math.round(pa[i] + (pb[i] - pa[i]) * amount);
      out += ("0" + value.toString(16)).slice(-2);
    }
    return out;
  }

  function parseHex(value) {
    var n = parseInt(value.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function fillFor(i) {
    if (btype[i] === SYMBOL) return islandColor(bisland[i]);
    var total = symCount[i];
    var share = total ? offMainland[i] / total : 0;
    return mix(COLOR.mainland, COLOR.archipelago, share);
  }

  /* ---- the frame --------------------------------------------------- */

  function resize() {
    dpr = window.devicePixelRatio || 1;
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    draw();
  }

  function fit() {
    view.scale = Math.min(W / D.extent[0], H / D.extent[1]) * 0.97;
    view.x = D.extent[0] / 2 - W / (2 * view.scale);
    view.y = D.extent[1] / 2 - H / (2 * view.scale);
  }

  function sx(worldX) { return (worldX - view.x) * view.scale; }
  function sy(worldY) { return (worldY - view.y) * view.scale; }

  function draw() {
    if (!D) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = COLOR.bg;
    ctx.fillRect(0, 0, W, H);
    drawn.i.length = drawn.x.length = drawn.y.length = 0;
    drawn.w.length = drawn.h.length = drawn.leaf.length = 0;
    rep.fill(-1);
    repEdge.fill(-1);
    walk(0);
    walkEdges(0);
    ctx.globalAlpha = 1;
    drawBoxes();
    aggregate();
    drawEdges();
    drawLabels();
    drawCrumb();
  }

  /* Assign every box a representative -- the ancestor that is actually
   * drawn at this zoom -- and collect what to paint. An off-screen box is
   * still given a representative (its edges may reach into view) but is
   * not descended into, which bounds the walk by the viewport rather than
   * by the repository. */
  function walk(i) {
    rep.fill(i, i, bend[i]);
    var x = sx(rx[i]), y = sy(ry[i]);
    var w = rw[i] * view.scale, h = rh[i] * view.scale;
    if (w < MIN_DRAW || h < MIN_DRAW) return;
    var near = x < W * 2 && y < H * 2 && x + w > -W && y + h > -H;
    if (!near) return;
    var onScreen = x < W && y < H && x + w > 0 && y + h > 0;
    var descend = w > CHILD_MIN && h > CHILD_MIN && i + 1 < bend[i];
    if (onScreen) {
      drawn.i.push(i); drawn.x.push(x); drawn.y.push(y);
      drawn.w.push(w); drawn.h.push(h); drawn.leaf.push(descend ? 0 : 1);
    }
    if (!descend) return;
    for (var child = i + 1; child < bend[i]; child = bend[child]) walk(child);
  }

  /* The same descent as `walk`, at the coarser edge threshold. Separate
   * rather than folded in, because the two answer different questions --
   * "what can I see" and "what can I follow" -- and a box can be legible
   * long before the lines between its children are. */
  function walkEdges(i) {
    repEdge.fill(i, i, bend[i]);
    var w = rw[i] * view.scale, h = rh[i] * view.scale;
    if (w < EDGE_MIN || h < EDGE_MIN || i + 1 >= bend[i]) return;
    var x = sx(rx[i]), y = sy(ry[i]);
    if (!(x < W * 2 && y < H * 2 && x + w > -W && y + h > -H)) return;
    for (var child = i + 1; child < bend[i]; child = bend[child]) walkEdges(child);
  }

  function drawBoxes() {
    ctx.lineWidth = 1;
    for (var k = 0; k < drawn.i.length; k++) {
      var i = drawn.i[k];
      /* The root is the repository itself. It is pickable and it anchors
       * the breadcrumb, but painting it would wash the whole canvas in one
       * colour and put its unexplained band across the foot of the screen,
       * where it would read as a property of the viewport. */
      if (i === 0) continue;
      var x = drawn.x[k], y = drawn.y[k], w = drawn.w[k], h = drawn.h[k];
      var dim = hlActive && !hl[i] ? 0.26 : 1;
      var total = symCount[i];
      /* The absence, drawn as one. The bottom band of every box is the
       * share of its symbols sitting on an island nothing recognised
       * explains, and it is left unpainted -- at symbol level a wholly
       * unexplained symbol is a hole, and at package level a package is
       * as hollow as its unexplained fraction. An `unexplained: 17` in a
       * text report is a footnote; this is the seventeen. */
      var hollow = total ? unexplained[i] / total : 0;
      var body = h * (1 - hollow);
      ctx.globalAlpha = dim * (btype[i] === SYMBOL ? 1 : btype[i] === FILE ? 0.62 : 0.4);
      ctx.fillStyle = fillFor(i);
      if (body > 0.4) ctx.fillRect(x, y, w, body);
      ctx.globalAlpha = dim * 0.55;
      ctx.strokeStyle = hollow > 0.001 ? COLOR.archipelago : COLOR.edge;
      if (w > 2.5 && h > 2.5) ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
      if (tracedCount[i] && w > 9 && h > 9) {
        /* A run was watched entering something in here. Node-level
         * provenance, in the same colour as an observed edge's halo. */
        ctx.globalAlpha = dim;
        ctx.fillStyle = COLOR.traced;
        var dot = Math.min(4, w / 5);
        ctx.fillRect(x + w - dot - 2, y + 2, dot, dot);
      }
      if (hlActive && hl[i] && btype[i] === SYMBOL) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = COLOR.fg;
        ctx.lineWidth = 1.6;
        ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
        ctx.lineWidth = 1;
      }
      if (i === selected) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = COLOR.accent;
        ctx.lineWidth = 2;
        ctx.strokeRect(x - 1, y - 1, w + 2, h + 2);
        ctx.lineWidth = 1;
      }
    }
    ctx.globalAlpha = 1;
  }

  /* Collapse every stored edge onto the pair of boxes that currently
   * represent its endpoints, keeping kind, tier and provenance apart.
   * Recomputed only when the representatives can have changed -- a pan
   * that does not cross the margin moves the endpoints and not the
   * bundles, so dragging re-projects a cached aggregation. */
  function aggregate() {
    var stamp = view.scale + ":" + Math.round(view.x / (W / view.scale) * 3) +
      ":" + Math.round(view.y / (H / view.scale) * 3) + ":" + (derived ? derived.stamp : "");
    if (bundles && stamp === bundleStamp) return;
    bundleStamp = stamp;
    var map = new Map();
    for (var e = 0; e < E; e++) {
      var a = repEdge[esrc[e]], b = repEdge[edst[e]];
      if (a < 0 || b < 0 || a === b) continue;
      var key = (a * N + b) * 24 + ekind[e] * 3 + etier[e] + (eobs[e] ? 12 : 0);
      map.set(key, (map.get(key) || 0) + 1);
    }
    if (derived) {
      /* The bare-name fan-out, expanded for one symbol only. It is not in
       * the stored graph and is LOW by construction, so it enters here
       * rather than in the payload's edges, and it is drawn in the tier
       * that says exactly that. */
      for (var d = 0; d < derived.pairs.length; d++) {
        var pair = derived.pairs[d];
        var pa = repEdge[pair[0]], pb = repEdge[pair[1]];
        if (pa < 0 || pb < 0 || pa === pb) continue;
        var dkey = (pa * N + pb) * 24 + pair[2] * 3 + 2;
        map.set(dkey, (map.get(dkey) || 0) + 1);
      }
    }
    var groups = [];
    for (var g = 0; g < 12; g++) groups.push([]);
    map.forEach(function (count, key) { groups[key % 24 % 12].push([key, count]); });
    var kept = [];
    bundleOverflow = 0;
    for (g = 0; g < 12; g++) {
      groups[g].sort(function (a, b) { return b[1] - a[1]; });
      bundleOverflow += Math.max(0, groups[g].length - BUNDLE_QUOTA);
      for (var j = 0; j < groups[g].length && j < BUNDLE_QUOTA; j++) kept.push(groups[g][j]);
    }
    /* Drawn weakest first, so the strongest tier and the heaviest bundles
     * are what survives the overlap rather than whatever happened to be
     * painted last. */
    kept.sort(function (a, b) {
      var ta = a[0] % 12 % 3, tb = b[0] % 12 % 3;
      if (ta !== tb) return tb - ta;
      return a[1] - b[1];
    });
    bundles = kept;
  }

  function drawEdges() {
    if (!bundles) return;
    ctx.lineCap = "round";
    /* A dense view is drawn fainter. Ten thousand lines at the opacity
     * that reads well for ten is a wash of colour that says nothing; this
     * keeps the tier ordering intact (HIGH is still the strongest line on
     * screen) while letting the boxes underneath stay visible. */
    var density = Math.max(0.16, Math.min(1, 450 / bundles.length));
    /* The focus is the box the pointer's target is DRAWN as at this zoom,
     * not the box under the pointer. At the whole repository an edge is a
     * relationship between two packages, so pointing anywhere inside one
     * asks about that package; zoom in and the same gesture asks about a
     * module, then about a symbol. */
    var under = hovered >= 0 ? hovered : selected;
    var focus = under >= 0 ? repEdge[under] : -1;
    for (var k = 0; k < bundles.length; k++) {
      var key = bundles[k][0], count = bundles[k][1];
      var meta = key % 24;
      var pair = (key - meta) / 24;
      var b = pair % N, a = (pair - b) / N;
      var observed = meta >= 12;
      var rest = meta % 12;
      var kind = (rest - rest % 3) / 3, tier = rest % 3;
      if (!kindOn[kind] || !tierOn[tier]) continue;

      /* Endpoints on the box borders, not at their centres. A container
       * at this zoom can be half the screen, and every line into it
       * meeting at one point turns a directory into a star whose shape
       * says nothing about the code. The border point moves with the
       * direction of the line, so a package's incoming edges arrive
       * spread along the side they actually come from. */
      var acx = rx[a] + rw[a] / 2, acy = ry[a] + rh[a] / 2;
      var bcx = rx[b] + rw[b] / 2, bcy = ry[b] + rh[b] / 2;
      var from = border(a, acx, acy, bcx, bcy), to = border(b, bcx, bcy, acx, acy);
      var ax = sx(from[0]), ay = sy(from[1]);
      var bx = sx(to[0]), by = sy(to[1]);
      if ((ax < 0 && bx < 0) || (ax > W && bx > W)) continue;
      if ((ay < 0 && by < 0) || (ay > H && by > H)) continue;

      var dim = density;
      if (hlActive) dim *= (hl[a] && hl[b]) ? 1 : 0.08;
      /* Focus and context. A repository whose packages are nearly
       * completely connected is a hairball however faintly it is drawn,
       * and no amount of opacity tuning makes 1,400 lines readable at
       * once. Pointing at a box is the reader's question -- "what does
       * THIS one touch" -- so the lines that touch it come forward and the
       * rest stay as context rather than disappearing. */
      if (focus >= 0) dim *= (a === focus || b === focus) ? 1.8 : 0.2;
      /* Width carries the tier and, on top of it, how many stored edges
       * this one line stands for -- logarithmically, so one bundle of a
       * thousand does not become a band across the picture. */
      var width = TIER_WIDTH[tier] * (1 + Math.min(1.1, Math.log(1 + count) / 4.5));
      ctx.strokeStyle = KIND_COLOR[kind];
      if (observed) {
        ctx.globalAlpha = Math.min(1, 0.3 * dim + 0.12);
        ctx.lineWidth = width * 3.4;
        ctx.setLineDash([]);
        line(ax, ay, bx, by);
      }
      ctx.globalAlpha = (observed ? 1 : TIER_ALPHA[tier]) * dim;
      ctx.lineWidth = width;
      ctx.setLineDash(KIND_DASH[kind]);
      line(ax, ay, bx, by);
      ctx.setLineDash([]);
      var length = Math.hypot(bx - ax, by - ay);
      if (length > 30 && dim > 0.45) arrow(ax, ay, bx, by, length, width);
    }
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
  }

  function border(i, cx, cy, tx, ty) {
    var dx = tx - cx, dy = ty - cy;
    if (!dx && !dy) return [cx, cy];
    var scaleX = dx ? (rw[i] / 2) / Math.abs(dx) : Infinity;
    var scaleY = dy ? (rh[i] / 2) / Math.abs(dy) : Infinity;
    var t = Math.min(1, Math.min(scaleX, scaleY));
    return [cx + dx * t, cy + dy * t];
  }

  function line(ax, ay, bx, by) {
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx, by);
    ctx.stroke();
  }

  function arrow(ax, ay, bx, by, length, width) {
    var ux = (bx - ax) / length, uy = (by - ay) / length;
    var size = Math.min(9, 3 + width * 1.6);
    var tipX = bx - ux * size * 0.8, tipY = by - uy * size * 0.8;
    ctx.beginPath();
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(tipX - ux * size + uy * size * 0.5, tipY - uy * size - ux * size * 0.5);
    ctx.lineTo(tipX - ux * size - uy * size * 0.5, tipY - uy * size + ux * size * 0.5);
    ctx.closePath();
    ctx.fill();
  }

  function drawLabels() {
    ctx.font = LABEL_FONT;
    ctx.textBaseline = "top";
    /* The label font is monospace, so a string's width is its length. That
     * is not a micro-optimisation: measuring instead, and truncating by
     * measuring one candidate at a time, cost 36 ms of a 42 ms frame on a
     * 37-file repository -- more than the boxes, the edge aggregation and
     * the lines put together. */
    if (!charWidth) charWidth = ctx.measureText("0").width;
    var written = 0;
    for (var k = 0; k < drawn.i.length && written < MAX_LABELS; k++) {
      var i = drawn.i[k], w = drawn.w[k], h = drawn.h[k];
      var leaf = drawn.leaf[k];
      if (leaf ? (w < LABEL_W || h < LABEL_H) : (w < 130 || h < 44)) continue;
      var name = D.box.name[i] || D.repo;
      var x = drawn.x[k] + 3, y = drawn.y[k] + 2;
      var text = fitText(name, w - 6);
      if (!text) continue;
      var width = text.length * charWidth;
      ctx.globalAlpha = hlActive && !hl[i] ? 0.4 : 0.92;
      ctx.fillStyle = "rgba(13,16,23,0.72)";
      ctx.fillRect(x - 2, y - 1, width + 4, 13);
      ctx.fillStyle = leaf ? COLOR.fg : COLOR.muted;
      ctx.fillText(text, x, y);
      written++;
    }
    ctx.globalAlpha = 1;
    if (bundleOverflow) {
      ctx.fillStyle = COLOR.archipelago;
      ctx.font = "11px ui-sans-serif, sans-serif";
      ctx.fillText(bundleOverflow + " further edge bundles not drawn — zoom in", 12, H - 20);
    }
  }

  function fitText(text, width) {
    var room = Math.floor(width / charWidth);
    if (room >= text.length) return text;
    if (room < 3) return "";
    return text.slice(0, room - 1) + "…";
  }

  function drawCrumb() {
    var target = hovered >= 0 ? hovered : selected;
    var level = "packages";
    var deepest = 0;
    for (var k = 0; k < drawn.i.length; k++) {
      if (drawn.leaf[k]) deepest = Math.max(deepest, btype[drawn.i[k]]);
    }
    if (deepest === SYMBOL) level = "symbols";
    else if (deepest === FILE) level = "modules";
    var text = '<span class="level">' + level + "</span>";
    if (target >= 0) text += " · " + ancestry(target);
    // Touching innerHTML forces a style recalculation, so only do it when
    // the line would actually say something different.
    if (text === lastCrumb) return;
    lastCrumb = text;
    $("crumb").innerHTML = text;
  }

  function ancestry(i) {
    var parts = [];
    for (var node = i; node >= 0; node = bparent[node]) {
      if (D.box.name[node]) parts.unshift(D.box.name[node]);
    }
    var last = parts.pop();
    return parts.join("/") + (parts.length ? "/" : "") + "<b>" + escapeHtml(last || "") + "</b>";
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* ---- picking and interaction ------------------------------------- */

  function pick(px, py) {
    for (var k = drawn.i.length - 1; k >= 0; k--) {
      if (px >= drawn.x[k] && px <= drawn.x[k] + drawn.w[k] &&
          py >= drawn.y[k] && py <= drawn.y[k] + drawn.h[k]) return drawn.i[k];
    }
    return -1;
  }

  function zoomTo(i, margin) {
    var pad = margin || 1.25;
    var scale = Math.min(W / (rw[i] * pad), H / (rh[i] * pad));
    view.scale = scale;
    view.x = rx[i] + rw[i] / 2 - W / (2 * scale);
    view.y = ry[i] + rh[i] / 2 - H / (2 * scale);
    draw();
  }

  function wire() {
    window.addEventListener("resize", resize);
    resize();

    var dragging = false, lastX = 0, lastY = 0, moved = 0;
    canvas.addEventListener("mousedown", function (event) {
      dragging = true; moved = 0;
      lastX = event.clientX; lastY = event.clientY;
      canvas.classList.add("dragging");
    });
    window.addEventListener("mouseup", function () {
      dragging = false;
      canvas.classList.remove("dragging");
    });
    window.addEventListener("mousemove", function (event) {
      var rect = canvas.getBoundingClientRect();
      var px = event.clientX - rect.left, py = event.clientY - rect.top;
      if (dragging) {
        moved += Math.abs(event.clientX - lastX) + Math.abs(event.clientY - lastY);
        view.x -= (event.clientX - lastX) / view.scale;
        view.y -= (event.clientY - lastY) / view.scale;
        lastX = event.clientX; lastY = event.clientY;
        draw();
        return;
      }
      if (px < 0 || py < 0 || px > W || py > H) { hideTip(); return; }
      var found = pick(px, py);
      if (found !== hovered) { hovered = found; draw(); }
      showTip(found, event.clientX, event.clientY);
    });
    canvas.addEventListener("wheel", function (event) {
      event.preventDefault();
      var rect = canvas.getBoundingClientRect();
      var px = event.clientX - rect.left, py = event.clientY - rect.top;
      var worldX = view.x + px / view.scale, worldY = view.y + py / view.scale;
      var factor = Math.pow(1.0016, -event.deltaY);
      var floor = Math.min(W / D.extent[0], H / D.extent[1]) / 4;
      view.scale = Math.max(floor, Math.min(view.scale * factor, 3000));
      view.x = worldX - px / view.scale;
      view.y = worldY - py / view.scale;
      draw();
    }, { passive: false });
    canvas.addEventListener("click", function (event) {
      if (moved > 4) return;
      var rect = canvas.getBoundingClientRect();
      select(pick(event.clientX - rect.left, event.clientY - rect.top));
    });
    canvas.addEventListener("dblclick", function (event) {
      var rect = canvas.getBoundingClientRect();
      var found = pick(event.clientX - rect.left, event.clientY - rect.top);
      if (found >= 0) zoomTo(found);
    });

    $("fit").addEventListener("click", function () { fit(); draw(); });
    $("clear").addEventListener("click", clearHighlight);
    $("legend-toggle").addEventListener("click", function () {
      $("legend").classList.toggle("hidden");
    });
    $("legend").querySelectorAll("[data-kind],[data-tier]").forEach(function (row) {
      row.classList.add("switchable");
      row.addEventListener("click", function () {
        var kind = row.getAttribute("data-kind"), tier = row.getAttribute("data-tier");
        if (kind !== null) kindOn[+kind] = kindOn[+kind] ? 0 : 1;
        if (tier !== null) tierOn[+tier] = tierOn[+tier] ? 0 : 1;
        row.classList.toggle("off");
        bundleStamp = null;
        draw();
      });
    });
    $("search").addEventListener("input", function (event) {
      search(event.target.value.trim());
    });
    window.addEventListener("keydown", function (event) {
      if (event.target.tagName === "INPUT") return;
      if (event.key === "Escape") clearHighlight();
      if (event.key === "Home") { fit(); draw(); }
    });
  }

  function showTip(i, clientX, clientY) {
    var tip = $("tip");
    if (i < 0) { tip.hidden = true; return; }
    var island = bisland[i] >= 0 ? D.islands[bisland[i]] : null;
    var lines = [];
    lines.push('<span class="n">' + escapeHtml(D.box.name[i] || D.repo) + "</span>");
    if (btype[i] === SYMBOL) {
      lines.push('<span class="m">' + escapeHtml(D.box.kind[i]) + " · " +
        D.box.line[i][0] + "–" + D.box.line[i][1] + "</span>");
    } else {
      lines.push('<span class="m">' + symCount[i] + " symbols · " +
        unexplained[i] + " unexplained</span>");
    }
    if (island) {
      lines.push('<span class="m">island ' + bisland[i] + " · " + island.size +
        " member(s) · " + (island.explained
          ? escapeHtml((island.found.join(", ") || "traced")) : "unexplained") + "</span>");
    }
    tip.innerHTML = lines.join("<br>");
    tip.hidden = false;
    var box = tip.getBoundingClientRect();
    var left = Math.min(clientX + 14, window.innerWidth - box.width - 8);
    var top = Math.min(clientY + 16, window.innerHeight - box.height - 8);
    tip.style.left = left + "px";
    tip.style.top = (top - 52) + "px";
  }

  function hideTip() { $("tip").hidden = true; hovered = -1; }

  /* ---- highlight --------------------------------------------------- */

  function setHighlight(indices, label) {
    hl.fill(0);
    for (var k = 0; k < indices.length; k++) {
      // Up the tree as well, so a package containing a hit stays lit while
      // the reader is still too far out to see the symbol itself.
      for (var node = indices[k]; node >= 0; node = bparent[node]) hl[node] = 1;
    }
    hlActive = indices.length > 0;
    highlighted = indices.slice();
    bundleStamp = null;
    var bar = $("hlbar");
    bar.hidden = !hlActive;
    if (!hlActive) return;
    bar.innerHTML = "showing <b>" + escapeHtml(label) + "</b> · " + indices.length +
      ' symbol(s) <button data-act="frame">frame</button>' +
      '<button data-act="drop">drop</button>';
    bar.querySelectorAll("button").forEach(function (button) {
      button.addEventListener("click", function () {
        if (button.getAttribute("data-act") === "frame") frameHighlight();
        else clearHighlight();
      });
    });
  }

  /* Put the whole answer on screen without leaving the picture. The point
   * of reading an `impact` result here rather than in a terminal is that
   * it keeps its surroundings, so this frames the hits and everything
   * between them rather than drawing them on their own. */
  function frameHighlight() {
    if (!highlighted.length) return;
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (var k = 0; k < highlighted.length; k++) {
      var i = highlighted[k];
      x0 = Math.min(x0, rx[i]); y0 = Math.min(y0, ry[i]);
      x1 = Math.max(x1, rx[i] + rw[i]); y1 = Math.max(y1, ry[i] + rh[i]);
    }
    var pad = Math.max((x1 - x0), (y1 - y0)) * 0.08 + 1;
    x0 -= pad; y0 -= pad; x1 += pad; y1 += pad;
    view.scale = Math.min(W / (x1 - x0), H / (y1 - y0));
    view.x = (x0 + x1) / 2 - W / (2 * view.scale);
    view.y = (y0 + y1) / 2 - H / (2 * view.scale);
    draw();
  }

  function clearHighlight() {
    hl.fill(0);
    hlActive = false;
    highlighted = [];
    derived = null;
    selected = -1;
    bundleStamp = null;
    $("search").value = "";
    $("panel").hidden = true;
    $("hlbar").hidden = true;
    draw();
  }

  function search(query) {
    if (!query) {
      hl.fill(0);
      hlActive = false;
      highlighted = [];
      bundleStamp = null;
      $("hlbar").hidden = true;
      draw();
      return;
    }
    var lowered = query.toLowerCase();
    var found = [];
    for (var i = 0; i < N && found.length < 500; i++) {
      if (btype[i] !== SYMBOL) continue;
      if (D.box.name[i].toLowerCase().indexOf(lowered) >= 0) found.push(i);
    }
    setHighlight(found, found.length + ' symbol(s) matching "' + query + '"');
    draw();
  }

  /* ---- the panel --------------------------------------------------- */

  function neighbours(i) {
    var into = [], out = [];
    for (var e = 0; e < E; e++) {
      if (edst[e] === i) into.push(e);
      else if (esrc[e] === i) out.push(e);
    }
    return { into: into, out: out };
  }

  function derivedCallers(i) {
    var names = D.fanout.reached[String(i)];
    if (!names) return null;
    var pairs = [], seen = new Set();
    for (var k = 0; k < names.length; k++) {
      var index = names[k];
      addPairs(D.fanout.from[index], 0);
      addPairs(D.fanout.base[index], 1);
    }
    function addPairs(list, kind) {
      if (!list) return;
      for (var j = 0; j < list.length; j++) {
        var key = list[j] + ":" + kind;
        if (seen.has(key)) continue;
        seen.add(key);
        pairs.push([list[j], i, kind]);
      }
    }
    return { pairs: pairs, stamp: "derived" + i, names: names.map(function (index) {
      return D.fanout.name[index];
    }) };
  }

  function select(i) {
    selected = i;
    derived = null;
    var panel = $("panel");
    if (i < 0) { panel.hidden = true; draw(); return; }
    panel.hidden = false;
    panel.innerHTML = describe(i);
    wirePanel(i);
    draw();
  }

  function describe(i) {
    var out = ['<button class="close" data-act="close">close</button>'];
    out.push("<h3>" + escapeHtml(D.box.name[i] || D.repo) + "</h3>");
    if (D.box.id[i]) out.push('<div class="id">' + escapeHtml(D.box.id[i]) + "</div>");
    else out.push('<div class="id">' + escapeHtml(pathOf(i)) + "/</div>");

    out.push("<dl>");
    if (btype[i] === SYMBOL || btype[i] === FILE) {
      out.push("<dt>kind</dt><dd>" + escapeHtml(D.box.kind[i]) + "</dd>");
      out.push("<dt>lines</dt><dd>" + D.box.line[i][0] + "–" + D.box.line[i][1] + "</dd>");
    }
    out.push("<dt>symbols</dt><dd>" + symCount[i] + "</dd>");
    if (tracedCount[i]) {
      out.push('<dt>observed</dt><dd class="ok">' + tracedCount[i] +
        " entered by a recorded run</dd>");
    }
    out.push("</dl>");

    var island = bisland[i] >= 0 ? D.islands[bisland[i]] : null;
    if (island) {
      out.push("<h4>island " + bisland[i] + "</h4><dl>");
      out.push("<dt>size</dt><dd>" + island.size + " symbol(s)</dd>");
      out.push("<dt>explained by</dt><dd>" + (island.explained
        ? escapeHtml(island.found.join(", ") ||
            (island.traced ? "traced (" + island.traced + " seen running)" : "NETWORK boundary"))
        : '<span class="gap">nothing this tool recognises</span>') + "</dd>");
      out.push("<dt>not found</dt><dd>" +
        escapeHtml(island.missing.join(", ") || "none") + "</dd>");
      if (island.boundary.length) {
        out.push("<dt>boundary</dt><dd>" + escapeHtml(island.boundary.join(", ")) + "</dd>");
      }
      out.push("</dl>");
      if (!island.explained) {
        out.push('<p class="gap">An unexplained island is a statement about this tool, ' +
          "not about the code: no resolved call, and none of the mechanisms above. " +
          "Do not read it as dead code.</p>");
      }
    }

    var counts = D.unresolved[String(i)];
    out.push("<h4>references with no edge</h4>");
    if (!counts) out.push("<p>none recorded in this body.</p>");
    else {
      out.push("<dl>");
      for (var r = 0; r < D.reasons.length; r++) {
        if (!counts[r]) continue;
        var cls = D.reasons[r] === "unknown" ? ' class="gap"' : "";
        out.push("<dt" + cls + ">" + D.reasons[r] + "</dt><dd>" + counts[r] + "</dd>");
      }
      out.push("</dl>");
    }

    var near = neighbours(i);
    out.push("<h4>edges</h4><dl>");
    out.push("<dt>into it</dt><dd>" + near.into.length + "</dd>");
    out.push("<dt>out of it</dt><dd>" + near.out.length + "</dd>");
    out.push("</dl>");
    out.push(edgeList(near.into, esrc, "callers and subclasses"));
    out.push(edgeList(near.out, edst, "what it reaches"));

    var fan = derivedCallers(i);
    if (fan && fan.pairs.length) {
      out.push("<h4>derived, LOW</h4>");
      out.push("<p>" + fan.pairs.length + " ambiguous reference(s) to <code>" +
        escapeHtml(fan.names.join("</code>, <code>")) +
        "</code> could mean this symbol. Not stored as edges — " +
        "<code>ambiguity.py</code> expands them on demand, and so does this.</p>");
    }

    out.push('<div class="actions">');
    out.push('<button data-act="zoom">zoom to</button>');
    out.push('<button data-act="callers">highlight dependents</button>');
    if (fan && fan.pairs.length) out.push('<button data-act="derived">draw LOW fan-out</button>');
    if (D.box.path[i]) out.push('<button data-act="source">source</button>');
    out.push("</div>");
    out.push('<div id="source-slot"></div>');
    return out.join("");
  }

  function edgeList(list, endpoint, title) {
    if (!list.length) return "";
    var out = ["<h4>" + title + "</h4><ul>"];
    for (var k = 0; k < Math.min(list.length, 12); k++) {
      var e = list[k], other = endpoint[e];
      out.push("<li>" + escapeHtml(D.box.id[other] || D.box.path[other] || "?") +
        ' <span class="m">' + D.kinds[ekind[e]] + " · " + D.tiers[etier[e]] +
        (eobs[e] ? " · observed" : "") + "</span></li>");
    }
    if (list.length > 12) out.push("<li>… " + (list.length - 12) + " more</li>");
    out.push("</ul>");
    return out.join("");
  }

  function pathOf(i) {
    var parts = [];
    for (var node = i; node >= 0; node = bparent[node]) {
      if (D.box.name[node]) parts.unshift(D.box.name[node]);
    }
    return parts.join("/");
  }

  function wirePanel(i) {
    var panel = $("panel");
    panel.querySelectorAll("button").forEach(function (button) {
      button.addEventListener("click", function () {
        var act = button.getAttribute("data-act");
        if (act === "close") { panel.hidden = true; selected = -1; draw(); }
        else if (act === "zoom") zoomTo(i);
        else if (act === "callers") {
          var near = neighbours(i);
          var found = [i];
          for (var k = 0; k < near.into.length; k++) found.push(esrc[near.into[k]]);
          setHighlight(found, found.length + " dependent(s) of " + D.box.name[i]);
          draw();
        } else if (act === "derived") {
          derived = derivedCallers(i);
          bundleStamp = null;
          draw();
        } else if (act === "source") showSource(i);
      });
    });
  }

  function showSource(i) {
    var slot = $("source-slot");
    slot.innerHTML = "<p>reading source…</p>";
    loadSource().then(function (files) {
      var text = files[D.box.path[i]];
      if (text === undefined) {
        slot.innerHTML = "<p>source was not embedded in this file (see " +
          "<code>--no-source</code>).</p>";
        return;
      }
      var all = text.split("\n");
      var from = Math.max(1, D.box.line[i][0]);
      var to = Math.min(all.length, D.box.line[i][1] || all.length);
      if (btype[i] === FILE) { from = 1; to = Math.min(all.length, 400); }
      var out = [];
      for (var n = from; n <= to; n++) {
        out.push('<span class="ln">' + String(n).padStart(5, " ") + "</span>  " +
          escapeHtml(all[n - 1] || ""));
      }
      slot.innerHTML = "<h4>source</h4><pre>" + out.join("\n") + "</pre>";
    });
  }

  function loadSource() {
    if (sourceText) return Promise.resolve(sourceText);
    if (!sourcePending) {
      sourcePending = inflate("source-data").then(function (files) {
        sourceText = files;
        return files;
      });
    }
    return sourcePending;
  }

  boot();
})();
