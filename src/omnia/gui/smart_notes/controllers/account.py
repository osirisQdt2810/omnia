"""The Account-tab controller: Usage, Keys, the playground, defaults, and voices.

Reads the models-in-use + recorded usage tables (synchronous, no network), fetches provider
credit/quota off-thread, runs the one-off playground generation, manages the central default
provider/model per kind + the Auto-detect voice map, persists provider keys/secrets, refreshes
the voice catalog, and the small browse-file / open-url / replay-audio actions.

Every network/LLM/TTS action runs off the Qt main thread and pushes its result back through a
page hook ONLY from the ``run_in_background`` success/failure callback. The shared
result→payload conversion (and the replayable last-audio clip) lives on the context, since the
Preview path uses it too. Only loaded inside Anki.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.gui.smart_notes.context import SmartNotesContext

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.engine import GenerationResult

logger = get_logger("smart_notes")


class AccountController:
    """Usage / Keys / playground / defaults / voices ops."""

    def __init__(self, ctx: SmartNotesContext) -> None:
        self._ctx = ctx

    def ops(self) -> dict[str, Callable[..., Any]]:
        """The ``{op_name: handler}`` map this controller owns."""
        return {
            "account_data": self.on_account_data,
            "account_credit": self.on_account_credit,
            "account_test": self.on_account_test,
            "account_keys": self.on_account_keys,
            "account_keys_credit": self.on_account_keys_credit,
            "set_default_model": self.on_set_default_model,
            "set_auto_voice": self.on_set_auto_voice,
            "refresh_voices": self.on_refresh_voices,
            "set_secrets": self.on_set_secrets,
            "browse_file": self.on_browse_file,
            "open_url": self.on_open_url,
            "replay_audio": self.on_replay_audio,
        }

    # --- Account tab -----------------------------------------------------------------
    def on_account_data(self, _data: dict[str, Any]) -> dict[str, Any]:
        """The Account tab's usage tables: models-in-use merged with the recorded usage.

        Synchronous + main-thread-safe: it only reads config + the fast JSON usage snapshot
        (no network). The credit line is fetched separately off-thread (``account_credit``).
        """
        from omnia.core.providers import usage
        from omnia.plugins.smart_notes.account import merge_usage, models_in_use

        models = models_in_use(
            self._ctx.store.load(),
            self._ctx.repo.llm_settings(),
            self._ctx.repo.tts_settings(),
        )
        rows = usage.default_recorder().snapshot()
        return {
            "models": {
                kind: merge_usage(models[kind], rows, kind)
                for kind in ("text", "image", "sound")
            },
            "defaults": self._defaults_payload(),
            "auto_voices": dict(self._ctx.repo.tts_settings().auto_voices),
        }

    def on_account_credit(self, _data: dict[str, Any]) -> None:
        """Fetch the OpenRouter credit off-thread; push it via ``window.__snCreditResult``.

        Best-effort: a non-OpenRouter provider or any error yields ``{error}`` (the page just
        hides the line). The fetch is network I/O, so it must not block the Qt main thread.
        """
        hub = self._ctx.build_hub()
        if hub is None:
            self._push_credit(error="Provider config error — see logs.")
            return

        provider_name = self._ctx.repo.llm_settings().provider

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
                error=self._ctx.friendly(exc, "Credit fetch failed")
            ),
            label="Omnia: checking credit…",
        )

    def on_account_test(self, data: dict[str, Any]) -> None:
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
        hub = self._ctx.build_hub()
        if hub is None:
            self._push_account_test(kind, error="Provider config error — see logs.")
            return
        service = GenerationService(hub)

        anki_compat.run_in_background(
            lambda: service.generate(rule, {}),
            on_success=lambda result: self._push_account_test(kind, result=result),
            on_failure=lambda exc: self._push_account_test(
                kind, error=self._ctx.friendly(exc, "Test failed")
            ),
            label="Omnia: account test…",
        )

    # --- default-model picker + key management --------------------------------------
    def on_set_default_model(self, data: dict[str, Any]) -> dict[str, Any]:
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
                self._ctx.repo.set_active_tts(provider, voice=model or None)
            elif kind == "image":
                self._ctx.repo.set_active_llm(provider, image_model=model or None)
            else:
                self._ctx.repo.set_active_llm(provider, text_model=model or None)
        except Exception:  # boundary: a bad write must not crash the dialog
            logger.exception("smart_notes: failed to set default model")
            return {"error": "Could not update the default model — see logs."}
        return {"defaults": self._defaults_payload()}

    def _defaults_payload(self) -> dict[str, Any]:
        """The central default (provider, model) per kind for the Account default picker."""
        from omnia.plugins.smart_notes.account import default_models

        return default_models(
            self._ctx.repo.llm_settings(), self._ctx.repo.tts_settings()
        )

    def on_set_auto_voice(self, data: dict[str, Any]) -> dict[str, Any]:
        """Set (or clear) one language's global Auto-detect voice; return the refreshed map.

        The ``[tts.auto_voices]`` map is the source of truth for Auto-detect generation: this
        only writes the ``lang → "provider:voice"`` entry (an empty value clears it) and never
        validates it against the catalog — a stale mapping surfaces at generation time.
        """
        lang = str(data.get("lang", "")).strip()
        value = str(data.get("value", ""))
        if not lang:
            return {"auto_voices": dict(self._ctx.repo.tts_settings().auto_voices)}
        try:
            self._ctx.repo.set_auto_voice(lang, value)
        except Exception:  # boundary: a bad write must not crash the dialog
            logger.exception("smart_notes: failed to set auto voice")
            return {"error": "Could not update the Auto-detect voice — see logs."}
        return {"auto_voices": dict(self._ctx.repo.tts_settings().auto_voices)}

    def on_refresh_voices(self, _data: dict[str, Any]) -> None:
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
            voice_cache.save_cached_voices(self._ctx.user_files_dir(), voices)
            payload = catalog_payload(voices)
            return {"auto_voice_options": payload["auto_voice_options"]}

        anki_compat.run_in_background(
            fetch,
            on_success=lambda res: self._push_voices_refreshed(options=res),
            on_failure=lambda exc: self._push_voices_refreshed(
                error=self._ctx.friendly(exc, "Refresh voices failed")
            ),
            label="Omnia: refreshing voices…",
        )

    def _push_voices_refreshed(
        self, *, options: Optional[dict[str, Any]] = None, error: str = ""
    ) -> None:
        """Send the refreshed Auto-detect options to ``window.__snVoicesRefreshed``."""
        payload: dict[str, Any] = {"error": error} if error else (options or {})
        self._ctx.eval_js(f"window.__snVoicesRefreshed({json.dumps(payload)});")

    def on_account_keys(self, _data: dict[str, Any]) -> dict[str, Any]:
        """The Keys subtab cards: each managed provider's credential fields + state."""
        from omnia.plugins.smart_notes.account import key_cards

        return {"providers": key_cards(self._ctx.repo.llm_settings())}

    def on_set_secrets(self, data: dict[str, Any]) -> dict[str, Any]:
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
            self._ctx.repo.set_provider_fields("llm", provider, updates)
        except Exception:  # boundary: surface a bad write instead of crashing
            logger.exception("smart_notes: failed to save secrets for %s", provider)
            return {"error": "Could not save — see logs."}
        return {"ok": True}

    def on_browse_file(self, data: dict[str, Any]) -> dict[str, Any]:
        """Pick a credential file (Vertex JSON), copy it into secrets/, store the ref.

        Returns the resolved absolute path of the secrets copy so the field shows where the
        key now lives (inside the add-on, portable — not the original source path).
        """
        provider = str(data.get("provider", ""))
        field = str(data.get("field", ""))
        path = anki_compat.pick_file(
            title="Select the service-account JSON key",
            file_filter="JSON files (*.json);;All files (*)",
            parent=self._ctx.parent_widget(),
        )
        if not path:
            return {"path": ""}
        if not (provider and field):
            return {"path": path}
        try:
            stored = self._ctx.repo.set_provider_credential_file(
                "llm", provider, field, path
            )
        except Exception:  # boundary: surface a bad copy/write instead of crashing
            logger.exception("smart_notes: failed to import credential file")
            return {"error": "Could not import the file — see logs."}
        return {"path": stored, "ok": True}

    def on_open_url(self, data: dict[str, Any]) -> None:
        """Open a provider's console/billing URL in the user's browser (http/https only)."""
        url = str(data.get("url", ""))
        if url.startswith(("http://", "https://")):
            anki_compat.open_external_url(url)

    def on_replay_audio(self, _data: dict[str, Any]) -> None:
        """Replay the last sound preview/test clip (the playground "Play again" button)."""
        if self._ctx.last_audio_path:
            anki_compat.replay_audio_file(self._ctx.last_audio_path)

    def on_account_keys_credit(self, _data: dict[str, Any]) -> None:
        """Fetch the OpenRouter balance from its configured key (off-thread) for the Keys subtab.

        Independent of which provider is currently active — it builds an OpenRouter provider
        straight from ``[llm.openrouter]`` so the quota bar works even when another provider is
        active. Pushed via ``window.__snKeysCreditResult``.
        """
        sub = self._ctx.repo.llm_settings().openrouter
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
                "openrouter", error=self._ctx.friendly(exc, "Credit fetch failed")
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
        self._ctx.eval_js(
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
        self._ctx.eval_js(f"window.__snCreditResult({json.dumps(payload)});")

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
            payload = self._ctx.result_payload(result)
        self._ctx.eval_js(f"window.__snAccountTestResult({json.dumps(payload)});")


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
