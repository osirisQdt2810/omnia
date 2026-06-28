/*
 * Omnia Web Clipper - background service worker (MV3).
 *
 * Receives a capture from the content script, reads settings from
 * chrome.storage, ensures the target deck exists, then adds a note to Anki via
 * AnkiConnect. The captured selection becomes the Omnia "base field" (a word OR
 * phrase); the sentence/context/url go into their mapped fields. Omnia Smart
 * Notes fills the remaining generated fields at review time.
 */

importScripts("shared.js");

const { loadSettings, ankiConnect } = self.OmniaClipper;

/**
 * Build the AnkiConnect note fields object from a capture + the field mapping.
 * Only non-empty target field names are included.
 */
function buildFields(capture, fieldMap) {
  const fields = {};
  for (const captureKey of Object.keys(fieldMap)) {
    const target = (fieldMap[captureKey] || "").trim();
    const value = capture[captureKey];
    if (target && value) {
      fields[target] = value;
    }
  }
  return fields;
}

/** Ensure the deck exists, then add the note. Returns the new note id. */
async function addCaptureToAnki(capture, settings) {
  const url = settings.ankiConnectUrl;
  const apiKey = settings.apiKey;

  const fields = buildFields(capture, settings.fieldMap);
  if (Object.keys(fields).length === 0) {
    throw new Error(
      "No note fields are mapped. Open the extension options and map at least " +
        "the selection to a note field."
    );
  }

  // createDeck is idempotent: it returns the deck id whether or not it existed.
  await ankiConnect(url, "createDeck", { deck: settings.deckName }, apiKey);

  const note = {
    deckName: settings.deckName,
    modelName: settings.modelName,
    fields: fields,
    tags: settings.tags || [],
    options: {
      allowDuplicate: !!settings.allowDuplicate,
    },
  };

  return ankiConnect(url, "addNote", { note: note }, apiKey);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "omnia-capture") {
    return false;
  }

  (async () => {
    try {
      const settings = await loadSettings();
      const noteId = await addCaptureToAnki(message.payload, settings);
      sendResponse({ ok: true, noteId: noteId });
    } catch (err) {
      sendResponse({ ok: false, error: err && err.message ? err.message : String(err) });
    }
  })();

  // Returning true keeps the message channel open for the async sendResponse.
  return true;
});
