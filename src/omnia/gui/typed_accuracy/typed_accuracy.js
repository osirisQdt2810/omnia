(function () {
  if (window.__TA_BOOTED) {
    try {
      if (typeof window.__TA_refresh === "function") window.__TA_refresh(true);
    } catch (e) {}
    return;
  }
  window.__TA_BOOTED = true;

  // Omnia bridge envelope: pycmd("omnia:" + {plugin, op, data}); the handler's return value
  // resolves the pycmd Promise. Mirrors core/reviewer/web_injector.py's MESSAGE_PREFIX.
  function dbg(msg) {
    try {
      pycmd("omnia:" + JSON.stringify({ plugin: "typed_accuracy", op: "dbg", data: { msg: String(msg) } }));
    } catch (e) {}
  }

  function pycmdAsync(op, data) {
    return new Promise((resolve) => {
      try {
        pycmd("omnia:" + JSON.stringify({ plugin: "typed_accuracy", op: op, data: data || {} }), resolve);
      } catch (e) {
        resolve({ ok: false, error: String(e) });
      }
    });
  }

  const BONUS_MS = 5 * 60 * 1000;

  function fmtInt(n) {
    return typeof n === "number" ? n.toLocaleString() : String(n ?? "—");
  }
  function fmtPct(n) {
    if (typeof n !== "number" || !isFinite(n)) return "—";
    return n.toFixed(1) + "%";
  }
  function setText(id, s) {
    const el = document.getElementById(id);
    if (el) el.textContent = s;
  }

  function findDeckSelectLikely() {
    const sels = Array.from(document.querySelectorAll("select"));
    if (!sels.length) return null;

    let best = null;
    let bestScore = -1;

    for (const s of sels) {
      const opts = Array.from(s.options || []);
      if (opts.length < 5) continue;

      let numericCount = 0;
      for (const o of opts) if (/^\d+$/.test(String(o.value))) numericCount++;

      const score = numericCount * 10 + opts.length;
      if (numericCount >= 3 && score > bestScore) {
        best = s;
        bestScore = score;
      }
    }
    return best;
  }

  async function getCurrentDeckDid() {
    const sel = findDeckSelectLikely();
    if (sel && /^\d+$/.test(String(sel.value))) return parseInt(sel.value, 10);

    const res = await pycmdAsync("get_current_did");
    if (res && res.ok && res.did) return parseInt(res.did, 10);
    return null;
  }

  function norm(s) {
    return String(s || "").replace(/\s+/g, " ").trim();
  }

  function findTextNodeExact(text) {
    const t = text;

    const aria = Array.from(document.querySelectorAll('[role="heading"]'));
    for (const el of aria) if (norm(el.textContent) === t) return el;

    const hs = Array.from(document.querySelectorAll("h1,h2,h3"));
    for (const el of hs) if (norm(el.textContent) === t) return el;

    const root = document.querySelector("main") || document.body;
    const candidates = Array.from(root.querySelectorAll("div,span"));
    for (const el of candidates) if (norm(el.textContent) === t) return el;

    return null;
  }

  function findGridContainer() {
    const todayEl = findTextNodeExact("Today");
    if (!todayEl) return null;

    const todayCard = todayEl.closest("section") || todayEl.closest("div");
    if (!todayCard) return null;

    let cur = todayCard.parentElement;
    for (let depth = 0; cur && depth < 15; depth++, cur = cur.parentElement) {
      if (cur.textContent && cur.textContent.includes("Today") && cur.textContent.includes("Future Due")) {
        try {
          const cs = getComputedStyle(cur);
          if (cs.display.includes("grid") || cs.display.includes("flex")) return cur;
        } catch (e) {
          return cur;
        }
      }
    }
    return null;
  }

  function makeCardElement() {
    const tpl = window.__TA_HTML_TEMPLATE;
    if (!tpl) {
      dbg("[JSDBG] missing __TA_HTML_TEMPLATE");
      return null;
    }
    const wrap = document.createElement("div");
    wrap.innerHTML = tpl;
    return wrap.firstElementChild;
  }

  function adoptCardClass(cardEl) {
    const sample =
      document.querySelector("section") ||
      document.querySelector(".card") ||
      document.querySelector("[class*='card']");
    if (sample && sample.className) {
      const keep = new Set(cardEl.className.split(/\s+/).filter(Boolean));
      const add = sample.className.split(/\s+/).filter(Boolean);
      for (const c of add) keep.add(c);
      cardEl.className = Array.from(keep).join(" ");
    }
  }

  function ensureMounted() {
    // Remove duplicates if Anki / observer storms created multiple nodes with the same id.
    try {
      const all = Array.from(document.querySelectorAll("#ta-card"));
      if (all.length > 1) {
        for (let i = 1; i < all.length; i++) all[i].remove();
      }
    } catch (e) {}

    let card = document.getElementById("ta-card");

    // If the card exists but looks "corrupted" (children wiped by re-render), rebuild it.
    try {
      if (card) {
        const hasTitle = !!card.querySelector(".ta-title");
        const hasDonut = !!card.querySelector("#ta_donut_wrap");
        const hasStats = !!card.querySelector("#ta_good_val") && !!card.querySelector("#ta_total_val");
        if (!hasTitle || !hasDonut || !hasStats) {
          card.remove();
          card = null;
        }
      }
    } catch (e) {
      // If anything goes wrong during validation, force rebuild.
      try {
        if (card) card.remove();
      } catch (e2) {}
      card = null;
    }

    if (card) return card;

    const container = findGridContainer();
    if (!container) return null;

    card = makeCardElement();
    if (!card) return null;

    adoptCardClass(card);

    // Ensure the id is correct even if the template changes.
    try {
      card.id = "ta-card";
    } catch (e) {}

    card.style.width = "auto";
    card.style.maxWidth = "none";
    card.style.minWidth = "0";
    card.style.gridColumn = "span 1";

    // Guard to prevent observer self-trigger loops during insertion.
    window.__TA_MOUNTING = true;
    try {
      const firstChild = container.querySelector("section, div");
      if (firstChild) container.insertBefore(card, firstChild);
      else container.insertBefore(card, container.firstChild || null);
    } finally {
      window.__TA_MOUNTING = false;
    }

    dbg("[JSDBG] mounted ta-card into grid container");
    return card;
  }

  function arcPath(cx, cy, r, startAngle, endAngle) {
    const rad = (a) => (a * Math.PI) / 180;
    const x1 = cx + r * Math.cos(rad(startAngle));
    const y1 = cy + r * Math.sin(rad(startAngle));
    const x2 = cx + r * Math.cos(rad(endAngle));
    const y2 = cy + r * Math.sin(rad(endAngle));
    const large = endAngle - startAngle > 180 ? 1 : 0;
    return `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
  }

  function renderDonut(svg, parts) {
    const total = parts.reduce((s, p) => s + (p.value || 0), 0);
    svg.innerHTML = "";

    const cx = 60, cy = 60, r = 46, stroke = 18;

    const base = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    base.setAttribute("cx", cx);
    base.setAttribute("cy", cy);
    base.setAttribute("r", r);
    base.setAttribute("fill", "none");
    base.setAttribute("stroke", "rgba(255,255,255,0.08)");
    base.setAttribute("stroke-width", stroke);
    svg.appendChild(base);

    if (!total) return;

    let angle = -90;
    for (const p of parts) {
      const v = p.value || 0;
      if (v <= 0) continue;
      const span = (v / total) * 360;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", arcPath(cx, cy, r, angle, angle + span));
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", p.color);
      path.setAttribute("stroke-width", stroke);
      path.setAttribute("stroke-linecap", "butt");
      svg.appendChild(path);
      angle += span;
    }
  }

  const state = {
    viewMode: "unique",
    lastKey: "",
    isRefreshing: false,
  };

  function getRangeFromUI() {
    const el = document.querySelector("input[name='ta_range']:checked");
    return el ? String(el.value) : "current";
  }

  function showCustomRow(show) {
    const row = document.getElementById("ta_custom_row");
    if (row) row.style.display = show ? "flex" : "none";
  }

  function toLocalInputValue(ms) {
    const d = new Date(ms);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function parseLocalInputValue(v) {
    if (!v) return null;
    const ms = Date.parse(v);
    if (!isFinite(ms)) return null;
    return ms;
  }

  async function computeRangeMs(did, rangeKey) {
    const now = Date.now();
    const endMs = now + BONUS_MS;

    if (rangeKey === "backlog") return { startMs: 0, endMs };
    if (rangeKey === "1m") return { startMs: now - 30 * 24 * 3600 * 1000, endMs };
    if (rangeKey === "1w") return { startMs: now - 7 * 24 * 3600 * 1000, endMs };
    if (rangeKey === "3d") return { startMs: now - 3 * 24 * 3600 * 1000, endMs };
    if (rangeKey === "1d") return { startMs: now - 1 * 24 * 3600 * 1000, endMs };

    if (rangeKey === "custom") {
      const fromEl = document.getElementById("ta_from");
      const toEl = document.getElementById("ta_to");
      const fromMs = parseLocalInputValue(fromEl?.value) ?? (now - 24 * 3600 * 1000);
      const toMs = parseLocalInputValue(toEl?.value) ?? now;
      return { startMs: fromMs, endMs: toMs + BONUS_MS };
    }

    const res = await pycmdAsync("get_session_open_ms", { did });
    const openMs = res && res.ok ? Number(res.openMs || 0) : 0;
    if (!openMs) return { startMs: now, endMs };
    return { startMs: openMs, endMs };
  }

  function applyCardResponsiveClass(card) {
    // Responsive based on the actual card width (not the window width).
    try {
      if (!card) return null;

      const rect = card.getBoundingClientRect();
      const w = Math.round(rect && rect.width ? rect.width : card.clientWidth || 0);
      if (!w) return null;

      let mode = "ta-wide";
      if (w < 520) mode = "ta-narrow";
      else if (w < 620) mode = "ta-medium";

      if (card.__taResponsiveMode !== mode) {
        card.classList.remove("ta-narrow", "ta-medium", "ta-wide");
        card.classList.add(mode);
        card.__taResponsiveMode = mode;
        dbg(`[UI] responsive mode=${mode} w=${w}`);
      }
      return mode;
    } catch (e) {
      return null;
    }
  }

  async function refresh(force = false) {
    if (state.isRefreshing) return;
    state.isRefreshing = true;

    try {
      const card = ensureMounted();
      if (!card) return;

      // Apply responsive class based on the actual card width in the Stats grid.
      applyCardResponsiveClass(card);

      bindUIOnce();

      const did = await getCurrentDeckDid();
      if (!did) return;

      const includeSubdecks = !!document.getElementById("ta_subdecks")?.checked;
      const rangeKey = getRangeFromUI();

      showCustomRow(rangeKey === "custom");

      if (rangeKey === "custom") {
        const fromEl = document.getElementById("ta_from");
        const toEl = document.getElementById("ta_to");

        if (fromEl && !fromEl.__taInit) {
          fromEl.__taInit = true;
          const n = new Date();
          n.setHours(0, 0, 0, 0);
          fromEl.value = toLocalInputValue(n.getTime());
        }
        if (toEl && !toEl.__taInit) {
          toEl.__taInit = true;
          toEl.value = toLocalInputValue(Date.now());
        }
      }

      const { startMs, endMs } = await computeRangeMs(did, rangeKey);

      const tick = Math.floor(Date.now() / 2000);
      const key = [did, includeSubdecks ? 1 : 0, rangeKey, state.viewMode, startMs, endMs, tick].join("|");
      if (!force && key === state.lastKey) return;
      state.lastKey = key;

      const res = await pycmdAsync("query", { did, includeSubdecks, startMs, endMs });
      if (!res || !res.ok) {
        setText("ta_err", "Query failed: " + (res?.error ?? "unknown"));
        return;
      }
      setText("ta_err", "");

      const data = res.data;
      const view = state.viewMode === "attempts" ? data.attempts : data.unique_last;

      setText("ta_good_val", fmtInt(view.good));
      setText("ta_bad_val", fmtInt(view.bad));
      setText("ta_miss_val", fmtInt(view.miss));
      setText("ta_empty_val", fmtInt(view.empty));

      setText("ta_good_pct", fmtPct(view.p_good));
      setText("ta_bad_pct", fmtPct(view.p_bad));
      setText("ta_miss_pct", fmtPct(view.p_miss));
      setText("ta_empty_pct", fmtPct(view.p_empty));

      setText("ta_total_val", fmtInt(view.total));
      setText("ta_center_num", fmtInt(view.total));
      setText("ta_center_label", state.viewMode === "attempts" ? "attempts" : "unique");

      const donut = document.getElementById("ta_donut");
      if (donut) {
        renderDonut(donut, [
          { value: view.good, color: "#00c853" },
          { value: view.bad, color: "#ff1744" },
          { value: view.miss, color: "#ffd600" },
          { value: view.empty, color: "#9e9e9e" },
        ]);
      }
    } finally {
      state.isRefreshing = false;
    }
  }

  function bindUIOnce() {
    const wrap = document.getElementById("ta_donut_wrap");
    if (wrap && !wrap.__taBound) {
      wrap.__taBound = true;
      wrap.addEventListener("click", () => {
        state.viewMode = state.viewMode === "unique" ? "attempts" : "unique";
        refresh(true);
      });
    }

    const sub = document.getElementById("ta_subdecks");
    if (sub && !sub.__taBound) {
      sub.__taBound = true;
      sub.addEventListener("change", () => refresh(true));
    }

    for (const r of document.querySelectorAll("input[name='ta_range']")) {
      if (r.__taBound) continue;
      r.__taBound = true;
      r.addEventListener("change", () => refresh(true));
    }

    const fromEl = document.getElementById("ta_from");
    const toEl = document.getElementById("ta_to");
    if (fromEl && !fromEl.__taBound) {
      fromEl.__taBound = true;
      fromEl.addEventListener("change", () => refresh(true));
    }
    if (toEl && !toEl.__taBound) {
      toEl.__taBound = true;
      toEl.addEventListener("change", () => refresh(true));
    }
  }

  function watchStatsRerender() {
    const obs = new MutationObserver(() => {
      const card = document.getElementById("ta-card");
      if (!card) {
        ensureMounted();
        refresh(true);
        return;
      }

      // Grid can change without removing the card; keep responsive mode in sync.
      applyCardResponsiveClass(card);
    });

    obs.observe(document.documentElement || document.body, {
      subtree: true,
      childList: true,
    });
  }

  async function boot() {
    dbg(`[JS] boot href=${location.href}`);

    let tries = 0;
    const tick = async () => {
      tries++;
      const mounted = ensureMounted();
      if (mounted) {
        watchStatsRerender();
        refresh(true);

        setInterval(() => {
          if (document.getElementById("ta-card")) refresh(false);
        }, 2000);
        return;
      }
      // Bounded poll: this JS is eval'd on EVERY styled webview (editor, browser, …), not
      // just the stats screen. Cap at ~3s (60×50ms) so a non-stats page where the grid never
      // appears stops scanning quickly instead of burning 10s of DOM work on, e.g., editor open.
      if (tries < 60) setTimeout(tick, 50);
    };

    tick();
  }

  window.__TA_refresh = refresh;
  boot();
})();
