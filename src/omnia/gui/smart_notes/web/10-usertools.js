
  /**
   * Smart Notes config page — the global Tools tab (a MIDDLE fragment of the page IIFE; it
   * neither opens nor closes it). Where a user AUTHORS a tool: describe a transform, let the
   * LLM write a complete Python `Tool` subclass, read the whole source, run it once on a
   * sample, then save it as a file in `user_files/tools/`.
   *
   * The order is the safety model, not a suggestion. Save stays disabled until a test run has
   * come back for the CURRENT source text, and editing the code clears that again — Python has
   * to be seen working before it is written to disk. The same rule is enforced in the
   * controller (`user_tool_save` refuses an untested source), because a disabled button is a
   * courtesy and the rule is a rule.
   *
   * `loadUserTools` is a hoisted function declaration called from 05-handlers.js's `showTab`,
   * which is concatenated BEFORE this file.
   */

  const utList = document.getElementById("sn-ut-list");
  const utWarnEl = document.getElementById("sn-ut-warn");
  const utWarnText = document.getElementById("sn-ut-warn-text");
  const utWarnOk = document.getElementById("sn-ut-warn-ok");
  const utWarnNo = document.getElementById("sn-ut-warn-no");
  const utBuiltins = document.getElementById("sn-ut-builtins");
  const utDirEl = document.getElementById("sn-ut-dir");
  const utOpenEl = document.getElementById("sn-ut-open");
  const utEditor = document.getElementById("sn-ut-editor");
  const utNewBtn = document.getElementById("sn-ut-new");
  const utLabelEl = document.getElementById("sn-ut-label");
  const utSlugEl = document.getElementById("sn-ut-slug");
  const utPromptEl = document.getElementById("sn-ut-prompt");
  const utGenBtn = document.getElementById("sn-ut-generate");
  const utGenMsg = document.getElementById("sn-ut-genmsg");
  const utSourceEl = document.getElementById("sn-ut-source");
  const utSampleEl = document.getElementById("sn-ut-sample");
  const utRunBtn = document.getElementById("sn-ut-run");
  const utTestMsg = document.getElementById("sn-ut-testmsg");
  const utOutEl = document.getElementById("sn-ut-out");
  const utRisksEl = document.getElementById("sn-ut-risks");
  const utPickEl = document.getElementById("sn-ut-pick");
  const utSampleFileEl = document.getElementById("sn-ut-sample-file");
  const utSaveBtn = document.getElementById("sn-ut-save");
  const utCancelBtn = document.getElementById("sn-ut-cancel");
  const utSaveMsg = document.getElementById("sn-ut-savemsg");

  // The slug being edited ("" = a new tool, so the name box picks it) and the source text the
  // last successful test ran against — Save is armed only while the box still holds THAT text.
  let utSlug = "";
  let utTestedSource = null;

  /** Fetch the Tools tab data and render both lists. */
  function loadUserTools() {
    send("user_tools", {}, renderUserTools);
  }

  // Opening the folder is the point: a path the user has to retype into Finder/Explorer is
  // homework, not a location. Qt does the platform difference on the Python side.
  utPickEl.addEventListener("click", pickSampleFile);
  // Typing over the reference means the staged file is no longer what is being tested; the
  // note stops claiming otherwise. The stage itself is cleared by the next pick or on close.
  utSampleEl.addEventListener("input", function () {
    utSampleFileEl.hidden = true;
  });

  utOpenEl.addEventListener("click", function () {
    send("user_tool_open_dir", {}, function (res) {
      if (res && res.error) {
        setMsg(res.error, true);
      }
    });
  });

  /**
   * Render the user's tools + the built-in cards.
   * @param {?Object} res {tools: [...], builtins: [...], directory: string}.
   */
  function renderUserTools(res) {
    const data = res || {};
    // The SHORT label, with the absolute path on hover. Both come from the backend: the path
    // is derived from the installed package's own location, so it is already right on every
    // platform — but inlining it in this sentence made a runtime value read as a hardcoded
    // macOS literal, and the folder is somewhere different on each OS anyway.
    utDirEl.textContent = data.directory_label || data.directory || "user_files/tools";
    utDirEl.title = data.directory || "";
    utList.innerHTML = "";
    const tools = data.tools || [];
    if (!tools.length) {
      utList.innerHTML =
        '<div class="sn-acct-empty">No tools of your own yet — describe one below.</div>';
    }
    tools.forEach(function (tool) {
      utList.appendChild(userToolCard(tool));
    });
    utBuiltins.innerHTML = "";
    (data.builtins || []).forEach(function (tool) {
      utBuiltins.appendChild(builtinToolCard(tool));
    });
  }

  /**
   * One of the user's tools: what it is, plus Edit / Regenerate / Delete.
   * @param {!Object} tool {slug, name, label, description, prompt, source, error}.
   * @return {!HTMLElement}
   */
  function userToolCard(tool) {
    const card = document.createElement("div");
    card.className = "sn-ut-card" + (tool.error ? " sn-ut-card-broken" : "");
    card.appendChild(toolCardHead(tool));

    const desc = document.createElement("div");
    desc.className = "sn-ut-desc";
    desc.textContent = tool.error
      ? "Could not load: " + tool.error
      : tool.description || "";
    card.appendChild(desc);

    const actions = document.createElement("div");
    actions.className = "sn-ut-actions";
    actions.appendChild(
      utButton("Edit", "Read and change this tool's code", function () {
        openUserTool(tool);
      })
    );
    actions.appendChild(
      utButton("Regenerate", "Write it again from its description", function () {
        openUserTool(tool);
        generateUserTool();
      })
    );
    actions.appendChild(
      utButton("Delete", "Remove this tool from this computer", function () {
        deleteUserTool(tool.slug, false);
      })
    );
    card.appendChild(actions);
    return card;
  }

  /**
   * One built-in tool, read-only.
   * @param {!Object} tool {name, label, description, kinds}.
   * @return {!HTMLElement}
   */
  function builtinToolCard(tool) {
    const card = document.createElement("div");
    card.className = "sn-ut-card";
    card.appendChild(toolCardHead(tool));
    const desc = document.createElement("div");
    desc.className = "sn-ut-desc";
    desc.textContent = tool.description || "";
    card.appendChild(desc);
    return card;
  }

  /**
   * The shared card header: label, the registry name, and the field types it can fill.
   * @param {!Object} tool The catalog entry.
   * @return {!HTMLElement}
   */
  function toolCardHead(tool) {
    const head = document.createElement("div");
    head.className = "sn-ut-head";
    const name = document.createElement("span");
    name.className = "sn-tool-name";
    name.textContent = tool.label || tool.name;
    head.appendChild(name);
    const id = document.createElement("span");
    id.className = "sn-ut-id";
    id.textContent = tool.name;
    head.appendChild(id);
    (tool.kinds || []).forEach(function (kind) {
      const chip = document.createElement("span");
      chip.className = "sn-tool-chip";
      chip.textContent = TYPE_LABELS[kind] || kind;
      head.appendChild(chip);
    });
    return head;
  }

  /**
   * Build one small action button.
   * @param {string} text The label.
   * @param {string} title The tooltip.
   * @param {function()} onClick The action.
   * @return {!HTMLButtonElement}
   */
  function utButton(text, title, onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sn-btn sn-ut-btn";
    btn.textContent = text;
    btn.title = title;
    btn.addEventListener("click", onClick);
    return btn;
  }

  /**
   * Open the editor on an existing tool (or, with no argument, on a blank new one).
   * @param {Object=} tool The tool to edit.
   */
  function openUserTool(tool) {
    const data = tool || {};
    utSlug = data.slug || "";
    utLabelEl.value = data.label || "";
    utLabelEl.disabled = !!utSlug;  // the slug IS the file name; renaming is a new tool
    utPromptEl.value = data.prompt || "";
    utSourceEl.value = data.source || "";
    utSampleEl.value = "";
    utOutEl.hidden = true;
    showToolRisks([]);  // stale banner must not outlive its code
    utSampleFileEl.hidden = true;
    utOutEl.textContent = "";
    utGenMsg.textContent = "";
    utTestMsg.textContent = "";
    utSaveMsg.textContent = "";
    utTestedSource = null;
    refreshUserToolSlug();
    refreshSaveState();
    utEditor.hidden = false;
    utEditor.scrollIntoView({block: "nearest"});
  }

  /** Show the slug the tool will be saved under (its file name and its name in a chain). */
  function refreshUserToolSlug() {
    const slug = utSlug || slugifyName(utLabelEl.value);
    utSlugEl.textContent = slug ? "user:" + slug : "";
  }

  /**
   * Mirror of the backend slug rule — display only; the controller validates for real.
   * @param {string} text The typed name.
   * @return {string}
   */
  function slugifyName(text) {
    return String(text || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40)
      .replace(/^-+|-+$/g, "");
  }

  /** Enable Save only while the box holds exactly the source a test run came back for. */
  function refreshSaveState() {
    const tested = utTestedSource !== null && utTestedSource === utSourceEl.value;
    utSaveBtn.disabled = !tested;
    utSaveBtn.title = tested
      ? "Save this tool into user_files/tools on this computer"
      : "Run the tool on a sample first — it is saved only after you have seen it work.";
  }

  /** Ask the backend to write the tool's code (off-thread; pushed back). */
  function generateUserTool() {
    const payload = {
      slug: utSlug,
      label: utLabelEl.value,
      prompt: utPromptEl.value,
      all_fields: fieldNames()
    };
    if (!payload.prompt.trim()) {
      utGenMsg.textContent = "Describe what the tool should do first.";
      return;
    }
    utGenBtn.disabled = true;
    utGenMsg.textContent = "Writing…";
    send("user_tool_generate", payload, function (res) {
      if (res && res.error) {
        utGenBtn.disabled = false;
        utGenMsg.textContent = res.error;
      }
    });
  }

  /**
   * Receive a generated source (or the failure) — the page hook the controller pushes to.
   * @param {string} slug The tool the reply is for.
   * @param {!Object} res {source} or {error}.
   */
  window.__snUserToolSource = function (slug, res) {
    utGenBtn.disabled = false;
    if (res && res.error) {
      utGenMsg.textContent = res.error;
      return;
    }
    utGenMsg.textContent = "Written — read it, then run it on a sample.";
    utSourceEl.value = (res && res.source) || "";
    // Before Run, not after it: pressing Run EXECUTES this code, and the review gate requires
    // pressing it, so a banner that only appears in the result describes damage already done.
    showToolRisks((res && res.risks) || []);
    if (!utSlug) {
      utSlug = slug;  // the name is fixed once code has been written for it
      utLabelEl.disabled = true;
      refreshUserToolSlug();
    }
    utTestedSource = null;
    refreshSaveState();
  };

  /** Run the current source once on the sample value (off-thread; pushed back). */
  function runUserToolTest() {
    if (!utSourceEl.value.trim()) {
      utTestMsg.textContent = "There is no code to run yet.";
      return;
    }
    utRunBtn.disabled = true;
    utTestMsg.textContent = "Running…";
    utOutEl.hidden = true;
    // The banner is NOT cleared here. It describes the code about to run, and clearing it at
    // the moment of execution is exactly backwards.
    send(
      "user_tool_test",
      {
        slug: utSlug,
        label: utLabelEl.value,
        source: utSourceEl.value,
        sample: utSampleEl.value,
        params: {}
      },
      function (res) {
        if (res && res.error) {
          utRunBtn.disabled = false;
          utTestMsg.textContent = res.error;
        }
      }
    );
  }

  /**
   * Receive a test-run outcome — the page hook the controller pushes to.
   * @param {string} slug The tool the reply is for.
   * @param {!Object} res {ok, status, output, detail} or {error}.
   */
  window.__snUserToolTested = function (slug, res) {
    utRunBtn.disabled = false;
    const result = res || {};
    if (result.error) {
      utTestMsg.textContent = result.error;
      utOutEl.hidden = true;
      // The banner describes code that did not run; leaving it up attributes the previous
      // tool's reach to this one.
      showToolRisks([]);
      return;
    }
    // The tool RAN: the user has now seen what it does, which is what Save waits for — even
    // when what it does is decline or fail.
    utTestedSource = utSourceEl.value;
    // A tool that reads media declines until a file is staged, and its own message will say
    // something like "no collection" — true from inside the tool, and unhelpful here, where
    // the actual fix is one button away. Point at it rather than leaving the user to guess.
    const needsFile = !result.ok && utSampleFileEl.hidden;
    utTestMsg.textContent = result.ok
      ? "It produced a result."
      : "It ran, but produced nothing (" + (result.status || "") + ")." +
        (needsFile ? " If it reads a file, pick one with “Choose file…”." : "");
    utOutEl.textContent = result.ok ? result.output : result.detail || "(no detail)";
    utOutEl.hidden = false;
    showToolRisks(result.risks || []);
    refreshSaveState();
  };

  /**
   * Choose a file for the sample, and put its Anki reference in the box.
   *
   * The chosen file is staged OUTSIDE the collection and the test's media folder points at the
   * stage, so testing never adds to synced media (see MediaSampleStage). Picking again
   * replaces the previous file rather than accumulating copies of everything browsed through.
   */
  function pickSampleFile() {
    utTestMsg.textContent = "";
    send("user_tool_pick_sample", {}, function (res) {
      const result = res || {};
      if (result.error) {
        utTestMsg.textContent = result.error;
        return;
      }
      if (!result.reference) {
        return;  // cancelled — leave whatever was typed alone
      }
      utSampleEl.value = result.reference;
      utSampleFileEl.textContent =
        "Testing against " + result.name + " — staged outside your collection.";
      utSampleFileEl.hidden = false;
    });
  }

  /**
   * Say what this tool reaches for, ABOVE the code, before it is approved.
   *
   * A user tool may import `os`, `subprocess` and the filesystem, so the import allowlist is
   * not the boundary — this review is. That only means something if the reader knows what to
   * look for, and finding a `subprocess` import on line 3 of forty lines of generated Python,
   * read once, is not a fair ask. A tool that only reshapes text says nothing at all, so the
   * banner appearing IS the signal.
   * @param {!Array<string>} risks Plain-language descriptions from the backend.
   */
  function showToolRisks(risks) {
    if (!risks.length) {
      utRisksEl.hidden = true;
      utRisksEl.textContent = "";
      return;
    }
    utRisksEl.textContent =
      "This tool " + risks.join(", ") + ". Read it before you save it.";
    utRisksEl.hidden = false;
  }

  /** Persist the reviewed + tested source as a file on this computer. */
  function saveUserTool() {
    utSaveMsg.textContent = "Saving…";
    send(
      "user_tool_save",
      {
        slug: utSlug,
        label: utLabelEl.value,
        prompt: utPromptEl.value,
        source: utSourceEl.value
      },
      function (res) {
        const result = res || {};
        if (result.error) {
          utSaveMsg.textContent = result.error;
          return;
        }
        utSaveMsg.textContent =
          "Saved as " + result.name + " — pick it from any field's Tools button.";
        utSlug = result.slug;
        loadUserTools();
      }
    );
  }

  /**
   * Delete a tool, asking first when fields still reference it.
   *
   * The confirmation is a two-step round trip rather than a browser dialog: the first call
   * answers "which fields use this?", and only a `confirm` call removes the file.
   * @param {string} slug The tool's slug.
   * @param {boolean} confirmed Whether the user already accepted the usage warning.
   */
  function deleteUserTool(slug, confirmed) {
    send("user_tool_delete", {slug: slug, confirm: confirmed}, function (res) {
      const result = res || {};
      if (result.error) {
        setMsg(result.error, true);
        return;
      }
      if (!result.ok && result.usages) {
        showDeleteWarning(slug, result.usages);
        return;
      }
      utWarnEl.hidden = true;
      if (utSlug === slug) {
        utEditor.hidden = true;
        utSlug = "";
      }
      loadUserTools();
    });
  }

  /**
   * Show which fields use a tool and let the user go ahead (or keep it).
   * @param {string} slug The tool's slug.
   * @param {!Array<string>} usages "NoteType · Field" strings.
   */
  function showDeleteWarning(slug, usages) {
    utWarnText.textContent =
      "“user:" +
      slug +
      "” is used by " +
      usages.length +
      " field(s): " +
      usages.join(", ") +
      ". Deleting it leaves those fields pointing at a tool this computer no longer has — " +
      "they skip it and try the next tool in their chain.";
    utWarnOk.onclick = function () {
      utWarnEl.hidden = true;
      deleteUserTool(slug, true);
    };
    utWarnEl.hidden = false;
  }

  utNewBtn.addEventListener("click", function () {
    openUserTool(null);
  });
  utWarnNo.addEventListener("click", function () {
    utWarnEl.hidden = true;
  });
  utLabelEl.addEventListener("input", refreshUserToolSlug);
  utGenBtn.addEventListener("click", generateUserTool);
  utSourceEl.addEventListener("input", refreshSaveState);
  utRunBtn.addEventListener("click", runUserToolTest);
  utSaveBtn.addEventListener("click", saveUserTool);
  utCancelBtn.addEventListener("click", function () {
    utEditor.hidden = true;
  });
