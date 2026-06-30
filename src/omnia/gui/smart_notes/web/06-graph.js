
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
    if (graphVisible) {
      renderGraph();
      fitView();
    }
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
