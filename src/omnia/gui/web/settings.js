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

  // The (i) help popover is anchored below its icon by default; on the last card of a
  // non-scrolling dialog that clips it under the window edge. Before it shows, measure the
  // room below the icon and flip the popover above when it won't fit (mirrors the graph
  // tooltip's flip). offsetHeight is readable while the tip is only visibility:hidden.
  document.querySelectorAll(".omnia-info").forEach(function (info) {
    const flip = function () {
      const tip = info.querySelector(".omnia-tip");
      if (!tip) {
        return;
      }
      info.classList.remove("omnia-tip-above");
      const rect = info.getBoundingClientRect();
      const below = window.innerHeight - rect.bottom;
      if (below < tip.offsetHeight + 16 && rect.top > below) {
        info.classList.add("omnia-tip-above");
      }
    };
    info.addEventListener("mouseenter", flip);
    info.addEventListener("focus", flip);
  });
})();
