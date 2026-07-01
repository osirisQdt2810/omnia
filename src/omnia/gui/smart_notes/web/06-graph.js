
  /**
   * Smart Notes config page — the field dependency-graph canvas (a MIDDLE fragment of the page
   * IIFE; it neither opens nor closes it). Renders the Python-laid-out graph as ONE pannable /
   * zoomable SVG viewport and edits edges: drag from a node's BORDER → another node adds a hard
   * edge, clicking an edge toggles hard↔soft, and Delete removes the selected one. Layout (pixel
   * x/y + bounds) is ALWAYS computed server-side (the `graph_recompute` op), so this fragment
   * never re-implements longest-path; it draws, pans/zooms, and mutates each row's `depends_on`
   * (the single source of truth `collectRows` reads, so the existing Save persists it). Node MOVES
   * persist per note type: a live drag writes `posOverride`, committed on drop into `savedPositions`
   * (seeded from the graph's `node_positions`) and sent back with recompute/save so a moved node
   * survives tab switch AND Save. A locked field can't be edited from the graph (its edges are
   * guarded; a hover unlock control flips its row lock off).
   *
   * CSP-safe: vanilla SVG + CSS only, no external libs. NO SVG <filter> drop-shadows and NO
   * overflow:auto scroller — both blank the page on QtWebEngine/macOS Metal; depth/pan/zoom use
   * stroke/opacity/gradients and a single <g> transform instead. Hoisted `function seedGraph` is
   * called by applyLoad in 05-handlers.js; hoisted `function collectPositions` is called by the
   * Save handler in 05-handlers.js (function declarations hoist across the whole IIFE).
   */

  const SVGNS = "http://www.w3.org/2000/svg";
  const graphSvg = document.getElementById("sn-graph-svg");
  const graphToast = document.getElementById("sn-graph-toast");
  const graphWarn = document.getElementById("sn-graph-warn");
  const viewFieldsBtn = document.getElementById("sn-view-fields");
  const viewGraphBtn = document.getElementById("sn-view-graph");
  const fieldsView = document.getElementById("sn-fields-view");
  const graphView = document.getElementById("sn-graph-view");
  const nodeTip = document.getElementById("sn-node-tip");

  // Graph→prompt diff popover (Feature 2) element handles.
  const diffPop = document.getElementById("sn-diff-pop");
  const diffTitle = document.getElementById("sn-diff-title");
  const diffClose = document.getElementById("sn-diff-close");
  const diffNotice = document.getElementById("sn-diff-notice");
  const diffOld = document.getElementById("sn-diff-old");
  const diffNew = document.getElementById("sn-diff-new");
  const diffErr = document.getElementById("sn-diff-err");
  const diffImprove = document.getElementById("sn-diff-improve");
  const diffDiscard = document.getElementById("sn-diff-discard");
  const diffApply = document.getElementById("sn-diff-apply");

  // Fallback node box size (Python sends w/h per node, but a dangling/legacy payload may not).
  const NODE_W = 180;
  const NODE_H = 46;
  const MOVE_THRESHOLD = 4; // px a body-drag must travel before it's a MOVE (else a click)
  const TEXT_HIT_PAD = 6; // px grace around a node's LABEL: over it = MOVE, outside = CONNECT + tip
  const TIP_DELAY = 250; // ms hover before the prompt tooltip appears
  const MIN_K = 0.3;
  const MAX_K = 2.2;
  const HARD_COLOR = "#e5484d";
  const SOFT_COLOR = "#30a46c";

  let graphData = {nodes: [], edges: [], bounds: {width: 0, height: 0}};
  let graphVisible = false;
  let selectedEdge = null; // {src, dst, derived} of the currently-selected edge
  let posOverride = {}; // name(lower) -> {x, y} LIVE move overrides (committed to savedPositions on drop)
  // Persistent user-pinned node positions (name(lower) -> {x, y, name}); seeded from the graph's
  // node_positions and sent back on recompute/save so a moved node survives tab switch + Save.
  let savedPositions = {};
  let tipTimer = 0; // pending hover-tooltip show delay timer
  let tipHideTimer = 0; // pending tooltip hide (grace delay so the cursor can reach the tooltip)
  let tipNode = ""; // node name the tooltip is currently scheduled/shown for (the non-text zone)

  // The graph→prompt sync baseline: a snapshot of each row's depends_on (the "last synced"
  // edge state). Taken on load, on save, and after a Feature-1 deps apply; Save
  // diffs the CURRENT rows against it to find each changed dependent node. {field(lower):
  // [{field, kind}]}.
  let lastSyncedDeps = {};
  // The active diff-popover queue context (null when idle): the topo-ordered list of changed
  // nodes still to process, the running synced count, and the open popover's field.
  let syncQueue = null;
  // Client-side pending edge deletions: keys "<dstLower>|<srcLower>" → true. An edge the user
  // deleted that is STILL derivable from the dependent's prompt ({{ref}}), so graphData.edges
  // still returns it. Pending until Save rewrites the prompt to drop the reference; used to hide
  // the edge immediately and to queue a purely-derived delete for reconciliation.
  let removedEdges = {};

  /** The removedEdges key for an edge (case-insensitive on both endpoints). */
  function edgeRemKey(dst, src) {
    return (dst || "").toLowerCase() + "|" + (src || "").toLowerCase();
  }

  /** Whether the edge dst←src is pending deletion. */
  function isEdgeRemoved(dst, src) {
    return !!removedEdges[edgeRemKey(dst, src)];
  }

  /** Drop every pending deletion targeting `field` (its prompt now reflects them, or it was skipped). */
  function clearRemovedFor(field) {
    const p = (field || "").toLowerCase() + "|";
    Object.keys(removedEdges).forEach(function (k) {
      if (k.indexOf(p) === 0) {
        delete removedEdges[k];
      }
    });
  }

  // Viewport transform applied to the single <g id="sn-graph-vp">.
  let view = {tx: 0, ty: 0, k: 1};
  let viewport = null; // the <g> all nodes/edges live inside

  // Active gesture (at most one): pan, connect (edge create), or move (node drag).
  let pan = null; // {x0, y0, tx0, ty0}
  let connect = null; // {src, line}
  let move = null; // {name, started, sx, sy, ox, oy, group}

  /** Create an SVG element. */
  function svgEl(tag) {
    return document.createElementNS(SVGNS, tag);
  }

  /**
   * Store the server-laid-out graph (nodes carry pixel x/y/w/h + a top-level bounds) and redraw
   * if the view is open. Clears the LIVE (in-progress drag) overrides and re-seeds the PERSISTENT
   * pinned positions from the graph's node_positions — the moved nodes the user has committed and
   * saved.
   * @param {?Object} graph {nodes:[{name,is_base,generatable,locked,column,row,x,y,w,h,lane}], edges:[...], bounds:{width,height}, node_positions:{name:[x,y]}}.
   */
  function seedGraph(graph) {
    graphData =
      graph && graph.nodes
        ? graph
        : {nodes: [], edges: [], bounds: {width: 0, height: 0}};
    posOverride = {};
    // A freshly-loaded note type has no pending deletions (its rows already match their prompts).
    removedEdges = {};
    seedSavedPositions(graphData.node_positions);
    // A freshly-loaded note type IS the synced baseline (the rows already match their prompts).
    snapshotSyncedDeps();
    if (graphVisible) {
      renderGraph();
      fitView();
    }
  }

  /**
   * Seed the persistent pinned-position map from a graph payload's node_positions ({name: [x,y]},
   * original case). Stored keyed by lower-case name but keeping the original case so
   * collectPositions can emit the config-facing map.
   * @param {?Object} positions The graph payload's node_positions map (or null/absent).
   */
  function seedSavedPositions(positions) {
    savedPositions = {};
    const map = positions || {};
    Object.keys(map).forEach(function (name) {
      const p = map[name];
      if (Array.isArray(p) && p.length === 2) {
        savedPositions[name.toLowerCase()] = {x: p[0], y: p[1], name: name};
      }
    });
  }

  /**
   * The user-pinned node positions to persist ({name: [x, y]}, original case) — sent with every
   * graph_recompute and the Save op so a moved node survives tab switch AND Save. HOISTED so the
   * Save handler in 05-handlers.js (concatenated before this file) can call it at click time.
   * @return {!Object}
   */
  function collectPositions() {
    const out = {};
    Object.keys(savedPositions).forEach(function (lc) {
      const p = savedPositions[lc];
      out[p.name] = [p.x, p.y];
    });
    return out;
  }

  /**
   * Snapshot every row's CURRENT depends_on as the graph→prompt sync baseline. Called whenever
   * the rows and their prompts are known to be in sync: on load, on save, and after a Feature-1
   * (prompt→graph) deps apply. Stored as {field(lower): [{field, kind}]}.
   */
  function snapshotSyncedDeps() {
    lastSyncedDeps = {};
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      lastSyncedDeps[(tr.dataset.field || "").toLowerCase()] = readDependsOn(tr).map(
        function (d) {
          return {field: d.field, kind: d.kind};
        }
      );
    });
  }

  /**
   * Re-lay out the graph from the live rows when the Dependencies view is open (no-op when it
   * isn't). Used after Auto-prompt fills the rows' depends_on so new edges show immediately.
   */
  function refreshGraphIfOpen() {
    if (graphVisible) {
      recomputeGraph();
    }
  }

  /**
   * Apply a field's reconciled dependency edges (the prompt→graph sync, Feature 1) onto its row,
   * then recolour the graph. Writes the whole {field, kind, auto}[] list onto the row's
   * data-depends-on (the single source of truth collectRows/readDependsOn read), so a
   * classifier-written `auto` flag round-trips and isn't downgraded to a user edge on save.
   * @param {string} field The dependent field whose edges were reconciled.
   * @param {!Array<!Object>} deps The reconciled depends_on entries ({field, kind, auto}).
   */
  function applyFieldDeps(field, deps) {
    let row = null;
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      if ((tr.dataset.field || "").toLowerCase() === (field || "").toLowerCase()) {
        row = tr;
      }
    });
    if (!row) {
      return;
    }
    row.dataset.dependsOn = JSON.stringify(deps || []);
    // The prompt→graph sync just made this field's edges match its prompt, so re-baseline it —
    // Save must not then treat a classifier-written edge as a user edge change.
    lastSyncedDeps[(field || "").toLowerCase()] = (deps || []).map(function (d) {
      return {field: d.field, kind: d.kind};
    });
    refreshGraphIfOpen();
  }

  /**
   * Receive the prompt→graph classify result (off-thread): apply each field's reconciled edges.
   * Applied PER FIELD so concurrent classify ops touching different fields don't clobber one
   * another. An {error} payload (no field entries) surfaces on the footer; otherwise a brief
   * "Dependencies updated" status confirms the recolour.
   * @param {?(Array<!Object>|Object)} items [{field, depends_on}] on success, {error} on failure.
   */
  window.__snDepsResult = function (items) {
    if (!Array.isArray(items)) {
      setMsg((items && items.error) || "Could not update dependencies — see logs.", true);
      return;
    }
    items.forEach(function (item) {
      if (item && item.field) {
        applyFieldDeps(item.field, item.depends_on || []);
      }
    });
    setMsg("Dependencies updated.", false);
  };

  /** Briefly show a status message over the graph. */
  function graphToastMsg(text) {
    graphToast.textContent = text;
    graphToast.hidden = false;
    clearTimeout(graphToastMsg._t);
    graphToastMsg._t = setTimeout(function () {
      graphToast.hidden = true;
    }, 2800);
  }

  /** Switch between the Fields table and the Dependencies graph. */
  function switchView(toGraph) {
    hideNodeTip();
    graphVisible = toGraph;
    viewGraphBtn.classList.toggle("sn-view-active", toGraph);
    viewFieldsBtn.classList.toggle("sn-view-active", !toGraph);
    graphView.hidden = !toGraph;
    fieldsView.hidden = toGraph;
    if (toGraph) {
      recomputeGraph(); // always re-lay out from the live rows on open
    }
  }
  viewFieldsBtn.addEventListener("click", function () {
    switchView(false);
  });
  viewGraphBtn.addEventListener("click", function () {
    switchView(true);
  });

  /**
   * Ask the server to re-lay out the graph from the current rows (layout stays in Python). On a
   * cycle/other error the server returns {error}; we toast it and keep the last good graph.
   */
  function recomputeGraph() {
    send(
      "graph_recompute",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        rows: collectRows(),
        positions: collectPositions()
      },
      function (res) {
        if (res && res.error) {
          graphToastMsg(res.error);
          // An optimistic add/toggle that the server rejected (e.g. a cycle the JS precheck
          // missed) must not leave a dangling selection pointing at an edge that never landed.
          selectedEdge = null;
          renderGraph();
          return;
        }
        if (res && res.graph) {
          graphData = res.graph;
          // The pinned positions were echoed back baked into the fresh layout — re-seed from the
          // response and drop the LIVE drag overrides (now committed into savedPositions/graph).
          seedSavedPositions(res.graph.node_positions);
          posOverride = {};
        }
        renderGraph();
        fitView();
      }
    );
  }

  // --- node geometry (pixels from Python, optionally overridden by an in-progress move) -------
  function nodeByName(name) {
    const lc = (name || "").toLowerCase();
    for (let i = 0; i < graphData.nodes.length; i++) {
      if (graphData.nodes[i].name.toLowerCase() === lc) {
        return graphData.nodes[i];
      }
    }
    return null;
  }
  function nodeSize(node) {
    return {w: node && node.w ? node.w : NODE_W, h: node && node.h ? node.h : NODE_H};
  }
  function nodePos(node) {
    const lc = (node.name || "").toLowerCase();
    if (posOverride[lc]) {
      return {x: posOverride[lc].x, y: posOverride[lc].y};
    }
    if (savedPositions[lc]) {
      return {x: savedPositions[lc].x, y: savedPositions[lc].y};
    }
    return {x: node.x || 0, y: node.y || 0};
  }
  function isSelected(edge) {
    return (
      selectedEdge &&
      selectedEdge.src.toLowerCase() === edge.src.toLowerCase() &&
      selectedEdge.dst.toLowerCase() === edge.dst.toLowerCase()
    );
  }

  // --- rendering -------------------------------------------------------------------------
  function renderGraph() {
    while (graphSvg.firstChild) {
      graphSvg.removeChild(graphSvg.firstChild);
    }
    const nodes = graphData.nodes || [];
    const edges = graphData.edges || [];
    const bounds = graphData.bounds || {width: 0, height: 0};
    const w = Math.max(1, bounds.width || 1);
    const h = Math.max(1, bounds.height || 1);
    graphSvg.setAttribute("viewBox", "0 0 " + w + " " + h);

    graphSvg.appendChild(buildDefs());

    viewport = svgEl("g");
    viewport.setAttribute("id", "sn-graph-vp");
    applyViewTransform();
    graphSvg.appendChild(viewport);

    edges.forEach(function (e) {
      if (isEdgeRemoved(e.dst, e.src)) {
        return; // pending-deleted (still derivable from the prompt) — hide until Save rewrites it
      }
      const wrap = edgeGroup(e);
      if (wrap) {
        viewport.appendChild(wrap);
      }
    });
    nodes.forEach(function (n) {
      viewport.appendChild(nodeGroup(n));
    });
    if (graphWarn) {
      // The cyclic edges are flagged red (sn-edge-cycle); show the persistent banner so the user
      // knows generation/save need the loop broken (the graph still renders the whole cycle).
      graphWarn.hidden = !(graphData && graphData.has_cycle);
    }
  }

  function applyViewTransform() {
    if (viewport) {
      viewport.setAttribute(
        "transform",
        "translate(" + view.tx + "," + view.ty + ") scale(" + view.k + ")"
      );
    }
  }

  /** <defs>: arrowheads + linear gradients for each edge kind. */
  function buildDefs() {
    const defs = svgEl("defs");
    defs.appendChild(arrowMarker("sn-arrow-hard", HARD_COLOR));
    defs.appendChild(arrowMarker("sn-arrow-soft", SOFT_COLOR));
    defs.appendChild(edgeGradient("sn-grad-hard", "#ff6b6b", HARD_COLOR));
    defs.appendChild(edgeGradient("sn-grad-soft", "#4cc38a", SOFT_COLOR));
    return defs;
  }

  function edgeGradient(id, from, to) {
    const g = svgEl("linearGradient");
    g.setAttribute("id", id);
    g.setAttribute("x1", "0");
    g.setAttribute("y1", "0");
    g.setAttribute("x2", "1");
    g.setAttribute("y2", "0");
    const a = svgEl("stop");
    a.setAttribute("offset", "0");
    a.setAttribute("stop-color", from);
    const b = svgEl("stop");
    b.setAttribute("offset", "1");
    b.setAttribute("stop-color", to);
    g.appendChild(a);
    g.appendChild(b);
    return g;
  }

  function arrowMarker(id, color) {
    const m = svgEl("marker");
    m.setAttribute("id", id);
    m.setAttribute("viewBox", "0 0 10 10");
    m.setAttribute("refX", "9");
    m.setAttribute("refY", "5");
    m.setAttribute("markerWidth", "5");
    m.setAttribute("markerHeight", "5");
    m.setAttribute("orient", "auto-start-reverse");
    const p = svgEl("path");
    p.setAttribute("d", "M0,0 L10,5 L0,10 z");
    p.setAttribute("fill", color);
    m.appendChild(p);
    return m;
  }

  /** The centre point of a node's box in world coords. */
  function nodeCenter(node) {
    const p = nodePos(node);
    const s = nodeSize(node);
    return {x: p.x + s.w / 2, y: p.y + s.h / 2};
  }

  /**
   * The point on a node's border rect where the ray from its centre toward (tx, ty) exits — so an
   * edge attaches to the nearest border point instead of a fixed port.
   * @param {!Object} node The node.
   * @param {number} tx Target x (world coords).
   * @param {number} ty Target y (world coords).
   * @return {{x: number, y: number}}
   */
  function borderPoint(node, tx, ty) {
    const p = nodePos(node);
    const s = nodeSize(node);
    const cx = p.x + s.w / 2;
    const cy = p.y + s.h / 2;
    const dx = tx - cx;
    const dy = ty - cy;
    if (dx === 0 && dy === 0) {
      return {x: cx + s.w / 2, y: cy};
    }
    const tX = dx !== 0 ? s.w / 2 / Math.abs(dx) : Infinity;
    const tY = dy !== 0 ? s.h / 2 / Math.abs(dy) : Infinity;
    const t = Math.min(tX, tY);
    return {x: cx + dx * t, y: cy + dy * t};
  }

  /**
   * The S-curve `d` for an edge, attaching to each node's border point along the line between
   * their centres. Control points follow the dominant axis so a mostly-horizontal edge bends
   * horizontally and a mostly-vertical one bends vertically.
   */
  function edgeD(s, d) {
    const sc = nodeCenter(s);
    const dc = nodeCenter(d);
    const a = borderPoint(s, dc.x, dc.y);
    const b = borderPoint(d, sc.x, sc.y);
    if (Math.abs(b.x - a.x) >= Math.abs(b.y - a.y)) {
      const mx = (a.x + b.x) / 2;
      return (
        "M" + a.x + "," + a.y + " C" + mx + "," + a.y + " " +
        mx + "," + b.y + " " + b.x + "," + b.y
      );
    }
    const my = (a.y + b.y) / 2;
    return (
      "M" + a.x + "," + a.y + " C" + a.x + "," + my + " " +
      b.x + "," + my + " " + b.x + "," + b.y
    );
  }

  /**
   * Build one edge as a group of: a wide transparent hit-twin (easy clicking), an optional
   * translucent glow under the selected edge (no SVG filter), and the visible stroke.
   */
  function edgeGroup(e) {
    const s = nodeByName(e.src);
    const d = nodeByName(e.dst);
    if (!s || !d) {
      return null; // dangling edge (renamed/missing field) — skip silently
    }
    const dd = edgeD(s, d);
    const soft = e.kind === "soft";
    const sel = isSelected(e);
    const g = svgEl("g");
    g.setAttribute("class", "sn-edge-g");

    const hit = svgEl("path");
    hit.setAttribute("d", dd);
    hit.setAttribute("class", "sn-edge-hit");
    hit.addEventListener("click", function (ev) {
      ev.stopPropagation();
      onEdgeClick(e);
    });
    g.appendChild(hit);

    if (sel) {
      const glow = svgEl("path");
      glow.setAttribute("d", dd);
      glow.setAttribute("class", "sn-edge-glow");
      glow.setAttribute("stroke", soft ? SOFT_COLOR : HARD_COLOR);
      g.appendChild(glow);
    }

    const path = svgEl("path");
    path.setAttribute("d", dd);
    path.setAttribute(
      "class",
      "sn-edge " +
        (soft ? "sn-edge-soft" : "sn-edge-hard") +
        (sel ? " sn-edge-selected" : "") +
        (e.cycle ? " sn-edge-cycle" : "")
    );
    path.setAttribute("stroke", "url(#" + (soft ? "sn-grad-soft" : "sn-grad-hard") + ")");
    if (e.derived) {
      path.setAttribute("stroke-dasharray", "7,5");
    }
    path.setAttribute(
      "marker-end",
      "url(#" + (soft ? "sn-arrow-soft" : "sn-arrow-hard") + ")"
    );
    path.addEventListener("click", function (ev) {
      ev.stopPropagation();
      onEdgeClick(e);
    });
    g.appendChild(path);
    return g;
  }

  function nodeGroup(n) {
    const g = svgEl("g");
    g.setAttribute(
      "class",
      "sn-node" +
        (n.is_base ? " sn-node-base" : "") +
        (n.generatable ? "" : " sn-node-ng") +
        (n.locked ? " sn-node-locked" : "")
    );
    g.setAttribute("data-node", n.name);
    const p = nodePos(n);
    const sz = nodeSize(n);
    g.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");

    const rect = svgEl("rect");
    rect.setAttribute("x", "0");
    rect.setAttribute("y", "0");
    rect.setAttribute("width", String(sz.w));
    rect.setAttribute("height", String(sz.h));
    rect.setAttribute("rx", "11");
    rect.setAttribute("class", "sn-node-box");

    const text = svgEl("text");
    text.setAttribute("x", String(sz.w / 2));
    text.setAttribute("y", String(sz.h / 2));
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("dominant-baseline", "central");
    text.textContent = n.name.length > 22 ? n.name.slice(0, 21) + "…" : n.name;

    g.appendChild(rect);
    g.appendChild(text);
    if (n.locked) {
      appendLockControls(g, n, sz);
    }

    // Text-zone interaction model: the LABEL is the grab handle (drag it to MOVE the node), the
    // rest of the box is the CONNECT zone (drag to add an edge) and shows the prompt tooltip on
    // hover. So dragging a node no longer fights the tooltip, and reading the prompt no longer
    // fights a move. `text` is measured live (its width depends on the label).
    g.addEventListener("mousedown", function (ev) {
      if (ev.button !== 0) {
        return;
      }
      if (isOverText(text, ev)) {
        startMove(n.name, ev); // over the label → move the node
      } else {
        ev.preventDefault();
        ev.stopPropagation();
        hideNodeTip();
        startConnect(n.name, ev); // outside the label → drag to connect
      }
    });
    // Cursor + tooltip by zone: over the label → grab cursor, no tooltip; outside → crosshair and
    // the prompt tooltip (scheduled once per entry into the non-text zone, not reset each move).
    g.addEventListener("mousemove", function (ev) {
      if (isOverText(text, ev)) {
        g.style.cursor = "grab";
        if (tipNode === n.name) {
          hideNodeTip();
        }
      } else {
        g.style.cursor = "crosshair";
        if (tipNode !== n.name) {
          tipNode = n.name;
          scheduleNodeTip(n, g);
        }
      }
    });
    // Returning to the node cancels a pending hide; leaving it hides after the grace delay.
    g.addEventListener("mouseenter", cancelTipHide);
    g.addEventListener("mouseleave", scheduleTipHide);
    return g;
  }

  /**
   * Add a locked field's badge + a hover-revealed unlock control to its node group. The badge is
   * a small 🔒 near the top-right; the unlock control (a circle + 🔓, shown on hover via CSS)
   * flips the row's lock off when clicked.
   * @param {!SVGGElement} g The node group.
   * @param {!Object} n The node payload.
   * @param {!Object} sz The node box size ({w, h}).
   */
  function appendLockControls(g, n, sz) {
    const badge = svgEl("text");
    badge.setAttribute("x", String(sz.w - 14));
    badge.setAttribute("y", "13");
    badge.setAttribute("class", "sn-lock-badge");
    badge.setAttribute("pointer-events", "none");
    badge.textContent = "🔒";
    g.appendChild(badge);

    const unlock = svgEl("g");
    unlock.setAttribute("class", "sn-unlock-btn");
    unlock.setAttribute(
      "transform",
      "translate(" + (sz.w - 14) + ",13)"
    );
    const circle = svgEl("circle");
    circle.setAttribute("cx", "0");
    circle.setAttribute("cy", "0");
    circle.setAttribute("r", "11");
    circle.setAttribute("class", "sn-unlock-circle");
    const glyph = svgEl("text");
    glyph.setAttribute("x", "0");
    glyph.setAttribute("y", "0");
    glyph.setAttribute("text-anchor", "middle");
    glyph.setAttribute("dominant-baseline", "central");
    glyph.setAttribute("class", "sn-unlock-glyph");
    glyph.textContent = "🔓";
    unlock.appendChild(circle);
    unlock.appendChild(glyph);
    unlock.addEventListener("mousedown", function (ev) {
      ev.stopPropagation(); // don't start a move/connect gesture
    });
    unlock.addEventListener("click", function (ev) {
      ev.stopPropagation();
      unlockField(n.name);
    });
    g.appendChild(unlock);
  }

  // --- prompt tooltip (Feature 1) -------------------------------------------------------
  /**
   * Whether the pointer is over a node's LABEL (its ``<text>``), with a small grace pad. The label
   * is the move handle; everywhere else in the box connects + shows the tooltip.
   * @param {!SVGTextElement} textEl The node's label element.
   * @param {!MouseEvent} ev The pointer event.
   * @return {boolean}
   */
  function isOverText(textEl, ev) {
    const b = textEl.getBoundingClientRect();
    return (
      ev.clientX >= b.left - TEXT_HIT_PAD &&
      ev.clientX <= b.right + TEXT_HIT_PAD &&
      ev.clientY >= b.top - TEXT_HIT_PAD &&
      ev.clientY <= b.bottom + TEXT_HIT_PAD
    );
  }

  /** Schedule the hover prompt tooltip for a node after a short delay. */
  function scheduleNodeTip(n, g) {
    cancelTipHide(); // moving onto a node cancels a pending hide from the previous one
    clearTimeout(tipTimer);
    tipTimer = setTimeout(function () {
      showNodeTip(n, g);
    }, TIP_DELAY);
  }

  /**
   * Show the styled prompt tooltip for a node, positioned to the right of its screen rect (flipped
   * left when it would overflow), clamped within the window with an 8px margin. A long prompt is
   * capped to the available viewport height and scrolls inside the card (the card is interactive),
   * so it is never cut off by the window edge.
   * @param {!Object} n The node payload.
   * @param {!SVGGElement} g The node group.
   */
  function showNodeTip(n, g) {
    if (!nodeTip) {
      return;
    }
    cancelTipHide();
    const row = rowByField(n.name);
    const prompt = row ? row.dataset.prompt || "" : "";
    nodeTip.innerHTML =
      '<div class="sn-tip-name">' +
      esc(n.name) +
      "</div>" +
      (prompt
        ? esc(prompt)
        : '<span style="opacity:.6">(no prompt yet)</span>');
    nodeTip.hidden = false;

    const margin = 8;
    // Cap the card to the viewport (never taller than the space we allow) so a long prompt
    // scrolls INSIDE it instead of running off the bottom edge. Set inline so it always wins.
    const maxH = Math.min(window.innerHeight - 2 * margin, 460);
    nodeTip.style.maxHeight = maxH + "px";

    const r = g.getBoundingClientRect();
    const tw = nodeTip.offsetWidth || 340;
    const th = nodeTip.offsetHeight || 120; // already clamped by maxHeight
    let left = r.right + margin;
    if (left + tw > window.innerWidth - margin) {
      left = r.left - tw - margin; // flip to the node's left when it won't fit on the right
    }
    let top = r.top;
    left = Math.max(margin, Math.min(left, window.innerWidth - tw - margin));
    top = Math.max(margin, Math.min(top, window.innerHeight - th - margin));
    nodeTip.style.left = left + "px";
    nodeTip.style.top = top + "px";
  }

  /** Hide the prompt tooltip after a short grace delay (lets the cursor travel onto the card). */
  function scheduleTipHide() {
    clearTimeout(tipHideTimer);
    tipHideTimer = setTimeout(hideNodeTip, 160);
  }

  /** Cancel a pending hide (the cursor reached a node or the tooltip). */
  function cancelTipHide() {
    clearTimeout(tipHideTimer);
  }

  /** Hide the prompt tooltip now and cancel any pending show/hide. */
  function hideNodeTip() {
    clearTimeout(tipTimer);
    clearTimeout(tipHideTimer);
    tipNode = "";
    if (nodeTip) {
      nodeTip.hidden = true;
    }
  }

  // The tooltip is interactive (scrollable): keep it open while the cursor is over it, hide when
  // the cursor leaves it.
  if (nodeTip) {
    nodeTip.addEventListener("mouseenter", cancelTipHide);
    nodeTip.addEventListener("mouseleave", hideNodeTip);
  }

  /**
   * Whether a field is locked — the ROW's live lock state is the source of truth (the table owns
   * the lock), falling back to the node's `locked` flag when there is no row (e.g. the base node).
   * @param {string} name The field name.
   * @return {boolean}
   */
  function isFieldLocked(name) {
    const row = rowByField(name);
    if (row) {
      const lock = row.querySelector(".sn-lock");
      return !!(lock && lock.classList.contains("sn-locked"));
    }
    const node = nodeByName(name);
    return !!(node && node.locked);
  }

  /**
   * Unlock a field FROM THE GRAPH: flip its table row's lock cell off exactly as the row's own
   * lock click does (remove `sn-locked`, restore the 🔓 glyph, re-apply the row lock state), then
   * recompute so the node re-renders unlocked.
   * @param {string} name The field to unlock.
   */
  function unlockField(name) {
    const row = rowByField(name);
    if (!row) {
      return;
    }
    const lock = row.querySelector(".sn-lock");
    if (lock) {
      lock.classList.remove("sn-locked");
      lock.textContent = "🔓";
      applyLockState(row);
    }
    recomputeGraph();
  }

  // --- coordinate mapping ---------------------------------------------------------------
  /**
   * Map a mouse event to WORLD coords (the space nodes are laid out in). Composition order:
   * screen → SVG-viewBox space (via getBoundingClientRect + the viewBox scale) → world (undo the
   * viewport translate/scale). The drag-line endpoint and any geometry math must use this so they
   * never drift from elementFromPoint (which is already transform-aware).
   */
  function screenToWorld(ev) {
    const r = graphSvg.getBoundingClientRect();
    const vb = graphSvg.viewBox.baseVal;
    const sx = r.width ? vb.width / r.width : 1;
    const sy = r.height ? vb.height / r.height : 1;
    const vbx = (ev.clientX - r.left) * sx; // point in viewBox space
    const vby = (ev.clientY - r.top) * sy;
    return {x: (vbx - view.tx) / view.k, y: (vby - view.ty) / view.k};
  }
  function nodeNameAt(clientX, clientY) {
    let el = document.elementFromPoint(clientX, clientY);
    while (el && el !== document) {
      if (el.getAttribute && el.getAttribute("data-node")) {
        return el.getAttribute("data-node");
      }
      el = el.parentNode;
    }
    return null;
  }

  // --- fit / pan / zoom -----------------------------------------------------------------
  /** Frame the whole graph (from the Python bounds) within the SVG viewBox. */
  function fitView() {
    const bounds = graphData.bounds || {width: 0, height: 0};
    const bw = bounds.width || 0;
    const bh = bounds.height || 0;
    const vb = graphSvg.viewBox.baseVal;
    if (!bw || !bh || !vb.width || !vb.height) {
      view = {tx: 0, ty: 0, k: 1};
      applyViewTransform();
      return;
    }
    const k = clampK(Math.min(vb.width / bw, vb.height / bh, 1));
    view = {
      tx: (vb.width - bw * k) / 2,
      ty: (vb.height - bh * k) / 2,
      k: k
    };
    applyViewTransform();
  }
  function clampK(k) {
    return Math.max(MIN_K, Math.min(MAX_K, k));
  }

  // Pan = drag empty canvas. mousedown on the bare SVG (not a node/edge) starts it; the screen
  // delta is mapped through the viewBox scale so the canvas tracks the cursor 1:1.
  graphSvg.addEventListener("mousedown", function (ev) {
    if (ev.target !== graphSvg || ev.button !== 0) {
      return;
    }
    hideNodeTip();
    pan = {x0: ev.clientX, y0: ev.clientY, tx0: view.tx, ty0: view.ty};
    graphSvg.classList.add("sn-dragging");
    if (selectedEdge) {
      selectedEdge = null;
      renderGraph();
    }
    document.addEventListener("mousemove", onPanMove);
    document.addEventListener("mouseup", onPanEnd);
    ev.preventDefault();
  });
  function onPanMove(ev) {
    if (!pan) {
      return;
    }
    const r = graphSvg.getBoundingClientRect();
    const vb = graphSvg.viewBox.baseVal;
    const sx = r.width ? vb.width / r.width : 1;
    const sy = r.height ? vb.height / r.height : 1;
    view.tx = pan.tx0 + (ev.clientX - pan.x0) * sx;
    view.ty = pan.ty0 + (ev.clientY - pan.y0) * sy;
    applyViewTransform();
  }
  function onPanEnd(ev) {
    document.removeEventListener("mousemove", onPanMove);
    document.removeEventListener("mouseup", onPanEnd);
    graphSvg.classList.remove("sn-dragging");
    pan = null;
  }

  // Cursor-anchored wheel zoom: keep the world point under the cursor fixed across the scale.
  graphSvg.addEventListener(
    "wheel",
    function (ev) {
      ev.preventDefault();
      const r = graphSvg.getBoundingClientRect();
      const vb = graphSvg.viewBox.baseVal;
      const sx = r.width ? vb.width / r.width : 1;
      const sy = r.height ? vb.height / r.height : 1;
      const vbx = (ev.clientX - r.left) * sx;
      const vby = (ev.clientY - r.top) * sy;
      const k1 = clampK(view.k * (ev.deltaY < 0 ? 1.1 : 1 / 1.1));
      // world point under the cursor must stay put: vb = tx + world*k.
      const wx = (vbx - view.tx) / view.k;
      const wy = (vby - view.ty) / view.k;
      view.tx = vbx - wx * k1;
      view.ty = vby - wy * k1;
      view.k = k1;
      applyViewTransform();
    },
    {passive: false}
  );

  // --- editing: connect to create, move to reposition, click to toggle, Delete to remove ----
  function startConnect(srcName, ev) {
    hideNodeTip();
    connect = {src: srcName, line: svgEl("path")};
    connect.line.setAttribute("class", "sn-drag-line");
    // Start the drag line at the source border point nearest the cursor.
    const src = nodeByName(srcName);
    const c = screenToWorld(ev);
    const a = borderPoint(src, c.x, c.y);
    connect.line.setAttribute("d", "M" + a.x + "," + a.y + " L" + a.x + "," + a.y);
    viewport.appendChild(connect.line);
    document.addEventListener("mousemove", onConnectMove);
    document.addEventListener("mouseup", onConnectEnd);
  }
  function onConnectMove(ev) {
    if (!connect) {
      return;
    }
    const src = nodeByName(connect.src);
    const p = screenToWorld(ev);
    // The source anchor slides along its border toward the current cursor each move.
    const a = borderPoint(src, p.x, p.y);
    if (Math.abs(p.x - a.x) >= Math.abs(p.y - a.y)) {
      const mx = (a.x + p.x) / 2;
      connect.line.setAttribute(
        "d",
        "M" + a.x + "," + a.y + " C" + mx + "," + a.y + " " +
        mx + "," + p.y + " " + p.x + "," + p.y
      );
    } else {
      const my = (a.y + p.y) / 2;
      connect.line.setAttribute(
        "d",
        "M" + a.x + "," + a.y + " C" + a.x + "," + my + " " +
        p.x + "," + my + " " + p.x + "," + p.y
      );
    }
  }
  function onConnectEnd(ev) {
    document.removeEventListener("mousemove", onConnectMove);
    document.removeEventListener("mouseup", onConnectEnd);
    if (!connect) {
      return; // defensive: a reseed/recompute mid-drag may have cleared it
    }
    if (connect.line && connect.line.parentNode) {
      connect.line.parentNode.removeChild(connect.line);
    }
    const target = nodeNameAt(ev.clientX, ev.clientY);
    const src = connect.src;
    connect = null;
    if (target && src && target.toLowerCase() !== src.toLowerCase()) {
      addEdge(src, target);
    }
  }

  /**
   * mousedown on a node's INTERIOR: start a MOVE that only "arms" once the pointer travels past
   * MOVE_THRESHOLD; under threshold + a quick mouseup is treated as a click/select (no move). A
   * move writes the LIVE posOverride map, committed into savedPositions on drop so it persists.
   */
  function startMove(name, ev) {
    if (ev.button !== 0) {
      return;
    }
    ev.preventDefault();
    hideNodeTip();
    const n = nodeByName(name);
    const p = nodePos(n);
    move = {
      name: name,
      started: false,
      sx: ev.clientX,
      sy: ev.clientY,
      ox: p.x,
      oy: p.y
    };
    document.addEventListener("mousemove", onMoveDrag);
    document.addEventListener("mouseup", onMoveEnd);
  }
  function onMoveDrag(ev) {
    if (!move) {
      return;
    }
    const dpx = ev.clientX - move.sx;
    const dpy = ev.clientY - move.sy;
    if (!move.started && Math.hypot(dpx, dpy) < MOVE_THRESHOLD) {
      return; // still a potential click
    }
    move.started = true;
    graphSvg.classList.add("sn-dragging"); // suppress position transitions during a live drag
    // Convert the screen delta into world units via the current viewBox+zoom scale.
    const r = graphSvg.getBoundingClientRect();
    const vb = graphSvg.viewBox.baseVal;
    const sx = r.width ? vb.width / r.width : 1;
    const sy = r.height ? vb.height / r.height : 1;
    const x = move.ox + (dpx * sx) / view.k;
    const y = move.oy + (dpy * sy) / view.k;
    const lc = move.name.toLowerCase();
    posOverride[lc] = {x: x, y: y};
    redrawMoved(move.name);
  }
  function onMoveEnd(ev) {
    document.removeEventListener("mousemove", onMoveDrag);
    document.removeEventListener("mouseup", onMoveEnd);
    graphSvg.classList.remove("sn-dragging");
    // Commit the live drag override into the persistent pinned map so it survives tab switch +
    // Save; keep savedPositions (do NOT clear it — other nodes stay pinned).
    if (move && move.started) {
      const lc = move.name.toLowerCase();
      const live = posOverride[lc];
      if (live) {
        savedPositions[lc] = {x: live.x, y: live.y, name: move.name};
      }
    }
    move = null;
  }

  /** Re-render after a node move so its edges follow (cheap: full redraw, positions cached). */
  function redrawMoved(name) {
    renderGraph();
  }

  function addEdge(src, dst) {
    if (isFieldLocked(dst)) {
      graphToastMsg("“" + dst + "” is locked — unlock it to change its dependencies.");
      return;
    }
    if (dst.toLowerCase() === (baseSel.value || "").toLowerCase()) {
      graphToastMsg("The base field is the input — it can’t depend on another field.");
      return;
    }
    if (wouldCreateCycle(src, dst)) {
      graphToastMsg("That would create a cycle.");
      return;
    }
    if (!updateRowDep(dst, src, "hard")) {
      graphToastMsg("Add “" + dst + "” to the field list first.");
      return;
    }
    selectedEdge = {src: src, dst: dst, derived: false};
    recomputeGraph();
  }

  function onEdgeClick(e) {
    // Click toggles hard↔soft AND selects (so Delete can then remove it). Toggling a derived
    // edge writes an explicit override entry on the dependent's row.
    if (isFieldLocked(e.dst)) {
      graphToastMsg("“" + e.dst + "” is locked — unlock it to change its dependencies.");
      return;
    }
    const newKind = e.kind === "soft" ? "hard" : "soft";
    updateRowDep(e.dst, e.src, newKind);
    selectedEdge = {src: e.src, dst: e.dst, derived: e.derived};
    recomputeGraph();
  }

  function removeEdge(sel) {
    if (isFieldLocked(sel.dst)) {
      graphToastMsg("“" + sel.dst + "” is locked — unlock it to change its dependencies.");
      return;
    }
    // Deleting any edge just hides it; the prompt is reconciled later, at Save.
    updateRowDep(sel.dst, sel.src, null); // drop any explicit entry
    if (sel.derived) {
      // Still derivable from the prompt's {{ref}} — mark it pending so it stays hidden until Save
      // rewrites the prompt to drop the reference.
      removedEdges[edgeRemKey(sel.dst, sel.src)] = true;
    }
    selectedEdge = null;
    recomputeGraph();
  }

  document.addEventListener("keydown", function (e) {
    if (!graphVisible || !selectedEdge) {
      return;
    }
    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      removeEdge(selectedEdge);
    }
  });

  /**
   * Detect whether adding the (hard) edge src→dst would create a HARD cycle: true if dst can
   * already reach src by following existing HARD edges (a→b = a is a prerequisite of b). A new
   * graph edge is hard, and only hard edges deadlock — reaching src via soft edges is fine (that
   * cycle stays breakable), so soft edges are ignored here, mirroring the server's hard-only
   * validity check.
   */
  function wouldCreateCycle(src, dst) {
    const adj = {};
    (graphData.edges || []).forEach(function (e) {
      if (e.kind !== "hard") {
        return; // soft edges never form a real (blocking) cycle
      }
      const a = e.src.toLowerCase();
      (adj[a] = adj[a] || []).push(e.dst.toLowerCase());
    });
    const target = src.toLowerCase();
    const seen = {};
    const stack = [dst.toLowerCase()];
    while (stack.length) {
      const cur = stack.pop();
      if (cur === target) {
        return true;
      }
      if (seen[cur]) {
        continue;
      }
      seen[cur] = true;
      (adj[cur] || []).forEach(function (n) {
        stack.push(n);
      });
    }
    return false;
  }

  /**
   * Add/update/remove the dependent field's explicit dependency on `srcField`.
   * @param {string} dstField The dependent field (must have a table row).
   * @param {string} srcField The prerequisite field.
   * @param {?string} kind "hard"/"soft" to set, or null to remove the entry.
   * @return {boolean} False if the dependent has no row (e.g. the base field).
   */
  function updateRowDep(dstField, srcField, kind) {
    let row = null;
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      if ((tr.dataset.field || "").toLowerCase() === dstField.toLowerCase()) {
        row = tr;
      }
    });
    if (!row) {
      return false;
    }
    const deps = readDependsOn(row);
    const lc = srcField.toLowerCase();
    let entry = null;
    for (let i = 0; i < deps.length; i++) {
      if ((deps[i].field || "").toLowerCase() === lc) {
        entry = deps[i];
        break;
      }
    }
    if (kind === null) {
      const kept = deps.filter(function (d) {
        return (d.field || "").toLowerCase() !== lc;
      });
      row.dataset.dependsOn = JSON.stringify(kept);
    } else if (entry) {
      entry.kind = kind;
      row.dataset.dependsOn = JSON.stringify(deps);
    } else {
      deps.push({field: srcField, kind: kind});
      row.dataset.dependsOn = JSON.stringify(deps);
    }
    return true;
  }

  // --- graph→prompt sync (Feature 2): changed-edge detection + lazy ordered queue ----------

  /** The current effective incoming edges at a field from graphData ({src, kind}[], no self). */
  function incomingEdges(field) {
    const lc = (field || "").toLowerCase();
    const out = [];
    (graphData.edges || []).forEach(function (e) {
      if (
        (e.dst || "").toLowerCase() === lc &&
        (e.src || "").toLowerCase() !== lc &&
        !isEdgeRemoved(field, e.src) // a pending-deleted edge is not part of the intended set
      ) {
        out.push({field: e.src, kind: e.kind});
      }
    });
    return out;
  }

  /**
   * Diff a field's baseline depends_on against an "after" {src, kind}[] into EdgeChanges
   * (add / remove / toggle, case-insensitive). The CANONICAL changed-edge diff — it runs
   * client-side against the live rows so the lazy per-node sync queue stays order-correct.
   * @param {!Array<!Object>} before The baseline edges ({field, kind}).
   * @param {!Array<!Object>} after The current edges ({field, kind}).
   * @return {!Array<!Object>} EdgeChanges ({action, src, old_kind, new_kind}).
   */
  function diffEdges(before, after) {
    const b = {};
    before.forEach(function (d) {
      const k = (d.field || "").toLowerCase();
      if (k && !b[k]) {
        b[k] = {src: d.field, kind: d.kind};
      }
    });
    const a = {};
    after.forEach(function (d) {
      const k = (d.field || "").toLowerCase();
      if (k && !a[k]) {
        a[k] = {src: d.field, kind: d.kind};
      }
    });
    const changes = [];
    Object.keys(a).forEach(function (k) {
      if (!b[k]) {
        changes.push({action: "add", src: a[k].src, old_kind: "", new_kind: a[k].kind});
      }
    });
    Object.keys(b).forEach(function (k) {
      if (!a[k]) {
        changes.push({action: "remove", src: b[k].src, old_kind: b[k].kind, new_kind: ""});
      }
    });
    Object.keys(a).forEach(function (k) {
      if (b[k] && b[k].kind !== a[k].kind) {
        changes.push({
          action: "toggle",
          src: a[k].src,
          old_kind: b[k].kind,
          new_kind: a[k].kind
        });
      }
    });
    return changes;
  }

  /**
   * The pending derived-deletion changes for a field NOT already covered by `changes`. A purely
   * derived edge lives only in the prompt's {{ref}} (not in depends_on), so diffEdges can't see its
   * deletion — recover each pending "<field>|<src>" as a remove change, reading the original-case
   * src + its kind from graphData.edges (falling back to the lower-cased src / "hard").
   * @param {string} field The dependent field.
   * @param {!Array<!Object>} changes The changes diffEdges already produced for this field.
   * @return {!Array<!Object>} The extra remove changes to append.
   */
  function derivedDeletionChanges(field, changes) {
    const covered = {};
    changes.forEach(function (c) {
      covered[(c.src || "").toLowerCase()] = true;
    });
    const prefix = field.toLowerCase() + "|";
    const extra = [];
    Object.keys(removedEdges).forEach(function (key) {
      if (key.indexOf(prefix) !== 0) {
        return;
      }
      const srcLower = key.slice(prefix.length);
      if (covered[srcLower]) {
        return; // an explicit remove/toggle already accounts for this src
      }
      let src = srcLower;
      let oldKind = "hard";
      (graphData.edges || []).forEach(function (e) {
        if (
          (e.dst || "").toLowerCase() === field.toLowerCase() &&
          (e.src || "").toLowerCase() === srcLower
        ) {
          src = e.src;
          oldKind = e.kind || "hard";
        }
      });
      extra.push({action: "remove", src: src, old_kind: oldKind, new_kind: ""});
    });
    return extra;
  }

  /**
   * Build the topo-ordered queue of changed dependent nodes: each row whose explicit depends_on
   * differs from the baseline OR has a pending derived-edge deletion, ordered so a node comes AFTER
   * any of its (changed) prerequisites. Each entry is {field, changes:[EdgeChange]}.
   * @return {!Array<!Object>}
   */
  function buildSyncQueue() {
    const changed = [];
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      const field = tr.dataset.field || "";
      // A locked field's prompt must never be rewritten by the sync — skip it entirely.
      if (isFieldLocked(field)) {
        return;
      }
      const before = lastSyncedDeps[field.toLowerCase()] || [];
      const after = readDependsOn(tr).map(function (d) {
        return {field: d.field, kind: d.kind};
      });
      const changes = diffEdges(before, after);
      // Fold in purely-derived deletions (in the prompt only, so invisible to diffEdges).
      const extra = derivedDeletionChanges(field, changes);
      const all = changes.concat(extra);
      if (all.length) {
        changed.push({field: field, changes: all});
      }
    });
    return topoOrderChanged(changed);
  }

  /**
   * Order the changed nodes so each comes after any changed node it depends on (a prerequisite).
   * Uses the current effective graph edges; nodes not in a dependency relation keep their order.
   * @param {!Array<!Object>} changed The changed nodes ({field, changes}).
   * @return {!Array<!Object>}
   */
  function topoOrderChanged(changed) {
    const inSet = {};
    changed.forEach(function (c) {
      inSet[c.field.toLowerCase()] = c;
    });
    // prereqs[x] = the changed fields x depends on (its incoming edges' sources, if also changed).
    const prereqs = {};
    changed.forEach(function (c) {
      const lc = c.field.toLowerCase();
      prereqs[lc] = incomingEdges(c.field)
        .map(function (e) {
          return e.field.toLowerCase();
        })
        .filter(function (src) {
          return inSet[src];
        });
    });
    const ordered = [];
    const done = {};
    const onStack = {};
    function visit(lc) {
      if (done[lc] || onStack[lc]) {
        return; // a cycle is impossible (the graph is acyclic), onStack guards regardless
      }
      onStack[lc] = true;
      (prereqs[lc] || []).forEach(visit);
      onStack[lc] = false;
      done[lc] = true;
      ordered.push(inSet[lc]);
    }
    changed.forEach(function (c) {
      visit(c.field.toLowerCase());
    });
    return ordered;
  }

  /**
   * Save entry point (folds the former "↻ Sync prompts" flow into the bottom Save button):
   * reconcile any changed edges through the review popovers FIRST, then persist. If nothing
   * changed, persist directly. HOISTED so the Save handler in 05-handlers.js can call it.
   */
  function beginSaveWithSync() {
    if (syncQueue) {
      return; // a save/sync is already running
    }
    const queue = buildSyncQueue();
    if (!queue.length) {
      performSave(); // nothing to reconcile → just persist
      return;
    }
    syncQueue = {pending: queue, synced: 0, field: ""};
    processNextSync(); // runs the review popovers; the drain branch calls performSave()
  }

  /**
   * Persist the config through the `save` op, then re-baseline. Called directly (no edge changes)
   * or after the sync queue drains. HOISTED so the Save handler in 05-handlers.js can call it.
   */
  function performSave() {
    saveBtn.disabled = true;
    send(
      "save",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        rows: collectRows(),
        decks: selectedDeckIds(),
        options: collectOptions(),
        // Persist the user-pinned graph node positions (collectPositions is hoisted from this file).
        positions: collectPositions()
      },
      function (res) {
        saveBtn.disabled = false;
        if (res && res.ok) {
          // A successful save makes the current edges the new graph→prompt sync baseline.
          removedEdges = {};
          snapshotSyncedDeps();
          setMsg("Saved.", false);
        } else {
          setMsg((res && res.error) || "Could not save — see logs.", true);
        }
      }
    );
  }

  /**
   * Process the next changed node LAZILY: request its rewrite against the CURRENT row state (so a
   * prior Apply is already reflected), then show the popover. When the queue drains, persist the
   * config. Computing each node's rewrite only when reached keeps the order correct.
   */
  function processNextSync() {
    if (!syncQueue) {
      return;
    }
    if (!syncQueue.pending.length) {
      syncQueue = null;
      closeDiffPopover(); // the queue drained — a final Apply must dismiss the popover too
      performSave(); // reconciliation done — persist (performSave re-baselines on success)
      return;
    }
    const node = syncQueue.pending[0];
    syncQueue.field = node.field;
    const tr = rowByField(node.field);
    if (!tr) {
      syncQueue.pending.shift(); // the field vanished — skip it
      processNextSync();
      return;
    }
    saveBtn.disabled = true;
    setMsg('<span class="sn-spin"></span>Rewriting “' + node.field + "” …", false);
    const intended = incomingEdges(node.field); // the FULL intended edge set at this node
    // For a 1-change node the kept_deps are the intended set minus the changed src.
    const change = node.changes[0];
    const kept = intended.filter(function (e) {
      return e.field.toLowerCase() !== (change.src || "").toLowerCase();
    });
    send(
      "rewrite_edges",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        changes: [
          {
            target: node.field,
            old_prompt: tr.dataset.prompt || "",
            kept_deps: kept,
            change: change,
            intended_depends_on: intended
          }
        ]
      },
      null
    );
  }

  /**
   * Receive the edge-rewrite result (off-thread). Opens the diff popover for the current queued
   * node with its old→new prompt; a guard-rail failure (ok=false) still opens it (old prompt +
   * a notice) so the user can hand-edit. An {error} payload aborts the queue with a toast.
   * @param {?(Array<!Object>|Object)} res [{field, old_prompt, new_prompt, ok, reason}] or {error}.
   */
  window.__snRewriteResult = function (res) {
    saveBtn.disabled = false;
    setMsg("");
    if (!syncQueue) {
      return;
    }
    if (!Array.isArray(res)) {
      graphToastMsg((res && res.error) || "Rewrite failed — see logs.");
      abortSync();
      return;
    }
    const node = syncQueue.pending[0];
    const result =
      res.find(function (r) {
        return (r.field || "").toLowerCase() === (node.field || "").toLowerCase();
      }) || res[0];
    if (!result) {
      advanceSync();
      return;
    }
    openDiffPopover({
      field: node.field,
      oldPrompt: result.old_prompt || "",
      newPrompt: result.ok ? result.new_prompt : result.old_prompt || "",
      intendedDeps: incomingEdges(node.field),
      notice: result.ok
        ? ""
        : "Couldn't auto-rewrite this prompt to match the edge change" +
          (result.reason ? " (" + esc(result.reason) + ")" : "") +
          ". Edit it below — Apply unlocks once it matches the dependencies.",
      onApply: function (text) {
        applySyncedPrompt(node, text);
      },
      onDiscard: advanceSync
    });
  };

  /** Abort the running queue (an error/cancel) without re-baselining. */
  function abortSync() {
    closeDiffPopover();
    syncQueue = null;
  }

  /** Skip the current node and process the next (Discard, or a vanished field). */
  function advanceSync() {
    closeDiffPopover();
    if (!syncQueue) {
      return;
    }
    const node = syncQueue.pending[0];
    syncQueue.pending.shift();
    if (node) {
      // A discarded/skipped node keeps its prompt, so revert its pending derived deletions — the
      // deleted edge comes back (it's still derivable from the unchanged prompt).
      clearRemovedFor(node.field);
      recomputeGraph();
    }
    processNextSync();
  }

  /**
   * Apply a reviewed rewrite for the current node: cycle-precheck FIRST (roll back if it would
   * cycle), then write the prompt AND set depends_on to the intended kinds in LOCKSTEP, recompute
   * (the server acyclic backstop runs), and advance.
   * @param {!Object} node The queued node ({field, changes}).
   * @param {string} text The reviewed Now-prompt to persist.
   */
  function applySyncedPrompt(node, text) {
    const tr = rowByField(node.field);
    if (!tr) {
      advanceSync();
      return;
    }
    const intended = incomingEdges(node.field);
    // Pre-Apply cycle check: an ADD whose source can already reach this node would cycle. Check
    // every intended edge defensively before writing anything (roll back, do not touch the row).
    for (let i = 0; i < intended.length; i++) {
      if (wouldCreateCycle(intended[i].field, node.field)) {
        graphToastMsg(
          "Applying this would create a cycle via “" + intended[i].field + "” — skipped."
        );
        advanceSync();
        return;
      }
    }
    tr.dataset.prompt = text;
    updatePromptSummary(tr);
    // Lockstep: the explicit depends_on becomes the intended kinds, so kind never drifts
    // unverified. Keep each existing entry's auto flag where the src is unchanged.
    setRowDepsLockstep(tr, intended);
    // The rewritten prompt now reflects any pending derived deletions for this node, so drop them.
    clearRemovedFor(node.field);
    recomputeGraph(); // server whole-graph validate_acyclic backstop
    syncQueue.synced += 1;
    syncQueue.pending.shift();
    processNextSync();
  }

  /**
   * Set a row's explicit depends_on to exactly `intended` ({field, kind}[]), in lockstep with the
   * just-applied prompt. Preserves the `auto` flag of an existing same-source entry so a
   * classifier edge stays auto; a brand-new edge is a user edge (auto=false).
   * @param {!HTMLElement} tr The dependent row.
   * @param {!Array<!Object>} intended The intended edges ({field, kind}).
   */
  function setRowDepsLockstep(tr, intended) {
    const prev = {};
    readDependsOn(tr).forEach(function (d) {
      prev[(d.field || "").toLowerCase()] = d;
    });
    const next = intended.map(function (e) {
      const was = prev[(e.field || "").toLowerCase()];
      return {field: e.field, kind: e.kind, auto: was ? !!was.auto : false};
    });
    tr.dataset.dependsOn = JSON.stringify(next);
  }

  // --- the reusable diff popover -----------------------------------------------------------
  let diffState = null; // {field, intendedDeps, timer, improving} while open; null when closed.

  /**
   * Receive a pinned-improve result on the DEDICATED hook. IGNORED unless the popover is still
   * open on this field — so a result that returns after the user Discarded/advanced can never
   * write an unverified prompt onto a row (W1).
   * @param {string} field The field the improve was for.
   * @param {?Object} res {prompt, ok, reason} on success, {error} on failure.
   */
  window.__snDiffImproveResult = function (field, res) {
    if (
      !diffState ||
      !diffState.improving ||
      (diffState.field || "").toLowerCase() !== (field || "").toLowerCase()
    ) {
      return; // stale / discarded / wrong field — drop it
    }
    setDiffImproving(false);
    if (res && res.prompt) {
      diffNew.value = res.prompt;
      diffNotice.hidden = true;
      if (res.ok === false && res.reason) {
        showDiffNotice("Improved, but it changed the dependencies — review below.");
      }
      validateDiffNow();
    } else {
      setDiffErr([(res && res.error) || "Improve failed — see logs."]);
    }
  };

  /**
   * Mark a pinned improve in flight (true) or done (false): while in flight, ALL of the popover's
   * exits (Improve / Discard / Apply / Close) are disabled so it can't advance mid-improve.
   * @param {boolean} on Whether an improve is in flight.
   */
  function setDiffImproving(on) {
    if (diffState) {
      diffState.improving = on;
    }
    diffImprove.disabled = on;
    diffDiscard.disabled = on;
    diffClose.disabled = on;
    if (on) {
      diffApply.disabled = true; // re-gated by validateDiffNow when the result lands
    }
  }

  /**
   * Open the graph→prompt diff popover: a "Was" (old, read-only) over an editable "Now",
   * gated by a debounced validate so Apply is disabled until the Now prompt derives EXACTLY the
   * intended dependency set (and has valid {{}} syntax). Anchored near the edited node, clamped
   * into view.
   * @param {!Object} o {field, oldPrompt, newPrompt, intendedDeps, notice, onApply, onDiscard}.
   */
  function openDiffPopover(o) {
    diffState = {
      field: o.field,
      intendedDeps: o.intendedDeps || [],
      onApply: o.onApply,
      onDiscard: o.onDiscard,
      timer: 0,
      improving: false
    };
    diffTitle.textContent = "Update “" + o.field + "” prompt";
    diffOld.textContent = o.oldPrompt || "(empty)";
    diffNew.value = o.newPrompt || "";
    if (o.notice) {
      showDiffNotice(o.notice);
    } else {
      diffNotice.hidden = true;
    }
    diffErr.hidden = true;
    diffImprove.disabled = false;
    diffDiscard.disabled = false;
    diffClose.disabled = false;
    diffPop.hidden = false;
    anchorDiffPopover(o.field);
    validateDiffNow();
    diffNew.focus();
  }

  function closeDiffPopover() {
    if (diffState && diffState.timer) {
      clearTimeout(diffState.timer);
    }
    diffState = null;
    diffPop.hidden = true;
  }

  /** Show an HTML-safe notice line in the popover (the guard-rail-failed hand-edit case). */
  function showDiffNotice(html) {
    diffNotice.innerHTML = html;
    diffNotice.hidden = false;
  }

  /** Position the popover near the node's screen box, clamped inside the viewport. */
  function anchorDiffPopover(field) {
    const g = graphSvg.querySelector('[data-node="' + cssAttr(field) + '"]');
    const margin = 12;
    const pw = diffPop.offsetWidth || 380;
    const ph = diffPop.offsetHeight || 320;
    let left;
    let top;
    if (g && g.getBoundingClientRect) {
      const r = g.getBoundingClientRect();
      left = r.right + margin;
      top = r.top;
      if (left + pw > window.innerWidth - margin) {
        left = r.left - pw - margin; // flip to the node's left when it won't fit on the right
      }
    } else {
      left = (window.innerWidth - pw) / 2;
      top = (window.innerHeight - ph) / 2;
    }
    left = Math.max(margin, Math.min(left, window.innerWidth - pw - margin));
    top = Math.max(margin, Math.min(top, window.innerHeight - ph - margin));
    diffPop.style.left = left + "px";
    diffPop.style.top = top + "px";
  }

  /** Escape a field name for a [data-node="…"] attribute selector. */
  function cssAttr(name) {
    return String(name).replace(/(["\\])/g, "\\$1");
  }

  diffNew.addEventListener("input", function () {
    if (diffState) {
      clearTimeout(diffState.timer);
      diffState.timer = setTimeout(validateDiffNow, 250); // debounce the guard-rail validate
    }
  });

  /** Run the live guard-rail validate for the current Now text and gate Apply on the result. */
  function validateDiffNow() {
    if (!diffState) {
      return;
    }
    const deps = diffState.intendedDeps.map(function (e) {
      return {field: e.field, kind: e.kind};
    });
    send(
      "validate_prompt",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        target_field: diffState.field,
        prompt: diffNew.value,
        intended_depends_on: deps
      },
      function (res) {
        if (!diffState || !res) {
          return;
        }
        if (res.error) {
          setDiffErr([res.error]);
          diffApply.disabled = true;
          return;
        }
        const c = res.consistency || {};
        const msgs = (res.syntax_errors || []).concat(c.messages || []);
        const ok = c.ok && (res.syntax_errors || []).length === 0;
        diffApply.disabled = !ok;
        if (ok) {
          diffErr.hidden = true;
        } else {
          setDiffErr(msgs.length ? msgs : ["This change doesn't match the dependencies."]);
        }
      }
    );
  }

  /** Render the inline validate error band (consistency messages + syntax errors). */
  function setDiffErr(messages) {
    diffErr.innerHTML = (messages || [])
      .map(function (m) {
        return esc(m);
      })
      .join("<br>");
    diffErr.hidden = !messages || !messages.length;
  }

  diffImprove.addEventListener("click", function () {
    if (!diffState || diffState.improving) {
      return;
    }
    setDiffImproving(true);
    showDiffNotice('<span class="sn-spin"></span>Improving the wording…');
    send(
      "improve_prompt_pinned",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        target_field: diffState.field,
        prompt: diffNew.value,
        fixed_deps: diffState.intendedDeps.map(function (e) {
          return {field: e.field, kind: e.kind};
        })
      },
      null
    );
  });

  diffApply.addEventListener("click", function () {
    if (diffState && !diffState.improving && !diffApply.disabled) {
      const apply = diffState.onApply;
      const text = diffNew.value;
      if (apply) {
        apply(text);
      }
    }
  });

  /** Exit the popover via Discard / Close: a no-op while a pinned improve is in flight. */
  function discardDiffPopover() {
    if (!diffState || diffState.improving) {
      return;
    }
    const discard = diffState.onDiscard;
    if (discard) {
      discard();
    } else {
      closeDiffPopover();
    }
  }
  diffDiscard.addEventListener("click", discardDiffPopover);
  diffClose.addEventListener("click", discardDiffPopover);
