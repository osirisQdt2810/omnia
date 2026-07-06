// Display Interval grading-bar label. Three self-contained, CSP-safe snippets, one per
// ``// ===NAME===`` section. The Python loader (``plugins/display_interval/__init__.py``)
// slices out a section and, for RENDER, replaces the ``__TEXT__`` placeholder with the
// JSON-encoded label and ``__COLOR__`` with the JSON-encoded hex colour (so the dynamic
// values stay in Python while the JS body lives here).
//
// These run in the reviewer's PERSISTENT bottom (grading) bar webview, not the card webview:
// the appended ``<div>`` survives across cards because per-card updates only touch an inner
// element, never document.body. Styling is applied imperatively (fixed bottom-right, bold,
// pointer-events:none so it never intercepts an ease-button click); a neutral shadow keeps it
// legible on both light and dark themes, and the colour itself is configured (Text color).
//
// The ``// ===NAME===`` markers, the ``__TEXT__`` / ``__COLOR__`` placeholder tokens, and the
// ``__TA_NEXT_IVL`` element id are load-bearing and stay verbatim.

// ===HIDE===
(function () {
  const d = document.getElementById('__TA_NEXT_IVL');
  if (d) {
    d.style.display = 'none';
  }
})();

// ===RENDER===
(function () {
  let el = document.getElementById('__TA_NEXT_IVL');
  if (!el) {
    el = document.createElement('div');
    el.id = '__TA_NEXT_IVL';
    el.style.position = 'fixed';
    el.style.right = '12px';
    el.style.top = '2px'; // top-right of the grading bar, above the "More" button
    el.style.zIndex = '999999';
    el.style.fontSize = '12px';
    el.style.fontWeight = '800';
    el.style.pointerEvents = 'none';
    el.style.userSelect = 'none';
    el.style.whiteSpace = 'nowrap';
    el.style.textShadow = '0 1px 2px rgba(0,0,0,0.35)';
    document.body.appendChild(el);
  }
  el.style.color = __COLOR__ || '#c62828';
  el.textContent = __TEXT__;
  el.style.display = 'block';
})();

// ===REMOVE===
(function () {
  const d = document.getElementById('__TA_NEXT_IVL');
  if (d && d.parentNode) {
    d.parentNode.removeChild(d);
  }
})();
