
  /**
   * Smart Notes config page — part 4 of 6 of the page IIFE.
   * The prompt-editor popup (a plain in-page overlay, not a nested webview) plus the
   * Preview/Improve actions it and the row ▶ button share. Improve and Preview run off the Qt
   * main thread, so their results arrive through the window.__sn*Result push hooks rather than
   * the synchronous bridge callback.
   */

  // The row currently open in the editor (null when the popup is closed).
  let modalRow = null;

  /**
   * Open the prompt editor for a row: seed the textarea + the {{Field}} reference hint.
   * @param {!HTMLTableRowElement} tr The row whose prompt is being edited.
   */
  function openPromptEditor(tr) {
    modalRow = tr;
    modalTitle.textContent = "Edit prompt — " + tr.dataset.field;
    modalPrompt.value = tr.dataset.prompt || "";
    setModalMsg("", false);
    setModalResult("", false);
    modalFields.textContent = fieldRefHint(tr.dataset.field);
    modal.hidden = false;
    modalPrompt.focus();
  }

  /** Close the editor without touching the row. */
  function closeModal() {
    modal.hidden = true;
    modalRow = null;
  }

  /**
   * The "Reference fields: {{Base}} {{Other}} …" hint for the field being edited.
   * @param {string} field The field currently edited (excluded from the list).
   * @return {string}
   */
  function fieldRefHint(field) {
    const names = [baseSel.value];
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      if (tr.dataset.field !== field) {
        names.push(tr.dataset.field);
      }
    });
    const refs = names
      .filter(Boolean)
      .map(function (n) {
        return "{{" + n + "}}";
      });
    return refs.length ? "Reference fields: " + refs.join("  ") : "";
  }

  /**
   * Set the editor's status line.
   * @param {string} html Message HTML (empty clears).
   * @param {boolean} isErr Render as an error.
   */
  function setModalMsg(html, isErr) {
    modalMsg.className = "sn-msg" + (isErr ? " sn-err" : "");
    modalMsg.innerHTML = html || "";
  }

  /**
   * Set the editor's result area.
   * @param {string} html Result HTML (empty clears).
   * @param {boolean} isErr Render as an error.
   */
  function setModalResult(html, isErr) {
    modalResult.className = "sn-modal-result" + (isErr ? " sn-err" : "");
    modalResult.innerHTML = html || "";
  }

  /**
   * Build the `preview` op payload for a row, optionally overriding the prompt with the text
   * currently in the editor.
   * @param {!HTMLTableRowElement} tr The row to preview.
   * @param {?string} promptOverride The in-editor prompt, or null to use the saved one.
   * @return {!Object}
   */
  function previewPayload(tr, promptOverride) {
    const type = tr.querySelector(".sn-type").value;
    const sound = isTts(type);
    return {
      note_type: noteTypeSel.value,
      base_field: baseSel.value,
      field: tr.dataset.field,
      type: type,
      prompt: promptOverride !== null ? promptOverride : tr.dataset.prompt || "",
      provider: tr.querySelector(".sn-provider").value,
      model: sound ? "" : tr.dataset.model || "",
      voice: sound ? tr.dataset.voice || "" : "",
      language: sound ? tr.dataset.language || "" : "",
      decks: selectedDeckIds()
    };
  }

  /**
   * Preview a row from its ▶ button (result shown on the footer line; audio is played).
   * @param {!HTMLTableRowElement} tr The row to preview.
   */
  function previewRow(tr) {
    if (tr.classList.contains("sn-row-locked")) {
      return;
    }
    setMsg('<span class="sn-spin"></span>Previewing ' + tr.dataset.field + "…", false);
    send("preview", previewPayload(tr, null), null);
  }

  modalClose.addEventListener("click", closeModal);
  modalCancel.addEventListener("click", closeModal);
  modal.addEventListener("click", function (e) {
    if (e.target === modal) {
      closeModal();
    }
  });

  modalSave.addEventListener("click", function () {
    if (modalRow) {
      modalRow.dataset.prompt = modalPrompt.value;
      updatePromptSummary(modalRow);
    }
    closeModal();
  });

  // ✨ Improve (mechanism X): rewrite the rough prompt in the editor into a polished one.
  modalImprove.addEventListener("click", function () {
    if (!modalRow) {
      return;
    }
    modalImprove.disabled = true;
    setModalMsg('<span class="sn-spin"></span>Improving the prompt…', false);
    send(
      "improve_prompt",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        field: modalRow.dataset.field,
        prompt: modalPrompt.value
      },
      null
    );
  });

  /**
   * Receive an Improve result (off-thread). Updates the editor when still open for the field,
   * otherwise writes straight into the row.
   * @param {string} field The field the result is for.
   * @param {?Object} res {prompt} on success, {error} on failure.
   */
  window.__snImproveResult = function (field, res) {
    modalImprove.disabled = false;
    if (res && res.prompt) {
      if (modalRow && modalRow.dataset.field === field && !modal.hidden) {
        modalPrompt.value = res.prompt;
        setModalMsg("Improved — review and Save prompt.", false);
      } else {
        const tr = rowByField(field);
        if (tr) {
          tr.dataset.prompt = res.prompt;
          updatePromptSummary(tr);
        }
      }
    } else {
      setModalMsg((res && res.error) || "Improve failed — see logs.", true);
    }
  };

  // ▶ Preview from inside the editor (uses the in-editor prompt text).
  modalPreview.addEventListener("click", function () {
    if (!modalRow) {
      return;
    }
    modalPreview.disabled = true;
    setModalMsg('<span class="sn-spin"></span>Generating a preview…', false);
    send("preview", previewPayload(modalRow, modalPrompt.value), null);
  });

  /**
   * Receive a Preview result (off-thread). Shows it in the editor when open for the field,
   * otherwise on the footer line.
   * @param {string} field The field the result is for.
   * @param {?Object} res {kind, text|message} on success, {error} on failure.
   */
  window.__snPreviewResult = function (field, res) {
    modalPreview.disabled = false;
    const inModal = modalRow && modalRow.dataset.field === field && !modal.hidden;
    const show = function (html, isErr) {
      if (inModal) {
        setModalMsg("", false);
        setModalResult(html, isErr);
      } else {
        setMsg(html, isErr);
      }
    };
    if (!res || res.error) {
      show((res && res.error) || "Preview failed — see logs.", true);
      return;
    }
    if (res.kind === "text") {
      show(res.text || "(empty result)", false);
    } else if (res.kind === "tts") {
      show("🔊 " + (res.message || "Audio preview played."), false);
    } else {
      show("🖼️ " + (res.message || "Image generated."), false);
    }
  };
