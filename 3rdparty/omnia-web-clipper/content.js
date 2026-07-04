/**
 * @fileoverview Omnia Web Clipper - content script.
 *
 * Runs on every page (<all_urls>). Two capture paths live here:
 *
 *   1. Double-click "+" (single words / quick selections). When the user selects
 *      text or double-clicks a word AND the extension is enabled with the mouse
 *      toggle on, a small floating "+" tooltip appears near the selection.
 *      Clicking it sends the capture to the background worker.
 *
 *   2. Right-click "Send to Anki (Omnia)" (phrases). The background worker owns
 *      the menu; it messages this script with "omnia-get-context" to read the
 *      sentence/context surrounding the current selection, then runs the same
 *      addNote flow. This script also renders the toast for that path.
 *
 * A capture payload is:
 *   - selection: the selected text (a single word OR a multi-word phrase),
 *   - sentence:  the sentence that contains the selection,
 *   - context:   a larger snippet (the containing paragraph/block),
 *   - pageTitle, url.
 *
 * Pure vanilla JS, no libraries, no build step. Self-contained.
 */

(() => {
  'use strict';

  const TOOLTIP_ID = 'omnia-clipper-tooltip';
  const TOAST_ID = 'omnia-clipper-toast';
  // Cap the context snippet so we never ship a whole article into a note field.
  const MAX_CONTEXT_CHARS = 600;
  const MAX_SENTENCE_CHARS = 400;

  // The most recent capture payload, frozen at the moment the selection was made.
  // We snapshot here (not on click) because clicking the tooltip can clear the
  // browser selection before we read it.
  let pendingCapture = null;

  // Cached enable flags so the (frequent) selection handler stays synchronous.
  // Seeded from storage on load and kept fresh via chrome.storage.onChanged.
  let enabled = true;
  let mouseEnabled = true;

  // -------------------------------------------------------------------------
  // Enable flags (master toggle + double-click toggle)
  // -------------------------------------------------------------------------

  /** Load the enable flags from storage into the module-level cache. */
  function refreshFlags() {
    chrome.storage.sync.get({enabled: true, mouseEnabled: true}, (stored) => {
      enabled = stored.enabled !== false;
      mouseEnabled = stored.mouseEnabled !== false;
      if (!enabled || !mouseEnabled) {
        removeTooltip();
      }
    });
  }

  if (chrome.storage && chrome.storage.onChanged) {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== 'sync') {
        return;
      }
      if (changes.enabled || changes.mouseEnabled) {
        refreshFlags();
      }
    });
  }

  // -------------------------------------------------------------------------
  // Selection -> capture payload
  // -------------------------------------------------------------------------

  /**
   * Build the capture payload from the current window selection.
   * @return {?Object} The capture payload, or null when there is no usable
   *     (non-whitespace) selection.
   */
  function buildCapture() {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
      return null;
    }
    const selectedText = collapseWhitespace(selection.toString());
    if (!selectedText) {
      return null;
    }

    const range = selection.getRangeAt(0);
    const block = nearestBlockElement(range.commonAncestorContainer);
    const blockText = block ? collapseWhitespace(block.innerText || block.textContent || '') : '';

    const sentence = clip(
      extractSentence(blockText, selectedText) || selectedText,
      MAX_SENTENCE_CHARS,
    );
    const context = clip(blockText || selectedText, MAX_CONTEXT_CHARS);

    return {
      selection: selectedText,
      sentence: sentence,
      context: context,
      // Combined disambiguation input: the exact sentence first (precise usage), then the
      // surrounding paragraph — map THIS single key to one note field to give the generator
      // both without needing two fields. Deduped when the block is just the sentence.
      context_full: combineContext(sentence, context),
      pageTitle: document.title || '',
      url: location.href,
    };
  }

  /**
   * Merge the exact sentence and the surrounding context into one snippet, sentence first for
   * emphasis. Falls back to whichever exists; avoids duplicating the sentence when it already
   * equals the context.
   * @param {string} sentence The exact containing sentence.
   * @param {string} context The surrounding paragraph/block.
   * @return {string} The combined snippet.
   */
  function combineContext(sentence, context) {
    if (sentence && context && sentence !== context) {
      return sentence + '\n\n' + context;
    }
    return context || sentence || '';
  }

  /**
   * Collapse runs of whitespace/newlines into single spaces and trim.
   * @param {string} text The raw text.
   * @return {string} The normalised text.
   */
  function collapseWhitespace(text) {
    return (text || '').replace(/\s+/g, ' ').trim();
  }

  /**
   * Truncate to a max length on a word boundary, adding an ellipsis.
   * @param {string} text The text to clip.
   * @param {number} maxChars The maximum length.
   * @return {string} The clipped text.
   */
  function clip(text, maxChars) {
    if (text.length <= maxChars) {
      return text;
    }
    const cut = text.slice(0, maxChars);
    const lastSpace = cut.lastIndexOf(' ');
    return (lastSpace > 0 ? cut.slice(0, lastSpace) : cut) + '…';
  }

  /**
   * Walk up from a DOM node to the nearest block-level element so the "context"
   * snippet is a meaningful paragraph rather than an inline fragment.
   * @param {?Node} node The starting node.
   * @return {!Element} The nearest block-level ancestor (or document.body).
   */
  function nearestBlockElement(node) {
    let el = node && node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    const blockTags = new Set([
      'P', 'DIV', 'LI', 'TD', 'TH', 'BLOCKQUOTE', 'SECTION', 'ARTICLE',
      'PRE', 'DD', 'DT', 'FIGCAPTION', 'ASIDE', 'MAIN', 'BODY',
    ]);
    while (el && el !== document.body) {
      if (blockTags.has(el.tagName)) {
        return el;
      }
      el = el.parentElement;
    }
    return el || document.body;
  }

  /**
   * Find the sentence inside `blockText` that contains `needle`.
   * Splits on sentence boundaries (., !, ?, and common CJK terminators).
   * Falls back to a windowed slice around the needle, then to the needle itself.
   * @param {string} blockText The containing block's text.
   * @param {string} needle The selected text to locate.
   * @return {string} The best-effort containing sentence.
   */
  function extractSentence(blockText, needle) {
    if (!blockText) {
      return '';
    }
    const idx = blockText.indexOf(needle);
    if (idx === -1) {
      // Selection text was normalised differently from the block; window around
      // a best-effort midpoint instead of failing outright.
      return windowAround(blockText, Math.floor(blockText.length / 2), needle.length);
    }

    // Sentence terminators followed by whitespace/quote/closing bracket.
    const boundary = /[.!?。！？]+["'”’\)\]]?\s+/g;
    let start = 0;
    let match;
    let sentenceStart = 0;
    let sentenceEnd = blockText.length;

    // Find the boundary immediately before the needle (sentence start) ...
    while ((match = boundary.exec(blockText)) !== null) {
      const boundaryEnd = match.index + match[0].length;
      if (boundaryEnd <= idx) {
        sentenceStart = boundaryEnd;
      } else {
        // ... and the first boundary at/after the needle (sentence end).
        sentenceEnd = boundaryEnd;
        break;
      }
      start = boundaryEnd;
    }
    void start;

    return collapseWhitespace(blockText.slice(sentenceStart, sentenceEnd));
  }

  /**
   * Fallback: a character window centred on `center` widened by the selection.
   * @param {string} text The block text.
   * @param {number} center The midpoint to window around.
   * @param {number} selLen The selection length, used to widen the window.
   * @return {string} The windowed, whitespace-collapsed text.
   */
  function windowAround(text, center, selLen) {
    const half = Math.max(120, selLen + 80);
    const from = Math.max(0, center - half);
    const to = Math.min(text.length, center + half);
    return collapseWhitespace(text.slice(from, to));
  }

  // -------------------------------------------------------------------------
  // Floating "+" tooltip
  // -------------------------------------------------------------------------

  /** Remove the "+" tooltip and clear the pending capture. */
  function removeTooltip() {
    const existing = document.getElementById(TOOLTIP_ID);
    if (existing) {
      existing.remove();
    }
    pendingCapture = null;
  }

  /**
   * Show the "+" button near the selection's bounding rectangle.
   * @param {!Object} capture The capture payload to attach to the button.
   */
  function showTooltip(capture) {
    removeTooltip();
    pendingCapture = capture;

    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) {
      return;
    }
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) {
      return;
    }

    const btn = document.createElement('div');
    btn.id = TOOLTIP_ID;
    btn.setAttribute('role', 'button');
    btn.setAttribute('aria-label', 'Add to Anki with Omnia');
    btn.title = 'Add to Anki (Omnia)';
    btn.textContent = '+';
    Object.assign(btn.style, {
      position: 'fixed',
      // Anchor just above the selection's top-right; clamp to viewport.
      top: Math.max(4, rect.top - 26) + 'px',
      left: Math.min(window.innerWidth - 26, rect.right + 5) + 'px',
      zIndex: '2147483647',
      width: '20px',
      height: '20px',
      lineHeight: '18px',
      textAlign: 'center',
      fontSize: '14px',
      fontWeight: '700',
      fontFamily: 'system-ui, -apple-system, Segoe UI, Roboto, sans-serif',
      color: '#ffffff',
      background: '#2d6cdf',
      border: '1px solid #1f4fb0',
      borderRadius: '50%',
      boxShadow: '0 2px 6px rgba(0,0,0,0.3)',
      cursor: 'pointer',
      userSelect: 'none',
      padding: '0',
    });

    // Use mousedown (not click) so we read the selection BEFORE it is cleared,
    // and preventDefault so the page's selection stays intact while we capture.
    btn.addEventListener('mousedown', (event) => {
      event.preventDefault();
      event.stopPropagation();
      sendCapture();
    });

    document.body.appendChild(btn);
  }

  /** Send the pending capture to the background worker and report the result. */
  function sendCapture() {
    if (!pendingCapture) {
      return;
    }
    const capture = pendingCapture;
    removeTooltip();
    showToast('Sending to Anki…', 'pending');

    chrome.runtime.sendMessage({type: 'omnia-capture', payload: capture}, (response) => {
      if (chrome.runtime.lastError) {
        showToast('Extension error: ' + chrome.runtime.lastError.message, 'error');
        return;
      }
      if (!response) {
        showToast('No response from background worker.', 'error');
        return;
      }
      if (response.ok) {
        showToast('Added to Anki: “' + truncateForToast(capture.selection) + '”', 'success');
      } else {
        showToast(response.error || 'Failed to add note.', 'error');
      }
    });
  }

  /**
   * Truncate a string for display inside a toast.
   * @param {string} text The text to truncate.
   * @return {string} The truncated text.
   */
  function truncateForToast(text) {
    return text.length > 40 ? text.slice(0, 39) + '…' : text;
  }

  // -------------------------------------------------------------------------
  // Toast (success / error feedback)
  // -------------------------------------------------------------------------

  /**
   * Show a transient toast in the bottom-right corner.
   * @param {string} message The message text.
   * @param {string} kind One of "success" | "error" | "pending".
   */
  function showToast(message, kind) {
    const existing = document.getElementById(TOAST_ID);
    if (existing) {
      existing.remove();
    }
    const toast = document.createElement('div');
    toast.id = TOAST_ID;
    toast.textContent = message;
    const palette = {
      success: {bg: '#1f8a4c', fg: '#ffffff'},
      error: {bg: '#c0392b', fg: '#ffffff'},
      pending: {bg: '#444b54', fg: '#ffffff'},
    };
    const colors = palette[kind] || palette.pending;
    Object.assign(toast.style, {
      position: 'fixed',
      bottom: '20px',
      right: '20px',
      zIndex: '2147483647',
      maxWidth: '320px',
      padding: '10px 14px',
      background: colors.bg,
      color: colors.fg,
      fontSize: '13px',
      fontFamily: 'system-ui, -apple-system, Segoe UI, Roboto, sans-serif',
      borderRadius: '6px',
      boxShadow: '0 3px 10px rgba(0,0,0,0.35)',
      lineHeight: '1.4',
    });
    document.body.appendChild(toast);

    // Errors linger longer so the user can read them.
    const ttl = kind === 'error' ? 6000 : kind === 'pending' ? 8000 : 3500;
    setTimeout(() => {
      if (toast.isConnected) {
        toast.remove();
      }
    }, ttl);
  }

  // -------------------------------------------------------------------------
  // Messages from the background worker (context-menu path)
  // -------------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message) {
      return false;
    }
    if (message.type === 'omnia-get-context') {
      // The right-click path: report the surrounding text for the current
      // selection so the background worker can build the same capture payload.
      const capture = buildCapture();
      if (!capture) {
        sendResponse({ok: false});
      } else {
        sendResponse({ok: true, context: capture});
      }
      return false;
    }
    if (message.type === 'omnia-toast') {
      // The background worker renders its feedback through our toast UI.
      showToast(message.message, message.kind);
      sendResponse({ok: true});
      return false;
    }
    return false;
  });

  // -------------------------------------------------------------------------
  // Event wiring
  // -------------------------------------------------------------------------

  // Debounce so dragging a selection does not spawn a tooltip on every mousemove.
  let debounceTimer = null;

  /** Selection-change handler: (re)build the capture and show/hide the "+". */
  function onSelectionEvent() {
    // The "+" tooltip is gated by BOTH the master toggle and the mouse toggle.
    if (!enabled || !mouseEnabled) {
      removeTooltip();
      return;
    }
    if (debounceTimer) {
      clearTimeout(debounceTimer);
    }
    debounceTimer = setTimeout(() => {
      const capture = buildCapture();
      if (capture) {
        showTooltip(capture);
      } else {
        removeTooltip();
      }
    }, 10);
  }

  document.addEventListener('mouseup', onSelectionEvent, true);
  document.addEventListener('dblclick', onSelectionEvent, true);

  // Dismiss the tooltip on an outside click or Escape.
  document.addEventListener(
    'mousedown',
    (event) => {
      const tooltip = document.getElementById(TOOLTIP_ID);
      if (tooltip && event.target !== tooltip) {
        removeTooltip();
      }
    },
    true,
  );
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      removeTooltip();
    }
  });
  document.addEventListener('scroll', removeTooltip, true);

  // Seed the enable flags as soon as the script loads.
  refreshFlags();
})();
