// Display Interval reviewer overlay. Two self-contained, CSP-safe snippets, one per
// ``// ===NAME===`` section. The Python loader (``plugins/display_interval/__init__.py``)
// slices out a section and, for RENDER, replaces the ``__TEXT__`` placeholder with the
// JSON-encoded label (so the dynamic text stays in Python; the JS body lives here).
//
// Styling is applied imperatively in JS (mirroring the reference) so it can switch on Anki's
// night-mode class at render time: white + shadow at night, red (#c62828) in day, bold, fixed
// bottom-right, and pointer-events:none so it never intercepts clicks.
//
// Reformatted to the Google JS Style Guide; the ``// ===NAME===`` markers, the ``__TEXT__``
// placeholder token, and the ``__TA_NEXT_IVL`` element id are load-bearing and stay verbatim.

// ===HIDE===
(function () {
  const d = document.getElementById('__TA_NEXT_IVL');
  if (d) {
    d.style.display = 'none';
  }
})();

// ===RENDER===
(function () {
  /**
   * Detect Anki night mode from the location hash, body class, or root class.
   * @return {boolean}
   */
  function night() {
    try {
      const s = (location && location.hash ? String(location.hash) : '').toLowerCase();
      if (s.indexOf('night') >= 0) {
        return true;
      }
    } catch (e) {}
    try {
      const b = document.body;
      if (b && (b.className || '').toLowerCase().indexOf('night') >= 0) {
        return true;
      }
    } catch (e) {}
    try {
      const de = document.documentElement;
      if (de && (de.className || '').toLowerCase().indexOf('night') >= 0) {
        return true;
      }
    } catch (e) {}
    return false;
  }
  let el = document.getElementById('__TA_NEXT_IVL');
  if (!el) {
    el = document.createElement('div');
    el.id = '__TA_NEXT_IVL';
    el.style.position = 'fixed';
    el.style.right = '14px';
    el.style.bottom = '4px';
    el.style.zIndex = '999999';
    el.style.fontSize = '12px';
    el.style.fontWeight = '800';
    el.style.pointerEvents = 'none';
    el.style.userSelect = 'none';
    el.style.whiteSpace = 'nowrap';
    document.body.appendChild(el);
  }
  if (night()) {
    el.style.color = '#ffffff';
    el.style.opacity = '0.85';
    el.style.textShadow = '0 1px 2px rgba(0,0,0,0.55)';
  } else {
    el.style.color = '#c62828';
    el.style.opacity = '0.90';
    el.style.textShadow = 'none';
  }
  el.textContent = __TEXT__;
  el.style.display = 'block';
})();
