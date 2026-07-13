
  /**
   * Smart Notes config page — part 4 of 6 of the page IIFE.
   * The prompt-editor popup (a plain in-page overlay, not a nested webview) plus the
   * Preview/Improve actions it and the row ▶ button share. Improve and Preview run off the Qt
   * main thread, so their results arrive through the window.__sn*Result push hooks rather than
   * the synchronous bridge callback.
   */

  // The row currently open in the editor (null when the popup is closed).
  let modalRow = null;

  // --- image lightbox --------------------------------------------------------------
  // Generated images are shown as a line + a "Preview" button (never inlined — a full-res
  // picture overflows the dialog and breaks the layout). The button opens this borderless
  // overlay across the whole Smart Notes UI; click anywhere or press Esc to close.
  /**
   * Open the full-screen image lightbox for a data URI.
   * @param {string} src The image data URI.
   */
  function openLightbox(src) {
    if (!src) {
      return;
    }
    lightboxImg.src = src;
    lightbox.hidden = false;
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lightboxImg.src = "";
  }

  lightbox.addEventListener("click", closeLightbox);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !lightbox.hidden) {
      closeLightbox();
    }
  });

  /**
   * Build an image-result block: a "🖼️ <message>" line + a Preview button that opens the
   * lightbox. Shared by the playground and the prompt-editor preview so a generated image is
   * presented identically and never overflows.
   * @param {!Object} payload {image: dataURI, message}.
   * @return {!HTMLElement}
   */
  function imageResultNode(payload) {
    const wrap = document.createElement("div");
    wrap.className = "sn-img-result";
    const line = document.createElement("div");
    line.textContent = "🖼️ " + (payload.message || "Image generated.");
    wrap.appendChild(line);
    if (payload.image) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sn-btn sn-img-preview";
      btn.textContent = "🔍 Preview image";
      btn.addEventListener("click", function () {
        openLightbox(payload.image);
      });
      wrap.appendChild(btn);
    }
    return wrap;
  }

  /**
   * Build the "inputs" block for a preview result: the fields this generation READ ({{Field}}) and
   * the sample values it ran against — so the user sees the INPUT, not just the output. Values are
   * user content, so they go in via textContent (never innerHTML). Returns null when there are no
   * inputs (e.g. an empty-prompt field with no source) so callers can skip it.
   * @param {?Array<!Object>} inputs [{field, value}] from the preview payload.
   * @return {?HTMLElement}
   */
  function inputsResultNode(inputs) {
    if (!inputs || !inputs.length) {
      return null;
    }
    const wrap = document.createElement("div");
    wrap.className = "sn-preview-inputs";
    const label = document.createElement("div");
    label.className = "sn-preview-inputs-label";
    label.textContent = "Ran on these input fields:";
    wrap.appendChild(label);
    inputs.forEach(function (inp) {
      const row = document.createElement("div");
      row.className = "sn-preview-input";
      const name = document.createElement("span");
      name.className = "sn-preview-input-field";
      name.textContent = "{{" + inp.field + "}}";
      const val = document.createElement("span");
      val.className = "sn-preview-input-value";
      if (inp.value) {
        val.textContent = inp.value;
      } else {
        val.textContent = "(empty)";
        val.classList.add("sn-preview-input-empty");
      }
      row.appendChild(name);
      row.appendChild(val);
      wrap.appendChild(row);
    });
    return wrap;
  }

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
    refreshModalWarn(); // surface a bad {{ref}} / brace right away on an already-saved prompt
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

  /** A lower-cased set of every field name that exists on this note type (base + all rows). */
  function knownFieldsLower() {
    const set = {};
    const add = function (name) {
      const key = (name || "").trim().toLowerCase();
      if (key) {
        set[key] = true;
      }
    };
    add(baseSel.value);
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      add(tr.dataset.field);
    });
    return set;
  }

  /**
   * Validate the editor's prompt (the guard rail for direct prompt edits): report ``{{Field}}``
   * references to fields that DON'T exist on this note type, and unbalanced ``{{ }}`` braces.
   * Cloze markers (``{{c1::…}}``) are not field refs and are ignored.
   * @param {string} prompt The prompt text to validate.
   * @return {!Object} ``{unknown: [displayRef, ...], syntaxBad: boolean}``.
   */
  function promptRefIssues(prompt) {
    const text = prompt || "";
    const known = knownFieldsLower();
    const unknown = [];
    const seen = {};
    const re = /\{\{(?!c\d+::)([^{}]+)\}\}/g;
    let match;
    while ((match = re.exec(text))) {
      const name = match[1].trim();
      const lc = name.toLowerCase();
      if (!lc || known[lc] || seen[lc]) {
        continue;
      }
      seen[lc] = true;
      unknown.push("{{" + name + "}}");
    }
    const opens = (text.match(/\{\{/g) || []).length;
    const closes = (text.match(/\}\}/g) || []).length;
    return {unknown: unknown, syntaxBad: opens !== closes};
  }

  /**
   * Guard for an IMAGE field's prompt. An image field's prompt is sent VERBATIM to the image model
   * as the picture description, so it must DESCRIBE the image — not instruct the model to WRITE OUT
   * a text prompt. Auto-prompt/Improve, being text-oriented, tend to produce "meta-prompts" ("…
   * generate an image generation prompt … output ONLY the prompt text") that make the image model
   * return text instead of a picture (finishReason=STOP). Flag the tell-tale phrasings.
   * @param {string} prompt The prompt text.
   * @return {string} A short problem clause, or "" when the prompt looks like a real image brief.
   */
  function imagePromptIssue(prompt) {
    const text = (prompt || "").toLowerCase();
    if (!text.trim()) {
      return "";
    }
    if (/output\s+only\b[^.]{0,60}\b(prompt|text)\b/.test(text)) {
      return "it tells the model to “output only … text/prompt”";
    }
    if (/\bimage[\s-]?(generation\s+)?prompt\b/.test(text)) {
      return "it asks the model to write an image PROMPT (text)";
    }
    if (/\b(dall[\s-]?e|dall·e|midjourney|stable\s+diffusion)\b/.test(text)) {
      return "it writes a prompt for another image tool (DALL·E / Midjourney)";
    }
    return "";
  }

  /** The kind (text|image|tts) of the row being edited, from its Type picker ("" if none). */
  function modalKind() {
    if (!modalRow) {
      return "";
    }
    const sel = modalRow.querySelector(".sn-type");
    return sel ? sel.value : "";
  }

  /**
   * Refresh the editor's guard-rail warning band from the current prompt. Returns true ONLY for a
   * BLOCKING issue (an unknown field ref or unbalanced braces), so Save can refuse. The image-prompt
   * heuristic is ADVISORY: it still shows its warning band but must NOT block Save (a valid image
   * brief can legitimately contain a flagged style word like "midjourney").
   * @return {boolean}
   */
  function refreshModalWarn() {
    if (!modalWarn) {
      return false;
    }
    const issues = promptRefIssues(modalPrompt.value);
    const parts = [];
    let blocking = false;
    if (issues.syntaxBad) {
      parts.push("Unbalanced <b>{{ }}</b> — check the braces.");
      blocking = true;
    }
    if (issues.unknown.length) {
      parts.push(
        "Not a field on this note type: <b>" +
          issues.unknown.map(esc).join(" ") +
          "</b> — fix or remove it (it won’t be filled in)."
      );
      blocking = true;
    }
    if (modalKind() === "image") {
      const imgIssue = imagePromptIssue(modalPrompt.value);
      if (imgIssue) {
        // Advisory only — warn, but don't set `blocking` (don't gate Save on a heuristic).
        parts.push(
          "This is an <b>image</b> field — its prompt is sent to the image model AS the picture " +
            "description, but this one reads like a text prompt (" +
            esc(imgIssue) +
            "), so the model returns text, not a picture. Describe the image directly, e.g. " +
            "“A photorealistic photo of {{Word}} …”."
        );
      }
    }
    if (parts.length) {
      modalWarn.innerHTML = "⚠ " + parts.join("<br>⚠ ");
      modalWarn.hidden = false;
      return blocking;
    }
    modalWarn.hidden = true;
    return false;
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
    setMsg('<span class="sn-spin"></span>Previewing ' + esc(tr.dataset.field) + "…", false);
    send("preview", previewPayload(tr, null), null);
  }

  modalClose.addEventListener("click", closeModal);
  modalCancel.addEventListener("click", closeModal);
  // Live guard rail: re-validate the prompt's refs/braces as the user types.
  modalPrompt.addEventListener("input", refreshModalWarn);
  modal.addEventListener("click", function (e) {
    if (e.target === modal) {
      closeModal();
    }
  });

  modalSave.addEventListener("click", function () {
    // Guard rail: refuse to save a prompt that references a non-existent field or has unbalanced
    // {{ }} — the warning band says what's wrong; fix it (or remove the ref) and Save again.
    if (modalRow && refreshModalWarn()) {
      return;
    }
    if (modalRow) {
      const oldPrompt = modalRow.dataset.prompt || "";
      const newPrompt = modalPrompt.value;
      modalRow.dataset.prompt = newPrompt;
      updatePromptSummary(modalRow);
      maybeClassifyDeps(modalRow, oldPrompt, newPrompt);
    }
    closeModal();
  });

  /**
   * After a prompt is saved, kick off the prompt→graph dependency sync (Feature 1) when the
   * prompt actually changed and references at least one field — the off-thread classifier labels
   * the new refs hard/soft and the result recolours the graph via window.__snDepsResult.
   * @param {!HTMLTableRowElement} tr The row whose prompt was saved.
   * @param {string} oldPrompt The prompt before this save.
   * @param {string} newPrompt The prompt after this save.
   */
  function maybeClassifyDeps(tr, oldPrompt, newPrompt) {
    // Skip when unchanged or there's no FIELD ref. The negative lookahead mirrors Python's
    // extract_field_refs so a cloze-only prompt ({{c1::…}}) doesn't fire a no-op classify.
    if (newPrompt === oldPrompt || !/\{\{(?!c\d+::)[^{}]+\}\}/.test(newPrompt)) {
      return;
    }
    setMsg('<span class="sn-spin"></span>↻ Updating dependencies…', false);
    send(
      "classify_deps",
      {
        note_type: noteTypeSel.value,
        base_field: baseSel.value,
        rows: [
          {
            field: tr.dataset.field,
            prompt: newPrompt,
            depends_on: readDependsOn(tr)
          }
        ]
      },
      null
    );
  }

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
        prompt: modalPrompt.value,
        type: modalKind()
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
        refreshModalWarn(); // the prompt just changed — re-evaluate the guard band, don't leave it stale
        setModalMsg("Improved — review and Save prompt.", false);
      } else {
        const tr = rowByField(field);
        if (tr) {
          tr.dataset.prompt = res.prompt;
          updatePromptSummary(tr);
        }
      }
    } else {
      setModalMsg(esc((res && res.error) || "Improve failed — see logs."), true);
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
    if (!res || res.error) {
      // The error can be ProviderError text (untrusted) written into an innerHTML sink — escape
      // it so it renders as text, not markup.
      const msg = esc((res && res.error) || "Preview failed — see logs.");
      if (inModal) {
        setModalMsg("", false);
        setModalResult(msg, true);
      } else {
        setMsg(msg, true);
      }
      return;
    }
    // Success: render the INPUT fields it ran on (when present) ABOVE the generated output, as DOM
    // nodes so user field values are safely escaped. Same layout in the modal and on the footer.
    const container = inModal ? modalResult : msgEl;
    if (inModal) {
      setModalMsg("", false);
      modalResult.className = "sn-modal-result";
    } else {
      msgEl.className = "sn-msg";
    }
    container.innerHTML = "";
    const inputsNode = inputsResultNode(res.inputs);
    if (inputsNode) {
      container.appendChild(inputsNode);
    }
    if (res.kind === "image") {
      // Don't inline the (often huge) image — a line + a Preview button (lightbox).
      container.appendChild(imageResultNode(res));
      return;
    }
    const out = document.createElement("div");
    out.className = "sn-preview-output";
    if (res.kind === "text") {
      out.textContent = res.text || "(empty result)"; // model output as TEXT, not HTML (XSS guard)
    } else if (res.kind === "tts") {
      out.textContent = "🔊 " + (res.message || "Audio preview played.");
    } else {
      out.textContent = "🖼️ " + (res.message || "Image generated.");
    }
    container.appendChild(out);
  };
