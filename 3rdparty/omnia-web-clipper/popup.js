/*
 * Omnia Web Clipper - popup logic.
 *
 * Shows whether AnkiConnect is reachable (a quick `version` call) and links to
 * the options page.
 */

(() => {
  "use strict";

  const { loadSettings, ankiConnect } = self.OmniaClipper;

  const dot = document.getElementById("dot");
  const statusText = document.getElementById("statusText");
  const detail = document.getElementById("detail");

  async function checkStatus() {
    const s = await loadSettings();
    detail.textContent = "Deck: " + s.deckName + "  •  Note type: " + s.modelName;
    try {
      const version = await ankiConnect(s.ankiConnectUrl, "version", {}, s.apiKey);
      dot.className = "dot ok";
      statusText.textContent = "AnkiConnect reachable (v" + version + ")";
    } catch (err) {
      dot.className = "dot err";
      statusText.textContent = "AnkiConnect not reachable";
      detail.textContent = err && err.message ? err.message : String(err);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("optionsBtn").addEventListener("click", () => {
      if (chrome.runtime.openOptionsPage) {
        chrome.runtime.openOptionsPage();
      } else {
        window.open(chrome.runtime.getURL("options.html"));
      }
    });
    checkStatus();
  });
})();
