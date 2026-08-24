/**
 * @fileoverview Smart Notes config page — Export / Import of one note type's setup.
 *
 * Export is one call. Import is deliberately two: the first reads the file and reports what
 * is in it, and only after the user has answered the collision question — import as a new
 * note type, or map onto an existing one — does the second write anything. An import can
 * rewrite prompts and drop rules, so none of that may first become visible afterwards.
 */

  const exportBtn = document.getElementById("sn-export");
  const importBtn = document.getElementById("sn-import");
  const importModal = document.getElementById("sn-import-modal");
  const importClose = document.getElementById("sn-import-close");
  const importCancel = document.getElementById("sn-import-cancel");
  const importGo = document.getElementById("sn-import-go");
  const importSummary = document.getElementById("sn-import-summary");
  const importCollision = document.getElementById("sn-import-collision");
  const importNewName = document.getElementById("sn-import-newname");
  const importNameErr = document.getElementById("sn-import-name-err");
  const importMapping = document.getElementById("sn-import-mapping");
  const importMapRows = document.getElementById("sn-import-map-rows");
  const importMapNote = document.getElementById("sn-import-map-note");
  const importResult = document.getElementById("sn-import-result");
  const importTools = document.getElementById("sn-import-tools");
  const importToolsList = document.getElementById("sn-import-tools-list");

  // What the last "read the file" call told us. Null when no import is in flight.
  let pendingImport = null;

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (ch) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch];
    });
  }

  function importMode() {
    const picked = document.querySelector("input[name=sn-import-mode]:checked");
    return picked ? picked.value : "clone";
  }

  // -- export --------------------------------------------------------------------------
  if (exportBtn) {
    exportBtn.addEventListener("click", function () {
      const noteType = noteTypeSel ? noteTypeSel.value : "";
      if (!noteType) {
        setMsg("Pick a note type first.", true);
        return;
      }
      exportBtn.disabled = true;
      send("export_note_type", {note_type: noteType}, function (res) {
        exportBtn.disabled = false;
        const out = typeof res === "string" ? JSON.parse(res) : res;
        if (!out || out.cancelled) return;
        if (!out.ok) {
          setMsg(out.error || "Export failed.", true);
          return;
        }
        let msg = "Exported " + out.fields + " field rules to " + out.path;
        if (out.tools && out.tools.length) msg += " (with " + out.tools.length + " user tool(s))";
        if (out.missing_tools && out.missing_tools.length) {
          msg += " — could NOT read: " + out.missing_tools.join(", ");
        }
        setMsg(msg, !!(out.missing_tools && out.missing_tools.length));
      });
    });
  }

  // -- import, step 1: read the file ----------------------------------------------------
  if (importBtn) {
    importBtn.addEventListener("click", function () {
      importBtn.disabled = true;
      send("read_import_file", {}, function (res) {
        importBtn.disabled = false;
        const out = typeof res === "string" ? JSON.parse(res) : res;
        if (!out || out.cancelled) return;
        if (!out.ok) {
          setMsg(out.error || "Could not read that file.", true);
          return;
        }
        pendingImport = out;
        showImportModal(out);
      });
    });
  }

  function showImportModal(info) {
    const src = info.source || {};
    const provenance = [src.machine, src.platform, src.exported_at]
      .filter(Boolean)
      .join(" · ");
    importSummary.innerHTML =
      "<div class='sn-import-name'>" + escapeHtml(info.note_type) + "</div>" +
      "<div class='sn-import-meta'>" +
      escapeHtml(info.rules + " field rules, " + info.enabled + " switched on" +
        (info.source_fields ? ", " + info.source_fields.length + " fields" : "")) +
      (provenance ? "<br>from " + escapeHtml(provenance) : "") +
      (info.user_tools && info.user_tools.length
        ? "<br>user tools: " + escapeHtml(info.user_tools.join(", "))
        : "") +
      (info.missing_tools && info.missing_tools.length
        ? "<br><span class='sn-import-warn'>missing tool source: " +
          escapeHtml(info.missing_tools.join(", ")) + "</span>"
        : "") +
      "</div>";

    importResult.hidden = true;
    importResult.innerHTML = "";
    importNameErr.hidden = true;
    importGo.disabled = false;
    importGo.textContent = "Import";

    buildToolApprovals(info.carried_tools || []);

    if (info.collides) {
      importCollision.hidden = false;
      importNewName.value = uniqueName(info.note_type, info.note_type_names || []);
      buildMappingRows(info);
      syncCollisionUi();
    } else {
      importCollision.hidden = true;
    }
    importModal.hidden = false;
  }

  function buildToolApprovals(tools) {
    // Installing a carried tool RUNS it. Show the source and what it reaches for, and install
    // only what the reader ticks — the same read-and-run review the Tools tab asks for.
    const needing = tools.filter(function (t) { return !t.already_installed; });
    importToolsList.innerHTML = "";
    importTools.hidden = needing.length === 0;
    needing.forEach(function (tool, index) {
      const wrap = document.createElement("div");
      wrap.className = "sn-import-tool";

      const row = document.createElement("label");
      row.className = "sn-import-tool-row";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "sn-import-tool-approve";
      box.setAttribute("data-tool", tool.name);
      const text = document.createElement("span");
      text.innerHTML =
        "<span class='sn-import-tool-name'>" + escapeHtml(tool.name) + "</span> — " +
        "I have read this code and want it to run here" +
        (tool.risks && tool.risks.length
          ? "<div class='sn-import-tool-risks'>Reaches for: " +
            escapeHtml(tool.risks.join("; ")) + "</div>"
          : "<div class='sn-import-tool-risks'>Only transforms text.</div>");
      row.appendChild(box);
      row.appendChild(text);
      wrap.appendChild(row);

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "sn-import-tool-toggle";
      toggle.textContent = "Show the code";
      const code = document.createElement("pre");
      code.className = "sn-import-tool-code";
      code.hidden = true;
      code.textContent = tool.code || "";
      toggle.addEventListener("click", function () {
        code.hidden = !code.hidden;
        toggle.textContent = code.hidden ? "Show the code" : "Hide the code";
      });
      wrap.appendChild(toggle);
      wrap.appendChild(code);
      importToolsList.appendChild(wrap);
      if (index === 0) code.hidden = true;
    });
  }

  function approvedTools() {
    const names = [];
    document.querySelectorAll(".sn-import-tool-approve").forEach(function (box) {
      if (box.checked) names.push(box.getAttribute("data-tool"));
    });
    return names;
  }

  function uniqueName(base, taken) {
    const used = {};
    (taken || []).forEach(function (n) { used[n] = true; });
    let candidate = base + " (imported)";
    let counter = 2;
    while (used[candidate]) candidate = base + " (imported " + counter++ + ")";
    return candidate;
  }

  function buildMappingRows(info) {
    const suggested = info.suggested_renames || {};
    const targets = info.target_fields || [];
    importMapRows.innerHTML = "";
    (info.source_fields || []).forEach(function (name) {
      const row = document.createElement("tr");
      const left = document.createElement("td");
      left.textContent = name;
      const right = document.createElement("td");
      const select = document.createElement("select");
      select.className = "sn-select sn-import-target";
      select.setAttribute("data-source", name);
      // "" is a real choice: a field with no counterpart here, whose rule is dropped.
      const skip = document.createElement("option");
      skip.value = "";
      skip.textContent = "— not imported —";
      select.appendChild(skip);
      targets.forEach(function (target) {
        const option = document.createElement("option");
        option.value = target;
        option.textContent = target;
        if (suggested[name] === target) option.selected = true;
        select.appendChild(option);
      });
      select.addEventListener("change", describeMapping);
      right.appendChild(select);
      row.appendChild(left);
      row.appendChild(right);
      importMapRows.appendChild(row);
    });
    describeMapping();
  }

  function collectRenames() {
    const renames = {};
    const seen = {};
    let duplicate = null;
    document.querySelectorAll(".sn-import-target").forEach(function (select) {
      const target = select.value;
      if (!target) return;
      if (seen[target]) duplicate = target;
      seen[target] = true;
      renames[select.getAttribute("data-source")] = target;
    });
    return {renames: renames, duplicate: duplicate};
  }

  function describeMapping() {
    const picked = collectRenames();
    const total = document.querySelectorAll(".sn-import-target").length;
    const mapped = Object.keys(picked.renames).length;
    let note = mapped + " of " + total + " fields mapped";
    if (total - mapped > 0) {
      note += "; the rules for the other " + (total - mapped) + " will be dropped";
    }
    if (picked.duplicate) {
      note = "Two incoming fields both map to “" + picked.duplicate +
        "” — each field here can take only one.";
    }
    importMapNote.textContent = note;
    importMapNote.className = picked.duplicate
      ? "sn-import-note sn-import-warn"
      : "sn-import-note";
    if (importMode() === "overwrite") importGo.disabled = !!picked.duplicate;
  }

  function syncCollisionUi() {
    const mode = importMode();
    importMapping.hidden = mode !== "overwrite";
    importNewName.disabled = mode !== "clone";
    if (mode === "overwrite") describeMapping();
    else importGo.disabled = false;
  }

  document.querySelectorAll("input[name=sn-import-mode]").forEach(function (radio) {
    radio.addEventListener("change", syncCollisionUi);
  });

  function closeImport() {
    importModal.hidden = true;
    pendingImport = null;
  }
  if (importClose) importClose.addEventListener("click", closeImport);
  if (importCancel) importCancel.addEventListener("click", closeImport);

  // -- import, step 2: apply -------------------------------------------------------------
  if (importGo) {
    importGo.addEventListener("click", function () {
      if (!pendingImport) return closeImport();
      const payload = {approved_tools: approvedTools()};
      if (!pendingImport.collides) {
        payload.mode = "create";
      } else if (importMode() === "clone") {
        const name = (importNewName.value || "").trim();
        const taken = (pendingImport.note_type_names || []).indexOf(name) !== -1;
        if (!name || taken) {
          importNameErr.textContent = name
            ? "That name is already taken here."
            : "Give the new note type a name.";
          importNameErr.hidden = false;
          return;
        }
        importNameErr.hidden = true;
        payload.mode = "clone";
        payload.target_name = name;
      } else {
        const picked = collectRenames();
        if (picked.duplicate) return;
        payload.mode = "overwrite";
        payload.target_name = pendingImport.note_type;
        payload.renames = picked.renames;
      }

      importGo.disabled = true;
      importGo.textContent = "Importing…";
      send("apply_import", payload, function (res) {
        const out = typeof res === "string" ? JSON.parse(res) : res;
        importGo.textContent = "Import";
        if (!out || !out.ok) {
          importGo.disabled = false;
          importResult.hidden = false;
          importResult.className = "sn-import-result sn-import-warn";
          importResult.textContent = (out && out.error) || "Import failed.";
          return;
        }
        renderImportResult(out);
      });
    });
  }

  function renderImportResult(out) {
    const lines = [];
    lines.push(
      (out.created ? "Created " : "Updated ") + "“" + escapeHtml(out.note_type) +
      "” with " + out.fields + " field rules."
    );
    if (out.tools && out.tools.length) {
      lines.push("Installed user tool(s): " + escapeHtml(out.tools.join(", ")));
    }
    if (out.unapproved_tools && out.unapproved_tools.length) {
      lines.push(
        "Not installed, because they were not ticked: " +
        escapeHtml(out.unapproved_tools.join(", ")) +
        ". Chains using them will not run until you add them yourself."
      );
    }
    if (out.tools_failed && out.tools_failed.length) {
      // The import applied, but a chain using this tool will not run — which the user has to
      // know, or they get a field that silently never generates.
      lines.push(
        "<span class='sn-import-warn'>These user tools could not be installed, so the " +
        "chains using them will not run: " + escapeHtml(out.tools_failed.join("; ")) +
        "</span>"
      );
    }
    (out.dropped_fields || []).length &&
      lines.push("Dropped rules with no field here: " + escapeHtml(out.dropped_fields.join(", ")));
    (out.dropped_dependencies || []).length &&
      lines.push("Dropped dependency edges: " + escapeHtml(out.dropped_dependencies.join(", ")));
    (out.dropped_tool_params || []).length &&
      lines.push(
        "<span class='sn-import-warn'>These tool parameters name a field with no " +
        "counterpart here, so those tools will read a field that does not exist: " +
        escapeHtml(out.dropped_tool_params.join("; ")) + "</span>"
      );
    (out.unchecked_tool_params || []).length &&
      lines.push(
        "Check these tool parameters by hand — the tool did not declare them as fields: " +
        escapeHtml(out.unchecked_tool_params.join("; "))
      );
    (out.warnings || []).forEach(function (w) { lines.push(escapeHtml(w)); });

    importResult.hidden = false;
    importResult.className = "sn-import-result";
    importResult.innerHTML = lines.join("<br>");
    importGo.disabled = true;
    // The note-type list and the table both changed underneath us. Re-list, select what was
    // just imported and load it, so the user is looking at the result rather than at the
    // stale render that was on screen when they opened the file.
    send("list_note_types", {}, function (names) {
      fill(noteTypeSel, names || [], out.note_type);
      loadNoteType();
    });
  }
