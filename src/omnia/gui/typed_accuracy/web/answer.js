(function () {
  function send(op, data) {
    try {
      pycmd("omnia:" + JSON.stringify({ plugin: "typed_accuracy", op: op, data: data }));
    } catch (e) {}
  }
  function textLen(root, selector) {
    var n = 0, els = root.querySelectorAll(selector);
    for (var i = 0; i < els.length; i++) n += (els[i].textContent || "").length;
    return n;
  }
  var tries = 0;
  function run() {
    tries++;
    var el = document.getElementById("typeans");
    if (!el) return;  // not a type-answer card

    var hasGood = el.querySelector(".typeGood") != null;
    var hasBad = el.querySelector(".typeBad") != null;
    var hasMiss = el.querySelector(".typeMissed") != null;
    var hadMarkup = hasGood || hasBad || hasMiss;

    if (hadMarkup) {
      var goodLen = textLen(el, ".typeGood");
      var badLen = textLen(el, ".typeBad");
      var missLen = textLen(el, ".typeMissed");
      var denom = goodLen + badLen + missLen;
      var ratio = denom ? goodLen / denom : 0.0;
      send("rated", { ratio: ratio, hasGood: hasGood, hasBad: hasBad, hasMiss: hasMiss });
      return;
    }

    if (tries < 40) { setTimeout(run, 50); return; }

    // No markup after polling: an empty typed answer. ratio 0 forces Hard.
    send("rated", { ratio: 0.0, hasGood: false, hasBad: false, hasMiss: false });
  }
  run();
})();
