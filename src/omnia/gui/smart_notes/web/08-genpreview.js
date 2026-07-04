/**
 * @fileoverview Smart Notes config page — GENERATION-ORDER PREVIEW (loaded between 06-graph and
 * 07-init; a fragment of the shared page IIFE, so it uses graphData / baseSel / esc directly).
 *
 * The "▶ Preview gen order" button animates the exact order fields are generated in, from a seed
 * the user picks: the base Word is always "given"; Context (and any other input) are optional
 * checkboxes. It REPLAYS the engine route (mirrors engine/ordering.order_rules + generate_note's
 * block gate): HARD edges order AND block (a field waits for its hard prerequisites and is blocked
 * when one is missing), SOFT edges order best-effort (the prerequisite generates first so the
 * dependent is richer). Purely a visual simulation over graphData — it generates nothing.
 */

const gpBtn = document.getElementById("sn-genpreview-btn");
const gpPanel = document.getElementById("sn-genpreview-panel");
const gpSeedList = document.getElementById("sn-gp-seed-list");
const gpPlay = document.getElementById("sn-gp-play");
const gpReplay = document.getElementById("sn-gp-replay");
const gpStatus = document.getElementById("sn-gp-status");
const gpClose = document.getElementById("sn-gp-close");

const GP_STEP_MS = 720; // per-field reveal delay
let gpTimer = null; // active step timer, so exit/replay can cancel a running animation

/** The node <g> element in the graph SVG for a field name (case-insensitive scan). */
function gpNodeEl(name) {
  const lc = (name || "").toLowerCase();
  let found = null;
  document.querySelectorAll("#sn-graph-svg [data-node]").forEach(function (g) {
    if ((g.getAttribute("data-node") || "").toLowerCase() === lc) {
      found = g;
    }
  });
  return found;
}

/** A field's prerequisite names of one kind ("hard"/"soft"), read from graphData edges. */
function gpPrereqs(name, kind) {
  const lc = (name || "").toLowerCase();
  const out = [];
  (graphData.edges || []).forEach(function (e) {
    if ((e.dst || "").toLowerCase() === lc && e.kind === kind) {
      out.push(e.src);
    }
  });
  return out;
}

/**
 * The topological generation order (excluding the base), mirroring engine order_rules: HARD edges
 * are strict constraints; SOFT edges are added only when they don't close a cycle (best-effort);
 * stable Kahn's breaks ties by input order.
 * @return {!Array<!Object>} The ordered non-base node payloads.
 */
function gpComputeOrder() {
  const nodes = (graphData.nodes || []).filter(function (n) {
    return !n.is_base;
  });
  const idx = {};
  nodes.forEach(function (n, i) {
    idx[n.name.toLowerCase()] = i;
  });
  const n = nodes.length;
  const hardAdj = [];
  for (let i = 0; i < n; i++) {
    hardAdj.push({});
  }
  const softEdges = [];
  (graphData.edges || []).forEach(function (e) {
    const s = idx[(e.src || "").toLowerCase()];
    const d = idx[(e.dst || "").toLowerCase()];
    if (s === undefined || d === undefined || s === d) {
      return; // an edge to/from the base field, or a self-loop
    }
    if (e.kind === "hard") {
      hardAdj[s][d] = true;
    } else {
      softEdges.push([s, d]);
    }
  });
  function reaches(from, to) {
    const stack = [from];
    const seen = {};
    while (stack.length) {
      const cur = stack.pop();
      if (cur === to) {
        return true;
      }
      if (seen[cur]) {
        continue;
      }
      seen[cur] = true;
      Object.keys(hardAdj[cur]).forEach(function (x) {
        stack.push(+x);
      });
    }
    return false;
  }
  softEdges.forEach(function (pair) {
    if (!reaches(pair[1], pair[0])) {
      hardAdj[pair[0]][pair[1]] = true; // keep the soft ordering unless it would cycle
    }
  });
  const indeg = new Array(n).fill(0);
  hardAdj.forEach(function (set) {
    Object.keys(set).forEach(function (d) {
      indeg[+d]++;
    });
  });
  const ready = [];
  for (let i = 0; i < n; i++) {
    if (indeg[i] === 0) {
      ready.push(i);
    }
  }
  const order = [];
  const done = {};
  while (ready.length) {
    ready.sort(function (a, b) {
      return a - b;
    });
    const u = ready.shift();
    order.push(u);
    done[u] = true;
    Object.keys(hardAdj[u]).forEach(function (v) {
      v = +v;
      if (--indeg[v] === 0) {
        ready.push(v);
      }
    });
  }
  for (let i = 0; i < n; i++) {
    if (!done[i]) {
      order.push(i); // a stray hard cycle (guarded elsewhere) — never drop a node
    }
  }
  return order.map(function (i) {
    return nodes[i];
  });
}

/** Pulse a "flowing" animation along every edge feeding INTO `name` (its incoming edges). */
function gpPulseEdgesInto(name) {
  const lc = (name || "").toLowerCase();
  document.querySelectorAll("#sn-graph-svg .sn-edge-g").forEach(function (g) {
    if ((g.getAttribute("data-dst") || "").toLowerCase() === lc) {
      g.classList.add("sn-edge-flow");
      setTimeout(function () {
        g.classList.remove("sn-edge-flow");
      }, GP_STEP_MS);
    }
  });
}

