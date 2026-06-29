
  /**
   * Smart Notes config page — part 3 of 6 of the page IIFE.
   * Row rendering + collection. Each non-base field is one row: a Generate toggle, a Lock
   * toggle (freeze + blur), a Type select (text / image / sound), a clickable Prompt summary
   * (opens the editor), and kind-aware Provider / Model / Voice pickers — Model applies to
   * text & image, Voice to sound. The Voice COLUMN only shows when at least one row is a sound
   * field; within a row the not-applicable cells are faded. The editable prompt/model/voice
   * live on the row's data-* attributes so collectRows reads a single source of truth. There is
   * no Language picker: a pinned voice fixes the language, and a voice left on "(provider
   * default)" lets the engine auto-detect it (the field's saved `language` is preserved as-is).
   */

  /**
   * Build the table row element for one field config.
   * @param {!Object} row The field config (field, enabled, type, prompt, …).
   * @return {!HTMLTableRowElement}
   */
  function renderRow(row) {
    const tr = document.createElement("tr");
    tr.dataset.field = row.field;
    tr.dataset.prompt = row.prompt || "";
    tr.dataset.model = row.model || "";
    tr.dataset.voice = row.voice || "";
    tr.dataset.language = row.language || "";

    const tdName = cell("sn-fieldname");
    tdName.textContent = row.field;
    tr.appendChild(tdName);

    const tdOn = cell("sn-center");
    tdOn.appendChild(makeToggle("sn-enabled", row.enabled));
    tr.appendChild(tdOn);

    tr.appendChild(makeLockCell(tr, row.prompt_locked));

    const tdType = cell("sn-lockable");
    const typeSel = makeTypeSelect(row.type || "text");
    typeSel.addEventListener("change", function () {
      onKindChange(tr, typeSel.value);
    });
    tdType.appendChild(typeSel);
    tr.appendChild(tdType);

    tr.appendChild(makePromptCell(tr));

    tr.appendChild(cell("sn-lockable sn-provider-cell"));
    tr.appendChild(cell("sn-lockable sn-model-cell"));
    tr.appendChild(cell("sn-lockable sn-col-voice sn-voice-cell"));

    const tdPrev = cell("sn-center sn-lockable");
    const prev = document.createElement("button");
    prev.type = "button";
    prev.className = "sn-iconbtn sn-preview";
    prev.textContent = "▶";
    prev.title = "Preview: generate this field for a random note";
    prev.addEventListener("click", function (e) {
      e.stopPropagation();
      previewRow(tr);
    });
    tdPrev.appendChild(prev);
    tr.appendChild(tdPrev);

    const tdOver = cell("sn-center sn-lockable");
    tdOver.appendChild(makeCheckbox("sn-overwrite", row.overwrite));
    tr.appendChild(tdOver);

    const kind = row.type || "text";
    rebuildProvider(tr, kind, row.provider || "");
    applyKindState(tr, kind);
    updatePromptSummary(tr);
    applyLockState(tr);
    return tr;
  }

  /**
   * Build the Type select, labelling the ``tts`` value as "sound".
   * @param {string} current The selected type value.
   * @return {!HTMLSelectElement}
   */
  function makeTypeSelect(current) {
    const sel = document.createElement("select");
    sel.className = "sn-type";
    SN_TYPES.forEach(function (t) {
      sel.appendChild(opt(t, TYPE_LABELS[t] || t, t === current));
    });
    return sel;
  }

  /**
   * Build the Lock cell: a toggle that freezes + blurs the row's settings when on.
   * @param {!HTMLTableRowElement} tr The owning row.
   * @param {boolean} locked Initial locked state.
   * @return {!HTMLTableCellElement}
   */
  function makeLockCell(tr, locked) {
    const td = cell("sn-center");
    const lock = document.createElement("button");
    lock.type = "button";
    lock.className = "sn-lock" + (locked ? " sn-locked" : "");
    lock.textContent = locked ? "🔒" : "🔓";
    lock.title = "Lock this field — freeze its settings; skipped by Auto-smart / Improve";
    lock.addEventListener("click", function () {
      const isLocked = lock.classList.toggle("sn-locked");
      lock.textContent = isLocked ? "🔒" : "🔓";
      applyLockState(tr);
    });
    td.appendChild(lock);
    return td;
  }

  /**
   * Build the Prompt cell: a clickable summary that opens the popup editor when unlocked.
   * @param {!HTMLTableRowElement} tr The owning row.
   * @return {!HTMLTableCellElement}
   */
  function makePromptCell(tr) {
    const td = cell("sn-prompt-cell sn-lockable");
    const summary = document.createElement("div");
    summary.className = "sn-prompt-summary";
    td.appendChild(summary);
    td.addEventListener("click", function () {
      if (!tr.classList.contains("sn-row-locked")) {
        openPromptEditor(tr);
      }
    });
    return td;
  }

  /**
   * Reflect the row's data-prompt in its summary cell (truncated; muted hint when empty).
   * @param {!HTMLTableRowElement} tr The row to refresh.
   */
  function updatePromptSummary(tr) {
    const summary = tr.querySelector(".sn-prompt-summary");
    const text = (tr.dataset.prompt || "").trim();
    if (text) {
      summary.textContent = text.length > 90 ? text.slice(0, 90) + "…" : text;
      summary.classList.remove("sn-prompt-empty");
    } else {
      summary.textContent = "Click to write a prompt…";
      summary.classList.add("sn-prompt-empty");
    }
  }

  /**
   * Fill a <select> into a cell from a list, preserving a saved value not in the list.
   * @param {!HTMLTableCellElement} td The cell to (re)fill.
   * @param {string} cls The select's class.
   * @param {!Array<!Object>} options Entries of {value, label}.
   * @param {string} saved The currently-saved value.
   * @param {function(string)} onChange Called with the new value on change.
   */
  function fillCellSelect(td, cls, options, saved, onChange) {
    const sel = document.createElement("select");
    sel.className = cls;
    let matched = false;
    options.forEach(function (o) {
      sel.appendChild(opt(o.value, o.label, o.value === saved));
      matched = matched || o.value === saved;
    });
    if (!matched && saved) {
      sel.appendChild(opt(saved, saved + " (saved)", true));
    }
    sel.addEventListener("change", function () {
      onChange(sel.value);
    });
    td.innerHTML = "";
    td.appendChild(sel);
  }

  /**
   * Rebuild the Provider picker for a kind (LLM providers for text/image, TTS for sound), then
   * the Model + Voice pickers (which depend on the chosen provider).
   * @param {!HTMLTableRowElement} tr The row.
   * @param {string} kind text | image | tts
   * @param {string} presetProvider Provider to preselect (validated against the kind's list).
   */
  function rebuildProvider(tr, kind, presetProvider) {
    const providers = [""].concat(providerNames(kind));
    const current = providers.indexOf(presetProvider) >= 0 ? presetProvider : "";
    const options = providers.map(function (p) {
      return {value: p, label: p === "" ? "(inherit)" : p};
    });
    fillCellSelect(
      tr.querySelector(".sn-provider-cell"),
      "sn-provider",
      options,
      current,
      function (value) {
        const k = tr.querySelector(".sn-type").value;
        rebuildModel(tr, k, value);
        rebuildVoice(tr, value);
      }
    );
    rebuildModel(tr, kind, current);
    rebuildVoice(tr, current);
  }

  /**
   * Rebuild the Model picker (text/image models for the provider; inherit when blank).
   * @param {!HTMLTableRowElement} tr The row.
   * @param {string} kind text | image | tts
   * @param {string} provider The selected provider.
   */
  function rebuildModel(tr, kind, provider) {
    const options = [{value: "", label: "(inherit)"}].concat(
      modelValues(kind, provider).map(function (m) {
        return {value: m, label: m};
      })
    );
    fillCellSelect(
      tr.querySelector(".sn-model-cell"),
      "sn-model",
      options,
      tr.dataset.model || "",
      function (value) {
        tr.dataset.model = value;
      }
    );
  }

  /**
   * Rebuild the Voice picker for a TTS provider (blank = the provider's default voice).
   * @param {!HTMLTableRowElement} tr The row.
   * @param {string} provider The selected TTS provider.
   */
  function rebuildVoice(tr, provider) {
    const options = [{value: "", label: "Auto-detect"}].concat(
      voiceEntries(provider).map(function (v) {
        return {value: v.voice, label: v.label};
      })
    );
    fillCellSelect(
      tr.querySelector(".sn-voice-cell"),
      "sn-voice",
      options,
      tr.dataset.voice || "",
      function (value) {
        tr.dataset.voice = value;
      }
    );
  }

  /**
   * Mark the cells that don't apply to a row's kind: Model is n/a for sound; Voice is n/a for
   * text/image (faded via .sn-na).
   * @param {!HTMLTableRowElement} tr The row.
   * @param {string} kind text | image | tts
   */
  function applyKindState(tr, kind) {
    const sound = isTts(kind);
    tr.querySelector(".sn-model-cell").classList.toggle("sn-na", sound);
    tr.querySelector(".sn-voice-cell").classList.toggle("sn-na", !sound);
  }

  /**
   * React to a Type change: re-scope Provider/Model/Voice to the new kind, refresh the
   * applicability fade, and show/hide the Voice column.
   * @param {!HTMLTableRowElement} tr The row.
   * @param {string} kind The new type value.
   */
  function onKindChange(tr, kind) {
    rebuildProvider(tr, kind, "");
    applyKindState(tr, kind);
    updateSoundColumns();
  }

  /**
   * Apply the row's lock state: toggle the blur class and disable the frozen controls (the
   * Generate switch and the Lock toggle stay live so generation can still be toggled).
   * @param {!HTMLTableRowElement} tr The row.
   */
  function applyLockState(tr) {
    const locked = tr.querySelector(".sn-lock").classList.contains("sn-locked");
    tr.classList.toggle("sn-row-locked", locked);
    [
      "sn-type",
      "sn-provider",
      "sn-model",
      "sn-voice",
      "sn-overwrite",
      "sn-preview"
    ].forEach(function (cls) {
      const el = tr.querySelector("." + cls);
      if (el) {
        el.disabled = locked;
      }
    });
  }

  /**
   * Sort the table rows by field name, toggling ascending ↔ descending. Reorders the existing
   * <tr> nodes (their state is preserved); a later re-render restores the note type's order.
   */
  function sortByField() {
    sortDir = sortDir === 1 ? -1 : 1;
    const rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
    rows.sort(function (a, b) {
      return sortDir * a.dataset.field.localeCompare(b.dataset.field);
    });
    rows.forEach(function (tr) {
      tbody.appendChild(tr);
    });
    sortBtn.textContent = sortDir === 1 ? "↑" : "↓";
    sortBtn.classList.add("sn-sort-active");
  }

  /** Show the Voice column only when at least one row is a sound field. */
  function updateSoundColumns() {
    let hasSound = false;
    Array.prototype.forEach.call(tbody.querySelectorAll(".sn-type"), function (sel) {
      if (sel.value === "tts") {
        hasSound = true;
      }
    });
    document.querySelector(".sn-table").classList.toggle("sn-has-sound", hasSound);
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
    updateSoundColumns();
  }

  /**
   * Build the deck hierarchy from the flat `allDecks` ("::"-separated names). Every level is a
   * real Anki deck, so each node carries its own deck id.
   * @return {!Object} A root node {seg, full, id, kids: [node, ...]}.
   */
  function buildDeckTree() {
    const root = {seg: "", full: "", id: null, kids: [], index: {}};
    allDecks.forEach(function (deck) {
      const parts = String(deck.name).split("::");
      let node = root;
      let path = "";
      parts.forEach(function (seg, i) {
        path = i === 0 ? seg : path + "::" + seg;
        let child = node.index[path];
        if (!child) {
          child = {seg: seg, full: path, id: null, kids: [], index: {}};
          node.index[path] = child;
          node.kids.push(child);
        }
        node = child;
      });
      node.id = parseInt(deck.id, 10);
    });
    return root;
  }

  /** All deck ids in a node's subtree (the node + every descendant). */
  function subtreeIds(node) {
    let ids = node.id != null ? [node.id] : [];
    node.kids.forEach(function (kid) {
      ids = ids.concat(subtreeIds(kid));
    });
    return ids;
  }

  /**
   * Render the deck picker as a collapsible, indented tree filtered by the search box.
   * Subdecks are hidden until their parent is expanded (a search auto-expands matches). A
   * deck's checkbox cascades to its whole subtree; "All decks" mode shows everything ticked +
   * disabled (the canonical "no restriction", persisted as []).
   */
  function renderDecks() {
    const term = (decksSearch.value || "").trim().toLowerCase();
    decksList.innerHTML = "";
    let shown = 0;

    function matches(node) {
      if (!term) {
        return true;
      }
      if (node.full.toLowerCase().indexOf(term) >= 0) {
        return true;
      }
      return node.kids.some(matches);
    }

    function renderKids(node, depth) {
      node.kids.forEach(function (child) {
        if (!matches(child)) {
          return;
        }
        shown += 1;
        decksList.appendChild(deckRow(child, depth, term));
        const expanded = term ? true : !!deckExpanded[child.full];
        if (child.kids.length && expanded) {
          renderKids(child, depth + 1);
        }
      });
    }

    if (deckTree) {
      renderKids(deckTree, 0);
    }
    decksEmpty.textContent =
      allDecks.length === 0
        ? "No decks in this collection."
        : shown === 0
          ? "No deck matches “" + decksSearch.value + "”."
          : "";
    decksAll.checked = deckAllMode;
    updateDecksSummary();
  }

  /**
   * Build one tree row: an expand toggle (for nodes with children), a cascading checkbox, and
   * the deck's leaf name, indented by depth.
   * @param {!Object} node The deck node.
   * @param {number} depth Indent level.
   * @param {string} term The active search term (forces expansion when set).
   * @return {!HTMLElement}
   */
  function deckRow(node, depth, term) {
    const row = document.createElement("div");
    row.className = "sn-decks-row";
    row.style.paddingLeft = 6 + depth * 18 + "px";

    const tog = document.createElement("span");
    tog.className = "sn-deck-tog";
    if (node.kids.length) {
      tog.textContent = term || deckExpanded[node.full] ? "▾" : "▸";
      tog.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        deckExpanded[node.full] = !deckExpanded[node.full];
        renderDecks();
      });
    }

    const label = document.createElement("label");
    label.className = "sn-deck-label";
    label.title = node.full;
    const cb = makeCheckbox(
      "sn-deck-cb",
      deckAllMode || selectedDecks.indexOf(node.id) >= 0
    );
    cb.disabled = deckAllMode;
    cb.addEventListener("change", function () {
      onDeckToggle(node, cb.checked);
    });
    const span = document.createElement("span");
    span.textContent = node.seg;
    label.appendChild(cb);
    label.appendChild(span);

    row.appendChild(tog);
    row.appendChild(label);
    return row;
  }

  /**
   * Toggle a deck (and its whole subtree) in the selection; an empty selection snaps back to
   * "All decks" mode.
   * @param {!Object} node The deck node toggled.
   * @param {boolean} checked The checkbox's new state.
   */
  function onDeckToggle(node, checked) {
    subtreeIds(node).forEach(function (id) {
      const at = selectedDecks.indexOf(id);
      if (checked && at < 0) {
        selectedDecks.push(id);
      } else if (!checked && at >= 0) {
        selectedDecks.splice(at, 1);
      }
    });
    if (selectedDecks.length === 0) {
      deckAllMode = true;
    }
    renderDecks();
  }

  /** Reflect the selection in the toolbar button label ("All decks" or "N deck(s)"). */
  function updateDecksSummary() {
    const n = selectedDecks.length;
    decksBtn.textContent =
      deckAllMode || n === 0 ? "All decks" : n + " deck" + (n === 1 ? "" : "s");
  }

  /**
   * Toggle a whole column from its header: flip every row's control for `col` to a single
   * shared state (if any is "off", turn all on; otherwise all off).
   * @param {string} col "generate" | "lock" | "overwrite"
   */
  function toggleAllColumn(col) {
    const rows = tbody.querySelectorAll("tr");
    if (col === "lock") {
      const turnOn = Array.prototype.some.call(rows, function (tr) {
        return !tr.querySelector(".sn-lock").classList.contains("sn-locked");
      });
      Array.prototype.forEach.call(rows, function (tr) {
        const lock = tr.querySelector(".sn-lock");
        lock.classList.toggle("sn-locked", turnOn);
        lock.textContent = turnOn ? "🔒" : "🔓";
        applyLockState(tr);
      });
      return;
    }
    const sel = col === "generate" ? ".sn-enabled" : ".sn-overwrite";
    const boxes = [];
    Array.prototype.forEach.call(rows, function (tr) {
      const box = tr.querySelector(sel);
      if (box && !box.disabled) {
        boxes.push(box);
      }
    });
    const turnOn = boxes.some(function (box) {
      return !box.checked;
    });
    boxes.forEach(function (box) {
      box.checked = turnOn;
    });
  }

  /**
   * Read every table row back into a list of row payloads.
   * @return {!Array<!Object>}
   */
  function collectRows() {
    const rows = [];
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      const type = tr.querySelector(".sn-type").value;
      const sound = isTts(type);
      rows.push({
        field: tr.dataset.field,
        enabled: tr.querySelector(".sn-enabled").checked,
        type: type,
        prompt: tr.dataset.prompt || "",
        prompt_locked: tr.querySelector(".sn-lock").classList.contains("sn-locked"),
        provider: tr.querySelector(".sn-provider").value,
        model: sound ? "" : tr.dataset.model || "",
        voice: sound ? tr.dataset.voice || "" : "",
        language: sound ? tr.dataset.language || "" : "",
        overwrite: tr.querySelector(".sn-overwrite").checked
      });
    });
    return rows;
  }
