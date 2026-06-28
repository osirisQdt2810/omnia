/**
 * @fileoverview Omnia Web Clipper - popup ("Quick options").
 *
 * A compact panel showing the Enabled pill toggle (writes to storage at once),
 * the currently-selected Deck and Note type as read-only labels, AnkiConnect
 * reachability (a quick `version` call), and an Options button.
 */

(() => {
  'use strict';

  const {loadSettings, saveSettings, ankiConnect} = self.OmniaClipper;

  const enabledToggle = document.getElementById('enabledToggle');
  const deckVal = document.getElementById('deckVal');
  const modelVal = document.getElementById('modelVal');
  const dot = document.getElementById('dot');
  const statusText = document.getElementById('statusText');
  const detail = document.getElementById('detail');

  /**
   * Probe AnkiConnect and update the reachability indicator.
   * @param {!Object} settings The resolved settings (for url + apiKey).
   */
  async function checkReachable(settings) {
    try {
      const version = await ankiConnect(settings.ankiConnectUrl, 'version', {}, settings.apiKey);
      dot.className = 'dot ok';
      statusText.textContent = 'AnkiConnect — Connected ✓ (v' + version + ')';
      detail.textContent = '';
    } catch (err) {
      dot.className = 'dot err';
      statusText.textContent = 'AnkiConnect — Not Connected ✗';
      detail.textContent = err && err.message ? err.message : String(err);
    }
  }

  /** Load settings, render the toggle + labels, then probe AnkiConnect. */
  async function init() {
    const s = await loadSettings();
    enabledToggle.setAttribute('aria-checked', String(s.enabled !== false));
    deckVal.textContent = s.deckName || '—';
    modelVal.textContent = s.modelName || '—';
    checkReachable(s);
  }

  document.addEventListener('DOMContentLoaded', () => {
    enabledToggle.addEventListener('click', async () => {
      const next = enabledToggle.getAttribute('aria-checked') !== 'true';
      enabledToggle.setAttribute('aria-checked', String(next));
      await saveSettings({enabled: next});
    });

    document.getElementById('optionsBtn').addEventListener('click', () => {
      if (chrome.runtime.openOptionsPage) {
        chrome.runtime.openOptionsPage();
      } else {
        window.open(chrome.runtime.getURL('options.html'));
      }
    });

    init();
  });
})();
