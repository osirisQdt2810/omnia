/**
 * @fileoverview Omnia Web Clipper - background service worker (MV3).
 *
 * Two capture paths converge here:
 *   1. The double-click "+" tooltip in content.js sends an "omnia-capture"
 *      message with a full capture payload.
 *   2. The right-click context menu (registered below, contexts: ["selection"])
 *      fires onClicked; we take info.selectionText and ask the content script
 *      for the surrounding sentence/context, then run the SAME addNote flow.
 *
 * In both cases we read settings from chrome.storage, ensure the target deck
 * exists, then add a note to Anki via AnkiConnect. The captured selection
 * becomes the Omnia "base field" (a word OR phrase); the sentence/context/url
 * go into their mapped fields. The Enabled toggle gates both paths.
 */

importScripts('shared.js');

const {loadSettings, ankiConnect} = self.OmniaClipper;

const CONTEXT_MENU_ID = 'omnia-clipper-send-selection';

// Per-model field-name cache (name -> string[]), so we don't re-query AnkiConnect on every add.
// The MV3 service worker may be torn down between events, which just clears this — harmless.
const modelFieldsCache = {};

/**
 * Build the AnkiConnect note fields object from a capture + the field mapping.
 * Only non-empty target field names are included.
 * @param {!Object} capture The capture payload (selection/sentence/context/...).
 * @param {!Object<string, string>} fieldMap Capture key -> note field name.
 * @return {!Object<string, string>} The note fields object for addNote.
 */
function buildFields(capture, fieldMap) {
  const fields = {};
  for (const captureKey of Object.keys(fieldMap)) {
    const target = (fieldMap[captureKey] || '').trim();
    const value = capture[captureKey];
    if (target && value) {
      fields[target] = value;
    }
  }
  return fields;
}

/**
 * The note type's FIRST field name (Anki's sort/empty-check field). Best-effort: returns '' on
 * any error so the caller can fall through to addNote and surface the real AnkiConnect error.
 * @param {string} url The AnkiConnect URL.
 * @param {string} modelName The note type name.
 * @param {string} apiKey The AnkiConnect apiKey (or '').
 * @return {!Promise<string>} The first field name, or '' if unknown.
 */
async function firstFieldName(url, modelName, apiKey) {
  try {
    if (!modelFieldsCache[modelName]) {
      modelFieldsCache[modelName] = await ankiConnect(
        url, 'modelFieldNames', {modelName: modelName}, apiKey,
      );
    }
    const names = modelFieldsCache[modelName];
    return Array.isArray(names) && names.length ? names[0] : '';
  } catch (e) {
    return '';
  }
}

/**
 * Ensure the deck exists, then add the note. Returns the new note id.
 * @param {!Object} capture The capture payload.
 * @param {!Object} settings The resolved extension settings.
 * @return {!Promise<number>} The new note id from AnkiConnect.
 */
async function addCaptureToAnki(capture, settings) {
  const url = settings.ankiConnectUrl;
  const apiKey = settings.apiKey;

  const fields = buildFields(capture, settings.fieldMap);
  if (Object.keys(fields).length === 0) {
    throw new Error(
      'No note fields are mapped. Open the extension options and map at least ' +
        'the selection to a note field.',
    );
  }

  // Anki rejects a note whose FIRST field is empty ("cannot create note because it is empty").
  // Some note types put an id/meta field first (e.g. AnkiVocabulary's "Note ID") that the mapping
  // never fills. Guarantee the first field is non-empty using the selected word (the base capture),
  // so a clip always succeeds regardless of the note type's field order.
  const firstField = await firstFieldName(url, settings.modelName, apiKey);
  if (firstField && !(fields[firstField] || '').trim()) {
    const base =
      (capture.selection || '').trim() ||
      Object.keys(fields)
        .map((k) => (fields[k] || '').trim())
        .find(Boolean) ||
      '';
    if (!base) {
      throw new Error(
        'Nothing was captured to fill the note. Select some text and try again.',
      );
    }
    fields[firstField] = base;
  }

  // createDeck is idempotent: it returns the deck id whether or not it existed.
  await ankiConnect(url, 'createDeck', {deck: settings.deckName}, apiKey);

  // Caller guard for Omnia auto-generation: when enabled, tag the note "omnia-autogen" so the
  // add-on's gateway recognises it (the source tag "omnia-web-clipper" comes from settings.tags).
  // A Set dedups so a source tag can't repeat. Both capture paths reach this one addNote flow.
  const tagSet = new Set(settings.tags || []);
  if (settings.autogen !== false) {
    tagSet.add('omnia-autogen');
  }

  const note = {
    deckName: settings.deckName,
    modelName: settings.modelName,
    fields: fields,
    tags: Array.from(tagSet),
    options: {
      allowDuplicate: !!settings.allowDuplicate,
    },
  };

  return ankiConnect(url, 'addNote', {note: note}, apiKey);
}

