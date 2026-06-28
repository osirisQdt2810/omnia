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