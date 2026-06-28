
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