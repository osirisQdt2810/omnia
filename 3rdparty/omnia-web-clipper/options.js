/*
 * Omnia Web Clipper - options page logic.
 *
 * Loads settings into the form, saves them to chrome.storage.sync, and runs a
 * "Test connection" that calls AnkiConnect `version` + `modelNames` and, for the
 * configured note type, `modelFieldNames` so the user can map fields correctly.
 */

(() => {
  "use strict";

  const { loadSettings, saveSettings, ankiConnect } = self.OmniaClipper;

  const el = (id) => document.getElementById(id);
  const statusEl = el("status");
  const testOutput = el("testOutput");

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = kind || "";
  }

  function writeTestOutput(text) {
    testOutput.style.display = "block";
    testOutput.textContent = text;
  }

  /** Populate the form from stored settings. */
  async function restore() {
    const s = await loadSettings();
    el("ankiConnectUrl").value = s.ankiConnectUrl;
    el("apiKey").value = s.apiKey;
    el("deckName").value = s.deckName;
    el("modelName").value = s.modelName;
    el("allowDuplicate").checked = !!s.allowDuplicate;
    el("tags").value = (s.tags || []).join(", ");
    el("f_selection").value = s.fieldMap.selection || "";
    el("f_sentence").value = s.fieldMap.sentence || "";
    el("f_context").value = s.fieldMap.context || "";
    el("f_url").value = s.fieldMap.url || "";
    el("f_pageTitle").value = s.fieldMap.pageTitle || "";
  }

  /** Read the form into a settings object. */
  function collect() {
    const tags = el("tags")
      .value.split(",")
      .map((t) => t.trim())
      .filter((t) => t.length > 0);
    return {
      ankiConnectUrl: el("ankiConnectUrl").value.trim() || "http://127.0.0.1:8765",
      apiKey: el("apiKey").value,
      deckName: el("deckName").value.trim() || "Omnia Capture",
      modelName: el("modelName").value.trim() || "Basic",
      allowDuplicate: el("allowDuplicate").checked,
      tags: tags,
      fieldMap: {
        selection: el("f_selection").value.trim(),
        sentence: el("f_sentence").value.trim(),
        context: el("f_context").value.trim(),
        url: el("f_url").value.trim(),
        pageTitle: el("f_pageTitle").value.trim(),
      },
    };
  }

  async function onSave() {
    await saveSettings(collect());
    setStatus("Saved.", "ok");
    setTimeout(() => setStatus("", ""), 2000);
  }

  async function onTest() {
    setStatus("Testing…", "");
    testOutput.style.display = "none";
    const s = collect();
    // Persist first so the test reflects exactly what will be used at capture time.
    await saveSettings(s);

    try {
      const version = await ankiConnect(s.ankiConnectUrl, "version", {}, s.apiKey);
      const models = await ankiConnect(s.ankiConnectUrl, "modelNames", {}, s.apiKey);

      const lines = [];
      lines.push("AnkiConnect reachable. API version: " + version);
      lines.push("");
      const hasModel = Array.isArray(models) && models.includes(s.modelName);
      lines.push('Note type "' + s.modelName + '": ' + (hasModel ? "found" : "NOT FOUND"));

      if (hasModel) {
        const fieldNames = await ankiConnect(
          s.ankiConnectUrl,
          "modelFieldNames",
          { modelName: s.modelName },
          s.apiKey
        );
        lines.push("Fields on this note type:");
        lines.push("  " + (fieldNames || []).join(", "));
        lines.push("");
        lines.push("Map your capture values to these exact field names above.");
      } else {
        lines.push("");
        lines.push("Available note types:");
        lines.push("  " + (models || []).join(", "));
      }

      writeTestOutput(lines.join("\n"));
      setStatus("Connected.", "ok");
    } catch (err) {
      writeTestOutput("Test failed:\n" + (err && err.message ? err.message : String(err)));
      setStatus("Connection failed.", "err");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    restore();
    el("saveBtn").addEventListener("click", onSave);
    el("testBtn").addEventListener("click", onTest);
  });
})();