/** Strip every preview visual (node states + edge flow) so the normal graph shows through. */
function gpResetVisuals() {
  document.querySelectorAll("#sn-graph-svg [data-node]").forEach(function (g) {
    g.classList.remove(
      "sn-gp-given",
      "sn-gp-pending",
      "sn-gp-gen",
      "sn-gp-blocked"
    );
  });
  document.querySelectorAll("#sn-graph-svg .sn-edge-flow").forEach(function (g) {
    g.classList.remove("sn-edge-flow");
  });
}

/** Cancel a running animation (leaves the current frame visible). */
function gpCancel() {
  if (gpTimer) {
    clearTimeout(gpTimer);
    gpTimer = null;
  }
}

/** Populate the seed checklist: the base is pinned "given"; every other field is optional. */
function gpBuildSeedList() {
  gpSeedList.innerHTML = "";
  const base = baseSel.value || "";
  const pin = document.createElement("span");
  pin.className = "sn-gp-seed-base";
  pin.textContent = base ? "🔵 " + base + " (always given)" : "(no base field)";
  gpSeedList.appendChild(pin);
  (graphData.nodes || []).forEach(function (nd) {
    if (nd.is_base || nd.name.toLowerCase() === base.toLowerCase()) {
      return;
    }
    const lbl = document.createElement("label");
    lbl.className = "sn-gp-seed-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = nd.name;
    // Pre-check a "Context"-like field, since Word + Context is the common clip seed.
    if (/context/i.test(nd.name)) {
      cb.checked = true;
    }
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + nd.name));
    gpSeedList.appendChild(lbl);
  });
}

/** Open the preview panel (only meaningful on the graph view). */
function gpOpen() {
  if (!gpPanel) {
    return;
  }
  gpBuildSeedList();
  gpResetVisuals();
  gpStatus.textContent =
    "Pick what you'll provide, then Play to see the generation order.";
  gpReplay.hidden = true;
  gpPlay.disabled = false;
  gpPanel.hidden = false;
}

/** Exit the preview: cancel, clear visuals, hide the panel. */
function gpExit() {
  gpCancel();
  gpResetVisuals();
  if (gpPanel) {
    gpPanel.hidden = true;
  }
}

/** Run the animation from the current seed selection. */
function gpPlay_() {
  gpCancel();
  const base = (baseSel.value || "").toLowerCase();
  const seed = {};
  if (base) {
    seed[base] = true;
  }
  gpSeedList
    .querySelectorAll("input[type=checkbox]:checked")
    .forEach(function (cb) {
      seed[cb.value.toLowerCase()] = true;
    });

  const order = gpComputeOrder();
  gpResetVisuals();
  (graphData.nodes || []).forEach(function (nd) {
    const el = gpNodeEl(nd.name);
    if (!el) {
      return;
    }
    el.classList.add(
      nd.is_base || seed[nd.name.toLowerCase()] ? "sn-gp-given" : "sn-gp-pending"
    );
  });

  const working = {};
  Object.keys(seed).forEach(function (k) {
    working[k] = true;
  });
  gpReplay.hidden = false;
  gpPlay.disabled = true;
  let step = 0;

  function next() {
    if (step >= order.length) {
      const gen = Object.keys(working).length - Object.keys(seed).length;
      gpStatus.innerHTML =
        "✓ Done — <b>" +
        gen +
        "</b> fields generate in order (blocked ones need a missing hard input).";
      gpPlay.disabled = false;
      gpTimer = null;
      return;
    }
    const node = order[step];
    step += 1;
    const el = gpNodeEl(node.name);
    if (el) {
      el.classList.remove("sn-gp-pending");
    }
    const hard = gpPrereqs(node.name, "hard");
    const missing = hard.filter(function (p) {
      return !working[p.toLowerCase()];
    });
    if (missing.length) {
      if (el) {
        el.classList.add("sn-gp-blocked");
      }
      gpStatus.innerHTML =
        "(" +
        step +
        "/" +
        order.length +
        ") ⛔ <b>" +
        esc(node.name) +
        "</b> blocked — missing hard input: " +
        missing.map(esc).join(", ");
    } else {
      if (el) {
        el.classList.add("sn-gp-gen");
        gpPulseEdgesInto(node.name);
      }
      working[node.name.toLowerCase()] = true;
      const soft = gpPrereqs(node.name, "soft").filter(function (p) {
        return working[p.toLowerCase()];
      });
      let msg = "(" + step + "/" + order.length + ") ⚡ <b>" + esc(node.name) + "</b>";
      if (hard.length) {
        msg += " ← " + hard.map(esc).join(", ");
      }
      if (soft.length) {
        msg += " · enriched by " + soft.map(esc).join(", ");
      }
      gpStatus.innerHTML = msg;
    }
    gpTimer = setTimeout(next, GP_STEP_MS);
  }
  gpStatus.textContent = "Seed given → generating in dependency order…";
  gpTimer = setTimeout(next, GP_STEP_MS);
}

if (gpBtn) {
  gpBtn.addEventListener("click", function () {
    if (gpPanel && !gpPanel.hidden) {
      gpExit();
    } else {
      gpOpen();
    }
  });
}
if (gpPlay) {
  gpPlay.addEventListener("click", gpPlay_);
}
if (gpReplay) {
  gpReplay.addEventListener("click", gpPlay_);
}
if (gpClose) {
  gpClose.addEventListener("click", gpExit);
}
