
  /**
   * Typed-accuracy stats panel — part 2 of 5 of the panel IIFE (load order matters).
   * Formatting helpers and DOM heuristics for locating the deck select and the stats grid.
   */

  const BONUS_MS = 5 * 60 * 1000;

  /**
   * Format a number with locale grouping, or an em dash for nullish values.
   * @param {*} n The value to format.
   * @return {string}
   */
  function fmtInt(n) {
    return typeof n === "number" ? n.toLocaleString() : String(n ?? "—");
  }

  /**
   * Format a percentage to one decimal place, or an em dash when not finite.
   * @param {*} n The value to format.
   * @return {string}
   */
  function fmtPct(n) {
    if (typeof n !== "number" || !isFinite(n)) {
      return "—";
    }
    return n.toFixed(1) + "%";
  }

  /**
   * Set the text content of an element by id (no-op when absent).
   * @param {string} id The element id.
   * @param {string} s The text to set.
   */
  function setText(id, s) {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = s;
    }
  }

  /**
   * Heuristically find the deck <select> on the stats screen (numeric-valued options).
   * @return {?HTMLSelectElement}
   */
  function findDeckSelectLikely() {
    const sels = Array.from(document.querySelectorAll("select"));
    if (!sels.length) {
      return null;
    }

    let best = null;
    let bestScore = -1;

    for (const s of sels) {
      const opts = Array.from(s.options || []);
      if (opts.length < 5) {
        continue;
      }

      let numericCount = 0;
      for (const o of opts) {
        if (/^\d+$/.test(String(o.value))) {
          numericCount++;
        }
      }

      const score = numericCount * 10 + opts.length;
      if (numericCount >= 3 && score > bestScore) {
        best = s;
        bestScore = score;
      }
    }
    return best;
  }

  /**
   * Resolve the current deck id from the UI select, falling back to the Python bridge.
   * @return {!Promise<?number>}
   */
  async function getCurrentDeckDid() {
    const sel = findDeckSelectLikely();
    if (sel && /^\d+$/.test(String(sel.value))) {
      return parseInt(sel.value, 10);
    }

    const res = await pycmdAsync("get_current_did");
    if (res && res.ok && res.did) {
      return parseInt(res.did, 10);
    }
    return null;
  }

  /**
   * Collapse whitespace and trim a string.
   * @param {*} s The value to normalize.
   * @return {string}
   */
  function norm(s) {
    return String(s || "").replace(/\s+/g, " ").trim();
  }

  /**
   * Find the first element whose normalized text exactly equals `text`.
   * @param {string} text The exact text to match.
   * @return {?Element}
   */
  function findTextNodeExact(text) {
    const t = text;

    const aria = Array.from(document.querySelectorAll('[role="heading"]'));
    for (const el of aria) {
      if (norm(el.textContent) === t) {
        return el;
      }
    }

    const hs = Array.from(document.querySelectorAll("h1,h2,h3"));
    for (const el of hs) {
      if (norm(el.textContent) === t) {
        return el;
      }
    }

    const root = document.querySelector("main") || document.body;
    const candidates = Array.from(root.querySelectorAll("div,span"));
    for (const el of candidates) {
      if (norm(el.textContent) === t) {
        return el;
      }
    }

    return null;
  }

  /**
   * Find the stats grid container that holds the "Today"/"Future Due" cards.
   * @return {?Element}
   */
  function findGridContainer() {
    const todayEl = findTextNodeExact("Today");
    if (!todayEl) {
      return null;
    }

    const todayCard = todayEl.closest("section") || todayEl.closest("div");
    if (!todayCard) {
      return null;
    }

    let cur = todayCard.parentElement;
    for (let depth = 0; cur && depth < 15; depth++, cur = cur.parentElement) {
      if (cur.textContent && cur.textContent.includes("Today") && cur.textContent.includes("Future Due")) {
        try {
          const cs = getComputedStyle(cur);
          if (cs.display.includes("grid") || cs.display.includes("flex")) {
            return cur;
          }
        } catch (e) {
          return cur;
        }
      }
    }
    return null;
  }
