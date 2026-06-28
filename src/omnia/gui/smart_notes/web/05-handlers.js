
  /**
   * Smart Notes config page — part 5 of 6 of the page IIFE.
   * Top-level event handlers: load a note type, change base field, create a field, run
   * Auto-smart, Improve-all, and save. Each posts the matching pycmd op through `send`.
   * Provider/model/voice options come from the baked catalog, not from the load response.
   */

  /**
   * Apply a `load`/`set_base_field` response to the page.
   * @param {?Object} res The op response (all_fields, base_field, rows).
   */
  function applyLoad(res) {
    if (!res) {
      return;
    }
    fill(baseSel, res.all_fields || [], res.base_field || "");
    renderRows(res.rows || []);
    allDecks = res.all_decks || [];
    selectedDecks = (res.decks || []).map(function (d) {
      return parseInt(d, 10);
    });
    renderDecks();
    setMsg("");
  }

  /**
   * The selected deck-id subset for the current note type ([] = all decks).
   * @return {!Array<number>}
   */
  function selectedDeckIds() {
    return selectedDecks.slice();
  }

  /** React to a per-deck checkbox toggle: maintain `selectedDecks`, then re-sync the panel. */
  function onDeckToggle(e) {
    const id = parseInt(e.target.dataset.deckId, 10);
    const at = selectedDecks.indexOf(id);
    if (e.target.checked && at < 0) {
      selectedDecks.push(id);
    } else if (!e.target.checked && at >= 0) {
      selectedDecks.splice(at, 1);
    }
    decksAll.checked = selectedDecks.length === 0;
    updateDecksSummary();
  }

  // The "All decks" master clears the subset (decks=[]); the toolbar button toggles the panel,
  // and an outside click closes it.
  decksAll.addEventListener("change", function () {
    if (decksAll.checked) {
      selectedDecks = [];
      renderDecks();
    } else if (selectedDecks.length === 0) {
      // Unticking "All" with nothing selected is meaningless — keep it ticked.
      decksAll.checked = true;
    }
  });
  decksBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    decksPanel.hidden = !decksPanel.hidden;
  });
  document.addEventListener("click", function (e) {
    if (!decksPanel.hidden && !decksPanel.contains(e.target) && e.target !== decksBtn) {
      decksPanel.hidden = true;
    }
  });

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
    send(
      "set_base_field",
      {note_type: noteTypeSel.value, base_field: baseSel.value},
      applyLoad
    );
  });

  document.getElementById("sn-create").addEventListener("click", function () {
    const name = window.prompt("New field name:");
    if (!name) {
      return;
    }
    send("create_field", {note_type: noteTypeSel.value, field_name: name}, function (res) {
      if (res && res.all_fields) {
        fill(baseSel, res.all_fields, baseSel.value);
        send(
          "set_base_field",
          {note_type: noteTypeSel.value, base_field: baseSel.value},
          applyLoad
        );
      } else if (res && res.error) {
        setMsg(res.error, true);
      }
    });
  });

  // Auto-smart and Improve-all run the LLM OFF the Qt main thread, so their results can't come
  // back through the synchronous bridge callback. Each handler kicks the work off (returning
  // immediately) and pushes the outcome back through a window.__sn*Result hook, leaving its
  // button disabled and the spinner up until then.
  window.__snAutoResult = function (res) {
    autoBtn.disabled = false;
    if (res && res.rows) {
      renderRows(res.rows);
      const n = typeof res.filled === "number" ? res.filled : res.rows.length;
      setMsg("Auto-smart wrote prompts for " + n + " field(s).", false);
    } else {
      setMsg((res && res.error) || "Auto-smart failed — see logs.", true);
    }
  };
  autoBtn.addEventListener("click", function () {
    autoBtn.disabled = true;
    setMsg('<span class="sn-spin"></span>Auto-smart is writing prompts…', false);
    send(
      "auto_smart",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        rows: collectRows(),
        decks: selectedDeckIds()
      },
      null
    );
  });

  window.__snImproveAllResult = function (res) {
    improveAllBtn.disabled = false;
    if (res && res.improved) {
      let n = 0;
      Object.keys(res.improved).forEach(function (field) {
        const tr = rowByField(field);
        if (tr) {
          tr.dataset.prompt = res.improved[field];
          updatePromptSummary(tr);
          n += 1;
        }
      });
      setMsg("Improved " + n + " prompt(s).", false);
    } else {
      setMsg((res && res.error) || "Improve all failed — see logs.", true);
    }
  };
  improveAllBtn.addEventListener("click", function () {
    improveAllBtn.disabled = true;
    setMsg('<span class="sn-spin"></span>Improving all unlocked prompts…', false);
    send(
      "improve_all",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        rows: collectRows(),
        decks: selectedDeckIds()
      },
      null
    );
  });

  saveBtn.addEventListener("click", function () {
    saveBtn.disabled = true;
    send(
      "save",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        rows: collectRows(),
        decks: selectedDeckIds()
      },
      function (res) {
        saveBtn.disabled = false;
        if (res && res.ok) {
          setMsg("Saved.", false);
        } else {
          setMsg((res && res.error) || "Could not save — see logs.", true);
        }
      }
    );
  });

  document.getElementById("sn-cancel").addEventListener("click", function () {
    send("cancel", {}, null);
  });
