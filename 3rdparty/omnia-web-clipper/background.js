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

  // createDeck is idempotent: it returns the deck id whether or not it existed.
  await ankiConnect(url, 'createDeck', {deck: settings.deckName}, apiKey);

  const note = {
    deckName: settings.deckName,
    modelName: settings.modelName,
    fields: fields,
    tags: settings.tags || [],
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

chrome.runtime.onInstalled.addListener(() => {
  registerContextMenu();
});

// The service worker may restart between events; re-assert the menu on startup.
if (chrome.runtime.onStartup) {
  chrome.runtime.onStartup.addListener(() => {
    registerContextMenu();
  });
}

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
