
  // Initial state is baked into window.__SN_INIT (server-side) so the page renders populated
  // without an init pycmd callback — Anki's bridge callback channel isn't ready the instant
  // this inline script runs. Fall back to a live list_note_types only if nothing was baked.
  var INIT = window.__SN_INIT || null;
  if (INIT && INIT.note_types && INIT.note_types.length) {
    fill(noteTypeSel, INIT.note_types, INIT.note_type || INIT.note_types[0] || "");
    applyLoad(INIT);
  } else {
    send("list_note_types", {}, function (names) {
      fill(noteTypeSel, names || [], (names && names[0]) || "");
      loadNoteType();
    });
  }
})();
