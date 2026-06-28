
  /**
   * Smart Notes config page — part 3 of 4 of the page IIFE (load order matters).
   * Event handlers: load a note type, change base field, create a field, run Auto-smart, and
   * save. Each posts the matching pycmd op through `send`.
   */

  /**
   * Apply a `load`/`set_base_field` response to the page.
   * @param {?Object} res The op response (providers, all_fields, base_field, rows).
   */
  function applyLoad(res) {
    if (!res) {
      return;
    }
    providers = res.providers || [];
    fill(baseSel, res.all_fields || [], res.base_field || "");
    renderRows(res.rows || []);
    setMsg("");
  }

  /** Load the currently selected note type (or clear the table when none). */
  function loadNoteType() {
    const nt = noteTypeSel.value;
    if (!nt) {
      renderRows([]);
      return;
    }
    send("load", {note_type: nt}, applyLoad);
  }

  noteTypeSel.addEventListener("change", loadNoteType);
  baseSel.addEventListener("change", function () {
    send("set_base_field", {note_type: noteTypeSel.value, base_field: baseSel.value}, applyLoad);
  });

  document.getElementById("sn-create").addEventListener("click", function () {
    const name = window.prompt("New field name:");
    if (!name) {
      return;
    }
    send("create_field", {note_type: noteTypeSel.value, field_name: name}, function (res) {
      if (res && res.all_fields) {
        fill(baseSel, res.all_fields, baseSel.value);
        send("set_base_field", {note_type: noteTypeSel.value, base_field: baseSel.value}, applyLoad);
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
