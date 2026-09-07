/**
 * @fileoverview Settings page behavior. Two jobs: switch between the category landing and a
 * category's detail view (both are already in the document — only one is visible), and wire
 * the per-plugin enable switches and "Configure…" buttons to the Omnia WebDialog bridge.
 * Each switch posts a `toggle` op and reflects a failed enable; each button posts a
 * `configure` op. The view switching is entirely client-side — no extra op, no round trip.
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

  const landing = document.getElementById("omnia-landing");
  // The tile that opened the current category, so Escape/Back can hand focus back to it.
  let openTile = null;

  /**
   * Restart an entrance animation on an element that is already in the DOM.
   * An element revealed from `display: none` animates on its own the first time, but on a
   * second visit the class is still there and nothing replays — removing it, forcing a
   * reflow, and re-adding it is what makes the browser start the animation over.
   * @param {!Element} el The view to re-animate.
   */
  function replay(el) {
    el.classList.remove("omnia-enter");
    void el.offsetWidth;
    el.classList.add("omnia-enter");
  }

  /**
   * Find the tile that opens a category.
   * @param {string} key The category's `data-category` handle.
   * @return {?Element} The tile, or null.
   */
  function tileFor(key) {
    // Safe to interpolate: the key is a Python-built slug restricted to [a-z0-9-].
    return document.querySelector('.omnia-tile[data-category="' + key + '"]');
  }

  /**
   * Show one category's detail view and hide the landing.
   * @param {!Element} tile The tile that was activated.
   */
  function showCategory(tile) {
    const key = tile.getAttribute("data-category");
    const view = document.querySelector('.omnia-category[data-category="' + key + '"]');
    if (!view) {
      return;
    }
    openTile = tile;
    landing.hidden = true;
    view.hidden = false;
    replay(view);
    document.body.dataset.view = "category";
    window.scrollTo(0, 0);
    // The heading, not the section: it names where you have arrived, and a ring around one
    // heading looks deliberate where a ring around the whole view looked like an error.
    (view.querySelector(".omnia-cat-name") || view).focus();
  }

  /** Hide whichever category is open and bring the landing back. */
  function showLanding() {
    document.querySelectorAll(".omnia-category").forEach(function (view) {
      view.hidden = true;
    });
    landing.hidden = false;
    replay(landing);
    document.body.dataset.view = "landing";
    window.scrollTo(0, 0);
    if (openTile) {
      openTile.focus();
      openTile = null;
    }
  }

  /**
   * Re-derive a category's "N of M on" tile label from its switches.
   * Counting the live DOM (rather than tracking a number) is what keeps a failed enable
   * honest: the toggle handler unchecks the input first, so the recount never sees it.
   * @param {!Element} card The `.omnia-card` whose switch just changed.
   */
  function refreshCount(card) {
    const view = card.closest(".omnia-category");
    if (!view) {
      return;
    }
    const tile = tileFor(view.getAttribute("data-category"));
    if (!tile) {
      return;
    }
    const on = view.querySelectorAll(".omnia-switch input:checked").length;
    // Only the number: the sentence around it is Python's, written once at render time.
    const label = tile.querySelector(".omnia-tile-on");
    if (label) {
      label.textContent = String(on);
    }
    tile.classList.toggle("omnia-on", on > 0);
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

  document.querySelectorAll(".omnia-tile").forEach(function (tile) {
    tile.addEventListener("click", function () {
      showCategory(tile);
    });
  });

  document.querySelectorAll(".omnia-back").forEach(function (btn) {
    btn.addEventListener("click", showLanding);
  });

  // Escape leaves a category; on the landing it is left alone so the dialog still closes.
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && document.body.dataset.view === "category") {
      ev.preventDefault();
      showLanding();
    }
  });

  document.querySelectorAll(".omnia-switch input").forEach(function (input) {
    input.addEventListener("change", function () {
      const card = input.closest(".omnia-card");
      const id = input.getAttribute("data-id");
      const enabled = input.checked;
      send("toggle", {id: id, enabled: enabled}, function (res) {
        const active = !!(res && res.active);
        const failed = enabled && !active;
        if (failed) {
          input.checked = false;
        }
        // Python already worked out the wording and handed it over; falling back to our own
        // only covers the bridge returning nothing (no media server -> inert callbacks).
        setStatus(card, (res && res.status) || (active ? "active" : "off"), failed);
        // AFTER the failed-enable uncheck above, so the tile can't count a switch that
        // bounced back off.
        refreshCount(card);
      });
    });
  });

  document.querySelectorAll(".omnia-configure").forEach(function (btn) {
    btn.addEventListener("click", function () {
      send("configure", {id: btn.getAttribute("data-id")}, null);
    });
  });

  /**
   * Re-state one card from Python. Configuring a plugin reloads it, and a reload can fail —
   * leaving a card that says "active" and a tile that counts it. There is no re-render to lean
   * on (that would throw the reader back to the landing), so the Qt side pushes the new state
   * for the one card it touched.
   * @param {{id: string, enabled: boolean, active: boolean, status: string}} state
   */
  window.omniaSettings = {
    setCardState: function (state) {
      // Matched by attribute rather than a built selector: a plugin id is not our string.
      const cards = document.querySelectorAll(".omnia-card");
      for (let i = 0; i < cards.length; i++) {
        if (cards[i].getAttribute("data-id") !== state.id) {
          continue;
        }
        const input = cards[i].querySelector(".omnia-switch input");
        if (input) {
          input.checked = !!state.enabled;
        }
        setStatus(cards[i], state.status, !!state.enabled && !state.active);
        refreshCount(cards[i]);
        return;
      }
    },
  };

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
