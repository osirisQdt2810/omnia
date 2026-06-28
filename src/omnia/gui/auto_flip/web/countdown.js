// Auto-flip reviewer countdown overlay. Three self-contained, CSP-safe snippets, one per
// marker section. The Python loader (``plugins/auto_flip/countdown.py``) splits on the
// ``// ===NAME===`` markers and replaces the ``__TOKEN__`` placeholders with JSON-encoded
// values (so the dynamic parts stay in Python; the JS body lives here). Each section below is
// reformatted to the Google JS Style Guide; the ``// ===NAME===`` markers and the ``__...__``
// placeholder tokens are load-bearing and MUST stay verbatim.

// ===BUILD===
(function () {
  const id = __EID__;
  const totalMs = __TOTAL_MS__;
  const circumference = __CIRCUMFERENCE__;
  const tickMs = __TICK_MS__;
  if (window.__omniaAutoflipTimers && window.__omniaAutoflipTimers[id]) {
    clearInterval(window.__omniaAutoflipTimers[id]);
  }
  if (!window.__omniaAutoflipTimers) {
    window.__omniaAutoflipTimers = {};
  }
  const existing = document.getElementById(id);
  if (existing) {
    existing.parentNode.removeChild(existing);
  }
  const box = document.createElement("div");
  box.id = id;
  box.style.cssText = "position:fixed;right:16px;bottom:16px;width:48px;height:48px;" +
      "z-index:2147483647;pointer-events:none;font:600 12px sans-serif;color:#888;";
  box.innerHTML =
      '<svg width="48" height="48" viewBox="0 0 48 48" style="position:absolute;' +
      'top:0;left:0;transform:rotate(-90deg);">' +
      '<circle cx="24" cy="24" r="__RADIUS__" fill="none" stroke="rgba(128,128,128,0.25)"' +
      ' stroke-width="4"></circle>' +
      '<circle class="omnia-ring" cx="24" cy="24" r="__RADIUS__" fill="none"' +
      ' stroke="currentColor" stroke-width="4" stroke-linecap="round"' +
      ' stroke-dasharray="' + circumference + '" stroke-dashoffset="0"></circle>' +
      '</svg>' +
      '<span class="omnia-secs" style="position:absolute;top:0;left:0;width:48px;' +
      'height:48px;display:flex;align-items:center;justify-content:center;"></span>';
  document.body.appendChild(box);
  const ring = box.querySelector(".omnia-ring");
  const label = box.querySelector(".omnia-secs");
  const start = Date.now();
  function render() {
    const elapsed = Date.now() - start;
    const remaining = Math.max(0, totalMs - elapsed);
    const fraction = totalMs > 0 ? remaining / totalMs : 0;
    if (ring) {
      ring.setAttribute("stroke-dashoffset", circumference * (1 - fraction));
    }
    if (label) {
      label.textContent = (remaining / 1000).toFixed(1);
    }
    if (remaining <= 0) {
      clearInterval(window.__omniaAutoflipTimers[id]);
      delete window.__omniaAutoflipTimers[id];
    }
  }
  render();
  window.__omniaAutoflipTimers[id] = setInterval(render, tickMs);
})();

// ===CLEAR===
(function () {
  const id = __EID__;
  if (window.__omniaAutoflipTimers && window.__omniaAutoflipTimers[id]) {
    clearInterval(window.__omniaAutoflipTimers[id]);
    delete window.__omniaAutoflipTimers[id];
  }
  const el = document.getElementById(id);
  if (el) {
    el.parentNode.removeChild(el);
  }
})();

// ===CANCELLED===
(function () {
  const id = __EID__;
  if (window.__omniaAutoflipTimers && window.__omniaAutoflipTimers[id]) {
    clearInterval(window.__omniaAutoflipTimers[id]);
    delete window.__omniaAutoflipTimers[id];
  }
  const el = document.getElementById(id);
  if (!el) {
    return;
  }
  el.style.color = __COLOR__;
  const ring = el.querySelector(".omnia-ring");
  if (ring) {
    ring.setAttribute("stroke-dashoffset", 0);
  }
})();
