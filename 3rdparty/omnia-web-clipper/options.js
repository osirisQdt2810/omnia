/**
 * @fileoverview Omnia Web Clipper - options page logic.
 *
 * Replaces the old free-text deck/model/field inputs with real <select>
 * dropdowns populated from AnkiConnect:
 *   - "Test connection" calls `version` (showing Connected ✓ / Not connected ✗),
 *     `deckNames` (the Deck dropdown), and `modelNames` (the Note type dropdown).
 *   - Changing the Note type calls `modelFieldNames(modelName)` and rebuilds the
 *     field-mapping dropdowns so the user maps each capture key to a real field.
 *   - The Enabled and Double-click "+" pill toggles write to chrome.storage.sync
 *     immediately. Save persists the rest of the form.
 */

(() => {
  'use strict';

  const {loadSettings, saveSettings, ankiConnect} = self.OmniaClipper;

  const CAPTURE_KEYS = [
    'selection',
    'sentence',
    'context',
    'context_full',
    'url',
    'pageTitle',
  ];
  const SKIP_LABEL = '(skip)';

  const el = (id) => document.getElementById(id);
  const statusEl = el('status');
  const testOutput = el('testOutput');

  // In-memory mirror of the field map so re-populating the note-type dropdowns
  // (which clears the field selects) can restore the user's prior choices.
  let fieldMapState = {};

  /**
   * Update the small inline save/test status line.
   * @param {string} text The status text.
   * @param {string=} kind "ok" | "err" | "" for styling.
   */
  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = kind || '';
  }

  /**
   * Update the AnkiConnect connection indicator (dot + label).
   * @param {string} text The label text.
   * @param {string} kind "ok" | "err" | "" (neutral).
   */
  function setConnStatus(text, kind) {
    el('connDot').className = 'dot' + (kind ? ' ' + kind : '');
    el('connText').textContent = text;
  }

  /**
   * Show the raw test output panel.
   * @param {string} text The multi-line output text.
   */
  function writeTestOutput(text) {
    testOutput.style.display = 'block';
    testOutput.textContent = text;
  }

  /**
   * Populate a <select> with string options, selecting `selected` if present.
   * If `selected` is non-empty but missing from `values`, it is kept as an extra
   * option so a previously-saved (currently-offline) value is not silently lost.
   * @param {!HTMLSelectElement} selectEl The target <select>.
   * @param {!Array<string>} values The option values.
   * @param {string} selected The value to pre-select.
   * @param {{includeSkip: boolean}=} opts When includeSkip, prepend a "(skip)"
   *     option whose value is the empty string.
   */
  function fillSelect(selectEl, values, selected, opts) {
    const includeSkip = !!(opts && opts.includeSkip);
    selectEl.textContent = '';

    if (includeSkip) {
      const skip = document.createElement('option');
      skip.value = '';
      skip.textContent = SKIP_LABEL;
      selectEl.appendChild(skip);
    }

    const all = values.slice();
    if (selected && !all.includes(selected)) {
      all.push(selected);
    }
    for (const value of all) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = value;
      selectEl.appendChild(option);
    }
    selectEl.value = includeSkip ? selected || '' : selected || (all.length ? all[0] : '');
  }

  /**
   * Build a pill toggle handler that writes its new state to storage at once.
   * @param {string} key The settings key the pill controls.
   * @param {!HTMLElement} pill The pill button element.
   */
  function wirePill(key, pill) {
    pill.addEventListener('click', async () => {
      const next = pill.getAttribute('aria-checked') !== 'true';
      pill.setAttribute('aria-checked', String(next));
      const patch = {};
      patch[key] = next;
      await saveSettings(patch);
    });
  }

  /**
   * Render the field-mapping dropdowns from a list of real field names.
   * @param {!Array<string>} fieldNames The note type's field names.
   */
  function renderFieldSelects(fieldNames) {
    for (const key of CAPTURE_KEYS) {
      fillSelect(el('f_' + key), fieldNames, fieldMapState[key] || '', {includeSkip: true});
    }
  }

  /** Populate the form from stored settings (without hitting the network). */
  async function restore() {
    const s = await loadSettings();
    el('ankiConnectUrl').value = s.ankiConnectUrl;
    el('apiKey').value = s.apiKey;
    el('allowDuplicate').checked = !!s.allowDuplicate;
    el('tags').value = (s.tags || []).join(', ');

    el('enabledToggle').setAttribute('aria-checked', String(s.enabled !== false));
    el('mouseToggle').setAttribute('aria-checked', String(s.mouseEnabled !== false));
    el('autogen').checked = s.autogen !== false;

    // Seed the dropdowns with the stored values so the page is usable even
    // before a successful connection; Test connection replaces these lists.
    fillSelect(el('deckName'), [], s.deckName, {includeSkip: false});
    fillSelect(el('modelName'), [], s.modelName, {includeSkip: false});

    fieldMapState = Object.assign({}, s.fieldMap);
    renderFieldSelects([]);
  }

  /**
   * Read the form into a full settings object (toggles are read from the pills).
   * @return {!Object} The settings to persist.
   */
  function collect() {
    const tags = el('tags')
      .value.split(',')
      .map((t) => t.trim())
      .filter((t) => t.length > 0);

    const fieldMap = {};
    for (const key of CAPTURE_KEYS) {
      fieldMap[key] = el('f_' + key).value.trim();
    }

    return {
      ankiConnectUrl: el('ankiConnectUrl').value.trim() || 'http://127.0.0.1:8765',
      apiKey: el('apiKey').value,
      enabled: el('enabledToggle').getAttribute('aria-checked') === 'true',
      mouseEnabled: el('mouseToggle').getAttribute('aria-checked') === 'true',
      autogen: el('autogen').checked,
      deckName: el('deckName').value.trim() || 'Omnia Capture',
      modelName: el('modelName').value.trim() || 'Basic',
      allowDuplicate: el('allowDuplicate').checked,
      tags: tags,
      fieldMap: fieldMap,
    };
  }

  /** Save the whole form to storage. */
  async function onSave() {
    fieldMapState = collect().fieldMap;
    await saveSettings(collect());
    setStatus('Saved.', 'ok');
    setTimeout(() => setStatus('', ''), 2000);
  }

  /**
   * Load the field names for the currently-selected note type and rebuild the
   * field-mapping dropdowns. Best-effort: failures leave a "(skip)"-only list.
   */
  async function loadFieldsForCurrentModel() {
    const url = el('ankiConnectUrl').value.trim();
    const apiKey = el('apiKey').value;
    const modelName = el('modelName').value;
    if (!modelName) {
      renderFieldSelects([]);
      return;
    }
    try {
      const fieldNames = await ankiConnect(url, 'modelFieldNames', {modelName: modelName}, apiKey);
      renderFieldSelects(Array.isArray(fieldNames) ? fieldNames : []);
    } catch (err) {
      renderFieldSelects([]);
    }
  }

  /**
   * Test connection: version + deckNames + modelNames + modelFieldNames. Updates
   * the connection indicator, fills every dropdown, and prints raw output.
   */
  async function onTest() {
    setConnStatus('Ankiconnect — testing…', '');
    testOutput.style.display = 'none';

    // Persist the connection fields first so the test reflects what capture uses.
    await saveSettings({
      ankiConnectUrl: el('ankiConnectUrl').value.trim() || 'http://127.0.0.1:8765',
      apiKey: el('apiKey').value,
    });

    const url = el('ankiConnectUrl').value.trim() || 'http://127.0.0.1:8765';
    const apiKey = el('apiKey').value;

    try {
      const version = await ankiConnect(url, 'version', {}, apiKey);
      setConnStatus('Ankiconnect — Connected ✓ (v' + version + ')', 'ok');

      const decks = await ankiConnect(url, 'deckNames', {}, apiKey);
      const models = await ankiConnect(url, 'modelNames', {}, apiKey);

      fillSelect(el('deckName'), Array.isArray(decks) ? decks : [], el('deckName').value, {
        includeSkip: false,
      });
      fillSelect(el('modelName'), Array.isArray(models) ? models : [], el('modelName').value, {
        includeSkip: false,
      });

      await loadFieldsForCurrentModel();

      const lines = [];
      lines.push('AnkiConnect reachable. API version: ' + version);
      lines.push('Decks: ' + (decks || []).length + ' loaded.');
      lines.push('Note types: ' + (models || []).join(', '));
      writeTestOutput(lines.join('\n'));
    } catch (err) {
      setConnStatus('Ankiconnect — Not Connected ✗', 'err');
      writeTestOutput('Test failed:\n' + (err && err.message ? err.message : String(err)));
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    el('saveBtn').addEventListener('click', onSave);
    el('testBtn').addEventListener('click', onTest);
    el('modelName').addEventListener('change', loadFieldsForCurrentModel);

    wirePill('enabled', el('enabledToggle'));
    wirePill('mouseEnabled', el('mouseToggle'));

    // Restore the form, then auto-test so the dropdowns are populated on open.
    restore().then(() => onTest());
  });
})();
