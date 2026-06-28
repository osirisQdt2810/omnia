/**
 * @fileoverview Typed-accuracy stats panel — part 1 of 5 of the panel IIFE.
 * This fragment OPENS the IIFE; load order matters (01-bridge, 02-format, 03-donut,
 * 04-table, 05-boot are concatenated with "\n" by gui/typed_accuracy/stats_injector.py). It
 * holds the boot guard and the Omnia bridge helpers.
 */

(function () {
  if (window.__TA_BOOTED) {
    try {
      if (typeof window.__TA_refresh === "function") {
        window.__TA_refresh(true);
      }
    } catch (e) {}
    return;
  }
  window.__TA_BOOTED = true;

  // Omnia bridge envelope: pycmd("omnia:" + {plugin, op, data}); the handler's return value
  // resolves the pycmd Promise. Mirrors core/reviewer/web_injector.py's MESSAGE_PREFIX.

  /**
   * Forward a debug message to the Python logger (best-effort).
   * @param {*} msg The message to log.
   */
  function dbg(msg) {
    try {
      pycmd("omnia:" + JSON.stringify({plugin: "typed_accuracy", op: "dbg", data: {msg: String(msg)}}));
    } catch (e) {}
  }

  /**
   * Post an Omnia op and resolve with the handler's return value.
   * @param {string} op The op name.
   * @param {!Object=} data The op payload.
   * @return {!Promise<*>}
   */
  function pycmdAsync(op, data) {
    return new Promise((resolve) => {
      try {
        pycmd("omnia:" + JSON.stringify({plugin: "typed_accuracy", op: op, data: data || {}}), resolve);
      } catch (e) {
        resolve({ok: false, error: String(e)});
      }
    });
  }
