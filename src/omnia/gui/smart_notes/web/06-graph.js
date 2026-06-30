
  /**
   * Smart Notes config page — the field dependency-graph canvas (a MIDDLE fragment of the page
   * IIFE; it neither opens nor closes it). Renders the Python-laid-out graph as ONE pannable /
   * zoomable SVG viewport and edits edges: drag a node's connector handle → another node adds a
   * hard edge, clicking an edge toggles hard↔soft, and Delete removes the selected one. Layout
   * (pixel x/y + bounds) is ALWAYS computed server-side (the `graph_recompute` op), so this
   * fragment never re-implements longest-path; it draws, pans/zooms, and mutates each row's
   * `depends_on` (the single source of truth `collectRows` reads, so the existing Save persists
   * it). Node MOVES are visual-only (an ephemeral posOverride map, wiped on recompute).
   *
   * CSP-safe: vanilla SVG + CSS only, no external libs. NO SVG <filter> drop-shadows and NO
   * overflow:auto scroller — both blank the page on QtWebEngine/macOS Metal; depth/pan/zoom use
   * stroke/opacity/gradients and a single <g> transform instead. Hoisted `function seedGraph` is
   * called by applyLoad in 05-handlers.js.
   */

  const SVGNS = "http://www.w3.org/2000/svg";
  const graphSvg = document.getElementById("sn-graph-svg");
  const graphToast = document.getElementById("sn-graph-toast");
  const viewFieldsBtn = document.getElementById("sn-view-fields");
  const viewGraphBtn = document.getElementById("sn-view-graph");
  const fieldsView = document.getElementById("sn-fields-view");
  const graphView = document.getElementById("sn-graph-view");
  const reloadBtn = document.getElementById("sn-graph-reload");

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
  const HANDLE_R = 7; // connector port radius (grows on hover via CSS)
  const MOVE_THRESHOLD = 4; // px a body-drag must travel before it's a MOVE (else a click)
  const MIN_K = 0.3;
  const MAX_K = 2.2;
  const HARD_COLOR = "#e5484d";
  const SOFT_COLOR = "#30a46c";

  let graphData = {nodes: [], edges: [], bounds: {width: 0, height: 0}};
  let graphVisible = false;
  let selectedEdge = null; // {src, dst, derived} of the currently-selected edge
  let posOverride = {}; // name(lower) -> {x, y} ephemeral move overrides (wiped on recompute)

  // The graph→prompt sync baseline: a snapshot of each row's depends_on (the "last synced"
  // edge state). Taken on load, on save, and after a Feature-1 deps apply; the Sync button
  // diffs the CURRENT rows against it to find each changed dependent node. {field(lower):
  // [{field, kind}]}.
  let lastSyncedDeps = {};
  // The active diff-popover queue context (null when idle): the topo-ordered list of changed
  // nodes still to process, the running synced count, and the open popover's field.
  let syncQueue = null;

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
   * if the view is open. Clears any ephemeral move overrides — positions are visual-only and the
   * server is the source of truth on each (re)seed.
   * @param {?Object} graph {nodes:[{name,is_base,generatable,column,row,x,y,w,h,lane}], edges:[...], bounds:{width,height}}.
   */
  function seedGraph(graph) {
    graphData =
      graph && graph.nodes
        ? graph
        : {nodes: [], edges: [], bounds: {width: 0, height: 0}};
    posOverride = {};
    // A freshly-loaded note type IS the synced baseline (the rows already match their prompts).
    snapshotSyncedDeps();
    if (graphVisible) {
      renderGraph();
      fitView();
    }
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
    // the Sync button must not then treat a classifier-written edge as a user edge change.
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
        rows: collectRows()
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
          posOverride = {}; // positions are visual-only — the fresh layout is authoritative
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
      const wrap = edgeGroup(e);
      if (wrap) {
        viewport.appendChild(wrap);
      }
    });
    nodes.forEach(function (n) {
      viewport.appendChild(nodeGroup(n));
    });
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
    m.setAttribute("markerWidth", "7");
    m.setAttribute("markerHeight", "7");
    m.setAttribute("orient", "auto-start-reverse");
    const p = svgEl("path");
    p.setAttribute("d", "M0,0 L10,5 L0,10 z");
    p.setAttribute("fill", color);
    m.appendChild(p);
    return m;
  }

  /** The bezier `d` for an edge from src's right port to dst's left port. */
  function edgeD(s, d) {
    const sp = nodePos(s);
    const dp = nodePos(d);
    const ss = nodeSize(s);
    const ds = nodeSize(d);
    const sx = sp.x + ss.w;
    const sy = sp.y + ss.h / 2;
    const ex = dp.x;
    const ey = dp.y + ds.h / 2;
    const dx = Math.max(40, Math.abs(ex - sx) / 2);
    return (
      "M" + sx + "," + sy + " C" + (sx + dx) + "," + sy + " " +
      (ex - dx) + "," + ey + " " + ex + "," + ey
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
      "sn-edge " + (soft ? "sn-edge-soft" : "sn-edge-hard") + (sel ? " sn-edge-selected" : "")
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
      "sn-node" + (n.is_base ? " sn-node-base" : "") + (n.generatable ? "" : " sn-node-ng")
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

    // The connector port on the right edge — mousedown here starts an EDGE CREATE.
    const handle = svgEl("circle");
    handle.setAttribute("cx", String(sz.w));
    handle.setAttribute("cy", String(sz.h / 2));
    handle.setAttribute("r", String(HANDLE_R));
    handle.setAttribute("class", "sn-handle");
    handle.addEventListener("mousedown", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      startConnect(n.name, ev);
    });

    const title = svgEl("title");
    title.textContent =
      n.name + (n.is_base ? " (base / input)" : n.generatable ? "" : " (not generated)");

    g.appendChild(rect);
    g.appendChild(text);
    g.appendChild(handle);
    g.appendChild(title);
    g.addEventListener("mousedown", function (ev) {
      startMove(n.name, ev);
    });
    return g;
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
    connect = {src: srcName, line: svgEl("path")};
    connect.line.setAttribute("class", "sn-drag-line");
    const a = anchorRight(srcName);
    connect.line.setAttribute("d", "M" + a.x + "," + a.y + " L" + a.x + "," + a.y);
    viewport.appendChild(connect.line);
    document.addEventListener("mousemove", onConnectMove);
    document.addEventListener("mouseup", onConnectEnd);
  }
  function onConnectMove(ev) {
    if (!connect) {
      return;
    }
    const a = anchorRight(connect.src);
    const p = screenToWorld(ev);
    const dx = Math.max(40, Math.abs(p.x - a.x) / 2);
    connect.line.setAttribute(
      "d",
      "M" + a.x + "," + a.y + " C" + (a.x + dx) + "," + a.y + " " +
      (p.x - dx) + "," + p.y + " " + p.x + "," + p.y
    );
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
   * mousedown on a node BODY: start a MOVE that only "arms" once the pointer travels past
   * MOVE_THRESHOLD; under threshold + a quick mouseup is treated as a click/select (no move). A
   * move writes the ephemeral posOverride map — node positions are visual-only, not persisted.
   */
  function startMove(name, ev) {
    if (ev.button !== 0) {
      return;
    }
    ev.preventDefault();
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
    move = null;
  }

  /** Re-render after a node move so its edges follow (cheap: full redraw, positions cached). */
  function redrawMoved(name) {
    renderGraph();
  }

  function anchorRight(name) {
    const n = nodeByName(name);
    const p = nodePos(n);
    const sz = nodeSize(n);
    return {x: p.x + sz.w, y: p.y + sz.h / 2};
  }

  function addEdge(src, dst) {
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
    const newKind = e.kind === "soft" ? "hard" : "soft";
    updateRowDep(e.dst, e.src, newKind);
    selectedEdge = {src: e.src, dst: e.dst, derived: e.derived};
    recomputeGraph();
  }

  function removeEdge(sel) {
    updateRowDep(sel.dst, sel.src, null);
    const wasDerived = sel.derived;
    selectedEdge = null;
    recomputeGraph();
    if (wasDerived) {
      graphToastMsg("That edge comes from a {{…}} reference in the field’s prompt — edit the prompt to remove it.");
    }
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
   * Detect whether adding the edge src→dst would create a cycle: true if dst can already reach
   * src by following existing edges (a→b = a is a prerequisite of b).
   */
  function wouldCreateCycle(src, dst) {
    const adj = {};
    (graphData.edges || []).forEach(function (e) {
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
        (e.src || "").toLowerCase() !== lc
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
   * Build the topo-ordered queue of changed dependent nodes: each row whose explicit depends_on
   * differs from the baseline, ordered so a node comes AFTER any of its (changed) prerequisites.
   * Each entry is {field, changes:[EdgeChange]}.
   * @return {!Array<!Object>}
   */
  function buildSyncQueue() {
    const changed = [];
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      const field = tr.dataset.field || "";
      const before = lastSyncedDeps[field.toLowerCase()] || [];
      const after = readDependsOn(tr).map(function (d) {
        return {field: d.field, kind: d.kind};
      });
      const changes = diffEdges(before, after);
      if (changes.length) {
        changed.push({field: field, changes: changes});
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

  // The reload control: start the lazy, order-correct rewrite queue.
  reloadBtn.addEventListener("click", function () {
    if (syncQueue) {
      return; // a sync is already running
    }
    const queue = buildSyncQueue();
    if (!queue.length) {
      graphToastMsg("No edge changes to sync.");
      return;
    }
    syncQueue = {pending: queue, synced: 0, field: ""};
    processNextSync();
  });

  /**
   * Process the next changed node LAZILY: request its rewrite against the CURRENT row state (so a
   * prior Apply is already reflected), then show the popover. When the queue drains, re-baseline
   * and toast the count. Computing each node's rewrite only when reached keeps the order correct.
   */
  function processNextSync() {
    if (!syncQueue) {
      return;
    }
    if (!syncQueue.pending.length) {
      const n = syncQueue.synced;
      syncQueue = null;
      snapshotSyncedDeps(); // the rows are now the new synced baseline
      refreshGraphIfOpen();
      if (n) {
        graphToastMsg("Synced " + n + " prompt" + (n === 1 ? "" : "s") + ".");
      }
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
    reloadBtn.disabled = true;
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
    reloadBtn.disabled = false;
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
    syncQueue.pending.shift();
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
