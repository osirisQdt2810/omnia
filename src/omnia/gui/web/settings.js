/**
 * @fileoverview Settings page behavior: wires the per-plugin enable switches and the
 * "Configure…" buttons to the Omnia WebDialog bridge. Each switch posts a `toggle` op and
 * reflects a failed enable; each button posts a `configure` op.
 */

(function () {
  /**
   * Post an Omnia envelope to Python via the WebDialog bridge.
   * @param {string} op The op name (`toggle` or `configure`).
   * @param {!Object} data The op payload.
   * @param {?function(*)} cb Callback resolved with the handler's return value.
   */
  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({plugin: "settings", op: op, data: data}), cb);
  }

  /**
   * Update a card's status line and failed-enable styling.
   * @param {!Element} card The `.omnia-card` element.
   * @param {string} text The status text to show.
   * @param {boolean} failed Whether to mark the card as failed-to-enable.
   */
  function setStatus(card, text, failed) {
    const s = card.querySelector(".omnia-card-status");
    if (s) {
      s.textContent = text;
    }
    card.classList.toggle("omnia-failed", !!failed);
  }

  document.querySelectorAll(".omnia-switch input").forEach(function (input) {
    input.addEventListener("change", function () {
      const card = input.closest(".omnia-card");
      const id = input.getAttribute("data-id");
      const enabled = input.checked;
      send("toggle", {id: id, enabled: enabled}, function (res) {
        const active = !!(res && res.active);
        if (enabled && !active) {
          input.checked = false;
          setStatus(card, "failed to enable — see logs", true);
        } else {
          setStatus(card, active ? "active" : "off", false);
        }
      });
    });
  });

  document.querySelectorAll(".omnia-configure").forEach(function (btn) {
    btn.addEventListener("click", function () {
      send("configure", {id: btn.getAttribute("data-id")}, null);
    });
  });
})();
