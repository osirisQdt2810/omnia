/*
 * Omnia Web Clipper - content script.
 *
 * Runs on every page (<all_urls>). When the user selects text or double-clicks a
 * word, it shows a small floating "+" tooltip near the selection. Clicking the
 * tooltip captures:
 *   - selection: the selected text (a single word OR a multi-word phrase),
 *   - sentence:  the sentence that contains the selection,
 *   - context:   a larger snippet (the containing paragraph/block),
 *   - pageTitle, url.
 * The capture is sent to the background service worker, which talks to AnkiConnect.
 *
 * Pure vanilla JS, no libraries, no build step. Self-contained.
 */

(() => {
  "use strict";

  const TOOLTIP_ID = "omnia-clipper-tooltip";
  const TOAST_ID = "omnia-clipper-toast";
  // Cap the context snippet so we never ship a whole article into a note field.
  const MAX_CONTEXT_CHARS = 600;
  const MAX_SENTENCE_CHARS = 400;

  // The most recent capture payload, frozen at the moment the selection was made.
  // We snapshot here (not on click) because clicking the tooltip can clear the
  // browser selection before we read it.
  let pendingCapture = null;

  // -------------------------------------------------------------------------
  // Selection -> capture payload
  // -------------------------------------------------------------------------

  /**
   * Build the capture payload from the current window selection.
   * Returns null when there is no usable (non-whitespace) selection.
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
    const blockText = block ? collapseWhitespace(block.innerText || block.textContent || "") : "";

    const sentence = extractSentence(blockText, selectedText) || selectedText;
    const context = clip(blockText || selectedText, MAX_CONTEXT_CHARS);

    return {
      selection: selectedText,
      sentence: clip(sentence, MAX_SENTENCE_CHARS),
      context: context,
      pageTitle: document.title || "",
      url: location.href,
    };
  }

  /** Collapse runs of whitespace/newlines into single spaces and trim. */
  function collapseWhitespace(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  /** Truncate to a max length on a word boundary, adding an ellipsis. */
  function clip(text, maxChars) {
    if (text.length <= maxChars) {
      return text;
    }
    const cut = text.slice(0, maxChars);
    const lastSpace = cut.lastIndexOf(" ");
    return (lastSpace > 0 ? cut.slice(0, lastSpace) : cut) + "…";
  }

  /**
   * Walk up from a DOM node to the nearest block-level element so the "context"
   * snippet is a meaningful paragraph rather than an inline fragment.
   */
  function nearestBlockElement(node) {
    let el = node && node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    const blockTags = new Set([
      "P", "DIV", "LI", "TD", "TH", "BLOCKQUOTE", "SECTION", "ARTICLE",
      "PRE", "DD", "DT", "FIGCAPTION", "ASIDE", "MAIN", "BODY",
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
   */
  function extractSentence(blockText, needle) {
    if (!blockText) {
      return "";
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

  /** Fallback: a character window centred on `center` widened by the selection. */
  function windowAround(text, center, selLen) {
    const half = Math.max(120, selLen + 80);
    const from = Math.max(0, center - half);
    const to = Math.min(text.length, center + half);
    return collapseWhitespace(text.slice(from, to));
  }

  // -------------------------------------------------------------------------
  // Floating "+" tooltip
  // -------------------------------------------------------------------------

  function removeTooltip() {
    const existing = document.getElementById(TOOLTIP_ID);
    if (existing) {
      existing.remove();
    }
    pendingCapture = null;
  }

  /** Show the "+" button near the selection's bounding rectangle. */
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

    const btn = document.createElement("div");
    btn.id = TOOLTIP_ID;
    btn.setAttribute("role", "button");
    btn.setAttribute("aria-label", "Add to Anki with Omnia");
    btn.title = "Add to Anki (Omnia)";
    btn.textContent = "+";
    Object.assign(btn.style, {
      position: "fixed",
      // Anchor just above the selection's top-right; clamp to viewport.
      top: Math.max(4, rect.top - 36) + "px",
      left: Math.min(window.innerWidth - 36, rect.right + 6) + "px",
      zIndex: "2147483647",
      width: "28px",
      height: "28px",
      lineHeight: "26px",
      textAlign: "center",
      fontSize: "20px",
      fontWeight: "700",
      fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
      color: "#ffffff",
      background: "#2d6cdf",
      border: "1px solid #1f4fb0",
      borderRadius: "50%",
      boxShadow: "0 2px 6px rgba(0,0,0,0.3)",
      cursor: "pointer",
      userSelect: "none",
      padding: "0",
    });

    // Use mousedown (not click) so we read the selection BEFORE it is cleared,
    // and preventDefault so the page's selection stays intact while we capture.
    btn.addEventListener("mousedown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      sendCapture();
    });

    document.body.appendChild(btn);
  }

  function sendCapture() {
    if (!pendingCapture) {
      return;
    }
    const capture = pendingCapture;
    removeTooltip();
    showToast("Sending to Anki…", "pending");

    chrome.runtime.sendMessage({ type: "omnia-capture", payload: capture }, (response) => {
      if (chrome.runtime.lastError) {
        showToast("Extension error: " + chrome.runtime.lastError.message, "error");
        return;
      }
      if (!response) {
        showToast("No response from background worker.", "error");
        return;
      }
      if (response.ok) {
        showToast("Added to Anki: “" + truncateForToast(capture.selection) + "”", "success");
      } else {
        showToast(response.error || "Failed to add note.", "error");
      }
    });
  }

  function truncateForToast(text) {
    return text.length > 40 ? text.slice(0, 39) + "…" : text;
  }

  // -------------------------------------------------------------------------
  // Toast (success / error feedback)
  // -------------------------------------------------------------------------

  function showToast(message, kind) {
    const existing = document.getElementById(TOAST_ID);
    if (existing) {
      existing.remove();
    }
    const toast = document.createElement("div");
    toast.id = TOAST_ID;
    toast.textContent = message;
    const palette = {
      success: { bg: "#1f8a4c", fg: "#ffffff" },
      error: { bg: "#c0392b", fg: "#ffffff" },
      pending: { bg: "#444b54", fg: "#ffffff" },
    };
    const colors = palette[kind] || palette.pending;
    Object.assign(toast.style, {
      position: "fixed",
      bottom: "20px",
      right: "20px",
      zIndex: "2147483647",
      maxWidth: "320px",
      padding: "10px 14px",
      background: colors.bg,
      color: colors.fg,
      fontSize: "13px",
      fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
      borderRadius: "6px",
      boxShadow: "0 3px 10px rgba(0,0,0,0.35)",
      lineHeight: "1.4",
    });
    document.body.appendChild(toast);

    // Errors linger longer so the user can read them.
    const ttl = kind === "error" ? 6000 : kind === "pending" ? 8000 : 3500;
    setTimeout(() => {
      if (toast.isConnected) {
        toast.remove();
      }
    }, ttl);
  }

  // -------------------------------------------------------------------------
  // Event wiring
  // -------------------------------------------------------------------------

  // Debounce so dragging a selection does not spawn a tooltip on every mousemove.
  let debounceTimer = null;

  function onSelectionEvent() {
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

  document.addEventListener("mouseup", onSelectionEvent, true);
  document.addEventListener("dblclick", onSelectionEvent, true);

  // Dismiss the tooltip on an outside click or Escape.
  document.addEventListener(
    "mousedown",
    (event) => {
      const tooltip = document.getElementById(TOOLTIP_ID);
      if (tooltip && event.target !== tooltip) {
        removeTooltip();
      }
    },
    true
  );
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      removeTooltip();
    }
  });
  document.addEventListener("scroll", removeTooltip, true);
})();
