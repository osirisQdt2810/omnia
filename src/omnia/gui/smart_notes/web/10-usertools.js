
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
  const utInputsEl = document.getElementById("sn-ut-inputs");
  const utRunBtn = document.getElementById("sn-ut-run");
  const utTestMsg = document.getElementById("sn-ut-testmsg");
  const utOutEl = document.getElementById("sn-ut-out");
  const utOutMediaEl = document.getElementById("sn-ut-outmedia");
  const utRisksEl = document.getElementById("sn-ut-risks");
  const utVideoEl = document.getElementById("sn-ut-video");
  const utVideoName = document.getElementById("sn-ut-video-name");
  const utVideoNote = document.getElementById("sn-ut-video-note");
  const utVideoPlay = document.getElementById("sn-ut-video-play");
  const utVideoClose = document.getElementById("sn-ut-video-close");
  const utSaveBtn = document.getElementById("sn-ut-save");
  const utCancelBtn = document.getElementById("sn-ut-cancel");
  const utSaveMsg = document.getElementById("sn-ut-savemsg");

  // The slug being edited ("" = a new tool, so the name box picks it) and the source text the
  // last successful test ran against — Save is armed only while the box still holds THAT text.
  let utSlug = "";
  let utTestedSource = null;

  // The Try-it form, one entry per input the draft declares:
  // {field, kind, value, name, valueEl, fileEl} — `value` is what the run posts (a typed string
  // or an Anki reference) and `name` the staged file behind it, if any. Both are carried so the
  // row can state one in terms of the other however it came to be rendered.
  // Rows are addressed through THIS array and never by
  // a built-up element id — a dynamic getElementById would escape the page's static-id rule,
  // which is what keeps a null lookup (and the dead dialog that follows it) test-visible.
  let utInputs = [];

  /** Fetch the Tools tab data and render both lists. */
  function loadUserTools() {
    send("user_tools", {}, renderUserTools);
  }

  // Opening the folder is the point: a path the user has to retype into Finder/Explorer is
  // homework, not a location. Qt does the platform difference on the Python side.
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
    utOutEl.hidden = true;
    utOutMediaEl.hidden = true;
    utOutMediaEl.innerHTML = "";
    // The tool's OWN reach, not a blank. Opening an existing tool ends in Run, which executes
    // it — so an empty banner over `import subprocess` would tell the reader the opposite of
    // the truth at the one moment it matters.
    showToolRisks(data.risks || []);
    // A tool being edited already declares its inputs; a blank new one has nothing to declare
    // yet, so it gets no form until code has been written for it.
    utInputs = [];
    if (utSourceEl.value.trim()) {
      refreshInputsFromEditor();
    } else {
      renderToolInputs([]);
    }
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
  /**
   * Re-read the draft: what it reaches for, and what it asks the tester for.
   *
   * ONE timer, two sends. Both answers are cheap AST reads of the SAME text, and firing them
   * together is what keeps the banner and the input form describing one draft rather than two
   * keystrokes apart. Debounced because either one is a round-trip per keystroke otherwise,
   * and both only have to be right by the time the reader looks up — well before Run.
   */
  let utDraftTimer = null;
  function refreshDraftFromEditor() {
    if (utDraftTimer) {
      clearTimeout(utDraftTimer);
    }
    utDraftTimer = setTimeout(function () {
      refreshRisksFromEditor();
      refreshInputsFromEditor();
    }, 300);
  }

  /** Ask the backend what the CURRENT editor contents reach for. */
  function refreshRisksFromEditor() {
    send("user_tool_risks", {source: utSourceEl.value}, function (res) {
      showToolRisks((res && res.risks) || []);
    });
  }

  /**
   * Ask the backend which inputs the CURRENT editor contents declare, and rebuild the form.
   *
   * A separate op from `user_tool_risks` on purpose — one responsibility per op name — and, like
   * it, the backend answers by READING the source, never by running it.
   */
  function refreshInputsFromEditor() {
    send("user_tool_inputs", {source: utSourceEl.value}, function (res) {
      const inputs = (res && res.inputs) || [];
      // Only when the form would actually come out different. This fires 300ms after a
      // keystroke in the SOURCE box, and most edits do not touch `input_kinds` at all — so
      // rebuilding unconditionally replaced the rows under a user who had since clicked into
      // one, taking their caret with it. The values survive a rebuild; the caret does not.
      if (!sameToolInputs(inputs)) {
        renderToolInputs(inputs);
      }
    });
  }

  /**
   * Whether the rendered form already IS the form these inputs describe.
   * @param {!Array<!Object>} inputs [{field, kind}] from the backend.
   * @return {boolean}
   */
  function sameToolInputs(inputs) {
    return (
      inputs.length === utInputs.length &&
      inputs.every(function (spec, index) {
        return (
          utInputs[index].field === String((spec && spec.field) || "") &&
          utInputs[index].kind === String((spec && spec.kind) || "text")
        );
      })
    );
  }

  /**
   * Rebuild the Try-it form: one control per input, labelled with that input's field name.
   *
   * Values already held for a field of the same name and kind SURVIVE the rebuild. The form is
   * re-fetched on every debounced source edit, so wiping what the user typed (or the file they
   * picked) on each keystroke would make the panel unusable.
   * @param {!Array<!Object>} inputs [{field, kind}] from the backend.
   */
  function renderToolInputs(inputs) {
    const held = {};
    utInputs.forEach(function (entry) {
      held[entry.kind + ":" + entry.field] = {value: entry.value, name: entry.name};
    });
    utInputsEl.innerHTML = "";
    utInputs = [];
    (inputs || []).forEach(function (spec) {
      const field = String((spec && spec.field) || "");
      if (!field) {
        return;
      }
      const kind = String((spec && spec.kind) || "text");
      const kept = held[kind + ":" + field] || {};
      const entry = {
        field: field,
        kind: kind,
        value: kept.value || "",
        // The staged file's own NAME, carried beside the reference rather than re-derived. The
        // pick used to write the name into the row and the rebuild the raw reference, so one
        // staged file read two different ways depending on whether a debounced rebuild had
        // happened since.
        name: kept.name || "",
        valueEl: null,
        fileEl: null
      };
      utInputsEl.appendChild(toolInputRow(entry));
      utInputs.push(entry);
    });
  }

  /**
   * Build one input row: a label, then a box to type in or a button that opens a file browser.
   *
   * EVERY row can take a file, including a text one. The tool's declaration decides the
   * PRIMARY control and the picker's filter — but a draft whose `input_kinds` could not be read
   * (absent, computed, or written before the declaration existed) renders as one text row, and
   * without an attach on that row such a tool has no way at all to be handed a file. The
   * affordance is per-input and derived, which a standalone Choose-file button beside Run —
   * there whatever the tool reads — is not.
   * @param {!Object} entry The form entry the row reads and writes.
   * @return {!HTMLElement}
   */
  function toolInputRow(entry) {
    const row = document.createElement("div");
    row.className = "sn-ut-input-row";
    row.setAttribute("data-kind", entry.kind);
    const name = document.createElement("span");
    name.className = "sn-ut-input-name";
    name.textContent = entry.field;
    row.appendChild(name);
    if (entry.kind === "text") {
      const box = document.createElement("textarea");
      box.className = "sn-ut-input-text";
      box.rows = 2;
      box.value = entry.value;
      box.addEventListener("input", function () {
        entry.value = box.value;
        // Typed over: whatever was staged is no longer what this row will be read as, so the
        // caption must stop claiming it.
        entry.name = "";
        showInputFile(entry);
      });
      entry.valueEl = box;
      row.appendChild(box);
      // Unfiltered: a draft that never said what this field holds cannot have a filter derived
      // from it, and a filter that hides the right file is worse than no filter at all.
      row.appendChild(
        pickRowButton(entry, "file", "📎", "Attach a file to " + entry.field)
      );
    } else {
      // A media input is PICKED, not typed: typing a reference by hand only resolves for a file
      // already in the collection, which is exactly what testing a new conversion does not have.
      row.appendChild(
        pickRowButton(
          entry,
          entry.kind,
          "📎 Choose " + entry.kind + "…",
          "Browse for the " + entry.kind + " file " + entry.field + " holds"
        )
      );
    }
    const file = document.createElement("span");
    file.className = "sn-ut-input-file";
    entry.fileEl = file;
    row.appendChild(file);
    showInputFile(entry);
    return row;
  }

  /**
   * The button that opens a file browser FOR ONE INPUT.
   * @param {!Object} entry The form entry the pick fills in.
   * @param {string} kind The kind the picker filters to ("file" = no filter).
   * @param {string} text The label.
   * @param {string} title The tooltip.
   * @return {!HTMLButtonElement}
   */
  function pickRowButton(entry, kind, text, title) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sn-btn sn-ut-input-pick";
    btn.textContent = text;
    btn.title = title;
    btn.addEventListener("click", function () {
      pickInputFile(entry.field, kind);
    });
    return btn;
  }

  /**
   * State what this input will be read as: its Anki reference, and the staged file behind it.
   *
   * ONE renderer for both paths — the pick and the debounced rebuild. They disagreed before:
   * the pick wrote the file's name and the rebuild the raw `[sound:…]`, so the same staged file
   * read two different ways and the requirement's "its Anki reference form is shown" held only
   * by accident. Empty for a row with nothing staged — a typed value is already visible in its
   * own box.
   * @param {!Object} entry The form entry to describe.
   */
  function showInputFile(entry) {
    if (!entry.fileEl) {
      return;
    }
    entry.fileEl.textContent = entry.name
      ? entry.value + " — " + entry.name + ", staged outside your collection."
      : "";
  }

  /**
   * The form's values as the note fields a test run reads.
   * @return {!Object<string, string>}
   */
  function collectToolInputs() {
    const values = {};
    utInputs.forEach(function (entry) {
      values[entry.field] = entry.value;
    });
    return values;
  }

  /**
   * Find a form entry by field name.
   * @param {string} field The input's field name.
   * @return {?Object}
   */
  function findToolInput(field) {
    let found = null;
    utInputs.forEach(function (entry) {
      if (entry.field === field) {
        found = entry;
      }
    });
    return found;
  }

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
    // The form follows the code that just landed — the tool is what says which inputs it takes.
    refreshInputsFromEditor();
    if (!utSlug) {
      utSlug = slug;  // the name is fixed once code has been written for it
      utLabelEl.disabled = true;
      refreshUserToolSlug();
    }
    utTestedSource = null;
    refreshSaveState();
  };

  /** Run the current source once on the form's values (off-thread; pushed back). */
  function runUserToolTest() {
    if (!utSourceEl.value.trim()) {
      utTestMsg.textContent = "There is no code to run yet.";
      return;
    }
    utRunBtn.disabled = true;
    utTestMsg.textContent = "Running…";
    utOutEl.hidden = true;
    utOutMediaEl.hidden = true;
    // Deliberately NO precondition on the form: a tool that declines because nothing was picked
    // still counts as tested — the user watched it decline, which is what the gate waits for.
    // The banner is NOT cleared here either. It describes the code about to run, and clearing
    // it at the moment of execution is exactly backwards.
    send(
      "user_tool_test",
      {
        slug: utSlug,
        label: utLabelEl.value,
        source: utSourceEl.value,
        inputs: collectToolInputs(),
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
   * @param {!Object} res {ok, status, output, detail, media} or {error}.
   */
  window.__snUserToolTested = function (slug, res) {
    utRunBtn.disabled = false;
    const result = res || {};
    if (result.error) {
      utTestMsg.textContent = result.error;
      utOutEl.hidden = true;
      utOutMediaEl.hidden = true;
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
    // the actual fix is one button away. Point at the first input that is still empty, whatever
    // its kind: a tool whose declaration could not be read gets ONE text row, and "it produced
    // nothing" with no further hint is exactly the dead end that row must not be.
    const empty = utInputs.filter(function (entry) {
      return !entry.value;
    });
    const hint = !result.ok && empty.length
      ? empty[0].kind === "text"
        ? " Give " + empty[0].field + " a value first — type one, or 📎 attach a file."
        : " Pick a file for " + empty[0].field + " first."
      : "";
    utTestMsg.textContent = result.ok
      ? "It produced a result."
      : "It ran, but produced nothing (" + (result.status || "") + ")." + hint;
    if (result.ok && result.media) {
      utOutEl.hidden = true;
      utOutEl.textContent = "";
      renderToolOutput(result.media);
    } else {
      utOutMediaEl.hidden = true;
      utOutMediaEl.innerHTML = "";
      utOutEl.textContent = result.ok ? result.output : result.detail || "(no detail)";
      utOutEl.hidden = false;
    }
    showToolRisks(result.risks || []);
    refreshSaveState();
  };

  /**
   * Render a produced FILE as one: its name and size, plus a way to see or hear it.
   *
   * A picture is inlined and opened in the existing lightbox. Sound and video never cross into
   * the page — this webview ships without the proprietary codecs, so they go to Anki's own
   * player instead, which is also what plays them on a card.
   * @param {!Object} media {kind, name, ext, size, playable, image, note}.
   */
  function renderToolOutput(media) {
    const icons = {image: "🖼️", audio: "🔊", video: "🎬"};
    utOutMediaEl.innerHTML = "";
    const line = document.createElement("div");
    line.className = "sn-ut-out-name";
    line.textContent =
      (icons[media.kind] || "📄") + " " + (media.name || "output") +
      " — " + (media.size || 0) + " bytes";
    utOutMediaEl.appendChild(line);
    if (media.kind === "image" && media.image) {
      utOutMediaEl.appendChild(utOutButton("🔍 View", function () {
        openLightbox(media.image);
      }));
    } else if (media.kind === "audio") {
      // Wrapped rather than passed straight through: the click Event would arrive as
      // playToolOutput's error sink and be called as a function on the first failure.
      utOutMediaEl.appendChild(utOutButton("🔊 Play", function () {
        playToolOutput();
      }));
    } else if (media.kind === "video") {
      utOutMediaEl.appendChild(utOutButton("🎬 Open", function () {
        openVideoPopup(media);
      }));
    } else if (media.note) {
      // The picture was too big to inline (or the file is something with no viewer here): the
      // name, the size and the reason are all present, so this is never a blank box.
      const note = document.createElement("div");
      note.className = "sn-ut-out-note";
      note.textContent = media.note;
      utOutMediaEl.appendChild(note);
    }
    utOutMediaEl.hidden = false;
  }

  /**
   * One button in the produced-file row.
   * @param {string} text The label.
   * @param {function()} onClick The action.
   * @return {!HTMLButtonElement}
   */
  function utOutButton(text, onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sn-btn";
    btn.textContent = text;
    btn.addEventListener("click", onClick);
    return btn;
  }

  /**
   * Hand the last produced file to Anki's player (the backend holds the bytes).
   * @param {function(string)=} onError Where to say it did not play. Defaults to the test
   *     message under the Run button — which the video popup's full-screen scrim COVERS, so
   *     that caller passes its own sink instead of the failure being invisible behind it.
   */
  function playToolOutput(onError) {
    send("user_tool_play_output", {}, function (res) {
      if (res && res.error) {
        if (onError) {
          onError(res.error);
        } else {
          utTestMsg.textContent = res.error;
        }
      }
    });
  }

  /**
   * Show a produced video and start it playing.
   *
   * There is no <video> element here on purpose: this webview cannot decode mp4/H.264 however
   * the bytes are delivered, so the popup names the file and starts Anki's own player — the one
   * that plays a card's video.
   *
   * A failure is reported INTO the popup. The default sink is the message under the Run button,
   * which this popup's full-screen scrim covers: "nothing happened and no reason anywhere" was
   * the exact shape of a play that failed here.
   * @param {!Object} media The media block from the test result.
   */
  function openVideoPopup(media) {
    utVideoName.textContent =
      (media.name || "video") + " — " + (media.size || 0) + " bytes";
    utVideoNote.textContent =
      "Playing through Anki's own player: this settings window cannot decode most video " +
      "formats, so the file is handed to the same player a card's video uses.";
    utVideoEl.hidden = false;
    playToolOutput(function (error) {
      utVideoNote.textContent = error;
    });
  }

  function closeVideoPopup() {
    utVideoEl.hidden = true;
  }

  /**
   * Choose a file for ONE input, and hold its Anki reference as that input's value.
   *
   * The chosen file is staged OUTSIDE the collection and the test's media folder points at the
   * stage, so testing never adds to synced media (see MediaSampleStage). Picking again for the
   * same input replaces its file; another input's stays staged.
   * @param {string} field The input's field name.
   * @param {string} kind The input's kind, which the picker filters on.
   */
  function pickInputFile(field, kind) {
    utTestMsg.textContent = "";
    send("user_tool_pick_sample", {field: field, kind: kind}, function (res) {
      const result = res || {};
      if (result.error) {
        utTestMsg.textContent = result.error;
        return;
      }
      if (!result.reference) {
        return;  // cancelled — leave the previous pick alone
      }
      // Looked up again rather than captured: the picker is modal and slow, and a debounced
      // re-render while it was open would have replaced the node this closure was holding.
      const entry = findToolInput(field);
      if (!entry) {
        return;
      }
      entry.value = result.reference;
      entry.name = result.name || "";
      if (entry.valueEl) {
        // A text row that was handed a file shows the reference it will be READ as, so the box
        // and the value the run posts can never say different things.
        entry.valueEl.value = entry.value;
      }
      showInputFile(entry);
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
  utSourceEl.addEventListener("input", function () {
    refreshSaveState();
    // Pasting a different tool over this one changes what will run AND what it reads; the
    // banner and the form both follow the text in the box rather than whatever arrived with it.
    refreshDraftFromEditor();
  });
  utRunBtn.addEventListener("click", runUserToolTest);
  // Wrapped: the click Event would otherwise arrive as playToolOutput's error sink, and the
  // popup's own note is where a failure has to appear anyway — its scrim covers the other one.
  utVideoPlay.addEventListener("click", function () {
    playToolOutput(function (error) {
      utVideoNote.textContent = error;
    });
  });
  utVideoClose.addEventListener("click", closeVideoPopup);
  // Backdrop only — a click inside the box must not close the popup out from under the button
  // the user is aiming at. Esc mirrors the lightbox, which is the other overlay in this page.
  utVideoEl.addEventListener("click", function (e) {
    if (e.target === utVideoEl) {
      closeVideoPopup();
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !utVideoEl.hidden) {
      closeVideoPopup();
    }
  });
  utSaveBtn.addEventListener("click", saveUserTool);
  utCancelBtn.addEventListener("click", function () {
    utEditor.hidden = true;
  });
