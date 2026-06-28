/**
 * @fileoverview Smart Notes config page — part 1 of 4 of the page IIFE.
 * This fragment OPENS the IIFE; load order matters (01-bridge, 02-render, 03-handlers,
 * 04-init are concatenated with "\n" by gui/smart_notes/html.py). It declares the shared
 * element handles and the bridge/select helpers used by the later parts.
 */

(function () {
  const noteTypeSel = document.getElementById("sn-note-type");
  const baseSel = document.getElementById("sn-base-field");
  const tbody = document.getElementById("sn-rows");
  const emptyEl = document.getElementById("sn-empty");
  const msgEl = document.getElementById("sn-msg");
  const autoBtn = document.getElementById("sn-auto");
  const saveBtn = document.getElementById("sn-save");
  let providers = [];

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
   * Set the status message line.
   * @param {string} text HTML message to show (empty clears it).
   * @param {boolean} isErr Whether to render it as an error.
   */
  function setMsg(text, isErr) {
    msgEl.className = "sn-msg" + (isErr ? " sn-err" : "");
    msgEl.innerHTML = text || "";
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
   * Build a text input.
   * @param {string} cls The input's class name.
   * @param {string} value Initial value.
   * @param {string} placeholder Placeholder text.
   * @return {!HTMLInputElement}
   */
  function makeInput(cls, value, placeholder) {
    const i = document.createElement("input");
    i.type = "text";
    i.className = cls;
    i.value = value || "";
    if (placeholder) {
      i.placeholder = placeholder;
    }
    return i;
  }
