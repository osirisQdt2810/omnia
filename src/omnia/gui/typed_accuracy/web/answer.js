/**
 * @fileoverview Answer-side typed-accuracy probe injected into the reviewer. Measures Anki's
 * typed-answer comparison markup (.typeGood/.typeBad/.typeMissed), polling briefly for
 * late-rendered markup, then ALWAYS reports a `rated` result — including the empty/no-markup
 * case (ratio 0). Non-type-answer cards (no #typeans) report nothing.
 */

(function () {
  /**
   * Post a fire-and-forget Omnia op (best-effort).
   * @param {string} op The op name.
   * @param {!Object} data The op payload.
   */
  function send(op, data) {
    try {
      pycmd("omnia:" + JSON.stringify({plugin: "typed_accuracy", op: op, data: data}));
    } catch (e) {}
  }

  /**
   * Sum the text length of every element matching `selector` under `root`.
   * @param {!Element} root The root element to search.
   * @param {string} selector The CSS selector.
   * @return {number}
   */
  function textLen(root, selector) {
    let n = 0;
    const els = root.querySelectorAll(selector);
    for (let i = 0; i < els.length; i++) {
      n += (els[i].textContent || "").length;
    }
    return n;
  }

  let tries = 0;

  /** Probe the typed-answer markup, polling for late renders, then report the result. */
  function run() {
    tries++;
    const el = document.getElementById("typeans");
    if (!el) {
      return; // not a type-answer card
    }

    const hasGood = el.querySelector(".typeGood") != null;
    const hasBad = el.querySelector(".typeBad") != null;
    const hasMiss = el.querySelector(".typeMissed") != null;
    const hadMarkup = hasGood || hasBad || hasMiss;

    if (hadMarkup) {
      const goodLen = textLen(el, ".typeGood");
      const badLen = textLen(el, ".typeBad");
      const missLen = textLen(el, ".typeMissed");
      const denom = goodLen + badLen + missLen;
      const ratio = denom ? goodLen / denom : 0.0;
      send("rated", {ratio: ratio, hasGood: hasGood, hasBad: hasBad, hasMiss: hasMiss});
      return;
    }

    if (tries < 40) {
      setTimeout(run, 50);
      return;
    }

    // No markup after polling: an empty typed answer. ratio 0 forces Hard.
    send("rated", {ratio: 0.0, hasGood: false, hasBad: false, hasMiss: false});
  }

  run();
})();
