
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