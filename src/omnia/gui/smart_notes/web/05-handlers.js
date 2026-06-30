
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
    deckAllMode = selectedDecks.length === 0;
    deckTree = buildDeckTree();
    renderDecks();
    applyOptions(res.options);
    setMsg("");
  }

  /**
   * The selected deck-id subset for the current note type ([] = all decks).
   * @return {!Array<number>}
   */
  function selectedDeckIds() {
    return deckAllMode ? [] : selectedDecks.slice();
  }

  // "All decks" mode = no restriction (persisted []), with every deck shown ticked + disabled.
  // Unticking it enables the tree for a subset; the toolbar button opens the modal (✕ / Done /
  // backdrop close it).
  decksAll.addEventListener("change", function () {
    deckAllMode = decksAll.checked;
    if (deckAllMode) {
      selectedDecks = [];
    }
    renderDecks();
  });
  decksSearch.addEventListener("input", renderDecks);

  function closeDecks() {
    decksModal.hidden = true;
  }

  decksBtn.addEventListener("click", function () {
    renderDecks();
    decksModal.hidden = false;
    decksSearch.focus();
  });
  decksClose.addEventListener("click", closeDecks);
  decksDone.addEventListener("click", closeDecks);
  decksModal.addEventListener("click", function (e) {
    if (e.target === decksModal) {
      closeDecks();
    }
  });

  // Clicking a Generate / Lock / Overwrite header toggles that column for ALL rows.
  Array.prototype.forEach.call(
    document.querySelectorAll(".sn-th-toggle"),
    function (th) {
      th.addEventListener("click", function () {
        toggleAllColumn(th.dataset.toggle);
      });
    }
  );

  // The sort button on the FIELD header sorts rows by field name (asc ↔ desc).
  sortBtn.addEventListener("click", sortByField);

  // Options modal — global Smart Notes flags, seeded from the load response, collected on save.
  /**
   * Seed the option checkboxes from the load response (regenerate defaults to true).
   * @param {?Object} opts {generate_at_review, regenerate_when_batching, allow_empty_fields}
   */
  function applyOptions(opts) {
    opts = opts || {};
    optGenReview.checked = !!opts.generate_at_review;
    optRegenBatch.checked = opts.regenerate_when_batching !== false;
    optAllowEmpty.checked = !!opts.allow_empty_fields;
  }

  /**
   * Read the option checkboxes back into the flags object sent with `save`.
   * @return {!Object}
   */
  function collectOptions() {
    return {
      generate_at_review: optGenReview.checked,
      regenerate_when_batching: optRegenBatch.checked,
      allow_empty_fields: optAllowEmpty.checked
    };
  }

  function closeOptions() {
    optionsModal.hidden = true;
  }

  optionsBtn.addEventListener("click", function () {
    // Always open on the General tab; Account + Advanced load their data on first show.
    showTab("general");
    optionsModal.hidden = false;
  });
  optionsClose.addEventListener("click", closeOptions);
  optionsDone.addEventListener("click", closeOptions);
  optionsModal.addEventListener("click", function (e) {
    if (e.target === optionsModal) {
      closeOptions();
    }
  });

  // The per-language Auto-detect voice map opens in its own popup (✕ / Done / backdrop close).
  function closeAutoVoices() {
    autovoicesModal.hidden = true;
  }
  autovoicesClose.addEventListener("click", closeAutoVoices);
  autovoicesDone.addEventListener("click", closeAutoVoices);
  autovoicesModal.addEventListener("click", function (e) {
    if (e.target === autovoicesModal) {
      closeAutoVoices();
    }
  });

  // --- Native runtimes (Advanced tab; ADR-005) -------------------------------------
  // Optional local engines (TTS/...) installed into an isolated venv. Each row is a checkbox
  // (checked = installed) + a status line. Ticking installs OFF-THREAD (the result is pushed
  // back through window.__snNativeRuntime*); unticking uninstalls (fast, synchronous callback).

  /** Fetch the registered native runtimes and (re)render the panel. */
  function loadNativeRuntimes() {
    send("native_runtimes", {}, renderNativeRuntimes);
  }

  /**
   * Render the grouped native-runtime rows into #sn-native-list.
   * @param {?Object} res {sections: [{section, runtimes: [{name, label, size_hint, installed}]}]}.
   */
  function renderNativeRuntimes(res) {
    nativeListEl.innerHTML = "";
    const sections = (res && res.sections) || [];
    if (!sections.length) {
      nativeListEl.innerHTML = '<div class="sn-acct-empty">No native runtimes available.</div>';
      return;
    }
    sections.forEach(function (group) {
      const head = document.createElement("div");
      head.className = "sn-native-section";
      head.textContent = (group.section || "").toUpperCase();
      nativeListEl.appendChild(head);
      (group.runtimes || []).forEach(function (rt) {
        nativeListEl.appendChild(nativeRuntimeRow(rt));
      });
    });
  }

  /**
   * Build one runtime row: a checkbox (checked = installed) + label/size + a status line.
   * @param {!Object} rt {name, label, size_hint, installed}.
   * @return {!HTMLElement}
   */
  function nativeRuntimeRow(rt) {
    const row = document.createElement("label");
    row.className = "sn-native-row";
    row.dataset.name = rt.name;

    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "sn-native-toggle";
    check.checked = !!rt.installed;
    check.title = rt.installed ? "Remove this runtime" : "Install this runtime";

    const main = document.createElement("div");
    main.className = "sn-native-main";
    const label = document.createElement("div");
    label.className = "sn-native-label";
    label.textContent = rt.label;
    const size = document.createElement("div");
    size.className = "sn-native-size";
    size.textContent = rt.size_hint || "";
    const status = document.createElement("div");
    status.className = "sn-native-status";
    setNativeStatus(status, rt.installed ? "Installed ✓" : "Not installed", rt.installed);
    main.appendChild(label);
    main.appendChild(size);
    main.appendChild(status);

    check.addEventListener("change", function () {
      if (check.checked) {
        check.disabled = true;
        setNativeStatus(status, '<span class="sn-spin"></span>Installing…', false, true);
        send("set_native_runtime", {name: rt.name, enabled: true}, null);
      } else {
        setNativeStatus(status, "Removing…", false);
        send("set_native_runtime", {name: rt.name, enabled: false}, function (r) {
          setNativeStatus(status, "Not installed", false);
          if (r && r.error) {
            setNativeStatus(status, "Error: " + r.error, false, false, true);
            check.checked = true;  // uninstall failed → it's still installed
          }
        });
      }
    });

    // Content on the left, the toggle on the right (the row is space-between).
    row.appendChild(main);
    row.appendChild(check);
    return row;
  }

  /**
   * Set a runtime row's status line (text or HTML), styling it as installed/idle/error.
   * @param {!HTMLElement} el The status node.
   * @param {string} text The status content.
   * @param {boolean} ok Whether to style it as installed (accent).
   * @param {boolean=} html Whether `text` is HTML (e.g. the spinner) vs plain text.
   * @param {boolean=} isErr Whether to style it as an error.
   */
  function setNativeStatus(el, text, ok, html, isErr) {
    el.className =
      "sn-native-status" + (ok ? " sn-native-ok" : "") + (isErr ? " sn-err" : "");
    if (html) {
      el.innerHTML = text;
    } else {
      el.textContent = text;
    }
  }

  /** Find a native-runtime row's status node by runtime name. */
  function nativeStatusFor(name) {
    const row = nativeListEl.querySelector('.sn-native-row[data-name="' + name + '"]');
    return row ? row.querySelector(".sn-native-status") : null;
  }

  /**
   * Receive an install progress line (off-thread) and show it on the runtime's status.
   * @param {string} name The runtime name.
   * @param {string} msg The progress line.
   */
  window.__snNativeRuntimeProgress = function (name, msg) {
    const status = nativeStatusFor(name);
    if (status) {
      // The backend message is already a full phrase ("creating runtime environment…",
      // "installing … (this may take a while)") — show it as-is, no "Installing…" prefix.
      setNativeStatus(status, '<span class="sn-spin"></span>' + esc(msg), false, true);
    }
  };

  /**
   * Receive the final install outcome (off-thread): installed ✓ or an error; sync the checkbox.
   * @param {string} name The runtime name.
   * @param {?Object} res {installed} on success, {installed:false, error} on failure.
   */
  window.__snNativeRuntimeDone = function (name, res) {
    const row = nativeListEl.querySelector('.sn-native-row[data-name="' + name + '"]');
    if (!row) {
      return;
    }
    const check = row.querySelector(".sn-native-toggle");
    const status = row.querySelector(".sn-native-status");
    check.disabled = false;
    if (res && res.installed) {
      check.checked = true;
      setNativeStatus(status, "Installed ✓", true);
    } else {
      check.checked = false;
      setNativeStatus(status, "Error: " + ((res && res.error) || "install failed"), false, false, true);
    }
  };

  // --- Account tab -----------------------------------------------------------------
  // The merged models-in-use + usage tables (per kind), the OpenRouter credit line, and the
  // playground all live behind the Account tab. Data is fetched lazily the first time the
  // Account tab is shown so opening Options stays instant.
  let acctModels = {text: [], image: [], sound: []};
  let acctDefaults = {
    text: {provider: "", model: ""},
    image: {provider: "", model: ""},
    sound: {provider: "", model: ""}
  };
  let acctSubtab = "text";
  let acctLoaded = false;
  // The saved global Auto-detect map (lang -> "provider:voice"), cached from account_data. The
  // SOURCE OF TRUTH for generation; the catalog's auto_voice_options only populate the picker.
  let acctAutoVoices = {};
  // Keys subtab: lazily loaded provider credential cards.
  let keysLoaded = false;
  let keysData = [];
  // Each kind sub-tab has its OWN playground (input + last result) — switching tabs must not
  // carry one kind's prompt/result into another. Keyed by sub-tab name (text/image/sound).
  const pgState = {
    text: {input: "", result: null},
    image: {input: "", result: null},
    sound: {input: "", result: null}
  };

  /** Map a generation kind (text/image/tts) to its Account sub-tab name. */
  function kindToSub(kind) {
    return kind === "tts" ? "sound" : kind;
  }

  /**
   * Switch the visible Options tab; lazy-load Account data the first time it's shown.
   * @param {string} name "general" or "account".
   */
  function showTab(name) {
    Array.prototype.forEach.call(document.querySelectorAll(".sn-tab"), function (t) {
      t.classList.toggle("sn-tab-active", t.dataset.tab === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".sn-tabpane"), function (p) {
      p.hidden = p.dataset.pane !== name;
    });
    if (name === "account" && !acctLoaded) {
      acctLoaded = true;
      send("account_data", {}, renderAccount);
      send("account_credit", {}, null);
    }
    if (name === "advanced") {
      loadNativeRuntimes();  // refresh install state each time the Advanced tab opens
    }
  }

  Array.prototype.forEach.call(document.querySelectorAll(".sn-tab"), function (tab) {
    tab.addEventListener("click", function () {
      showTab(tab.dataset.tab);
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll(".sn-subtab"), function (sub) {
    sub.addEventListener("click", function () {
      savePlayground();  // stash the OUTGOING kind's prompt before switching
      acctSubtab = sub.dataset.subtab;
      Array.prototype.forEach.call(document.querySelectorAll(".sn-subtab"), function (s) {
        s.classList.toggle("sn-subtab-active", s.dataset.subtab === acctSubtab);
      });
      showAccountSubtab();
    });
  });

  /** Stash the current playground prompt into the active kind's state (no-op on Keys). */
  function savePlayground() {
    if (pgState[acctSubtab]) {
      pgState[acctSubtab].input = acctInput.value;
    }
  }

  /** Restore the active kind's playground prompt + last result (clears on Keys). */
  function restorePlayground() {
    const st = pgState[acctSubtab];
    acctInput.value = st ? st.input : "";
    acctMsgEl.textContent = "";
    renderPlaygroundResult(st ? st.result : null);
  }

  /**
   * Show the active Account sub-tab: the kind panes (text/image/sound) share a default-model
   * picker + usage table + playground; the Keys sub-tab swaps in the credential cards.
   */
  function showAccountSubtab() {
    const isKeys = acctSubtab === "keys";
    acctKindEl.hidden = isKeys;
    keysEl.hidden = !isKeys;
    if (isKeys) {
      if (!keysLoaded) {
        keysLoaded = true;
        send("account_keys", {}, renderKeys);
      }
      // Refresh the OpenRouter quota bar (off-thread; best-effort).
      send("account_keys_credit", {}, null);
    } else {
      renderAccountPane();
      restorePlayground();
    }
  }

  /**
   * Receive the account_data response: cache the merged models + central defaults and render
   * the active kind pane.
   * @param {?Object} res {models: {text, image, sound}, defaults: {text, image, sound}}.
   */
  function renderAccount(res) {
    acctModels = (res && res.models) || {text: [], image: [], sound: []};
    if (res && res.defaults) {
      acctDefaults = res.defaults;
    }
    if (res && res.auto_voices) {
      acctAutoVoices = res.auto_voices;
    }
    renderAccountPane();
  }

  /** Render a kind sub-tab: the default-model picker, the Auto-detect editor (sound), the usage table. */
  function renderAccountPane() {
    renderDefaultPicker();
    renderAutoVoices();
    renderUsageTable();
  }

  /**
   * Render the global "Auto-detect voices" editor (sound subtab only): one row per language
   * mapping its detected language to a concrete provider·voice. The map is the source of truth
   * for generation; this editor only writes lang -> "provider:voice" via set_auto_voice. A
   * saved value not in the current options is preserved as a trailing "… (saved)" option so a
   * seed/refresh difference (or a value picked elsewhere) never silently drops the mapping.
   */
  function renderAutoVoices() {
    const isSound = acctSubtab === "sound";
    acctAutoVoicesEl.hidden = !isSound;
    acctAutoVoicesEl.innerHTML = "";
    if (!isSound) {
      return;
    }

    const head = document.createElement("div");
    head.className = "sn-acct-default-title";
    head.textContent =
      "Auto-detect voices — a sound field left on “Auto-detect” speaks in the detected " +
      "language using the voice you map here.";
    acctAutoVoicesEl.appendChild(head);

    // The section stays compact: a button to open the per-language map (a popup), and Refresh.
    const row = document.createElement("div");
    row.className = "sn-acct-test-row";

    const configure = document.createElement("button");
    configure.type = "button";
    configure.className = "sn-btn sn-btn-magic";
    configure.textContent = "Choose voices…";
    configure.addEventListener("click", openAutoVoices);
    row.appendChild(configure);

    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "sn-btn sn-acct-refresh-voices";
    refresh.textContent = "↻ Refresh voices";
    refresh.title = "Fetch the full edge_tts voice list to enrich the choices";
    refresh.addEventListener("click", function () {
      refresh.disabled = true;
      refresh.textContent = "↻ Refreshing…";
      send("refresh_voices", {}, null);
    });
    row.appendChild(refresh);

    acctAutoVoicesEl.appendChild(row);
  }

  /** Open the per-language Auto-detect voice-map popup and render its rows. */
  function openAutoVoices() {
    renderAutoVoicesList();
    autovoicesModal.hidden = false;
  }

  /** Render one row per language (label + cross-provider voice select) into the popup list. */
  function renderAutoVoicesList() {
    autovoicesListEl.innerHTML = "";
    (CATALOG.languages || []).forEach(function (lang) {
      if (!lang.code) {
        return;
      }
      autovoicesListEl.appendChild(autoVoiceRow(lang));
    });
  }

  /**
   * Build one language's Auto-detect row: the language label + a select of its cross-provider
   * voice options (with a leading "(not set)" and the saved value preserved).
   * @param {!Object} lang {code, label}.
   * @return {!HTMLElement}
   */
  function autoVoiceRow(lang) {
    const opts = (CATALOG.auto_voice_options && CATALOG.auto_voice_options[lang.code]) || [];
    const options = [{value: "", label: "(not set)"}].concat(opts);
    const wrap = labeledCell(lang.label);
    wrap.label.classList.add("sn-acct-autovoice-row");
    fillCellSelect(
      wrap.cell,
      "sn-acct-autovoice-sel",
      options,
      acctAutoVoices[lang.code] || "",
      function (value) {
        acctAutoVoices[lang.code] = value;
        send("set_auto_voice", {lang: lang.code, value: value}, function (res) {
          if (res && res.auto_voices) {
            acctAutoVoices = res.auto_voices;
          }
        });
      }
    );
    return wrap.label;
  }

  /**
   * Receive the refreshed Auto-detect options (off-thread): merge them into the catalog and
   * re-render the editor. NEVER touches the saved map — purely additive to the picker.
   * @param {?Object} res {auto_voice_options} on success, {error} on failure.
   */
  window.__snVoicesRefreshed = function (res) {
    if (res && res.auto_voice_options) {
      CATALOG.auto_voice_options = res.auto_voice_options;
    }
    if (acctSubtab === "sound") {
      renderAutoVoices();  // re-enables the section's Refresh button
      if (!autovoicesModal.hidden) {
        renderAutoVoicesList();  // refresh the open popup's dropdowns
      }
    }
  };

  /**
   * Render the central default-model picker for the active kind: a provider select + a
   * model/voice select, seeded from the cached defaults. Changing either persists the central
   * [llm]/[tts] default (used for detect-language, Auto-prompt, Improve, and inherited fields).
   */
  function renderDefaultPicker() {
    if (acctSubtab === "keys") {
      return;
    }
    const kind = acctSubtab;
    const def = acctDefaults[kind] || {provider: "", model: ""};
    const isSound = kind === "sound";
    const provOpts = providerNames(isSound ? "tts" : kind).map(function (p) {
      return {value: p, label: p};
    });
    const modelList = isSound
      ? voiceEntries(def.provider).map(function (v) {
          return {value: v.voice, label: v.label};
        })
      : modelValues(kind, def.provider).map(function (m) {
          return {value: m, label: m};
        });
    const modelOpts = isSound
      ? [{value: "", label: "(provider default)"}].concat(modelList)
      : modelList;

    acctDefaultEl.innerHTML = "";
    const title = document.createElement("div");
    title.className = "sn-acct-default-title";
    title.textContent =
      "Default " +
      (isSound ? "voice" : "model") +
      " — used for detect-language, ✨ Auto-prompt, ✦ Improve, and any field left on “(inherit)”.";
    acctDefaultEl.appendChild(title);

    const row = document.createElement("div");
    row.className = "sn-acct-default-row";
    const prov = labeledCell("Provider");
    fillCellSelect(prov.cell, "sn-acct-default-sel", provOpts, def.provider, function (value) {
      applyDefaultModel(kind, value, "");
    });
    const mdl = labeledCell(isSound ? "Voice" : "Model");
    fillCellSelect(mdl.cell, "sn-acct-default-sel", modelOpts, def.model, function (value) {
      applyDefaultModel(kind, def.provider, value);
    });
    row.appendChild(prov.label);
    row.appendChild(mdl.label);
    acctDefaultEl.appendChild(row);
  }

  /**
   * Build a labelled control wrapper: a <label> with a caption span + an inner cell the
   * caller fills (e.g. via fillCellSelect).
   * @param {string} text The caption.
   * @return {{label: !HTMLElement, cell: !HTMLElement}}
   */
  function labeledCell(text) {
    const label = document.createElement("label");
    label.className = "sn-acct-default-field";
    const span = document.createElement("span");
    span.className = "sn-acct-default-label";
    span.textContent = text;
    const cellDiv = document.createElement("div");
    label.appendChild(span);
    label.appendChild(cellDiv);
    return {label: label, cell: cellDiv};
  }

  /**
   * Persist a central default-model change and refresh the picker from the server's response
   * (the server recomputes both text & image since they share one LLM provider).
   * @param {string} kind text | image | sound
   * @param {string} provider The chosen provider.
   * @param {string} model The chosen model/voice ("" = keep the provider's stored model).
   */
  function applyDefaultModel(kind, provider, model) {
    send(
      "set_default_model",
      {kind: kind, provider: provider, model: model},
      function (res) {
        if (res && res.defaults) {
          acctDefaults = res.defaults;
        }
        if (res && res.error) {
          acctMsgEl.className = "sn-msg sn-err";
          acctMsgEl.textContent = res.error;
        }
        renderDefaultPicker();
      }
    );
  }

  /** Render the usage table for the active sub-tab (kind) from the cached models. */
  function renderUsageTable() {
    const rows = acctModels[acctSubtab] || [];
    if (!rows.length) {
      acctUsageEl.innerHTML = '<div class="sn-acct-empty">No models in use for this kind yet.</div>';
      return;
    }
    let html =
      '<table class="sn-acct-table"><thead><tr>' +
      "<th>Provider</th><th>Model</th><th>Calls</th><th>In</th><th>Out</th><th>Last</th>" +
      "</tr></thead><tbody>";
    rows.forEach(function (r) {
      html +=
        "<tr><td>" +
        esc(r.provider) +
        "</td><td>" +
        esc(r.model) +
        '</td><td class="sn-acct-num">' +
        (r.calls || 0) +
        '</td><td class="sn-acct-num">' +
        amount(r.in_tokens, r.in_chars) +
        '</td><td class="sn-acct-num">' +
        amount(r.out_tokens, r.out_chars) +
        "</td><td>" +
        fmtTime(r.last_used_ts) +
        "</td></tr>";
    });
    acctUsageEl.innerHTML = html + "</tbody></table>";
  }

  /**
   * Format a usage amount: exact tokens when the provider reported them, else the rough
   * character count (TTS / google_translate don't return tokens).
   * @param {?number} tokens Exact token count (0/absent → fall back to chars).
   * @param {?number} chars Character count.
   * @return {string}
   */
  function amount(tokens, chars) {
    if (tokens) {
      return tokens + " tok";
    }
    return chars ? "~" + chars + " ch" : "0";
  }

  /**
   * Format a unix timestamp (seconds) for the "Last" column ("—" when never used).
   * @param {?number} ts Epoch seconds, or null.
   * @return {string}
   */
  function fmtTime(ts) {
    if (!ts) {
      return "—";
    }
    return new Date(ts * 1000).toLocaleString();
  }

  /**
   * HTML-escape a string for safe table insertion.
   * @param {*} value The value to escape.
   * @return {string}
   */
  function esc(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  /**
   * Receive the OpenRouter credit (off-thread). Shows remaining/total or hides on error.
   * @param {?Object} res {remaining, total, used} or {error}.
   */
  window.__snCreditResult = function (res) {
    if (res && typeof res.remaining === "number") {
      acctCreditEl.className = "sn-acct-credit sn-acct-credit-ok";
      acctCreditEl.textContent =
        "OpenRouter credit: " +
        res.remaining.toFixed(2) +
        " remaining of " +
        (res.total || 0).toFixed(2) +
        " (used " +
        (res.used || 0).toFixed(2) +
        ").";
      return;
    }
    // No fetchable balance — show the honest per-provider note (not a blank line).
    acctCreditEl.className = "sn-acct-credit";
    acctCreditEl.textContent = (res && (res.note || res.error)) || "";
  };

  // Playground: run the active kind's provider once with the typed input.
  acctRunBtn.addEventListener("click", function () {
    const input = acctInput.value;
    if (!input.trim()) {
      acctMsgEl.className = "sn-msg sn-err";
      acctMsgEl.textContent = "Type something to test first.";
      return;
    }
    savePlayground();  // keep this kind's prompt
    acctRunBtn.disabled = true;
    acctMsgEl.className = "sn-msg";
    acctMsgEl.innerHTML = '<span class="sn-spin"></span>Running…';
    acctOutEl.className = "sn-acct-out";
    acctOutEl.innerHTML = "";
    // The engine dispatches on the generation kind, where sound is "tts".
    const kind = acctSubtab === "sound" ? "tts" : acctSubtab;
    // Test exactly what the picker shows (its first option may not be a saved default yet),
    // not the stored config: read the live provider + model/voice selects.
    const sels = acctDefaultEl.querySelectorAll(".sn-acct-default-sel");
    const provider = sels[0] ? sels[0].value : "";
    const pick = sels[1] ? sels[1].value : "";
    const isSound = acctSubtab === "sound";
    send(
      "account_test",
      {
        kind: kind,
        input: input,
        provider: provider,
        model: isSound ? "" : pick,
        voice: isSound ? pick : "",
      },
      null
    );
  });

  /**
   * Receive an account-test result (off-thread): render it + remember it for its kind's tab.
   * @param {?Object} res {kind, text|image|message} on success, {kind, error} on failure.
   */
  window.__snAccountTestResult = function (res) {
    acctRunBtn.disabled = false;
    acctMsgEl.textContent = "";
    renderPlaygroundResult(res);
    if (res && res.kind) {
      pgState[kindToSub(res.kind)].result = res;
    }
  };

  /**
   * Render a playground result into the output area: text HTML, the generated image, or a
   * sound line with a "Play again" button. Null clears it.
   * @param {?Object} res The result payload (or null to clear).
   */
  function renderPlaygroundResult(res) {
    if (!res) {
      acctOutEl.className = "sn-acct-out";
      acctOutEl.innerHTML = "";
      return;
    }
    if (res.error) {
      acctOutEl.className = "sn-acct-out sn-err";
      acctOutEl.textContent = res.error;
      return;
    }
    acctOutEl.className = "sn-acct-out";
    acctOutEl.innerHTML = "";
    if (res.kind === "text") {
      acctOutEl.innerHTML = res.text || "(empty result)";
    } else if (res.kind === "image") {
      // A generated image can be several MB and would overflow the dialog if rendered inline —
      // show a line + a Preview button that opens the borderless full-screen lightbox.
      acctOutEl.appendChild(imageResultNode(res));
    } else if (res.kind === "tts") {
      const line = document.createElement("div");
      line.textContent = "🔊 " + (res.message || "Audio played.");
      acctOutEl.appendChild(line);
      if (res.can_replay) {
        const replay = document.createElement("button");
        replay.type = "button";
        replay.className = "sn-btn sn-acct-replay";
        replay.textContent = "🔊 Play again";
        replay.addEventListener("click", function () {
          send("replay_audio", {}, null);
        });
        acctOutEl.appendChild(replay);
      }
    }
  }

  // --- Keys subtab -----------------------------------------------------------------
  // Per-provider credential cards: masked key/secret inputs with an eye reveal, an inline
  // Save (or Browse for a file field), an honest quota story (a live bar only for OpenRouter;
  // a note for providers whose quota lives in their console), and a console link.

  /**
   * Render the Keys subtab from the account_keys response.
   * @param {?Object} res {providers: [{id, label, console, credit, note, active, fields}]}.
   */
  function renderKeys(res) {
    keysData = (res && res.providers) || [];
    keysEl.innerHTML = "";
    if (!keysData.length) {
      keysEl.innerHTML = '<div class="sn-acct-empty">No managed providers.</div>';
      return;
    }
    keysData.forEach(function (card) {
      keysEl.appendChild(keyCard(card));
    });
  }

  /**
   * Build one provider's credential card: a header (title + active chip + an inline Save on the
   * right), its credential field rows, then the credit/quota block. ONE Save per card; keeping it
   * in the header row (rather than a separate footer) makes the card compact so several cards fit
   * without pushing the dialog off-screen.
   * @param {!Object} card The provider card payload.
   * @return {!HTMLElement}
   */
  function keyCard(card) {
    const el = document.createElement("div");
    el.className = "sn-key-card";
    el.dataset.provider = card.id;

    const head = document.createElement("div");
    head.className = "sn-key-head";
    const title = document.createElement("div");
    title.className = "sn-key-title";
    title.textContent = card.label;
    head.appendChild(title);
    if (card.active) {
      const chip = document.createElement("span");
      chip.className = "sn-key-chip";
      chip.textContent = "active";
      head.appendChild(chip);
    }

    // Save + status pushed to the far right of the header (one Save per card; a file field
    // already persists on Browse).
    const actions = document.createElement("div");
    actions.className = "sn-key-actions";
    const status = document.createElement("span");
    status.className = "sn-key-status";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "sn-btn sn-btn-primary sn-key-save";
    save.textContent = "Save";
    save.addEventListener("click", function () {
      saveKeyCard(card, el, status);
    });
    actions.appendChild(status);
    actions.appendChild(save);
    head.appendChild(actions);
    el.appendChild(head);

    (card.fields || []).forEach(function (f) {
      el.appendChild(keyField(card.id, f));
    });

    el.appendChild(keyCredit(card));
    return el;
  }

  /**
   * Build one credential field row: a masked/text/file input + an eye reveal (secret) or a
   * Browse button (file). The field's key + kind ride on the input's dataset so the card's
   * single Save can collect them.
   * @param {string} provider The owning provider id.
   * @param {!Object} f {key, label, type, value}.
   * @return {!HTMLElement}
   */
  function keyField(provider, f) {
    const row = document.createElement("div");
    row.className = "sn-key-field";

    const label = document.createElement("div");
    label.className = "sn-key-label";
    label.textContent = f.label;
    row.appendChild(label);

    const controls = document.createElement("div");
    controls.className = "sn-key-controls";

    const input = document.createElement("input");
    input.type = f.type === "secret" ? "password" : "text";
    input.className = "sn-key-input";
    input.value = f.value || "";
    input.dataset.key = f.key;
    input.dataset.ftype = f.type;
    if (f.type === "file") {
      input.readOnly = true;
      input.placeholder = f.placeholder || "No file selected";
    } else if (f.type === "secret") {
      input.placeholder = f.placeholder || "Not set";
    } else if (f.placeholder) {
      input.placeholder = f.placeholder;
    }
    controls.appendChild(input);

    if (f.type === "secret") {
      const eye = document.createElement("button");
      eye.type = "button";
      eye.className = "sn-eye";
      eye.textContent = "👁";
      eye.title = "Show / hide";
      eye.addEventListener("click", function () {
        const reveal = input.type === "password";
        input.type = reveal ? "text" : "password";
        eye.classList.toggle("sn-eye-on", reveal);
      });
      controls.appendChild(eye);
    }

    if (f.type === "file") {
      const browse = document.createElement("button");
      browse.type = "button";
      browse.className = "sn-btn sn-key-browse";
      browse.textContent = "Browse…";
      browse.addEventListener("click", function () {
        send("browse_file", {provider: provider, field: f.key}, function (r) {
          if (r && r.path) {
            input.value = r.path;
          } else if (r && r.error) {
            flashKey(row, r.error, true);
          }
        });
      });
      controls.appendChild(browse);
    }

    row.appendChild(controls);
    return row;
  }

  /**
   * Save a whole provider card's editable fields in one request, then refresh the quota bar
   * (so a freshly-pasted OpenRouter key re-checks credit immediately).
   * @param {!Object} card The provider card payload.
   * @param {!HTMLElement} el The card element.
   * @param {!HTMLElement} status The card's status span.
   */
  function saveKeyCard(card, el, status) {
    const fields = [];
    Array.prototype.forEach.call(el.querySelectorAll(".sn-key-input"), function (inp) {
      fields.push({key: inp.dataset.key, type: inp.dataset.ftype, value: inp.value});
    });
    status.className = "sn-key-status";
    status.textContent = "Saving…";
    send("set_secrets", {provider: card.id, fields: fields}, function (r) {
      if (r && r.ok) {
        status.className = "sn-key-status sn-key-ok";
        status.textContent = "✓ Saved";
        if (card.credit === "live") {
          send("account_keys_credit", {}, null);
        }
      } else {
        status.className = "sn-key-status sn-err";
        status.textContent = (r && r.error) || "Save failed";
      }
    });
  }

  /**
   * Show a transient error in a key field row (used by Browse failures).
   * @param {!HTMLElement} row The field row.
   * @param {string} text The status text.
   * @param {boolean} isErr Whether to render it as an error.
   */
  function flashKey(row, text, isErr) {
    let status = row.querySelector(".sn-key-status");
    if (!status) {
      status = document.createElement("span");
      status.className = "sn-key-status";
      row.querySelector(".sn-key-controls").appendChild(status);
    }
    status.className = "sn-key-status" + (isErr ? " sn-err" : " sn-key-ok");
    status.textContent = text;
  }

  /**
   * Build the credit/quota block for a card: a live bar for OpenRouter, an honest note
   * otherwise, plus a console link.
   * @param {!Object} card The provider card payload.
   * @return {!HTMLElement}
   */
  function keyCredit(card) {
    const box = document.createElement("div");
    box.className = "sn-key-credit";
    box.dataset.provider = card.id;
    if (card.credit === "live") {
      const quota = document.createElement("div");
      quota.className = "sn-quota";
      quota.innerHTML =
        '<div class="sn-quota-bar"><div class="sn-quota-fill"></div></div>' +
        '<div class="sn-quota-text">Checking credit…</div>';
      box.appendChild(quota);
    } else if (card.note) {
      const note = document.createElement("div");
      note.className = "sn-key-note";
      note.textContent = card.note;
      box.appendChild(note);
    }
    if (card.console && card.console.length === 2) {
      const link = document.createElement("button");
      link.type = "button";
      link.className = "sn-key-link";
      link.textContent = "Open " + card.console[0] + " ↗";
      link.addEventListener("click", function () {
        send("open_url", {url: card.console[1]}, null);
      });
      box.appendChild(link);
    }
    return box;
  }

  /**
   * Receive an OpenRouter balance for a Keys card (off-thread): fill the quota bar, or show
   * the error. At zero balance, reveal a red "top up" button.
   * @param {string} provider The provider id the credit is for.
   * @param {?Object} res {remaining, total, used} or {error}.
   */
  window.__snKeysCreditResult = function (provider, res) {
    const box = keysEl.querySelector(
      '.sn-key-credit[data-provider="' + provider + '"]'
    );
    if (!box) {
      return;
    }
    const fill = box.querySelector(".sn-quota-fill");
    const text = box.querySelector(".sn-quota-text");
    if (!fill || !text) {
      return;
    }
    if (res && typeof res.remaining === "number") {
      // total can legitimately be 0 (a key with no purchased credit) — still show the
      // balance rather than the "unavailable" fallback (that was the old bug).
      const total = res.total || 0;
      const pct = total > 0 ? Math.max(0, Math.min(100, (res.remaining / total) * 100)) : 0;
      fill.style.width = pct.toFixed(0) + "%";
      box.classList.toggle("sn-quota-low", total > 0 && pct <= 10);
      text.textContent =
        total > 0
          ? "$" +
            res.remaining.toFixed(2) +
            " left of $" +
            total.toFixed(2) +
            " (" +
            pct.toFixed(0) +
            "%)"
          : "$" + res.remaining.toFixed(2) + " available (no prepaid credit on this key).";
      if (res.remaining <= 0 && total > 0) {
        showReset(box);
      }
    } else {
      fill.style.width = "0%";
      text.textContent = (res && res.error) || "Credit unavailable.";
    }
  };

  /**
   * Reveal the red "out of credit — top up" button on a card (once).
   * @param {!HTMLElement} box The card's credit block.
   */
  function showReset(box) {
    if (box.querySelector(".sn-key-reset")) {
      return;
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sn-key-reset";
    btn.textContent = "Out of credit — top up ↗";
    btn.addEventListener("click", function () {
      send("open_url", {url: "https://openrouter.ai/settings/credits"}, null);
    });
    box.appendChild(btn);
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
      setMsg("Auto-prompt wrote prompts for " + n + " field(s).", false);
    } else {
      setMsg((res && res.error) || "Auto-prompt failed — see logs.", true);
    }
  };
  autoBtn.addEventListener("click", function () {
    autoBtn.disabled = true;
    setMsg('<span class="sn-spin"></span>Auto-prompt is writing prompts…', false);
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
        decks: selectedDeckIds(),
        options: collectOptions()
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
