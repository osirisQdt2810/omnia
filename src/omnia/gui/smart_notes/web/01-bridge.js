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
  const sortBtn = document.getElementById("sn-sort-field");

  // Field-name sort direction for the table: 0 none, 1 ascending, -1 descending.
  let sortDir = 0;

  // Decks picker handles (a note-type-level scope: which decks this config applies to). The
  // button opens a modal with a search box + a multi-column grid of deck checkboxes.
  const decksBtn = document.getElementById("sn-decks-btn");
  const decksModal = document.getElementById("sn-decks-modal");
  const decksClose = document.getElementById("sn-decks-close");
  const decksDone = document.getElementById("sn-decks-done");
  const decksSearch = document.getElementById("sn-decks-search");
  const decksAll = document.getElementById("sn-decks-all");
  const decksList = document.getElementById("sn-decks-list");
  const decksEmpty = document.getElementById("sn-decks-empty");

  // The full deck list ([{id, name}, ...]) + the selected subset (deck ids), seeded from the
  // load response and read back into the save/auto/improve/preview payloads.
  let allDecks = [];
  let selectedDecks = [];
  // deckAllMode true = apply to ALL decks (persisted as []); false = only `selectedDecks`.
  let deckAllMode = true;
  // The deck hierarchy built from the "::" paths + which nodes are expanded (subdecks are
  // hidden by default so a big, deeply-nested tree stays scannable).
  let deckTree = null;
  const deckExpanded = {};

  // Options modal handles + the global Smart Notes flags (apply to every note type).
  const optionsBtn = document.getElementById("sn-options");
  const optionsModal = document.getElementById("sn-options-modal");
  const optionsClose = document.getElementById("sn-options-close");
  const optionsDone = document.getElementById("sn-options-done");
  const optGenReview = document.getElementById("sn-opt-gen-review");
  const optRegenBatch = document.getElementById("sn-opt-regen-batch");
  const optAllowEmpty = document.getElementById("sn-opt-allow-empty");
  const nativeListEl = document.getElementById("sn-native-list");

  // Tabbed Options dialog: General (the flags above) + Account (usage tables, OpenRouter
  // credit, and a test playground). Handles for the tab strip, the per-kind sub-tabs, and the
  // account panes live here; the logic is in 05-handlers.js (which owns the Options modal).
  const acctKindEl = document.getElementById("sn-acct-kind");
  const acctDefaultEl = document.getElementById("sn-acct-default");
  // The global "Auto-detect voices" editor (sound subtab only): one row per language mapping
  // its detected language to a concrete provider·voice in [tts.auto_voices].
  const acctAutoVoicesEl = document.getElementById("sn-acct-autovoices");
  // The per-language voice map lives in its own modal (the section shows only two buttons).
  const autovoicesModal = document.getElementById("sn-autovoices-modal");
  const autovoicesListEl = document.getElementById("sn-autovoices-list");
  const autovoicesClose = document.getElementById("sn-autovoices-close");
  const autovoicesDone = document.getElementById("sn-autovoices-done");
  const acctUsageEl = document.getElementById("sn-acct-usage");
  const acctCreditEl = document.getElementById("sn-acct-credit");
  const acctInput = document.getElementById("sn-acct-input");
  const acctRunBtn = document.getElementById("sn-acct-run");
  const acctMsgEl = document.getElementById("sn-acct-msg");
  const acctOutEl = document.getElementById("sn-acct-out");
  // Keys subtab: provider credential cards (masked key/secret fields, eye reveal, save,
  // file-browse, an honest quota story). Rendered by 05-handlers from the account_keys op.
  const keysEl = document.getElementById("sn-keys");

  // Full-screen image lightbox: a generated image is never rendered inline (it can be huge and
  // overflow the dialog) — the result shows a line + a Preview button that opens this borderless
  // overlay over the whole UI.
  const lightbox = document.getElementById("sn-lightbox");
  const lightboxImg = document.getElementById("sn-lightbox-img");

  // Prompt-editor popup handles.
  const modal = document.getElementById("sn-modal");
  const modalTitle = document.getElementById("sn-modal-title");
  const modalPrompt = document.getElementById("sn-modal-prompt");
  const modalFields = document.getElementById("sn-modal-fields");
  const modalWarn = document.getElementById("sn-modal-warn");
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
