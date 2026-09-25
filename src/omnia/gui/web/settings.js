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

  // Omnia's own actions sit on the header row rather than in the grid: they open a window
  // instead of a category, and a tile among the features would imply Sync is one of them.
  // Bound by the ATTRIBUTE, not by the button class, so moving one of these somewhere else on
  // the page cannot quietly disconnect it — which is exactly what happened when the Sync tile
  // became a header button and the tile handler stopped seeing it.
  document.querySelectorAll("[data-action]").forEach(function (button) {
    button.addEventListener("click", function () {
      send(button.getAttribute("data-action"), {});
    });
  });

  /**
   * Show how far a background job on an action button has got.
   *
   * Pushed in by Python rather than polled from here: the job outlives this page, and a page
   * that asked would have to know what to ask about.
   *
   * @param {string} op Which action button, by its data-action.
   * @param {?number} percent 0-100, or null when the size is not yet known.
   * @param {string} tip What the hover readout says. Empty clears it.
   */
  function setActionProgress(op, percent, tip) {
    const button = document.querySelector('[data-action="' + op + '"]');
    if (!button) { return; }
    const readout = button.querySelector(".omnia-action-tip");
    if (readout) { readout.textContent = tip || ""; }
    if (percent === null || percent === undefined) {
      button.setAttribute("data-progress", tip ? "unknown" : "none");
      button.style.removeProperty("--progress");
      return;
    }
    button.setAttribute("data-progress", "known");
    button.style.setProperty("--progress", percent + "%");
  }

  // `:not(.omnia-config-back)`: the config panel's Back wears the same class for the same look
  // but returns to the CATEGORY it was opened from, not to the landing.
  /**
   * Show how far a background job on a PLUGIN CARD has got.
   *
   * The same idea as `setActionProgress`, on a different element: pushed in by Python rather
   * than polled from here, because the job outlives this page and a page that asked would have
   * to know what to ask about. A card is left alone — and its strip stays hidden — unless
   * Python sends something for it, so a plugin with no job never grows a bar.
   *
   * @param {string} id The plugin id, matched against the card's data-id.
   * @param {?number} percent 0-100, or null when the size is not yet known.
   * @param {string} text The count, e.g. "142 of 300". Empty hides the whole strip.
   * @param {boolean} stoppable Whether the Stop button accepts a press.
   */
  function setCardProgress(id, percent, text, stoppable) {
    const cards = document.querySelectorAll(".omnia-card");
    for (let i = 0; i < cards.length; i++) {
      if (cards[i].getAttribute("data-id") !== id) {
        continue;
      }
      // The CARD carries the state: it is the bar, and the readout beside Configure is a
      // reading of it. Both are driven from the one attribute so they cannot disagree.
      const card = cards[i];
      const readout = card.querySelector(".omnia-card-job-text");
      if (readout) {
        readout.textContent = text || "";
      }
      const stop = card.querySelector(".omnia-card-job-stop");
      if (stop) {
        stop.disabled = !stoppable;
      }
      if (!text) {
        card.setAttribute("data-progress", "none");
        card.style.removeProperty("--progress");
        return;
      }
      if (percent === null || percent === undefined) {
        card.setAttribute("data-progress", "unknown");
        card.style.removeProperty("--progress");
        return;
      }
      card.setAttribute("data-progress", "known");
      card.style.setProperty("--progress", percent + "%");
      return;
    }
  }

  document.querySelectorAll(".omnia-card-job-stop").forEach(function (button) {
    button.addEventListener("click", function () {
      // Disabled immediately so a second press cannot ask twice, and left that way: the next
      // poll re-states it from what the job actually reports.
      button.disabled = true;
      send("stop-job", {id: button.getAttribute("data-stop")}, null);
    });
  });

  document.querySelectorAll(".omnia-back:not(.omnia-config-back)").forEach(function (btn) {
    btn.addEventListener("click", showLanding);
  });

  // Escape unwinds ONE step: config -> category -> landing. On the landing it is left alone so
  // the dialog still closes, which is the behaviour people expect of the outermost view.
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") {
      return;
    }
    const view = document.body.dataset.view;
    if (view === "config") {
      ev.preventDefault();
      // A dropdown swallows the first Escape: closing a popup is what that key does there.
      if (!closeOpenDropdown()) {
        leaveConfig();
      }
      return;
    }
    if (view === "category") {
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
      // Python answers with the panel payload for a plugin whose settings are declared fields,
      // and with null for one that owns a bespoke dialog — which it opens itself, on a later
      // event-loop turn (see SettingsDialog._on_configure).
      send("configure", {id: btn.getAttribute("data-id")}, function (payload) {
        if (payload && payload.fields) {
          openConfig(payload, btn);
        }
      });
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
    setActionProgress: setActionProgress,
    setCardProgress: setCardProgress,
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

  // ======================================================================================
  // Config panel — one plugin's settings, rendered in place of a second window.
  //
  // Everything below builds DOM with createElement and textContent rather than innerHTML. Not
  // ceremony: a field's value is whatever the user last typed into it, and a label is authored
  // by a plugin — neither has any business being parsed as markup.
  //
  // WHICH control a field gets is decided in Python (gui/config_panel.py), where it can be
  // tested without a browser. This half draws what it is told and reads the answers back.
  // ======================================================================================

  const configView = document.getElementById("omnia-config");
  const configFields = document.getElementById("omnia-config-fields");
  const configName = document.getElementById("omnia-config-name");
  const configNote = document.getElementById("omnia-config-note");

  // The open panel: which plugin, which category to go back to, and one reader per field.
  let config = null;
  // The Configure button that opened it, so Back/Escape hands focus to where the eye was.
  let configButton = null;

  /** @return {?Element} The open dropdown, if any. */
  function openDropdown() {
    return configView.querySelector(".omnia-drop.omnia-open");
  }

  /**
   * Close whichever dropdown is open.
   * @return {boolean} Whether one was actually open — Escape uses this to decide whether it
   *     has already been spent on closing a popup, or should leave the panel.
   */
  function closeOpenDropdown() {
    const drop = openDropdown();
    if (!drop) {
      return false;
    }
    drop.classList.remove("omnia-open");
    const row = drop.closest(".omnia-field");
    if (row) { row.classList.remove("omnia-raised"); }
    const list = drop.querySelector(".omnia-drop-list");
    const btn = drop.querySelector(".omnia-drop-btn");
    if (list) { list.hidden = true; }
    if (btn) { btn.setAttribute("aria-expanded", "false"); btn.focus(); }
    return true;
  }

  /**
   * Build an element.
   * @param {string} tag The tag name.
   * @param {string=} cls Class name.
   * @param {string=} text Text content.
   * @return {!Element}
   */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  /**
   * A switch, drawn exactly like the ones on the cards so the page has one kind of switch.
   * @param {!Object} field The field payload.
   * @return {{node: !Element, read: function(): *}}
   */
  function switchControl(field) {
    const label = el("label", "omnia-switch");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!field.value;
    label.appendChild(input);
    label.appendChild(el("span", "omnia-slider"));
    return {node: label, read: function () { return input.checked; }};
  }

  /**
   * A segmented control: every option visible, one click to pick.
   * @param {!Object} field The field payload.
   * @return {{node: !Element, read: function(): *}}
   */
  function segmentedControl(field) {
    const group = el("div", "omnia-seg");
    group.setAttribute("role", "group");
    let current = String(field.value);
    const buttons = [];
    (field.choices || []).forEach(function (choice) {
      const btn = el("button", "omnia-seg-btn", choice);
      btn.type = "button";
      btn.setAttribute("aria-pressed", String(choice === current));
      btn.addEventListener("click", function () {
        current = choice;
        buttons.forEach(function (other) {
          other.setAttribute("aria-pressed", String(other === btn));
        });
      });
      buttons.push(btn);
      group.appendChild(btn);
    });
    return {node: group, read: function () { return current; }};
  }

  /**
   * A dropdown. Omnia's own rather than a native `<select>`, whose popup is drawn by the OS
   * and cannot be given this page's gradient, radius or animation.
   * @param {!Object} field The field payload.
   * @return {{node: !Element, read: function(): *}}
   */
  function dropdownControl(field) {
    const wrap = el("div", "omnia-drop");
    const btn = el("button", "omnia-drop-btn");
    btn.type = "button";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    const value = el("span", "omnia-drop-value", labelFor(field, field.value));
    btn.appendChild(value);
    btn.appendChild(el("span", "omnia-drop-caret", "\u25be"));
    const list = el("div", "omnia-drop-list");
    list.setAttribute("role", "listbox");
    list.hidden = true;
    let current = String(field.value);

    (field.choices || []).forEach(function (choice) {
      const item = el("button", "omnia-drop-item");
      item.type = "button";
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(choice === current));
      item.appendChild(el("span", "omnia-drop-tick", "\u2713"));
      item.appendChild(el("span", null, labelFor(field, choice)));
      item.addEventListener("click", function () {
        current = choice;
        value.textContent = labelFor(field, choice);
        list.querySelectorAll(".omnia-drop-item").forEach(function (other) {
          other.setAttribute("aria-selected", String(other === item));
        });
        closeOpenDropdown();
      });
      list.appendChild(item);
    });

    btn.addEventListener("click", function () {
      const isOpen = wrap.classList.contains("omnia-open");
      closeOpenDropdown();
      if (isOpen) {
        return;  // it was open and the click closed it
      }
      wrap.classList.add("omnia-open");
      const row = wrap.closest(".omnia-field");
      if (row) { row.classList.add("omnia-raised"); }
      list.hidden = false;
      btn.setAttribute("aria-expanded", "true");
      const selected = list.querySelector('[aria-selected="true"]');
      if (selected) { selected.focus(); }
    });

    wrap.appendChild(btn);
    wrap.appendChild(list);
    return {node: wrap, read: function () { return current; }};
  }

  /**
   * What a choice reads as on screen.
   *
   * An empty option means "whatever Omnia is set to" rather than "nothing", and a blank line in
   * a dropdown is a line people skip over wondering whether it is broken.
   * @param {!Object} field The field payload.
   * @param {*} choice The stored value.
   * @return {string}
   */
  function labelFor(field, choice) {
    const text = choice === null || choice === undefined ? "" : String(choice);
    return text === "" ? "Omnia\u2019s default" : text;
  }

  /**
   * A slider with a live readout, for a number that has both ends. The bounds are the useful
   * part of such a setting and a spin box hides them.
   * @param {!Object} field The field payload.
   * @return {{node: !Element, read: function(): *}}
   */
  function sliderControl(field) {
    const row = el("div", "omnia-slide-row");
    const input = document.createElement("input");
    input.type = "range";
    input.className = "omnia-range";
    input.min = String(field.min);
    input.max = String(field.max);
    // `any`, NOT the field's step. A range input snaps its value to `min + n * step`, and the
    // two are rarely aligned: Audio Speed's rate is min 0.25 step 0.1, so a stored 1.0 landed
    // on 1.05 the moment the panel opened. Opening settings to LOOK at them and pressing Save
    // would then have shifted every rate by 0.05, silently. The step still governs what a drag
    // produces — see `snap` — it just no longer rewrites the value on the way in.
    input.step = "any";
    input.value = String(field.value);
    const isInt = field.kind === "int";
    // An EDITABLE readout, not a label. Dragging is for "about here"; a particular value is
    // typed. Without it the reachable values are whatever the track's pixel count allows, and
    // a default you nudged off can be unreachable — which is what a range input is, and why the
    // spin box this replaced was not simply worse.
    const readout = document.createElement("input");
    readout.type = "number";
    readout.className = "omnia-slide-value";
    readout.min = String(field.min);
    readout.max = String(field.max);
    readout.step = String(field.step);
    readout.setAttribute("aria-label", field.label);
    const step = Number(field.step) || (isInt ? 1 : 0.1);

    /**
     * The nearest value on the step grid, anchored at zero rather than at `min`.
     *
     * Anchored at zero so a rate lands on 1.0, 1.1, 1.2 — the numbers someone means — instead
     * of on 1.05, 1.15 as it would if the grid started wherever the minimum happened to be.
     * `toFixed` mops up the binary-float dust that `round(v / 0.1) * 0.1` leaves behind.
     */
    const snap = function (value) {
      const on = Math.round(Number(value) / step) * step;
      return isInt ? Math.round(on) : Number(on.toFixed(4));
    };

    // The value to report. Until the slider is actually moved it is the STORED one, untouched:
    // a form must not change a setting because it was looked at.
    let picked = isInt ? Math.round(Number(field.value)) : Number(field.value);

    const low = Number(field.min);
    const high = Number(field.max);
    const clamp = function (value) { return Math.min(high, Math.max(low, value)); };

    // `writeBox` is skipped while the box itself is being typed into: rewriting the input under
    // the cursor turns "1" on the way to "12" into a fight with the user.
    const paint = function (writeBox) {
      const span = high - low;
      const at = span ? ((picked - low) / span) * 100 : 0;
      input.style.setProperty("--fill", Math.max(0, Math.min(100, at)) + "%");
      input.value = String(picked);
      if (writeBox) {
        readout.value = String(picked);
      }
    };

    input.addEventListener("input", function () {
      picked = clamp(snap(input.value));
      paint(true);
    });
    // The range's own arrow keys move by a fraction of the span, because `step` is "any" — so
    // they are handled here instead, by exactly one step, which is what the declared step is
    // for. Page keys move ten, the way a spin box does.
    input.addEventListener("keydown", function (ev) {
      const by = {ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1,
                  PageDown: -10, PageUp: 10}[ev.key];
      if (by === undefined) {
        return;
      }
      ev.preventDefault();
      picked = clamp(snap(picked + by * step));
      paint(true);
    });
    readout.addEventListener("input", function () {
      const typed = isInt ? parseInt(readout.value, 10) : parseFloat(readout.value);
      if (isNaN(typed)) {
        return;  // mid-edit: "", "-", "1." are all on the way to something
      }
      picked = clamp(typed);
      paint(false);
    });
    // Only once editing ends is the box allowed to tidy what was typed — out-of-range back
    // inside it, an abandoned edit back to the value that is actually stored.
    readout.addEventListener("blur", function () {
      paint(true);
    });
    paint(true);

    row.appendChild(input);
    row.appendChild(readout);
    return {node: row, read: function () { return picked; }};
  }

  /**
   * One track, two handles: two marks that cut the same axis.
   *
   * Two separate sliders can express the same pair, and that is what this replaces. What they
   * cannot show is the RELATIONSHIP, which is the whole content of the setting — three bands
   * and where they start. Read as two numbers it has to be reconstructed every time; read as
   * one track it is the picture.
   *
   * Dragging the handles together collapses the top band, and that IS the off switch: the
   * upper mark stores 0, which the grader already reads as "one cutoff". So turning the
   * feature off is the same gesture as narrowing it, rather than a separate checkbox that
   * could disagree with the handles.
   * @param {!Object} field The field payload, carrying an `upper` block.
   * @return {{node: !Element, read: function(): *, extra: !Object}}
   */
  function rangeControl(field) {
    const upper = field.upper || {};
    const low = Number(field.min);
    const high = Number(field.max);
    const step = Number(field.step) || 0.05;
    const span = high - low;

    const wrap = el("div", "omnia-range2");
    const track = el("div", "omnia-range2-track");
    const fill = el("div", "omnia-range2-fill");      // between the handles: the middle band
    const top = el("div", "omnia-range2-top");        // above the upper handle: the top band
    track.appendChild(fill);
    track.appendChild(top);

    const snap = function (value) {
      return Number((Math.round(Number(value) / step) * step).toFixed(4));
    };
    const clamp = function (value) { return Math.min(high, Math.max(low, value)); };

    let lo = clamp(snap(Number(field.value)));
    // 0 means "no top band" and is BELOW the lower mark, so it cannot be a handle position.
    // The handle parks on the lower mark instead, which is the same thing said visually: no
    // gap, no band.
    let hi = Number(upper.value) > lo ? clamp(snap(Number(upper.value))) : lo;

    const makeHandle = function (labelText) {
      const h = el("div", "omnia-range2-handle");
      h.tabIndex = 0;
      h.setAttribute("role", "slider");
      h.setAttribute("aria-label", labelText);
      return h;
    };
    const loHandle = makeHandle(field.label);
    const hiHandle = makeHandle(upper.label || "Upper");
    track.appendChild(loHandle);
    track.appendChild(hiHandle);

    const legend = el("div", "omnia-range2-legend");
    const loOut = el("span", "omnia-range2-out");
    const hiOut = el("span", "omnia-range2-out");

    const pct = function (v) { return span ? ((v - low) / span) * 100 : 0; };

    const paint = function () {
      const a = pct(lo);
      const b = pct(hi);
      loHandle.style.left = a + "%";
      hiHandle.style.left = b + "%";
      fill.style.left = a + "%";
      fill.style.width = Math.max(0, b - a) + "%";
      top.style.left = b + "%";
      top.style.width = Math.max(0, 100 - b) + "%";
      loHandle.setAttribute("aria-valuenow", String(lo));
      hiHandle.setAttribute("aria-valuenow", String(hi));
      loOut.textContent = field.label + ": " + lo;
      // Says "off" rather than showing 0, because 0 is how it is STORED and not what it means.
      hiOut.textContent = (upper.label || "Upper") + ": " + (hi > lo ? String(hi) : "off");
      wrap.classList.toggle("omnia-range2-collapsed", !(hi > lo));
    };

    /** Move one handle, keeping the lower at or below the upper. */
    const set = function (which, value) {
      const v = clamp(snap(value));
      if (which === "lo") {
        lo = v;
        if (hi < lo) { hi = lo; }   // pushing the lower mark past the upper collapses the band
      } else {
        hi = Math.max(v, lo);       // the upper can never sit below the lower
      }
      paint();
    };

    const valueAt = function (clientX) {
      const box = track.getBoundingClientRect();
      if (!box.width) { return lo; }
      return low + ((clientX - box.left) / box.width) * span;
    };

    /** Drag whichever handle is nearer the press, so the track responds where it is clicked. */
    const startDrag = function (ev, forced) {
      const at = valueAt(ev.clientX);
      const which = forced || (Math.abs(at - lo) <= Math.abs(at - hi) ? "lo" : "hi");
      set(which, at);
      const move = function (e) { set(which, valueAt(e.clientX)); };
      const stop = function () {
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", stop);
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", stop);
      ev.preventDefault();
    };

    track.addEventListener("mousedown", function (ev) { startDrag(ev, null); });
    loHandle.addEventListener("mousedown", function (ev) { ev.stopPropagation(); startDrag(ev, "lo"); });
    hiHandle.addEventListener("mousedown", function (ev) { ev.stopPropagation(); startDrag(ev, "hi"); });

    const keys = function (which, current) {
      return function (ev) {
        const by = {ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1,
                    PageDown: -10, PageUp: 10}[ev.key];
        if (by === undefined) { return; }
        ev.preventDefault();
        set(which, current() + by * step);
      };
    };
    loHandle.addEventListener("keydown", keys("lo", function () { return lo; }));
    hiHandle.addEventListener("keydown", keys("hi", function () { return hi; }));

    legend.appendChild(loOut);
    legend.appendChild(hiOut);
    wrap.appendChild(track);
    wrap.appendChild(legend);
    paint();

    const extra = {};
    // Stored as 0 when the handles meet: that is what the grader reads as "no top band", so
    // the page writes the same value the engine's default already means.
    extra[upper.key] = function () { return hi > lo ? hi : 0; };
    return {node: wrap, read: function () { return lo; }, extra: extra};
  }

  /**
   * A plain number field, for a number with an open end — a slider needs somewhere to stop.
   * @param {!Object} field The field payload.
   * @return {{node: !Element, read: function(): *}}
   */
  function numberControl(field) {
    const input = document.createElement("input");
    input.type = "number";
    input.className = "omnia-input";
    input.value = String(field.value);
    input.step = String(field.step);
    if (field.min !== null && field.min !== undefined) { input.min = String(field.min); }
    if (field.max !== null && field.max !== undefined) { input.max = String(field.max); }
    const isInt = field.kind === "int";
    return {
      node: input,
      read: function () {
        const n = isInt ? parseInt(input.value, 10) : parseFloat(input.value);
        if (isNaN(n)) {
          return field.value;
        }
        // CLAMPED, like the slider. `min`/`max` on a number input only drive `:invalid`
        // styling — `.value` still hands back whatever was typed, and a bound the settings
        // model enforces (`Field(ge=…, le=…)`) would then reject the saved section. That
        // leaves the plugin unloadable AND its panel unopenable, with nothing said.
        const low = field.min === null || field.min === undefined ? n : Number(field.min);
        const high = field.max === null || field.max === undefined ? n : Number(field.max);
        return Math.min(high, Math.max(low, n));
      },
    };
  }

  /**
   * A text field. `secret` is the same control with the characters hidden.
   * @param {!Object} field The field payload.
   * @return {{node: !Element, read: function(): *}}
   */
  function textControl(field) {
    const input = document.createElement("input");
    input.type = field.control === "secret" ? "password" : "text";
    input.className = "omnia-input";
    input.value = String(field.value);
    return {node: input, read: function () { return input.value; }};
  }

  /**
   * A colour swatch plus the hex it stands for, because a colour people can also type is a
   * colour they can copy out of somewhere else.
   * @param {!Object} field The field payload.
   * @return {{node: !Element, read: function(): *}}
   */
  function colorControl(field) {
    const wrap = el("div", "omnia-color");
    const input = document.createElement("input");
    input.type = "color";
    const start = /^#[0-9a-fA-F]{6}$/.test(String(field.value)) ? String(field.value) : "#000000";
    input.value = start;
    const hex = el("span", "omnia-color-hex", start);
    input.addEventListener("input", function () { hex.textContent = input.value; });
    wrap.appendChild(input);
    wrap.appendChild(hex);
    return {node: wrap, read: function () { return input.value; }};
  }

  const CONTROLS = {
    switch: switchControl,
    segmented: segmentedControl,
    dropdown: dropdownControl,
    slider: sliderControl,
    range: rangeControl,
    number: numberControl,
    text: textControl,
    secret: textControl,
    color: colorControl,
  };

  /**
   * Build one field row.
   * @param {!Object} field The field payload.
   * @param {number} index Its position, for the entrance stagger.
   * @return {{node: !Element, read: function(): *}}
   */
  function fieldRow(field, index) {
    const row = el("div", "omnia-field");
    row.style.setProperty("--i", String(index));
    row.setAttribute("data-control", field.control);
    // The hover wash, on its own clipped layer so the row itself can let a dropdown out.
    row.appendChild(el("span", "omnia-field-wash"));
    const head = el("div", "omnia-field-head");
    head.appendChild(el("div", "omnia-field-label", field.label));
    const control = (CONTROLS[field.control] || textControl)(field);
    const holder = el("div", "omnia-field-control");
    holder.appendChild(control.node);
    // A switch and a colour chip are small enough to sit on the label's line; everything else
    // needs the width and goes underneath.
    if (field.control === "switch" || field.control === "color") {
      head.appendChild(holder);
      row.appendChild(head);
    } else {
      row.appendChild(head);
      row.appendChild(holder);
    }
    appendHelp(row, field.help || "");
    // `extra` forwarded, not swallowed: a control that owns a second setting registers it
    // through here, and a row that dropped it would save only half of what the user set.
    return {node: row, read: control.read, extra: control.extra || {}};
  }

  /**
   * Render help text into `parent`: the first paragraph, then the rest behind a toggle.
   *
   * A plugin's help is written for someone deciding what to set, so the first paragraph says
   * what the setting does and what follows says why it is worth setting. Printing all of it
   * under every row turns a form into an essay — which is what the first draft of this panel
   * looked like.
   *
   * @param {!Element} parent The field row.
   * @param {string} help The authored help text.
   */
  function appendHelp(parent, help) {
    const paragraphs = String(help).split(/\n\s*\n/).filter(function (p) { return p.trim(); });
    if (!paragraphs.length) {
      return;
    }
    parent.appendChild(helpBlock(paragraphs[0]));
    if (paragraphs.length === 1) {
      return;
    }
    const rest = el("div", "omnia-field-more");
    rest.hidden = true;
    paragraphs.slice(1).forEach(function (text) { rest.appendChild(helpBlock(text)); });
    const toggle = el("button", "omnia-field-why", "Why this matters");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", function () {
      rest.hidden = !rest.hidden;
      toggle.textContent = rest.hidden ? "Why this matters" : "Show less";
      toggle.setAttribute("aria-expanded", String(!rest.hidden));
    });
    parent.appendChild(toggle);
    parent.appendChild(rest);
  }

  /**
   * One paragraph of help, with ``inline code`` rendered as code.
   *
   * The plugins author these with RST double backticks, which reached the screen as literal
   * characters — `` ``vi`` `` rather than a value you could type. Built as nodes rather than
   * markup: the text is authored by a plugin, and nothing here needs an HTML parser.
   *
   * @param {string} text One paragraph.
   * @return {!Element}
   */
  function helpBlock(text) {
    const block = el("div", "omnia-field-help");
    // Split on ``code`` spans, keeping them: odd indexes are the code, even the prose.
    String(text).split(/``([^`]+)``/).forEach(function (part, index) {
      if (!part) {
        return;
      }
      block.appendChild(index % 2 ? el("code", null, part) : document.createTextNode(part));
    });
    return block;
  }

  /**
   * Show one plugin's settings.
   * @param {!Object} payload What Python sent: id, name, category, accent, fields.
   * @param {?Element} button The Configure button that opened it.
   */
  function openConfig(payload, button) {
    configButton = button || null;
    const readers = {};
    configFields.textContent = "";
    (payload.fields || []).forEach(function (field, index) {
      const built = fieldRow(field, index);
      readers[field.key] = built.read;
      // A control may own more than one setting — a two-handled range writes both of its
      // marks. Registering the extra readers here keeps saving a plain "read every key"
      // loop rather than something that knows which controls are special.
      Object.keys(built.extra || {}).forEach(function (key) {
        readers[key] = built.extra[key];
      });
      configFields.appendChild(built.node);
    });
    if (!(payload.fields || []).length) {
      configFields.appendChild(el("div", "omnia-config-empty", "This feature has no options."));
    }
    config = {id: payload.id, category: payload.category || "", readers: readers};

    configName.textContent = payload.name || "";
    configNote.textContent = "";
    // The category's own gradient, so the panel is visibly the same colour as the tile that
    // opened it rather than a generic form that could belong to anything.
    const accent = payload.accent || [];
    if (accent.length === 2) {
      configView.style.setProperty("--cat-from", accent[0]);
      configView.style.setProperty("--cat-to", accent[1]);
    }

    document.querySelectorAll(".omnia-category").forEach(function (view) { view.hidden = true; });
    landing.hidden = true;
    configView.hidden = false;
    replay(configView);
    document.body.dataset.view = "config";
    window.scrollTo(0, 0);
    configName.focus();
  }

  /** Leave the panel without saving, back to the category it was opened from. */
  function leaveConfig() {
    const key = config ? config.category : "";
    config = null;
    configView.hidden = true;
    const tile = key ? tileFor(key) : null;
    if (tile) {
      showCategory(tile);
      if (configButton) {
        configButton.focus();
        configButton = null;
      }
      return;
    }
    showLanding();
  }

  /** Read every control and ask Python to persist it. */
  function saveConfig() {
    if (!config) {
      return;
    }
    const values = {};
    Object.keys(config.readers).forEach(function (key) {
      values[key] = config.readers[key]();
    });
    const id = config.id;
    configNote.textContent = "Saving\u2026";
    send("save-config", {id: id, values: values}, function (res) {
      // A save that could not be applied is not a save that did not happen: the value is
      // stored, and what failed is re-applying it to a running plugin. Saying so beats closing
      // the panel as though all was well.
      if (res && res.error) {
        configNote.textContent = res.error;
        return;
      }
      leaveConfig();
    });
  }

  configView.querySelector(".omnia-config-back").addEventListener("click", leaveConfig);
  configView.querySelector(".omnia-config-cancel").addEventListener("click", leaveConfig);
  configView.querySelector(".omnia-config-save").addEventListener("click", saveConfig);

  // A click anywhere else closes an open dropdown, which is what a popup does.
  document.addEventListener("click", function (ev) {
    const drop = openDropdown();
    if (drop && !drop.contains(ev.target)) {
      closeOpenDropdown();
    }
  }, true);

})();
