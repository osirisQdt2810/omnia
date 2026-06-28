/**
 * @fileoverview Smart Notes config page — part 1 of 6 of the page IIFE.
 * This fragment OPENS the IIFE; load order matters (01-bridge, 02-catalog, 03-render,
 * 04-modal, 05-handlers, 06-init are concatenated with "\n" by gui/smart_notes/html.py). It
 * declares the shared element handles, the baked catalog, and the bridge/select helpers used
 * by the later parts.
 */

(function () {
  const noteTypeSel = document.getElementById("sn-note-type");
  const baseSel = document.getElementById("sn-base-field");
  const tbody = document.getElementById("sn-rows");
  const emptyEl = document.getElementById("sn-empty");
  const msgEl = document.getElementById("sn-msg");
  const autoBtn = document.getElementById("sn-auto");
  const improveAllBtn = document.getElementById("sn-improve-all");
  const saveBtn = document.getElementById("sn-save");

  // Decks picker handles (a note-type-level scope: which decks this config applies to).
  const decksBtn = document.getElementById("sn-decks-btn");
  const decksPanel = document.getElementById("sn-decks-panel");
  const decksAll = document.getElementById("sn-decks-all");
  const decksList = document.getElementById("sn-decks-list");

  // The full deck list ([{id, name}, ...]) + the selected subset (deck ids; [] = all decks),
  // both seeded from the load response and read back into the save/auto/improve/preview payloads.
  let allDecks = [];
  let selectedDecks = [];

  // Prompt-editor popup handles.
  const modal = document.getElementById("sn-modal");
  const modalTitle = document.getElementById("sn-modal-title");
  const modalPrompt = document.getElementById("sn-modal-prompt");
  const modalFields = document.getElementById("sn-modal-fields");
  const modalMsg = document.getElementById("sn-modal-msg");
  const modalResult = document.getElementById("sn-modal-result");
  const modalImprove = document.getElementById("sn-modal-improve");
  const modalPreview = document.getElementById("sn-modal-preview");
  const modalSave = document.getElementById("sn-modal-save");
  const modalCancel = document.getElementById("sn-modal-cancel");
  const modalClose = document.getElementById("sn-modal-close");

  // Provider / model / voice catalog baked into the page (see core/providers/catalog.py).
  const CATALOG = window.__SN_CATALOG || {};

  // Display labels for the Type dropdown ("tts" reads as "sound" in the UI; the stored value
  // stays "tts" so the config model / engine are unchanged).
  const TYPE_LABELS = {text: "text", image: "image", tts: "sound"};

  /**
   * Post an Omnia envelope to Python via the WebDialog bridge.
   * @param {string} op The op name.
   * @param {!Object} data The op payload.
   * @param {?function(*)} cb Callback resolved with the handler's return value.
   */
  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({plugin: "smart_notes", op: op, data: data}), cb);
  }

  /**
   * Set the footer status message line.
   * @param {string} text HTML message to show (empty clears it).
   * @param {boolean} isErr Whether to render it as an error.
   */
  function setMsg(text, isErr) {
    msgEl.className = "sn-msg" + (isErr ? " sn-err" : "");
    msgEl.innerHTML = text || "";
  }

  /**
   * Find the table row for a field name (defensive iteration; no selector escaping needed).
   * @param {string} field The field name.
   * @return {?HTMLTableRowElement}
   */
  function rowByField(field) {
    let found = null;
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      if (tr.dataset.field === field) {
        found = tr;
      }
    });
    return found;
  }

  /**
   * Build an <option> element.
   * @param {string} value The option value.
   * @param {string} label The option label.
   * @param {boolean} selected Whether the option starts selected.
   * @return {!HTMLOptionElement}
   */
  function opt(value, label, selected) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    if (selected) {
      o.selected = true;
    }
    return o;
  }

  /**
   * Replace a <select>'s options with `values`, selecting `current`.
   * @param {!HTMLSelectElement} select The select to fill.
   * @param {!Array<string>} values The option values/labels.
   * @param {string} current The value to mark selected.
   */
  function fill(select, values, current) {
    select.innerHTML = "";
    values.forEach(function (v) {
      select.appendChild(opt(v, v, v === current));
    });
  }

  /**
   * Build a checkbox input.
   * @param {string} cls Extra class name.
   * @param {boolean} checked Initial checked state.
   * @return {!HTMLInputElement}
   */
  function makeCheckbox(cls, checked) {
    const c = document.createElement("input");
    c.type = "checkbox";
    c.className = "sn-check " + cls;
    c.checked = !!checked;
    return c;
  }

  /**
   * Build an on/off toggle switch (a checkbox styled as a sliding pill).
   * @param {string} cls Extra class name.
   * @param {boolean} checked Initial checked state.
   * @return {!HTMLInputElement}
   */
  function makeToggle(cls, checked) {
    const c = document.createElement("input");
    c.type = "checkbox";
    c.className = "sn-toggle " + cls;
    c.checked = !!checked;
    return c;
  }

  /**
   * Build a <select> from values; an empty value renders as "(inherit)".
   * @param {string} cls The select's class name.
   * @param {!Array<string>} values The option values.
   * @param {string} current The value to mark selected.
   * @return {!HTMLSelectElement}
   */
  function makeSelect(cls, values, current) {
    const s = document.createElement("select");
    s.className = cls;
    values.forEach(function (v) {
      s.appendChild(opt(v, v === "" ? "(inherit)" : v, v === current));
    });
    return s;
  }

  /**
   * Build an empty table cell.
   * @param {string=} cls Optional class name.
   * @return {!HTMLTableCellElement}
   */
  function cell(cls) {
    const td = document.createElement("td");
    if (cls) {
      td.className = cls;
    }
    return td;
  }
