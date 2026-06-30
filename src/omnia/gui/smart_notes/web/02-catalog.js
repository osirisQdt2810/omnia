
  /**
   * Smart Notes config page — part 2 of 6 of the page IIFE.
   * Catalog helpers: which Provider names, Model ids, and TTS Voices to offer for a given
   * generation kind, read from the baked `window.__SN_CATALOG`. The Provider and Model/Voice
   * dropdowns are rebuilt from these whenever the kind or provider changes.
   */

  /**
   * Whether a generation kind speaks audio (uses the TTS provider + Voice picker).
   * @param {string} kind text | image | tts
   * @return {boolean}
   */
  function isTts(kind) {
    return kind === "tts";
  }

  /**
   * The provider names offered for a kind: TTS providers for sound, LLM providers otherwise.
   * @param {string} kind text | image | tts
   * @return {!Array<string>}
   */
  function providerNames(kind) {
    // image uses its own list (only providers that actually generate images — no openrouter,
    // which has no /images/generations endpoint); tts uses tts_providers; text uses llm.
    const names =
      kind === "image"
        ? CATALOG.image_providers
        : isTts(kind)
          ? CATALOG.tts_providers
          : CATALOG.llm_providers;
    return (names || []).slice();
  }

  /**
   * The curated model ids for a (kind, provider): image models for image, text models else.
   * @param {string} kind text | image | tts
   * @param {string} provider The selected provider.
   * @return {!Array<string>}
   */
  function modelValues(kind, provider) {
    const map = kind === "image" ? CATALOG.image_models : CATALOG.text_models;
    return map && map[provider] ? map[provider].slice() : [];
  }

  /**
   * The curated voices for a TTS provider.
   * @param {string} provider The selected TTS provider.
   * @return {!Array<!Object>} Entries of {voice, label, language, gender, model}.
   */
  function voiceEntries(provider) {
    return CATALOG.voices && CATALOG.voices[provider] ? CATALOG.voices[provider] : [];
  }

  /**
   * The languages offered in the sound-field Language picker.
   * @return {!Array<!Object>} Entries of {code, label} ("" code = auto-detect).
   */
  function languageOptions() {
    return (CATALOG.languages || []).slice();
  }
