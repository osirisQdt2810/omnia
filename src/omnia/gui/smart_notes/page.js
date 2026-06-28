
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

  function renderRow(row) {
    var tr = document.createElement("tr");
    tr.setAttribute("data-field", row.field);

    var tdName = document.createElement("td");
    tdName.className = "sn-fieldname"; tdName.textContent = row.field;
    tr.appendChild(tdName);

    var tdOn = document.createElement("td"); tdOn.className = "sn-center";
    tdOn.appendChild(makeCheckbox("sn-enabled", row.enabled));
    tr.appendChild(tdOn);

    var tdType = document.createElement("td");
    tdType.appendChild(makeSelect("sn-type", SN_TYPES, row.type || "text"));
    tr.appendChild(tdType);

    var tdPrompt = document.createElement("td");
    var wrap = document.createElement("div"); wrap.className = "sn-prompt-cell";
    var ta = document.createElement("textarea");
    ta.className = "sn-prompt"; ta.value = row.prompt || "";
    ta.placeholder = "e.g. Give the IPA for {{" + (baseSel.value || "Base") + "}}";
    var lock = document.createElement("button");
    lock.type = "button"; lock.className = "sn-lock" + (row.prompt_locked ? " sn-locked" : "");
    lock.textContent = row.prompt_locked ? "🔒" : "🔓";
    lock.title = "Lock to protect this prompt/type from Auto-smart";
    lock.addEventListener("click", function () {
      var locked = lock.classList.toggle("sn-locked");
      lock.textContent = locked ? "🔒" : "🔓";
    });
    wrap.appendChild(ta); wrap.appendChild(lock);
    tdPrompt.appendChild(wrap);
    tr.appendChild(tdPrompt);

    var providerVals = [""].concat(providers);
    var tdProvider = document.createElement("td");
    tdProvider.appendChild(makeSelect("sn-provider", providerVals, row.provider || ""));
    tr.appendChild(tdProvider);

    var tdModel = document.createElement("td");
    tdModel.appendChild(makeInput("sn-model", row.model, "(inherit)"));
    tr.appendChild(tdModel);

    var tdOver = document.createElement("td"); tdOver.className = "sn-center";
    tdOver.appendChild(makeCheckbox("sn-overwrite", row.overwrite));
    tr.appendChild(tdOver);

    return tr;
  }

  function renderRows(rows) {
    tbody.innerHTML = "";
    rows.forEach(function (row) { tbody.appendChild(renderRow(row)); });
    emptyEl.textContent = rows.length
      ? ""
      : "This note type has no other fields to generate. Add one with “+ Create field”.";
  }

  function collectRows() {
    var rows = [];
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      var lock = tr.querySelector(".sn-lock");
      rows.push({
        field: tr.getAttribute("data-field"),
        enabled: tr.querySelector(".sn-enabled").checked,
        type: tr.querySelector(".sn-type").value,
        prompt: tr.querySelector(".sn-prompt").value,
        prompt_locked: lock.classList.contains("sn-locked"),
        provider: tr.querySelector(".sn-provider").value,
        model: tr.querySelector(".sn-model").value,
        voice: tr.getAttribute("data-voice") || "",
        overwrite: tr.querySelector(".sn-overwrite").checked
      });
    });
    return rows;
  }

  function applyLoad(res) {
    if (!res) return;
    providers = res.providers || [];
    fill(baseSel, res.all_fields || [], res.base_field || "");
    renderRows(res.rows || []);
    setMsg("");
  }

  function loadNoteType() {
    var nt = noteTypeSel.value;
    if (!nt) { renderRows([]); return; }
    send("load", { note_type: nt }, applyLoad);
  }

  noteTypeSel.addEventListener("change", loadNoteType);
  baseSel.addEventListener("change", function () {
    send("set_base_field", { note_type: noteTypeSel.value, base_field: baseSel.value }, applyLoad);
  });

  document.getElementById("sn-create").addEventListener("click", function () {
    var name = window.prompt("New field name:");
    if (!name) return;
    send("create_field", { note_type: noteTypeSel.value, field_name: name }, function (res) {
      if (res && res.all_fields) {
        fill(baseSel, res.all_fields, baseSel.value);
        send("set_base_field", { note_type: noteTypeSel.value, base_field: baseSel.value }, applyLoad);
      } else if (res && res.error) {
        setMsg(res.error, true);
      }
    });
  });

  // Auto-smart runs the LLM OFF the Qt main thread, so its result can't come back through the
  // synchronous bridge callback. The handler kicks the work off (returning immediately) and
  // pushes the outcome back by calling window.__snAutoResult — leaving the button disabled and
  // the spinner up until then.
  window.__snAutoResult = function (res) {
    autoBtn.disabled = false;
    if (res && res.rows) {
      renderRows(res.rows);
      setMsg("Auto-smart filled in the enabled, unlocked fields.", false);
    } else {
      setMsg((res && res.error) || "Auto-smart failed — see logs.", true);
    }
  };
  autoBtn.addEventListener("click", function () {
    autoBtn.disabled = true;
    setMsg('<span class="sn-spin"></span>Auto-smart is writing prompts…', false);
    send("auto_smart", {
      note_type: noteTypeSel.value,
      base_field: baseSel.value,
      rows: collectRows()
    }, null);
  });

  saveBtn.addEventListener("click", function () {
    saveBtn.disabled = true;
    send("save", {
      note_type: noteTypeSel.value,
      base_field: baseSel.value,
      rows: collectRows()
    }, function (res) {
      saveBtn.disabled = false;
      if (res && res.ok) {
        setMsg("Saved.", false);
      } else {
        setMsg((res && res.error) || "Could not save — see logs.", true);
      }
    });
  });

  document.getElementById("sn-cancel").addEventListener("click", function () {
    send("cancel", {}, null);
  });

  // Initial state is baked into window.__SN_INIT (server-side) so the page renders populated
  // without an init pycmd callback — Anki's bridge callback channel isn't ready the instant
  // this inline script runs. Fall back to a live list_note_types only if nothing was baked.
  var INIT = window.__SN_INIT || null;
  if (INIT && INIT.note_types && INIT.note_types.length) {
    fill(noteTypeSel, INIT.note_types, INIT.note_type || INIT.note_types[0] || "");
    applyLoad(INIT);
  } else {
    send("list_note_types", {}, function (names) {
      fill(noteTypeSel, names || [], (names && names[0]) || "");
      loadNoteType();
    });
  }
})();
