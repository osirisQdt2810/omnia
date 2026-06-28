
(function () {
  var noteTypeSel = document.getElementById("sn-note-type");
  var baseSel = document.getElementById("sn-base-field");
  var tbody = document.getElementById("sn-rows");
  var emptyEl = document.getElementById("sn-empty");
  var msgEl = document.getElementById("sn-msg");
  var autoBtn = document.getElementById("sn-auto");
  var saveBtn = document.getElementById("sn-save");
  var providers = [];

  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({ plugin: "smart_notes", op: op, data: data }), cb);
  }
  function setMsg(text, isErr) {
    msgEl.className = "sn-msg" + (isErr ? " sn-err" : "");
    msgEl.innerHTML = text || "";
  }
  function opt(value, label, selected) {
    var o = document.createElement("option");
    o.value = value; o.textContent = label;
    if (selected) o.selected = true;
    return o;
  }
  function fill(select, values, current) {
    select.innerHTML = "";
    values.forEach(function (v) { select.appendChild(opt(v, v, v === current)); });
  }

  function makeCheckbox(cls, checked) {
    var c = document.createElement("input");
    c.type = "checkbox"; c.className = "sn-check " + cls; c.checked = !!checked;
    return c;
  }
  function makeSelect(cls, values, current) {
    var s = document.createElement("select");
    s.className = cls;
    values.forEach(function (v) {
      s.appendChild(opt(v, v === "" ? "(inherit)" : v, v === current));
    });
    return s;
  }
  function makeInput(cls, value, placeholder) {
    var i = document.createElement("input");
    i.type = "text"; i.className = cls; i.value = value || "";
    if (placeholder) i.placeholder = placeholder;
    return i;
  }