/**
 * Ask the content script in a given tab for the sentence/context surrounding
 * the current selection. Falls back to a minimal payload (selection only) when
 * the content script cannot answer (e.g. it is not injected on that page).
 * @param {number} tabId The id of the tab the menu was clicked in.
 * @param {string} selectionText The raw info.selectionText from the menu event.
 * @return {!Promise<!Object>} A capture payload for addCaptureToAnki.
 */
function requestContext(tabId, selectionText) {
  const fallback = {
    selection: (selectionText || '').replace(/\s+/g, ' ').trim(),
    sentence: '',
    context: '',
    context_full: '',
    pageTitle: '',
    url: '',
  };
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, {type: 'omnia-get-context'}, (response) => {
      if (chrome.runtime.lastError || !response || !response.ok) {
        resolve(fallback);
        return;
      }
      const ctx = response.context || {};
      // Prefer the selection the menu reported (it is exactly what the user
      // highlighted), but take the surrounding text from the content script.
      resolve({
        selection: fallback.selection || ctx.selection || '',
        sentence: ctx.sentence || '',
        context: ctx.context || '',
        // The combined sentence+context field — the options page recommends mapping THIS to a
        // single note field, so it must survive the right-click path too (the "+" path already
        // posts the full payload). Omitting it left a mapped Context field empty on every clip.
        context_full: ctx.context_full || '',
        pageTitle: ctx.pageTitle || '',
        url: ctx.url || '',
      });
    });
  });
}

/**
 * Show a transient toast inside the page via the content script, so the
 * right-click path gives the same visible feedback as the "+" path.
 * @param {number} tabId The tab to show the toast in.
 * @param {string} message The toast text.
 * @param {string} kind One of "success" | "error" | "pending".
 */
function notifyTab(tabId, message, kind) {
  chrome.tabs.sendMessage(
    tabId,
    {type: 'omnia-toast', message: message, kind: kind},
    () => void chrome.runtime.lastError,
  );
}

/**
 * Register (or re-register) the right-click context menu item. Called on
 * install/update; removeAll first so reloads do not throw "duplicate id".
 */
function registerContextMenu() {
  if (!chrome.contextMenus) {
    return;
  }
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: CONTEXT_MENU_ID,
      title: 'Send to Anki (Omnia)',
      contexts: ['selection'],
    });
  });
}

// Re-inject the content script into already-open http(s) tabs. Chrome does NOT auto-inject content
// scripts into existing tabs when the extension is (re)loaded/updated — so without this, clicking
// "+" in an already-open tab silently fails (its content script's extension context is dead) until
// the user refreshes that page. executeScript is best-effort: it rejects on restricted pages
// (chrome://, the Web Store, view-source, PDFs) which we simply ignore.
async function reinjectContentScript() {
  if (!chrome.scripting || !chrome.tabs) {
    return;
  }
  try {
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
      if (!tab.id) {
        continue;
      }
      chrome.scripting
        .executeScript({target: {tabId: tab.id}, files: ['content.js']})
        .catch(() => {});
    }
  } catch (_e) {
    // No scripting/tabs access or the query failed — nothing to re-inject.
  }
}

chrome.runtime.onInstalled.addListener(() => {
  registerContextMenu();
  reinjectContentScript();
});

// The service worker may restart between events; re-assert the menu on startup.
if (chrome.runtime.onStartup) {
  chrome.runtime.onStartup.addListener(() => {
    registerContextMenu();
    reinjectContentScript();
  });
}

// Runs on every service-worker start (including right after a reload, when onInstalled does NOT
// fire) so open tabs get a live content script without a manual page refresh.
reinjectContentScript();

if (chrome.contextMenus) {
  chrome.contextMenus.onClicked.addListener((info, tab) => {
    if (info.menuItemId !== CONTEXT_MENU_ID || !tab || tab.id === undefined) {
      return;
    }
    (async () => {
      const settings = await loadSettings();
      if (!settings.enabled) {
        // Master toggle off: do nothing (mirrors the "+" path being hidden).
        return;
      }
      notifyTab(tab.id, 'Sending to Anki…', 'pending');
      try {
        const capture = await requestContext(tab.id, info.selectionText);
        if (!capture.selection) {
          notifyTab(tab.id, 'Nothing selected to send.', 'error');
          return;
        }
        await addCaptureToAnki(capture, settings);
        const shown =
          capture.selection.length > 40
            ? capture.selection.slice(0, 39) + '…'
            : capture.selection;
        notifyTab(tab.id, 'Added to Anki: “' + shown + '”', 'success');
      } catch (err) {
        notifyTab(tab.id, err && err.message ? err.message : String(err), 'error');
      }
    })();
  });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== 'omnia-capture') {
    return false;
  }

  (async () => {
    try {
      const settings = await loadSettings();
      if (!settings.enabled) {
        sendResponse({ok: false, error: 'Omnia Web Clipper is disabled. Enable it in options.'});
        return;
      }
      const noteId = await addCaptureToAnki(message.payload, settings);
      sendResponse({ok: true, noteId: noteId});
    } catch (err) {
      sendResponse({ok: false, error: err && err.message ? err.message : String(err)});
    }
  })();

  // Returning true keeps the message channel open for the async sendResponse.
  return true;
});
