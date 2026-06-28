/*
 * Omnia Web Clipper - shared settings + AnkiConnect client.
 *
 * Loaded by both the service worker (via importScripts) and the options/popup
 * pages (via <script>). It must NOT touch the DOM. Everything is attached to a
 * single global `OmniaClipper` so the two load styles work the same way.
 */

(function (root) {
  "use strict";

  // Default settings. The field mapping maps a CAPTURE KEY to an Anki note FIELD
  // NAME. The defaults assume an Omnia-friendly note type with these fields; the
  // user can remap any of them on the options page. A blank target means "do not
  // send this capture key".
  const DEFAULTS = {
    ankiConnectUrl: "http://127.0.0.1:8765",
    apiKey: "", // AnkiConnect "apiKey" option; empty when AnkiConnect apiKey is null.
    deckName: "Omnia Capture",
    modelName: "Basic",
    allowDuplicate: true,
    tags: ["omnia-web-clipper"],
    // capture key -> Anki note field name
    fieldMap: {
      selection: "Front", // the base field (the word OR phrase Omnia generates from)
      sentence: "", // e.g. "Sentence" or "Context"
      context: "", // e.g. "Context"
      url: "", // e.g. "Source"
      pageTitle: "", // e.g. "Title"
    },
  };

  /** Read settings from chrome.storage.sync, merged over DEFAULTS. */
  function loadSettings() {
    return new Promise((resolve) => {
      chrome.storage.sync.get(DEFAULTS, (stored) => {
        // Deep-merge fieldMap so a partial stored map keeps default keys.
        const merged = Object.assign({}, DEFAULTS, stored);
        merged.fieldMap = Object.assign({}, DEFAULTS.fieldMap, stored.fieldMap || {});
        resolve(merged);
      });
    });
  }

  /** Persist a settings object to chrome.storage.sync. */
  function saveSettings(settings) {
    return new Promise((resolve) => {
      chrome.storage.sync.set(settings, () => resolve());
    });
  }

  /**
   * Low-level AnkiConnect call. Returns the `result` field or throws an Error
   * carrying AnkiConnect's `error` string or a transport/CORS explanation.
   */
  async function ankiConnect(url, action, params, apiKey) {
    const body = { action: action, version: 6, params: params || {} };
    if (apiKey) {
      body.key = apiKey;
    }

    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (err) {
      // A failed fetch here is almost always one of: Anki not running, the
      // AnkiConnect add-on not installed, or a CORS rejection (the extension's
      // origin is not in webCorsOriginList).
      throw new Error(
        "Could not reach AnkiConnect at " +
          url +
          ". Make sure Anki is running with the AnkiConnect add-on, and that this " +
          "extension's origin is allowed in AnkiConnect's webCorsOriginList " +
          "(see the README). Underlying error: " +
          (err && err.message ? err.message : String(err))
      );
    }

    if (!response.ok) {
      throw new Error("AnkiConnect HTTP " + response.status + " " + response.statusText);
    }

    let data;
    try {
      data = await response.json();
    } catch (err) {
      throw new Error("AnkiConnect returned a non-JSON response.");
    }

    // AnkiConnect v6 always returns {result, error}; error is non-null on failure.
    if (data && data.error) {
      throw new Error(data.error);
    }
    return data ? data.result : undefined;
  }

  root.OmniaClipper = {
    DEFAULTS: DEFAULTS,
    loadSettings: loadSettings,
    saveSettings: saveSettings,
    ankiConnect: ankiConnect,
  };
})(typeof self !== "undefined" ? self : this);
