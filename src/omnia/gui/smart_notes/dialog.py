"""The Smart Notes config dialog — a polished webview table over the note-type model.

A :class:`~omnia.gui.web_dialog.WebDialog`-hosted page (gradient header, rounded card, a
scrolling per-field table, light/dark) that edits ONE note type's
:class:`~omnia.plugins.smart_notes.config.SmartNotesNoteTypeConfig` at a time: pick the note type,
designate its base (input) field + the decks it applies to, then for every other field set
whether to Generate it, whether to Lock it (blurs + protects from Auto-smart/Improve), its type
(text/image/sound), a prompt (edited in a popup, not inline), a kind-aware provider + model,
voice + language for sound fields, and an overwrite flag. Clicking the Generate / Lock /
Overwrite column header toggles that column for ALL rows. A ⚙ Options modal edits the global
flags (generate-at-review, regenerate-when-batching, allow-empty-sources).

Three off-thread LLM actions push their result back through page hooks: ✨ Auto-prompt writes a
prompt+type for the Generate-on + unlocked fields; ✨ Improve (per field, and "Improve all")
rewrites a rough prompt into a polished one; ▶ Preview generates a sample for one field on a
random note. The provider/model/voice options come from a baked catalog
(:func:`~omnia.core.providers.catalog.catalog_payload`).

The page markup and the pure row↔config mapping live in ``html.py``; this class is the thin
Qt/Anki glue handling the ``pycmd`` ops and persisting through the config repo. Only loaded
inside Anki.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from aqt.theme import theme_manager

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.providers import ProviderError, ProviderHub, available_llm_providers
from omnia.core.providers.catalog import catalog_payload
from omnia.core.providers.native_runtime import default_manager
from omnia.gui.smart_notes.html import (
    build_smart_notes_html,
    graph_payload,
    load_payload,
    merge_note_type_into,
    native_runtimes_payload,
    note_type_config_from_payload,
    row_to_payload,
    rows_for_note_type,
    set_native_runtime,
)
from omnia.gui.web_dialog import WebDialog
from omnia.plugins.smart_notes.integration import SmartNotesStore

if TYPE_CHECKING:
    from omnia.core.config import ConfigRepository
    from omnia.plugins.smart_notes.config import SmartNotesNoteTypeConfig
    from omnia.plugins.smart_notes.engine import GenerationResult

logger = get_logger("smart_notes")


class SmartNotesDialog(WebDialog):
    """Per-note-type Smart Notes table: base field + per-field generation config + Auto-smart."""

    def __init__(self, repo: ConfigRepository, parent: Any = None) -> None:
        self._repo = repo
        # Per-note-type rules persist in the collection (synced); provider config (llm/tts)
        # stays in the TOML config via self._repo (see _build_hub).
        self._store = SmartNotesStore()
        # The temp clip of the last sound preview/test, replayed by the "Play again" button.
        self._last_audio_path = ""
        # The native-runtime sidecar manager (ADR-005), built once and shared by the
        # native_runtimes / set_native_runtime ops so install state + tracked servers persist.
        self._native_manager = default_manager()
        super().__init__(
            parent,
            title="Smart Notes ✨",
            html=build_smart_notes_html(
                dark=theme_manager.night_mode,
                init=self._initial_state(),
                catalog=catalog_payload(self._cached_fetched_voices()),
            ),
            handlers={
                "list_note_types": self._on_list_note_types,
                "load": self._on_load,
                "set_base_field": self._on_set_base_field,
                "create_field": self._on_create_field,
                "graph_recompute": self._on_graph_recompute,
                "auto_smart": self._on_auto_smart,
                "improve_prompt": self._on_improve_prompt,
                "improve_all": self._on_improve_all,
                "preview": self._on_preview,
                "account_data": self._on_account_data,
                "account_credit": self._on_account_credit,
                "account_test": self._on_account_test,
                "set_default_model": self._on_set_default_model,
                "set_auto_voice": self._on_set_auto_voice,
                "refresh_voices": self._on_refresh_voices,
                "account_keys": self._on_account_keys,
                "account_keys_credit": self._on_account_keys_credit,
                "set_secrets": self._on_set_secrets,
                "native_runtimes": self._on_native_runtimes,
                "set_native_runtime": self._on_set_native_runtime,
                "browse_file": self._on_browse_file,
                "open_url": self._on_open_url,
                "replay_audio": self._on_replay_audio,
                "save": self._on_save,
                "cancel": self._on_cancel,
            },
            width=1040,
            height=640,
        )

    def _initial_state(self) -> dict[str, Any]:
        """The data baked into the page so it renders populated without an init pycmd.

        Seeds the note-type list + the first note type's load payload (base field, fields,
        rows, providers). The init pycmd callback is unreliable (the bridge channel isn't ready
        when the page's inline script first runs), so the first paint must not depend on it.
        """
        note_types = anki_compat.note_type_names()
        if not note_types:
            return {"note_types": []}
        first = note_types[0]
        payload = load_payload(
            first,
            self._settings().note_type_config(first),
            anki_compat.note_type_field_names(first),
            available_llm_providers(),
            all_decks=self._all_decks(),
        )
        return {"note_types": note_types, **payload, "options": self._options_payload()}

    # --- pycmd handlers --------------------------------------------------------------
    def _on_list_note_types(self, _data: dict[str, Any]) -> list[str]:
        return anki_compat.note_type_names()

    def _on_load(self, data: dict[str, Any]) -> dict[str, Any]:
        note_type = str(data.get("note_type", ""))
        payload = load_payload(
            note_type,
            self._settings().note_type_config(note_type),
            anki_compat.note_type_field_names(note_type),
            available_llm_providers(),
            all_decks=self._all_decks(),
        )
        payload["options"] = self._options_payload()
        return payload

    def _on_set_base_field(self, data: dict[str, Any]) -> dict[str, Any]:
        # Re-render the rows for the chosen base field, keeping any saved config for the rest.
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        config = self._settings().note_type_config(note_type)
        all_fields = anki_compat.note_type_field_names(note_type)
        rows = rows_for_note_type(config, all_fields, base_field)
        row_payloads = [row_to_payload(row) for row in rows]
        return {
            "note_type": note_type,
            "base_field": base_field,
            "all_fields": all_fields,
            "rows": row_payloads,
            "providers": available_llm_providers(),
            "decks": list(config.decks) if config else [],
            "all_decks": self._all_decks(),
            "options": self._options_payload(),
            "graph": graph_payload(
                note_type_config_from_payload(note_type, base_field, row_payloads)
            ),
        }

    def _on_graph_recompute(self, data: dict[str, Any]) -> dict[str, Any]:
        """Re-lay out the field dependency graph from the page's current rows.

        The JS posts the live rows (including each row's ``depends_on``) after every structural
        edge edit and on first opening the Dependencies view; this builds a config and returns a
        freshly laid-out :func:`graph_payload` so the layout is always computed in Python. A
        cycle (the server-side backstop via ``FieldGraph.from_config`` / ``laid_out``) or any
        other failure returns ``{error}`` so the dialog never crashes and the page can revert the
        optimistic change.
        """
        config = note_type_config_from_payload(
            str(data.get("note_type", "")),
            str(data.get("base_field", "")),
            list(data.get("rows", [])),
        )
        try:
            return {"graph": graph_payload(config)}
        except (
            Exception
        ) as exc:  # boundary: a cycle/bad payload must not crash the dialog
            logger.exception("smart_notes: failed to recompute field graph")
            return {"error": f"Could not lay out the graph: {exc}"}

    def _on_create_field(self, data: dict[str, Any]) -> dict[str, Any]:
        note_type = str(data.get("note_type", ""))
        field_name = str(data.get("field_name", "")).strip()
        if not field_name:
            return {"error": "Field name cannot be empty."}
        # Boundary: surface a schema-change failure to the page instead of crashing the dialog.
        try:
            all_fields = anki_compat.add_note_type_field(note_type, field_name)
        except Exception as exc:
            logger.exception("smart_notes: failed to add field %r", field_name)
            return {"error": f"Could not add the field: {exc}"}
        return {"all_fields": all_fields}

    def _on_auto_smart(self, data: dict[str, Any]) -> None:
        """Run Auto-prompt off the Qt main thread; push the result back via the page hook.

        Returns None immediately — the LLM call can't block the main thread, so the new rows
        are delivered to the page through ``window.__snAutoResult`` once the background op
        finishes (success or a friendly ProviderError message). Reports a clear, actionable
        message when there is nothing to fill (no Generate-on + unlocked field) instead of
        silently succeeding.
        """
        config = note_type_config_from_payload(
            str(data.get("note_type", "")),
            str(data.get("base_field", "")),
            list(data.get("rows", [])),
            list(data.get("decks", [])),
        )

        from omnia.plugins.smart_notes.authoring import PromptAuthor, candidate_fields

        candidates = candidate_fields(config)
        if not candidates:
            self._push_auto_result(
                error="Nothing to fill — switch Generate on (and unlock) for at least one "
                "field, then run Auto-prompt."
            )
            return
        hub = self._build_hub()
        if hub is None:
            self._push_auto_result(error="Provider config error — see logs.")
            return

        anki_compat.run_in_background(
            lambda: PromptAuthor(hub.llm()).auto_smart(config),
            on_success=lambda updated: self._push_auto_result(
                config=updated, filled=len(candidates)
            ),
            on_failure=self._on_auto_failure,
            label="Omnia: auto-prompt…",
        )

    def _on_improve_prompt(self, data: dict[str, Any]) -> None:
        """Rewrite ONE field's rough prompt into a polished one (off-thread; pushed back).

        Mechanism X: the user types a short/rough request and clicks ✨ Improve in the prompt
        editor; the result returns through ``window.__snImproveResult``.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        field = str(data.get("field", ""))
        rough = str(data.get("prompt", ""))
        if not rough.strip():
            self._push_improve(
                field, error="Type a rough prompt first, then Improve it."
            )
            return
        hub = self._build_hub()
        if hub is None:
            self._push_improve(field, error="Provider config error — see logs.")
            return
        other_fields = [
            name
            for name in anki_compat.note_type_field_names(note_type)
            if name != field
        ]

        from omnia.plugins.smart_notes.authoring import PromptAuthor

        anki_compat.run_in_background(
            lambda: PromptAuthor(hub.llm()).improve(
                note_type=note_type,
                base_field=base_field,
                target_field=field,
                rough=rough,
                other_fields=other_fields,
            ),
            on_success=lambda text: self._push_improve(field, prompt=text),
            on_failure=lambda exc: self._push_improve(
                field, error=self._friendly(exc, "Improve failed")
            ),
            label="Omnia: improving prompt…",
        )

    def _on_improve_all(self, data: dict[str, Any]) -> None:
        """Rewrite every Generate-on + unlocked field's rough prompt at once (off-thread; pushed).

        The result returns through ``window.__snImproveAllResult`` as ``{field: prompt}``.
        """
        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        items = [
            (str(row.get("field", "")), str(row.get("prompt", "")))
            for row in data.get("rows", [])
            if row.get("enabled")
            and not row.get("prompt_locked")
            and str(row.get("field", "")) != base_field
            and str(row.get("prompt", "")).strip()
        ]
        if not items:
            self._push_improve_all(
                error="No field has a prompt to improve — switch Generate on (and unlock) a "
                "field with a prompt first."
            )
            return
        hub = self._build_hub()
        if hub is None:
            self._push_improve_all(error="Provider config error — see logs.")
            return

        from omnia.plugins.smart_notes.authoring import PromptAuthor

        anki_compat.run_in_background(
            lambda: PromptAuthor(hub.llm()).improve_all(
                note_type=note_type, base_field=base_field, items=items
            ),
            on_success=lambda mapping: self._push_improve_all(improved=mapping),
            on_failure=lambda exc: self._push_improve_all(
                error=self._friendly(exc, "Improve all failed")
            ),
            label="Omnia: improving prompts…",
        )

    def _on_preview(self, data: dict[str, Any]) -> None:
        """Generate a sample for one field against a real (or fabricated) note (off-thread).

        Lets the user test a prompt before saving; the result returns through
        ``window.__snPreviewResult``.
        """
        from omnia.plugins.smart_notes.config import SmartNotesFieldRule
        from omnia.plugins.smart_notes.engine import GenerationService

        note_type = str(data.get("note_type", ""))
        base_field = str(data.get("base_field", ""))
        field = str(data.get("field", ""))
        kind = str(data.get("type", "text"))
        prompt = str(data.get("prompt", ""))
        rule = SmartNotesFieldRule(
            note_type=note_type,
            # Mirror compile_note_type_rules: with no prompt the base field is the source.
            source_field="" if prompt else base_field,
            target_field=field,
            kind=kind,
            prompt=prompt,
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            voice=str(data.get("voice", "")),
            language=str(data.get("language", "")),
        )
        fields = self._preview_fields(note_type, base_field)
        hub = self._build_hub()
        if hub is None:
            self._push_preview(field, error="Provider config error — see logs.")
            return
        service = GenerationService(hub)

        anki_compat.run_in_background(
            lambda: service.generate(rule, fields),
            on_success=lambda result: self._push_preview(field, result=result),
            on_failure=lambda exc: self._push_preview(
                field, error=self._friendly(exc, "Preview failed")
            ),
            label="Omnia: preview…",
        )

    def _preview_fields(self, note_type: str, base_field: str) -> dict[str, str]:
        """The field values a preview runs against.

        Uses the FIRST existing note of ``note_type`` when there is one; otherwise fabricates a
        sample (all fields blank). Either way, a blank base field is seeded with a sample word —
        most prompts self-guard to output nothing for an empty base, which is exactly the
        "(empty result)" the preview was hitting, so the seed makes the preview meaningful.
        """
        note = anki_compat.random_note_of_type(note_type or None)
        if note is not None:
            fields = {name: note[name] for name in note.keys()}  # noqa: SIM118
        else:
            fields = {name: "" for name in anki_compat.note_type_field_names(note_type)}
        if base_field and not str(fields.get(base_field, "")).strip():
            fields[base_field] = "example"
        return fields

    # --- Account tab -----------------------------------------------------------------
    def _on_account_data(self, _data: dict[str, Any]) -> dict[str, Any]:
        """The Account tab's usage tables: models-in-use merged with the recorded usage.

        Synchronous + main-thread-safe: it only reads config + the fast JSON usage snapshot
        (no network). The credit line is fetched separately off-thread (``account_credit``).
        """
        from omnia.core.providers import usage
        from omnia.plugins.smart_notes.account import merge_usage, models_in_use

        models = models_in_use(
            self._store.load(),
            self._repo.llm_settings(),
            self._repo.tts_settings(),
        )
        rows = usage.default_recorder().snapshot()
        return {
            "models": {
                kind: merge_usage(models[kind], rows, kind)
                for kind in ("text", "image", "sound")
            },
            "defaults": self._defaults_payload(),
            "auto_voices": dict(self._repo.tts_settings().auto_voices),
        }

    def _on_account_credit(self, _data: dict[str, Any]) -> None:
        """Fetch the OpenRouter credit off-thread; push it via ``window.__snCreditResult``.

        Best-effort: a non-OpenRouter provider or any error yields ``{error}`` (the page just
        hides the line). The fetch is network I/O, so it must not block the Qt main thread.
        """
        hub = self._build_hub()
        if hub is None:
            self._push_credit(error="Provider config error — see logs.")
            return

        provider_name = self._repo.llm_settings().provider

        def fetch() -> dict[str, Any]:
            provider = hub.llm()
            fetch_credit = getattr(provider, "fetch_credit", None)
            credit = fetch_credit() if callable(fetch_credit) else None
            # Only OpenRouter exposes a real credit balance to an API key; for everyone else
            # surface an honest note rather than a blank line (the data lives in their console).
            return (
                credit if credit is not None else {"note": _credit_note(provider_name)}
            )

        anki_compat.run_in_background(
            fetch,
            on_success=lambda credit: self._push_credit(credit=credit),
            on_failure=lambda exc: self._push_credit(
                error=self._friendly(exc, "Credit fetch failed")
            ),
            label="Omnia: checking credit…",
        )

    def _on_account_test(self, data: dict[str, Any]) -> None:
        """Run a one-off generation from the Account playground (off-thread; pushed back).

        Builds a self-contained rule from the playground input (the prompt is used verbatim;
        for sound the input is the spoken text) and runs it through the generation service.
        The result returns through ``window.__snAccountTestResult``.
        """
        from omnia.plugins.smart_notes.config import SmartNotesFieldRule
        from omnia.plugins.smart_notes.engine import GenerationService

        kind = str(data.get("kind", "text"))
        prompt = str(data.get("input", ""))
        if not prompt.strip():
            self._push_account_test(kind, error="Type something to test first.")
            return
        rule = SmartNotesFieldRule(
            kind=kind,
            prompt=prompt,
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            voice=str(data.get("voice", "")),
        )
        hub = self._build_hub()
        if hub is None:
            self._push_account_test(kind, error="Provider config error — see logs.")
            return
        service = GenerationService(hub)

        anki_compat.run_in_background(
            lambda: service.generate(rule, {}),
            on_success=lambda result: self._push_account_test(kind, result=result),
            on_failure=lambda exc: self._push_account_test(
                kind, error=self._friendly(exc, "Test failed")
            ),
            label="Omnia: account test…",
        )

    # --- default-model picker + key management --------------------------------------
    def _on_set_default_model(self, data: dict[str, Any]) -> dict[str, Any]:
        """Set the central default provider+model for a kind (text/image → LLM, sound → TTS).

        These central defaults drive the meta-tasks (language-detect, ✨ Auto-prompt,
        ✦ Improve) and any field left on "(inherit)". Persists to providers.toml and returns
        the refreshed defaults so the picker reflects the saved state.
        """
        kind = str(data.get("kind", "text"))
        provider = str(data.get("provider", "")).strip()
        model = str(data.get("model", ""))
        if not provider:
            return {"defaults": self._defaults_payload()}
        try:
            if kind == "sound":
                self._repo.set_active_tts(provider, voice=model or None)
            elif kind == "image":
                self._repo.set_active_llm(provider, image_model=model or None)
            else:
                self._repo.set_active_llm(provider, text_model=model or None)
        except Exception:  # boundary: a bad write must not crash the dialog
            logger.exception("smart_notes: failed to set default model")
            return {"error": "Could not update the default model — see logs."}
        return {"defaults": self._defaults_payload()}

    def _defaults_payload(self) -> dict[str, Any]:
        """The central default (provider, model) per kind for the Account default picker."""
        from omnia.plugins.smart_notes.account import default_models

        return default_models(self._repo.llm_settings(), self._repo.tts_settings())

    def _on_set_auto_voice(self, data: dict[str, Any]) -> dict[str, Any]:
        """Set (or clear) one language's global Auto-detect voice; return the refreshed map.

        The ``[tts.auto_voices]`` map is the source of truth for Auto-detect generation: this
        only writes the ``lang → "provider:voice"`` entry (an empty value clears it) and never
        validates it against the catalog — a stale mapping surfaces at generation time.
        """
        lang = str(data.get("lang", "")).strip()
        value = str(data.get("value", ""))
        if not lang:
            return {"auto_voices": dict(self._repo.tts_settings().auto_voices)}
        try:
            self._repo.set_auto_voice(lang, value)
        except Exception:  # boundary: a bad write must not crash the dialog
            logger.exception("smart_notes: failed to set auto voice")
            return {"error": "Could not update the Auto-detect voice — see logs."}
        return {"auto_voices": dict(self._repo.tts_settings().auto_voices)}

    def _on_refresh_voices(self, _data: dict[str, Any]) -> None:
        """Refresh the voice catalog off-thread (provider-agnostic), cache it, push the options.

        Calls the generic :func:`omnia.core.providers.tts.refresh_voices` — each provider decides
        whether it can fetch a live list (only edge_tts does this pass; google_cloud needs auth);
        no concrete provider is named here. Enriches ONLY the dropdown options (the fetched voices
        merged over the curated seed); it never touches the saved ``[tts.auto_voices]`` map.
        Pushed back through ``window.__snVoicesRefreshed`` with the rebuilt
        ``auto_voice_options``.
        """

        def fetch() -> dict[str, object]:
            from omnia.core.providers import tts, voice_cache
            from omnia.core.providers.catalog import catalog_payload

            voices = tts.refresh_voices()
            voice_cache.save_cached_voices(self._user_files_dir(), voices)
            payload = catalog_payload(voices)
            return {"auto_voice_options": payload["auto_voice_options"]}

        anki_compat.run_in_background(
            fetch,
            on_success=lambda res: self._push_voices_refreshed(options=res),
            on_failure=lambda exc: self._push_voices_refreshed(
                error=self._friendly(exc, "Refresh voices failed")
            ),
            label="Omnia: refreshing voices…",
        )

    def _push_voices_refreshed(
        self, *, options: Optional[dict[str, Any]] = None, error: str = ""
    ) -> None:
        """Send the refreshed Auto-detect options to ``window.__snVoicesRefreshed``."""
        payload: dict[str, Any] = {"error": error} if error else (options or {})
        self.eval_js(f"window.__snVoicesRefreshed({json.dumps(payload)});")

    @staticmethod
    def _user_files_dir() -> Path:
        """The add-on's ``user_files`` directory (where the fetched-voice cache lives)."""
        from omnia import addon_user_files_dir

        return addon_user_files_dir()

    def _cached_fetched_voices(self) -> dict[str, Any]:
        """The cached Refresh result merged into the baked catalog (``{}`` when absent).

        Offline-safe: when no cache exists the Auto-detect dropdowns fall back to the curated
        seed. Provider-agnostic — whatever providers were fetched + cached are merged.
        """
        from omnia.core.providers import voice_cache

        return voice_cache.load_cached_voices(self._user_files_dir())

    def _on_account_keys(self, _data: dict[str, Any]) -> dict[str, Any]:
        """The Keys subtab cards: each managed provider's credential fields + state."""
        from omnia.plugins.smart_notes.account import key_cards

        return {"providers": key_cards(self._repo.llm_settings())}

    def _on_set_secrets(self, data: dict[str, Any]) -> dict[str, Any]:
        """Persist a provider card's editable fields in one write (one Save per card).

        Each field carries its kind so secrets are routed into the secrets store (only a
        ``secret:`` ref hits providers.toml) while plain fields (project/location) stay inline.
        """
        provider = str(data.get("provider", ""))
        fields = list(data.get("fields", []))
        if not provider:
            return {"error": "Missing provider."}
        updates = [
            (str(f.get("key", "")), str(f.get("type", "")), str(f.get("value", "")))
            for f in fields
            if f.get("key")
        ]
        try:
            self._repo.set_provider_fields("llm", provider, updates)
        except Exception:  # boundary: surface a bad write instead of crashing
            logger.exception("smart_notes: failed to save secrets for %s", provider)
            return {"error": "Could not save — see logs."}
        return {"ok": True}

    # --- Native runtimes (Options → General; ADR-005) --------------------------------
    def _on_native_runtimes(self, _data: dict[str, Any]) -> dict[str, Any]:
        """The Native-runtimes panel data: each registered runtime grouped by section + state.

        Synchronous + main-thread-safe — it only reads the registry and checks each venv's
        install marker on disk (no subprocess/network). Fetched lazily when the General tab
        renders the panel.
        """
        return native_runtimes_payload(self._native_manager)

    def _on_set_native_runtime(self, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Install (enabled=True, OFF-THREAD) or uninstall (enabled=False, sync) one runtime.

        Delegates the install/uninstall decision to the pure :func:`set_native_runtime` helper,
        injecting the off-thread runner + the page push hooks. Installing creates a venv and
        pip-installs the runtime (viet-tts pulls ~GB), so it runs off the Qt main thread via
        :func:`run_in_background` and pushes progress + the final outcome through
        ``window.__snNativeRuntime*``. Uninstalling stops the sidecar + deletes the venv (fast)
        and returns the refreshed row state immediately.
        """
        name = str(data.get("name", ""))
        try:
            return set_native_runtime(
                self._native_manager,
                name,
                bool(data.get("enabled", False)),
                run_async=self._run_native_install,
                push_progress=self._push_native_progress,
                push_done=self._push_native_done,
            )
        except (
            Exception
        ):  # boundary: a bad uninstall (rmtree) must not crash the dialog
            logger.exception("smart_notes: failed to toggle runtime %r", name)
            return {
                "name": name,
                "installed": False,
                "error": "Could not update — see logs.",
            }

    def _run_native_install(
        self,
        op: Any,
        on_success: Any,
        on_failure: Any,
    ) -> None:
        """Run a runtime install off the Qt main thread (the helper's ``run_async`` seam)."""
        anki_compat.run_in_background(
            op,
            on_success=lambda _none: on_success(),
            on_failure=on_failure,
            label="Omnia: installing native runtime…",
        )

    def _push_native_progress(self, name: str, message: str) -> None:
        """Send a runtime install progress line to ``window.__snNativeRuntimeProgress``.

        MUST marshal to the Qt main thread: this is invoked from the install op's
        ``on_progress`` callback, which runs on the ``run_in_background`` WORKER thread, and
        touching the WebView (``eval_js`` → ``webview.eval``) off the main thread hard-crashes
        Qt (a native segfault, not a catchable Python exception). ``_push_native_done`` needs no
        such marshalling — it runs from QueryOp's success/failure, already on the main thread.
        """
        anki_compat.run_on_main(
            lambda: self.eval_js(
                f"window.__snNativeRuntimeProgress({json.dumps(name)}, {json.dumps(message)});"
            )
        )

    def _push_native_done(self, name: str, installed: bool, error: str = "") -> None:
        """Send a runtime install outcome to ``window.__snNativeRuntimeDone``."""
        result: dict[str, Any] = (
            {"installed": False, "error": error} if error else {"installed": installed}
        )
        self.eval_js(
            f"window.__snNativeRuntimeDone({json.dumps(name)}, {json.dumps(result)});"
        )

    def _on_browse_file(self, data: dict[str, Any]) -> dict[str, Any]:
        """Pick a credential file (Vertex JSON), copy it into secrets/, store the ref.

        Returns the resolved absolute path of the secrets copy so the field shows where the
        key now lives (inside the add-on, portable — not the original source path).
        """
        provider = str(data.get("provider", ""))
        field = str(data.get("field", ""))
        path = anki_compat.pick_file(
            title="Select the service-account JSON key",
            file_filter="JSON files (*.json);;All files (*)",
            parent=self,
        )
        if not path:
            return {"path": ""}
        if not (provider and field):
            return {"path": path}
        try:
            stored = self._repo.set_provider_credential_file(
                "llm", provider, field, path
            )
        except Exception:  # boundary: surface a bad copy/write instead of crashing
            logger.exception("smart_notes: failed to import credential file")
            return {"error": "Could not import the file — see logs."}
        return {"path": stored, "ok": True}

    def _on_open_url(self, data: dict[str, Any]) -> None:
        """Open a provider's console/billing URL in the user's browser (http/https only)."""
        url = str(data.get("url", ""))
        if url.startswith(("http://", "https://")):
            anki_compat.open_external_url(url)

    def _on_replay_audio(self, _data: dict[str, Any]) -> None:
        """Replay the last sound preview/test clip (the playground "Play again" button)."""
        if self._last_audio_path:
            anki_compat.replay_audio_file(self._last_audio_path)

    def _on_account_keys_credit(self, _data: dict[str, Any]) -> None:
        """Fetch the OpenRouter balance from its configured key (off-thread) for the Keys subtab.

        Independent of which provider is currently active — it builds an OpenRouter provider
        straight from ``[llm.openrouter]`` so the quota bar works even when another provider is
        active. Pushed via ``window.__snKeysCreditResult``.
        """
        sub = self._repo.llm_settings().openrouter
        if not sub.api_key:
            self._push_keys_credit(
                "openrouter", error="Add an OpenRouter API key to see live credit."
            )
            return

        def fetch() -> Optional[dict[str, Any]]:
            from omnia.core.providers.llm.openai_compatible import (
                OpenAICompatibleProvider,
            )

            provider = OpenAICompatibleProvider(
                api_key=sub.api_key,
                base_url=sub.base_url or "https://openrouter.ai/api/v1",
            )
            return provider.fetch_credit()

        anki_compat.run_in_background(
            fetch,
            on_success=lambda credit: self._push_keys_credit(
                "openrouter", credit=credit
            ),
            on_failure=lambda exc: self._push_keys_credit(
                "openrouter", error=self._friendly(exc, "Credit fetch failed")
            ),
            label="Omnia: checking OpenRouter credit…",
        )

    def _push_keys_credit(
        self,
        provider: str,
        *,
        credit: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        """Send a Keys-subtab credit outcome to ``window.__snKeysCreditResult``."""
        if error:
            payload: dict[str, Any] = {"error": error}
        elif credit is None:
            payload = {"error": "OpenRouter didn't return a balance."}
        else:
            payload = dict(credit)
        self.eval_js(
            f"window.__snKeysCreditResult({json.dumps(provider)}, {json.dumps(payload)});"
        )

    def _push_credit(
        self, *, credit: Optional[dict[str, Any]] = None, error: str = ""
    ) -> None:
        """Send the OpenRouter credit outcome to ``window.__snCreditResult``."""
        if error:
            payload: dict[str, Any] = {"error": error}
        elif credit is None:
            payload = {"error": "No credit info for this provider."}
        else:
            payload = dict(credit)
        self.eval_js(f"window.__snCreditResult({json.dumps(payload)});")

    def _push_account_test(
        self,
        kind: str,
        *,
        result: Optional[GenerationResult] = None,
        error: str = "",
    ) -> None:
        """Send an account-test outcome to ``window.__snAccountTestResult``."""
        if error:
            payload: dict[str, Any] = {"kind": kind, "error": error}
        elif result is None:
            payload = {"kind": kind, "error": "Test produced no result."}
        else:
            payload = self._result_payload(result)
        self.eval_js(f"window.__snAccountTestResult({json.dumps(payload)});")

    def _on_save(self, data: dict[str, Any]) -> dict[str, Any]:
        config = note_type_config_from_payload(
            str(data.get("note_type", "")),
            str(data.get("base_field", "")),
            list(data.get("rows", [])),
            list(data.get("decks", [])),
        )
        if not config.note_type:
            return {"error": "Pick a note type first."}
        # Per-note-type rules persist in the COLLECTION (synced), not the TOML config. The
        # global option flags ride along on the same SmartNotesSettings.
        settings = self._store.load()
        merged = merge_note_type_into(list(settings.note_types), config)
        opts = dict(data.get("options", {}))
        self._store.save(
            settings.copy(
                update={
                    "note_types": merged,
                    "generate_at_review": bool(
                        opts.get("generate_at_review", settings.generate_at_review)
                    ),
                    "regenerate_when_batching": bool(
                        opts.get(
                            "regenerate_when_batching",
                            settings.regenerate_when_batching,
                        )
                    ),
                    "allow_empty_fields": bool(
                        opts.get("allow_empty_fields", settings.allow_empty_fields)
                    ),
                }
            )
        )
        return {"ok": True}

    def _on_cancel(self, _data: dict[str, Any]) -> None:
        self.reject()

    # --- helpers ---------------------------------------------------------------------
    def _settings(self) -> Any:
        # Per-note-type rules persist in the collection (synced), not in the TOML config.
        return self._store.load()

    def _options_payload(self) -> dict[str, Any]:
        """The global Smart Notes option flags for the Options modal."""
        settings = self._settings()
        return {
            "generate_at_review": settings.generate_at_review,
            "regenerate_when_batching": settings.regenerate_when_batching,
            "allow_empty_fields": settings.allow_empty_fields,
        }

    @staticmethod
    def _all_decks() -> list[dict[str, Any]]:
        """The full deck list for the picker as ``[{id, name}, ...]`` objects for the JS."""
        return [
            {"id": deck_id, "name": name} for deck_id, name in anki_compat.deck_names()
        ]

    def _build_hub(self) -> Optional[ProviderHub]:
        try:
            return ProviderHub(self._repo.llm_settings(), self._repo.tts_settings())
        except Exception:  # boundary: a bad provider config must not crash the dialog
            logger.exception("smart_notes: could not build provider hub for auto-smart")
            return None

    def _on_auto_failure(self, exc: Exception) -> None:
        self._push_auto_result(error=self._friendly(exc, "Auto-prompt failed"))

    def _friendly(self, exc: Exception, prefix: str) -> str:
        """A short user-facing message for ``exc`` (ProviderError verbatim; else log + generic)."""
        if isinstance(exc, ProviderError):
            return f"{prefix}: {exc}"
        logger.exception("smart_notes: %s raised", prefix)
        return f"{prefix} — see logs."

    def _push_auto_result(
        self,
        *,
        config: Optional[SmartNotesNoteTypeConfig] = None,
        filled: int = 0,
        error: str = "",
    ) -> None:
        """Send the Auto-smart outcome to the page's ``window.__snAutoResult`` hook."""
        if error:
            result: dict[str, Any] = {"error": error}
        else:
            assert config is not None
            result = {
                "rows": [row_to_payload(row) for row in config.fields],
                "filled": filled,
            }
        self.eval_js(f"window.__snAutoResult({json.dumps(result)});")

    def _push_improve(self, field: str, *, prompt: str = "", error: str = "") -> None:
        """Send one Improve outcome to the page's ``window.__snImproveResult`` hook."""
        result: dict[str, Any] = {"error": error} if error else {"prompt": prompt}
        self.eval_js(
            f"window.__snImproveResult({json.dumps(field)}, {json.dumps(result)});"
        )

    def _push_improve_all(
        self, *, improved: Optional[dict[str, str]] = None, error: str = ""
    ) -> None:
        """Send the Improve-all outcome to the page's ``window.__snImproveAllResult`` hook."""
        result: dict[str, Any] = (
            {"error": error} if error else {"improved": improved or {}}
        )
        self.eval_js(f"window.__snImproveAllResult({json.dumps(result)});")

    def _push_preview(
        self,
        field: str,
        *,
        result: Optional[GenerationResult] = None,
        error: str = "",
    ) -> None:
        """Send a Preview outcome to the page's ``window.__snPreviewResult`` hook.

        Text previews carry the rendered HTML; audio is played here and reported as a note;
        an image is reported as generated (not inserted — this is only a preview).
        """
        if error:
            payload: dict[str, Any] = {"error": error}
        elif result is None:
            payload = {"error": "Preview produced no result."}
        else:
            payload = self._result_payload(result)
        self.eval_js(
            f"window.__snPreviewResult({json.dumps(field)}, {json.dumps(payload)});"
        )

    def _result_payload(self, result: GenerationResult) -> dict[str, Any]:
        """Convert a generation result to the page payload (shared by preview + account test).

        Text carries the rendered HTML; an image is returned as a ``data:`` URI so the page can
        actually show it; audio is played here, remembered for replay, and reported as a note.
        Nothing is inserted into a note — this is only a preview/test.
        """
        if result.kind == "text":
            return {"kind": "text", "text": result.text or ""}
        if result.kind == "image":
            import base64

            b64 = base64.b64encode(result.data or b"").decode("ascii")
            ext = result.ext or "png"
            return {
                "kind": "image",
                "image": f"data:image/{ext};base64,{b64}",
                "message": "Image generated.",
            }
        self._last_audio_path = anki_compat.play_audio(result.data or b"", result.ext)
        return {
            "kind": "tts",
            "message": "Audio preview played.",
            "can_replay": True,
        }


# Honest per-provider note for when no credit/quota is fetchable via an API key. Only
# OpenRouter exposes a real balance; OpenAI deprecated its key billing API, and Google
# (AI Studio / Vertex) keeps quota in the Cloud Console (Vertex is pay-as-you-go — no
# prepaid credit). The free TTS providers have nothing to report.
_CREDIT_NOTES = {
    "openrouter": "Couldn't reach OpenRouter for the balance — check the key / network.",
    "openai": "OpenAI doesn't expose credit/quota to an API key — see platform.openai.com.",
    "openai_compatible": "This endpoint exposes no credit/quota API — see its dashboard.",
    "gemini": "Gemini (AI Studio) has no key-accessible quota — see Google AI Studio / Cloud Console.",
    "gemini_vertex": "Vertex AI is pay-as-you-go (no prepaid credit); quota lives in the GCP Console.",
}


def _credit_note(provider: str) -> str:
    """An honest one-liner explaining why a provider shows no fetchable credit/quota."""
    return _CREDIT_NOTES.get(
        provider, "This provider exposes no credit/quota API — check its console."
    )
