
  /**
   * Smart Notes config page — the field dependency-graph view (a MIDDLE fragment of the page
   * IIFE; it neither opens nor closes it). Renders the Python-laid-out graph as SVG and edits
   * edges: drag node→node adds a hard edge, clicking an edge toggles hard↔soft, and Delete
   * removes the selected one. Layout is ALWAYS computed server-side (the `graph_recompute` op),
   * so this fragment never re-implements longest-path; it only draws and mutates each row's
   * `depends_on` (the single source of truth `collectRows` reads, so the existing Save persists
   * it). Hoisted `function seedGraph` is called by applyLoad in 05-handlers.js.
   */

  const SVGNS = "http://www.w3.org/2000/svg";
  const graphSvg = document.getElementById("sn-graph-svg");
  const graphToast = document.getElementById("sn-graph-toast");
  const viewFieldsBtn = document.getElementById("sn-view-fields");
  const viewGraphBtn = document.getElementById("sn-view-graph");
  const fieldsView = document.getElementById("sn-fields-view");
  const graphView = document.getElementById("sn-graph-view");

  // Layout constants (px). Columns = topological layers (x), rows = within-layer index (y).
  const COL_W = 210;
  const ROW_H = 72;
  const NODE_W = 156;
  const NODE_H = 40;
  const PAD = 24;
  const HARD_COLOR = "#e5484d";
  const SOFT_COLOR = "#30a46c";

  let graphData = {nodes: [], edges: []};
  let graphVisible = false;
  let selectedEdge = null; // {src, dst, derived} of the currently-selected edge
  let dragSrc = null;
  let dragLine = null;

  /** Create an SVG element. */
  function svgEl(tag) {
    return document.createElementNS(SVGNS, tag);
  }

  /**
   * Store the server-laid-out graph and redraw if the view is open.
   * @param {?Object} graph {nodes:[{name,is_base,generatable,column,row}], edges:[{src,dst,kind,derived}]}.
   */
  function seedGraph(graph) {
    graphData = graph && graph.nodes ? graph : {nodes: [], edges: []};
    if (graphVisible) {
      renderGraph();
    }
  }

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
          renderGraph();
          return;
        }
        if (res && res.graph) {
          graphData = res.graph;
        }
        renderGraph();
      }
    );
  }

  // --- node/edge geometry ----------------------------------------------------------------
  function nodePos(node) {
    return {x: PAD + node.column * COL_W, y: PAD + node.row * ROW_H};
  }
  function nodeByName(name) {
    const lc = (name || "").toLowerCase();
    for (let i = 0; i < graphData.nodes.length; i++) {
      if (graphData.nodes[i].name.toLowerCase() === lc) {
        return graphData.nodes[i];
      }
    }
    return null;
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
    let maxCol = 0;
    let maxRow = 0;
    nodes.forEach(function (n) {
      maxCol = Math.max(maxCol, n.column);
      maxRow = Math.max(maxRow, n.row);
    });
    const w = PAD * 2 + (maxCol + 1) * COL_W;
    const h = PAD * 2 + (maxRow + 1) * ROW_H;
    graphSvg.setAttribute("width", String(w));
    graphSvg.setAttribute("height", String(h));
    graphSvg.setAttribute("viewBox", "0 0 " + w + " " + h);

    const defs = svgEl("defs");
    defs.appendChild(arrowMarker("sn-arrow-hard", HARD_COLOR));
    defs.appendChild(arrowMarker("sn-arrow-soft", SOFT_COLOR));
    graphSvg.appendChild(defs);

    edges.forEach(function (e) {
      graphSvg.appendChild(edgePath(e));
    });
    nodes.forEach(function (n) {
      graphSvg.appendChild(nodeGroup(n));
    });
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

  function edgePath(e) {
    const s = nodeByName(e.src);
    const d = nodeByName(e.dst);
    const path = svgEl("path");
    if (!s || !d) {
      return path; // dangling edge (renamed/missing field) — skip silently
    }
    const sp = nodePos(s);
    const dp = nodePos(d);
    const sx = sp.x + NODE_W;
    const sy = sp.y + NODE_H / 2;
    const ex = dp.x;
    const ey = dp.y + NODE_H / 2;
    const dx = Math.max(40, Math.abs(ex - sx) / 2);
    path.setAttribute(
      "d",
      "M" + sx + "," + sy + " C" + (sx + dx) + "," + sy + " " + (ex - dx) + "," + ey + " " + ex + "," + ey
    );
    const soft = e.kind === "soft";
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", soft ? SOFT_COLOR : HARD_COLOR);
    path.setAttribute("stroke-width", isSelected(e) ? "4" : "2");
    if (e.derived) {
      path.setAttribute("stroke-dasharray", "6,4");
    }
    path.setAttribute("marker-end", "url(#" + (soft ? "sn-arrow-soft" : "sn-arrow-hard") + ")");
    path.setAttribute("class", "sn-edge" + (isSelected(e) ? " sn-edge-selected" : ""));
    path.addEventListener("click", function (ev) {
      ev.stopPropagation();
      onEdgeClick(e);
    });
    return path;
  }

  function nodeGroup(n) {
    const g = svgEl("g");
    g.setAttribute(
      "class",
      "sn-node" + (n.is_base ? " sn-node-base" : "") + (n.generatable ? "" : " sn-node-ng")
    );
    g.setAttribute("data-node", n.name);
    const p = nodePos(n);
    const rect = svgEl("rect");
    rect.setAttribute("x", String(p.x));
    rect.setAttribute("y", String(p.y));
    rect.setAttribute("width", String(NODE_W));
    rect.setAttribute("height", String(NODE_H));
    rect.setAttribute("rx", "8");
    const text = svgEl("text");
    text.setAttribute("x", String(p.x + NODE_W / 2));
    text.setAttribute("y", String(p.y + NODE_H / 2));
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("dominant-baseline", "central");
    text.textContent = n.name.length > 20 ? n.name.slice(0, 19) + "…" : n.name;
    const title = svgEl("title");
    title.textContent = n.name + (n.is_base ? " (base / input)" : n.generatable ? "" : " (not generated)");
    g.appendChild(rect);
    g.appendChild(text);
    g.appendChild(title);
    g.addEventListener("mousedown", function (ev) {
      startDrag(n.name, ev);
    });
    return g;
  }

  // --- editing: drag to create, click to toggle, Delete to remove ------------------------
  function startDrag(srcName, ev) {
    ev.preventDefault();
    dragSrc = srcName;
    dragLine = svgEl("line");
    dragLine.setAttribute("class", "sn-drag-line");
    const a = anchorRight(srcName);
    setLineEnds(dragLine, a.x, a.y, a.x, a.y);
    graphSvg.appendChild(dragLine);
    document.addEventListener("mousemove", onDragMove);
    document.addEventListener("mouseup", onDragEnd);
  }
  function onDragMove(ev) {
    if (!dragLine) {
      return;
    }
    const a = anchorRight(dragSrc);
    const p = toSvgCoords(ev);
    setLineEnds(dragLine, a.x, a.y, p.x, p.y);
  }
  function onDragEnd(ev) {
    document.removeEventListener("mousemove", onDragMove);
    document.removeEventListener("mouseup", onDragEnd);
    if (dragLine && dragLine.parentNode) {
      dragLine.parentNode.removeChild(dragLine);
    }
    const target = nodeNameAt(ev.clientX, ev.clientY);
    const src = dragSrc;
    dragLine = null;
    dragSrc = null;
    if (target && src && target.toLowerCase() !== src.toLowerCase()) {
      addEdge(src, target);
    }
  }

  function anchorRight(name) {
    const n = nodeByName(name);
    const p = nodePos(n);
    return {x: p.x + NODE_W, y: p.y + NODE_H / 2};
  }
  function setLineEnds(line, x1, y1, x2, y2) {
    line.setAttribute("x1", String(x1));
    line.setAttribute("y1", String(y1));
    line.setAttribute("x2", String(x2));
    line.setAttribute("y2", String(y2));
  }
  function toSvgCoords(ev) {
    const r = graphSvg.getBoundingClientRect();
    const vb = graphSvg.viewBox.baseVal;
    const sx = r.width ? vb.width / r.width : 1;
    const sy = r.height ? vb.height / r.height : 1;
    return {x: (ev.clientX - r.left) * sx, y: (ev.clientY - r.top) * sy};
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
  graphSvg.addEventListener("click", function () {
    if (selectedEdge) {
      selectedEdge = null;
      renderGraph();
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
