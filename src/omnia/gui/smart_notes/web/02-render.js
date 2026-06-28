
  /**
   * Smart Notes config page — part 2 of 4 of the page IIFE (load order matters).
   * Row rendering and collection: builds one table row per non-base field and reads the
   * editable state back out of the DOM.
   */

  /**
   * Build the table row element for one field config.
   * @param {!Object} row The field config (field, enabled, type, prompt, …).
   * @return {!HTMLTableRowElement}
   */
  function renderRow(row) {
    const tr = document.createElement("tr");
    tr.setAttribute("data-field", row.field);

    const tdName = document.createElement("td");
    tdName.className = "sn-fieldname";
    tdName.textContent = row.field;
    tr.appendChild(tdName);

    const tdOn = document.createElement("td");
    tdOn.className = "sn-center";
    tdOn.appendChild(makeCheckbox("sn-enabled", row.enabled));
    tr.appendChild(tdOn);

    const tdType = document.createElement("td");
    tdType.appendChild(makeSelect("sn-type", SN_TYPES, row.type || "text"));
    tr.appendChild(tdType);

    const tdPrompt = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "sn-prompt-cell";
    const ta = document.createElement("textarea");
    ta.className = "sn-prompt";
    ta.value = row.prompt || "";
    ta.placeholder = "e.g. Give the IPA for {{" + (baseSel.value || "Base") + "}}";
    const lock = document.createElement("button");
    lock.type = "button";
    lock.className = "sn-lock" + (row.prompt_locked ? " sn-locked" : "");
    lock.textContent = row.prompt_locked ? "🔒" : "🔓";
    lock.title = "Lock to protect this prompt/type from Auto-smart";
    lock.addEventListener("click", function () {
      const locked = lock.classList.toggle("sn-locked");
      lock.textContent = locked ? "🔒" : "🔓";
    });
    wrap.appendChild(ta);
    wrap.appendChild(lock);
    tdPrompt.appendChild(wrap);
    tr.appendChild(tdPrompt);

    const providerVals = [""].concat(providers);
    const tdProvider = document.createElement("td");
    tdProvider.appendChild(makeSelect("sn-provider", providerVals, row.provider || ""));
    tr.appendChild(tdProvider);

    const tdModel = document.createElement("td");
    tdModel.appendChild(makeInput("sn-model", row.model, "(inherit)"));
    tr.appendChild(tdModel);

    const tdOver = document.createElement("td");
    tdOver.className = "sn-center";
    tdOver.appendChild(makeCheckbox("sn-overwrite", row.overwrite));
    tr.appendChild(tdOver);

    return tr;
  }

  /**
   * Render all rows into the table body, showing the empty-state hint when there are none.
   * @param {!Array<!Object>} rows The field configs to render.
   */
  function renderRows(rows) {
    tbody.innerHTML = "";
    rows.forEach(function (row) {
      tbody.appendChild(renderRow(row));
    });
    emptyEl.textContent = rows.length
      ? ""
      : "This note type has no other fields to generate. Add one with “+ Create field”.";
  }

  /**
   * Read every table row back into a list of row payloads.
   * @return {!Array<!Object>}
   */
  function collectRows() {
    const rows = [];
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      const lock = tr.querySelector(".sn-lock");
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
